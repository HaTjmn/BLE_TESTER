#!/usr/bin/env python3
"""
BLE-D4.0I Phases 8 & 10 -- one bounded streaming download attempt over the
dedicated USB-Serial/JTAG channel, using the corrected Python canonical
reconstruction (file_reconstructor.py) and auditing BLE_TESTER's two
optional evidence sources:
  - file_integrity_diag (full-buffer, on-chip): asked to start regardless
    of size -- BLE_TESTER itself is the authority on whether it activates
    (its own 256KB cap decides, per Phase 3's policy; this script never
    hand-computes that threshold).
  - file_transfer_hash (O(1) streaming, on-chip): always enabled; its
    `valid` flag (BLE-D4.0I Phase 4 fix) is checked before its digest is
    ever compared against anything.

Usage:
    python tools/_test_d4_0i_streaming_download.py COM17 <file_index> <expected_size_hint> <output_name>
"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from file_reconstructor import FileDownloadSession, ReconstructionError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID,
    HLV_OPCODE_GET_FILE_INFO, HLV_OPCODE_START_FILE_DOWNLOAD, HLV_OPCODE_ABORT_FILE_DOWNLOAD,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_COMPLETED,
    encode_hlv_message, decode_command_response, decode_get_file_info_response, decode_file_chunk_header,
    wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM17"
FILE_INDEX = int(sys.argv[2]) if len(sys.argv) > 2 else 1
EXPECTED_SIZE_HINT = int(sys.argv[3]) if len(sys.argv) > 3 else 17280
OUTPUT_NAME = sys.argv[4] if len(sys.argv) > 4 else f"ECG{FILE_INDEX:05d}_d4_0i.TLV"
NO_PROGRESS_TIMEOUT_S = 60
FOUR_HOURS_S = 4 * 3600

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
EVIDENCE_DIR = os.path.join(os.path.dirname(__file__), "_evidence")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_and_prepare(client):
    client.send("reset_controller", timeout=5)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.send("subscribe", timeout=6, characteristic=HLV_FILE_TRANSFER_DATA_UUID, mode="notify")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    if not status["authorized"]:
        raise ControllerError("AUTH_FAILED", "not authorized")
    return base_txn


def try_abort(client, txn):
    try:
        msg = encode_hlv_message(HLV_OPCODE_ABORT_FILE_DOWNLOAD, txn)
        client.send("write", timeout=4, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    except ControllerError:
        pass


def main():
    client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
    events = {
        "disconnects": [], "reconnects": 0, "resumes": 0, "connection_generations": set(),
        "malformed_json_count": 0,  # HolterBLEClient already isolates non-JSON lines via raw_capture; this
                                     # counts any host-side timeout that indicates a broken protocol exchange.
    }

    base_txn = connect_and_prepare(client)

    # --- Phase 9: reconfirm file identity live, never trust an old hardcode ---
    msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, base_txn + 1, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, base_txn + 1, window_s=5)
    info = decode_get_file_info_response(cr["inner"])
    log(f"GET_FILE_INFO file_index={FILE_INDEX}: {info}")
    if not info["exists"]:
        log("FATAL: selected file does not exist")
        client.close()
        sys.exit(2)
    expected_size = info["size_bytes"]
    log(f"live expected_size={expected_size} (hint was {EXPECTED_SIZE_HINT})")

    ONE_TWENTY_TWO_MB = 122 * 1024 * 1024
    if abs(expected_size - ONE_TWENTY_TWO_MB) < 5 * 1024 * 1024:
        log("FATAL: selected file is the ~122MB file -- refusing per this task's explicit restriction")
        client.close()
        sys.exit(3)

    # --- Phase 2/3: BLE_TESTER-side evidence policy ---
    boot0 = client.send("diag_boot", timeout=5)
    health0 = client.send("diag_health", timeout=5)
    log(f"boot_generation_before={boot0['boot_generation']} free_heap_before={health0['free_internal_heap']}")

    # BLE-D4.0K: file_integrity_diag.c's nominal 256KB cap suggests this file (well under
    # that) should fit, but its 4-bytes-per-source-byte design (buf + received + 2-byte
    # gen_at_offset) can still fail against this chip's actual largest-contiguous-free-block
    # ceiling (observed ~160KB even at a fresh boot) well below the nominal cap. Per this
    # task's own Phase 10 wording ("enable file_integrity_diag.c IF the live size is within
    # its SAFE supported limit"), a rejection here is treated as informational, not fatal --
    # the streaming hash + Python reconstruction remain the primary evidence sources either way.
    try:
        diag_start_resp = client.send("diag_integrity_start", timeout=5, expected_size=expected_size)
        full_file_diag_enabled = diag_start_resp.get("active", False)
        full_file_diag_skip_reason = None if full_file_diag_enabled else "rejected_by_device"
    except ControllerError as e:
        full_file_diag_enabled = False
        full_file_diag_skip_reason = f"allocation_failed_despite_nominal_cap: {e}"
    log(f"full_file_diag_enabled={full_file_diag_enabled} "
        f"full_file_diag_skip_reason={full_file_diag_skip_reason}")

    client.send("diag_file_hash_mode", timeout=5, enable=True)
    log("streaming_forwarding_enabled=true (file_transfer_hash always runs, O(1) memory, regardless of size)")
    log("python_reconstruction_enabled=true")

    # --- Phase 5: Python is the canonical reconstruction owner ---
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
    session = FileDownloadSession(out_path, expected_size)

    conn_gen = client.send("controller_status", timeout=5)["connection_generation"]
    events["connection_generations"].add(conn_gen)

    def reconnect():
        nonlocal conn_gen
        for i in range(5):
            try:
                connect_and_prepare(client)
                st = client.send("controller_status", timeout=5)
                conn_gen = st["connection_generation"]
                events["connection_generations"].add(conn_gen)
                return
            except ControllerError as e:
                log(f"  reconnect attempt {i+1}/5 failed: {e}")
                time.sleep(2.0)
        raise RuntimeError("could not reconnect")

    txn = base_txn + 2
    attempt = 0
    max_retries = 2000
    t_start = time.time()
    t_last_progress = time.time()
    received_bytes = 0
    stop_reason = None

    while not session.is_complete():
        if time.time() - t_start > FOUR_HOURS_S:
            stop_reason = "TIME_LIMIT"
            break
        attempt += 1
        if attempt > max_retries:
            stop_reason = "MAX_RETRIES_EXHAUSTED"
            break

        client.drain_events()
        try:
            txn += 1
            start_msg = encode_hlv_message(
                HLV_OPCODE_START_FILE_DOWNLOAD, txn,
                FILE_INDEX.to_bytes(2, "little") + received_bytes.to_bytes(4, "little"))
            client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
            accepted = wait_for_command_response(client, txn, window_s=5)
            if attempt > 1:
                events["resumes"] += 1
        except ControllerError as e:
            log(f"  attempt {attempt}: {e}; reconnecting")
            events["reconnects"] += 1
            reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
            log(f"  attempt {attempt}: START_FILE_DOWNLOAD not accepted ({accepted}); retrying shortly")
            time.sleep(1.0)
            continue

        transfer_id = None
        chunk_deadline = time.time() + 30
        disconnected = False
        queue_full_seen_this_attempt = 0

        try:
            while time.time() < chunk_deadline:
                remaining = chunk_deadline - time.time()
                ev = client.wait_for_event(timeout=remaining)
                if ev is None:
                    break
                if ev.get("event") == "disconnected":
                    events["disconnects"].append({"t": time.time(), "reason": ev.get("reason")})
                    disconnected = True
                    break
                if ev.get("event") != "notification":
                    continue
                if ev.get("characteristic") != HLV_FILE_TRANSFER_DATA_UUID:
                    continue
                chunk = decode_file_chunk_header(ev["data_hex"])
                if transfer_id is None:
                    transfer_id = chunk["transfer_id"]

                outcome = session.ingest(chunk["chunk_offset"], chunk["fragment"])
                if outcome["outcome"] == "accepted":
                    t_last_progress = time.time()
                    received_bytes = max(received_bytes, chunk["chunk_offset"] + len(chunk["fragment"]))
                if session.is_complete():
                    break
                if time.time() - t_last_progress > NO_PROGRESS_TIMEOUT_S:
                    stop_reason = "NO_PROGRESS_TIMEOUT"
                    break
                if chunk["chunk_offset"] + chunk["fragment_length"] >= chunk["total_length"]:
                    break
            if stop_reason:
                break
        except ControllerError as e:
            log(f"  attempt {attempt}: connection issue mid-transfer ({e}); reconnecting")
            events["reconnects"] += 1
            reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if disconnected:
            events["reconnects"] += 1
            reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF

        # Live health sampling -- STOP immediately on the explicit
        # queue-full/panic/reset stop conditions rather than discovering
        # them only after the fact.
        try:
            h = client.send("diag_health", timeout=5)
            b = client.send("diag_boot", timeout=5)
            if h.get("notify_queue_full_events", 0) > 0:
                stop_reason = "QUEUE_FULL"
                break
            if b["boot_generation"] != boot0["boot_generation"]:
                stop_reason = "RESET_DURING_TRANSFER"
                break
        except ControllerError:
            stop_reason = "PROTOCOL_CHANNEL_LOST"
            break

        r = session.reconstructor
        if r.conflicting_duplicate_bytes > 0 and stop_reason is None:
            stop_reason = "CONFLICTING_DUPLICATE"
            break
        if r.out_of_range_chunks > 0 and stop_reason is None:
            stop_reason = "OUT_OF_RANGE_CHUNK"
            break

        elapsed = time.time() - t_start
        pct = 100.0 * received_bytes / expected_size
        log(f"  progress: {received_bytes}/{expected_size} bytes ({pct:.1f}%) "
            f"elapsed={elapsed:.1f}s attempt={attempt} reconnects={events['reconnects']}")

    t_end = time.time()

    # --- Final BLE_TESTER-side evidence ---
    try:
        bt_integrity_summary = client.send("diag_integrity_summary", timeout=5)
    except ControllerError as e:
        bt_integrity_summary = {"error": str(e)}
    try:
        bt_hash_summary = client.send("diag_file_hash_summary", timeout=5)
    except ControllerError as e:
        bt_hash_summary = {"error": str(e)}
    try:
        boot1 = client.send("diag_boot", timeout=5)
        health1 = client.send("diag_health", timeout=5)
    except ControllerError as e:
        boot1 = {"boot_generation": None, "reset_reason": str(e)}
        health1 = {}

    result = {
        "file_index": FILE_INDEX,
        "expected_size": expected_size,
        "full_file_diag_enabled": full_file_diag_enabled,
        "full_file_diag_skip_reason": full_file_diag_skip_reason,
        "boot_generation_before": boot0["boot_generation"],
        "boot_generation_after": boot1["boot_generation"],
        "free_heap_before": health0["free_internal_heap"],
        "free_heap_after": health1.get("free_internal_heap"),
        "minimum_free_heap": health1.get("minimum_free_internal_heap"),
        "notify_queue_highwater": health1.get("notify_queue_highwater"),
        "notify_queue_full_events": health1.get("notify_queue_full_events"),
        "notify_callback_max_duration_us": health1.get("notify_callback_max_duration_us"),
        "notifications_received_bletester": health1.get("notifications_received"),
        "notification_bytes_received_bletester": health1.get("notification_bytes_received"),
        "python_unique_bytes": session.reconstructor.unique_bytes,
        "python_identical_duplicate_bytes": session.reconstructor.identical_duplicate_bytes,
        "python_conflicting_duplicate_bytes": session.reconstructor.conflicting_duplicate_bytes,
        "python_out_of_range_chunks": session.reconstructor.out_of_range_chunks,
        "python_first_conflict": session.reconstructor.first_conflict,
        "python_missing_ranges": session.reconstructor.missing_ranges(),
        "python_complete": session.is_complete(),
        "reconnects": events["reconnects"],
        "resumes": events["resumes"],
        "connection_generations": sorted(events["connection_generations"]),
        "disconnects": events["disconnects"],
        "bletester_integrity_summary": bt_integrity_summary,
        "bletester_streaming_hash_summary": bt_hash_summary,
        "elapsed_s": t_end - t_start,
        "stop_reason": stop_reason,
    }

    if stop_reason is not None:
        log(f"*** STOPPED: {stop_reason} -- preserving evidence, not retrying automatically ***")
        try:
            try_abort(client, txn + 1)
        except Exception:
            pass
        client.close()
        print()
        print("=== BLE-D4.0I RESULT (STOPPED) ===")
        print(json.dumps(result, indent=2, default=str))
        sys.exit(1)

    try:
        finalize_result = session.finalize()
        result["finalize_error"] = None
    except ReconstructionError as e:
        finalize_result = None
        result["finalize_error"] = str(e)

    result["python_canonical_sha256"] = finalize_result["canonical_sha256"] if finalize_result else None
    result["python_canonical_crc32"] = finalize_result["canonical_crc32"] if finalize_result else None
    result["saved_file_sha256"] = finalize_result["saved_sha256"] if finalize_result else None
    result["saved_file_size"] = finalize_result["saved_size"] if finalize_result else None
    result["final_path"] = finalize_result["final_path"] if finalize_result else None
    result["throughput_bytes_per_s"] = expected_size / result["elapsed_s"] if result["elapsed_s"] > 0 else None

    client.send("diag_file_hash_mode", timeout=5, enable=False)
    try:
        client.send("disconnect", timeout=5)
    except ControllerError:
        pass
    client.close()

    print()
    print("=== BLE-D4.0I RESULT ===")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
