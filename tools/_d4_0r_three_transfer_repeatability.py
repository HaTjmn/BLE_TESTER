#!/usr/bin/env python3
"""
BLE-D4.0R -- three consecutive end-to-end downloads of the existing
12,301,872-byte deterministic TLIV file (file_index=3, generated in
BLE-D4.0Q), over ONE uninterrupted BLE connection and ONE authenticated
session. Pure orchestration of already-existing APIs -- no reconstruction,
retry, protocol, or timing change of any kind.

Architecture (unchanged from BLE-D4.0P/Q): HolterLEV owns SD read + BLE
TX + local retry; BLE_TESTER owns bounded notification forwarding + its
own O(1) streaming SHA-256 (whole-file diag_integrity_start is
deliberately never used -- the file exceeds BLE_TESTER's own
MAX_DIAG_FILE_SIZE); Python owns absolute-offset reconstruction, a fresh
.part file per run, fsync, close, reopen, verify, rename-on-success only.

Usage:
    python _d4_0r_three_transfer_repeatability.py
"""
import sys, os, time, json, re, threading

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from file_reconstructor import FileDownloadSession, ReconstructionError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID,
    HLV_STATUS_UUID,
    HLV_OPCODE_GET_FILE_INFO, HLV_OPCODE_LIST_FILES,
    HLV_OPCODE_START_FILE_DOWNLOAD, HLV_OPCODE_ABORT_FILE_DOWNLOAD,
    HLV_RESPONSE_PHASE_ACCEPTED,
    encode_hlv_message, decode_status_bytes, decode_list_files_response,
    decode_get_file_info_response, decode_file_chunk_header,
    wait_for_command_response, authenticate_raw,
)

HLV_OPCODE_DIAG_DELETE_INVALID_TEST_FILE = 0x0028
HLV_OPCODE_DIAG_GET_TRANSFER_STATS = 0x0029
HLV_STATUS_V2_UUID = "7b8e0045-6a52-4f4d-9a33-71c02e6b4d90"
HLV_DIAG_RETRY_STATS_PAGE = 17

DELETE_ERROR_NAMES = {
    0: "NONE (deleted)", 1: "NOT_IDLE", 2: "SD_NOT_READY", 3: "DOWNLOAD_ACTIVE",
    4: "DELETE_ACTIVE", 5: "ALREADY_RUNNING", 6: "FILE_NOT_FOUND",
    7: "VALIDATE_CALL_FAILED", 8: "FILE_IS_VALID (refused -- safety gate)",
    9: "UNLINK_FAILED",
}

HOLTERLEV_PORT = "COM23"
BLETESTER_PORT = "COM17"

FILE_INDEX = 3
EXPECTED_SIZE = 12301872
EXPECTED_SOURCE_SHA256 = "e898365da5098f6a7680710132ba0335a3ed750e6954101c8555d881bdfcd79c"
EXISTING_SIZES = sorted([45984, 945792, 12301872])

NO_PROGRESS_TIMEOUT_S = 90
TOTAL_TEST_TIMEOUT_S = 45 * 60

SCRATCHPAD_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRATCHPAD_DIR, "d4_0r_downloads")
EVIDENCE_DIR = os.path.join(SCRATCHPAD_DIR, "d4_0r_evidence")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

CONSOLE_LOG_PATH = os.path.join(EVIDENCE_DIR, "holterlev_console_capture.txt")
RAW_CAPTURE_PATH = os.path.join(EVIDENCE_DIR, "bletester_raw_lines.jsonl")
RESULT_PATH = os.path.join(EVIDENCE_DIR, "result.json")

RESET_MARKERS = [
    re.compile(r"rst:0x", re.IGNORECASE),
    re.compile(r"Guru Meditation", re.IGNORECASE),
    re.compile(r"stack overflow", re.IGNORECASE),
    re.compile(r"CORRUPTED", re.IGNORECASE),
    re.compile(r"abort\(\) was called", re.IGNORECASE),
    re.compile(r"assert failed", re.IGNORECASE),
    re.compile(r"Backtrace:", re.IGNORECASE),
]

TEST_START_TIME = time.time()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_overall_timeout():
    if time.time() - TEST_START_TIME > TOTAL_TEST_TIMEOUT_S:
        raise RuntimeError(f"Overall 45-minute test timeout exceeded")


class ConsoleWatcher(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.reboot_event = threading.Event()
        self.first_marker_line = None
        self.stop_flag = threading.Event()
        self._ser = None

    def run(self):
        try:
            self._ser = serial.Serial(self.port, 115200, timeout=0.5)
        except Exception as e:
            log(f"ConsoleWatcher: FATAL -- could not open {self.port}: {e}")
            return
        buf = b""
        with open(CONSOLE_LOG_PATH, "ab") as f:
            while not self.stop_flag.is_set():
                try:
                    chunk = self._ser.read(4096)
                except Exception as e:
                    log(f"ConsoleWatcher: read error: {e}")
                    break
                if not chunk:
                    continue
                f.write(chunk)
                f.flush()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode(errors="replace")
                    if not self.reboot_event.is_set():
                        for pat in RESET_MARKERS:
                            if pat.search(text):
                                self.first_marker_line = text
                                self.reboot_event.set()
                                log(f"*** ConsoleWatcher: RESET MARKER DETECTED: {text!r} ***")
                                break
        try:
            self._ser.close()
        except Exception:
            pass

    def stop(self):
        self.stop_flag.set()


def read_boot_id(client):
    resp = client.send("read", timeout=6, characteristic=HLV_STATUS_V2_UUID, timeout_ms=5000)
    data = bytes.fromhex(resp["data_hex"])
    return data[24] | (data[25] << 8) | (data[26] << 16) | (data[27] << 24)


def encode_diag_get_transfer_stats_request(page):
    return bytes([page & 0xFF])


def decode_diag_get_transfer_stats_aggregate(inner):
    def u32(o):
        return inner[o] | (inner[o+1] << 8) | (inner[o+2] << 16) | (inner[o+3] << 24)
    def u16(o):
        return inner[o] | (inner[o+1] << 8)
    return {
        "chunks_prepared": u32(0), "chunks_scheduled": u32(4), "schedule_failures": u32(8),
        "notify_attempts": u32(12), "notify_successes": u32(16), "notify_alloc_failures": u32(20),
        "notify_rc_failures": u32(24), "pending_send_callbacks": u32(28), "app_outstanding_max": u32(32),
        "worker_queue_highwater": u32(36), "worker_queue_full_count": u32(40),
        "min_free_internal_heap": u32(44), "largest_free_internal_block_min_ever": u32(48),
        "schedule_to_complete_us_min": u32(52), "schedule_to_complete_us_max": u32(56),
        "schedule_to_complete_us_avg": u32(60),
        "msys1_blocks": u16(64), "msys1_min_free_ever": u16(66),
        "msys2_blocks": u16(68), "msys2_min_free_ever": u16(70),
        "acl_blocks": u16(72), "acl_min_free_ever": u16(74),
        "failure_count_total": u32(76),
    }


def decode_diag_get_transfer_stats_ring_page(inner):
    def u16(o):
        return inner[o] | (inner[o+1] << 8)
    def u32(o):
        return inner[o] | (inner[o+1] << 8) | (inner[o+2] << 16) | (inner[o+3] << 24)
    def u64(o):
        v = 0
        for i in range(8):
            v |= inner[o+i] << (8*i)
        return v
    page = {
        "page_index": inner[0], "total_pages": inner[1],
        "snapshot_generation": u32(2), "failure_count_total": u32(6),
        "failure_ring_overwritten_count": u32(10), "entries_in_page": inner[14],
    }
    # BLE-D4.0R: the ring entry (offset 15, 61 bytes/entry, at most 1
    # entry/page per the BLE-D4.0O 1-entry/16-page wire redesign) is the
    # ONLY on-wire source of a genuine point-in-time heap/mempool
    # snapshot -- captured at the exact moment of a transient failure,
    # never on-demand. Decoded here (not just the page header) so a
    # per-run "closest available point-in-time reading" can be reported
    # honestly, distinct from the aggregate's historical-minimum fields.
    if page["entries_in_page"] >= 1:
        e = 15
        page["last_entry"] = {
            "ring_seq": u32(e+0), "ts_us": u64(e+4),
            "transfer_generation": u32(e+12), "chunk_sequence": u32(e+16),
            "file_offset": u32(e+20), "requested_len": u16(e+24),
            "conn_handle": u16(e+26), "was_alloc_failure": bool(inner[e+28]),
            "notify_rc": u32(e+29),
            "msys1_free": u16(e+33), "msys1_min_free_ever": u16(e+35), "msys1_blocks": u16(e+37),
            "msys2_free": u16(e+39), "msys2_min_free_ever": u16(e+41), "msys2_blocks": u16(e+43),
            "acl_free": u16(e+45), "acl_min_free_ever": u16(e+47), "acl_blocks": u16(e+49),
            "worker_queue_depth": u16(e+51),
            "free_internal_heap": u32(e+53), "largest_free_internal_block": u32(e+57),
        }
    else:
        page["last_entry"] = None
    return page


def decode_diag_get_retry_stats(inner):
    def u32(o):
        return inner[o] | (inner[o+1] << 8) | (inner[o+2] << 16) | (inner[o+3] << 24)
    return {
        "transient_failures_detected": u32(0), "local_retries_scheduled": u32(4),
        "local_retry_attempts": u32(8), "local_retry_successes": u32(12),
        "max_retries_for_one_chunk": u32(16), "cumulative_retry_delay_ms": u32(20),
        "retry_exhaustion_count": u32(24), "stale_retry_events_ignored": u32(28),
        "timer_arm_failures": u32(32), "pending_chunk_checksum_mismatches": u32(36),
        "total_start_events": u32(40),
    }


def query_transfer_stats(client, base_txn, page):
    txn = base_txn + page + 1
    msg = encode_hlv_message(HLV_OPCODE_DIAG_GET_TRANSFER_STATS, txn, encode_diag_get_transfer_stats_request(page))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, txn, window_s=6)
    if cr is None:
        raise RuntimeError(f"no Command Response for DIAG_GET_TRANSFER_STATS page={page}")
    if cr["result_code"] != 0:
        raise RuntimeError(f"DIAG_GET_TRANSFER_STATS page={page} result_code={cr['result_code']}")
    return cr["inner"]


def collect_full_diagnostics(client, base_txn):
    aggregate = decode_diag_get_transfer_stats_aggregate(query_transfer_stats(client, base_txn, 0))
    ring_pages = []
    page1 = decode_diag_get_transfer_stats_ring_page(query_transfer_stats(client, base_txn, 1))
    total_pages = page1["total_pages"]
    ring_pages.append(page1)
    for p in range(2, total_pages + 1):
        ring_pages.append(decode_diag_get_transfer_stats_ring_page(query_transfer_stats(client, base_txn, p)))
    retry_stats = decode_diag_get_retry_stats(query_transfer_stats(client, base_txn, HLV_DIAG_RETRY_STATS_PAGE))
    snapshot_generations = {p["snapshot_generation"] for p in ring_pages}
    snapshot_coherent = (len(snapshot_generations) == 1) and (len(ring_pages) == total_pages)

    # The genuinely-current on-wire heap/mempool snapshot exists ONLY at
    # the exact moment of a transient failure (holter_ble_transfer_diag.c's
    # failure path) -- there is no on-demand "current" query anywhere in
    # the firmware (confirmed by source inspection: the periodic UART
    # telemetry in holter_diagnostics.c prints only GAP/SD/stack-HWM
    # summaries, never heap_caps_get_free_size()/largest_free_block(),
    # and the boot-time HEAP: line in print_log.c never repeats). So the
    # single most-recent ring entry (by ring_seq) across all pages is
    # surfaced here as the closest available point-in-time reading for
    # this run, explicitly labeled as NOT a clean run-boundary snapshot.
    entries_with_data = [p["last_entry"] for p in ring_pages if p["last_entry"] is not None]
    most_recent_entry = max(entries_with_data, key=lambda e: e["ring_seq"]) if entries_with_data else None

    return {
        "aggregate": aggregate,
        "retry_stats": retry_stats,
        "snapshot_generations_seen": sorted(snapshot_generations),
        "pages_retrieved": len(ring_pages),
        "total_pages_declared": total_pages,
        "snapshot_coherent_across_pages": snapshot_coherent,
        "failure_ring_overwritten_count": ring_pages[0]["failure_ring_overwritten_count"] if ring_pages else None,
        "point_in_time_snapshot_at_most_recent_transient_failure": most_recent_entry,
        "point_in_time_snapshot_note": (
            "This is a point-in-time heap/mempool reading captured at the moment of the most "
            "recent transient failure during THIS run's own diagnostic epoch (ring_seq shown), "
            "NOT a clean before/after run-boundary snapshot. No on-demand 'current heap/mempool' "
            "query exists anywhere in the firmware without a transient failure having just "
            "occurred -- confirmed by source inspection, not changed for this task. "
            "None if this run had zero transient failures."
        ),
    }


def try_abort(client, txn):
    try:
        msg = encode_hlv_message(HLV_OPCODE_ABORT_FILE_DOWNLOAD, txn)
        client.send("write", timeout=4, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    except ControllerError:
        pass


def run_one_transfer(client, watcher, run_number, base_txn, boot_id_before_this_run):
    """Runs exactly one transfer over the ALREADY-established connection/session.
    Returns (result_dict, stop_reason_or_None). Never reconnects, never
    re-authenticates -- the caller's own connection/session is reused
    verbatim."""
    out_path = os.path.join(OUTPUT_DIR, f"ECG{FILE_INDEX:05d}_run{run_number}.TLV")
    session = FileDownloadSession(out_path, EXPECTED_SIZE)

    txn = base_txn + (run_number * 100000)
    t_start = time.time()
    t_last_progress = time.time()
    transfer_id = None
    received_bytes = 0
    stop_reason = None

    txn += 1
    start_msg = encode_hlv_message(
        HLV_OPCODE_START_FILE_DOWNLOAD, txn, FILE_INDEX.to_bytes(2, "little") + (0).to_bytes(4, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
    accepted = wait_for_command_response(client, txn, window_s=5)
    if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
        return None, f"START_FILE_DOWNLOAD not accepted for run {run_number}: {accepted}"
    log(f"  Run {run_number}: START_FILE_DOWNLOAD accepted (txn={txn}) -- the ONLY START for this run")

    last_log_pct = -1
    disconnected_this_run = False
    while not session.is_complete():
        check_overall_timeout()
        if watcher.reboot_event.is_set():
            stop_reason = "RESET_DURING_TRANSFER (console marker)"
            break
        if time.time() - t_last_progress > NO_PROGRESS_TIMEOUT_S:
            stop_reason = "NO_PROGRESS_TIMEOUT"
            break

        ev = client.wait_for_event(timeout=5)
        if ev is None:
            continue
        if ev.get("event") == "disconnected":
            disconnected_this_run = True
            stop_reason = "BLE_DISCONNECT_OCCURRED"
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
            pct = int(100.0 * received_bytes / EXPECTED_SIZE)
            if pct != last_log_pct and pct % 10 == 0:
                last_log_pct = pct
                log(f"  Run {run_number}: {received_bytes}/{EXPECTED_SIZE} bytes ({pct}%) elapsed={time.time()-t_start:.1f}s")

        r = session.reconstructor
        if r.conflicting_duplicate_bytes > 0:
            stop_reason = "CONFLICTING_DUPLICATE"
            break
        if r.out_of_range_chunks > 0:
            stop_reason = "OUT_OF_RANGE_CHUNK"
            break

    t_end = time.time()

    result = {
        "run_number": run_number,
        "file_index": FILE_INDEX,
        "expected_size": EXPECTED_SIZE,
        "start_file_download_count": 1,
        "elapsed_s": t_end - t_start,
        "received_bytes": received_bytes,
        "stop_reason": stop_reason,
    }

    if stop_reason is not None:
        try_abort(client, txn + 1)
        return result, stop_reason

    # ---- Finalize + verify the PC-side file ----
    try:
        finalize_result = session.finalize()
        result["finalize_error"] = None
    except ReconstructionError as e:
        finalize_result = None
        result["finalize_error"] = str(e)
        return result, f"FINALIZE_ERROR: {e}"

    result["python_unique_bytes"] = session.reconstructor.unique_bytes
    result["python_identical_duplicate_bytes"] = session.reconstructor.identical_duplicate_bytes
    result["python_conflicting_duplicate_bytes"] = session.reconstructor.conflicting_duplicate_bytes
    result["python_out_of_range_chunks"] = session.reconstructor.out_of_range_chunks
    result["python_canonical_sha256"] = finalize_result["canonical_sha256"]
    result["saved_file_sha256"] = finalize_result["saved_sha256"]
    result["saved_file_size"] = finalize_result["saved_size"]
    result["final_path"] = finalize_result["final_path"]
    result["throughput_bytes_per_s"] = EXPECTED_SIZE / result["elapsed_s"] if result["elapsed_s"] > 0 else None

    if result["saved_file_size"] != EXPECTED_SIZE:
        return result, f"SAVED_FILE_SIZE_MISMATCH: {result['saved_file_size']} != {EXPECTED_SIZE}"
    if result["saved_file_sha256"] != EXPECTED_SOURCE_SHA256 or result["python_canonical_sha256"] != EXPECTED_SOURCE_SHA256:
        return result, "SAVED_OR_CANONICAL_SHA256_MISMATCH"

    # ---- Query diagnostics (post-transfer, before the NEXT run's own auto-reset) ----
    try:
        result["diagnostics"] = collect_full_diagnostics(client, txn)
    except Exception as e:
        return result, f"DIAGNOSTIC_RETRIEVAL_FAILED: {e}"

    # ---- BLE_TESTER streaming hash ----
    try:
        bletester_hash_summary = client.send("diag_file_hash_summary", timeout=5)
    except ControllerError as e:
        bletester_hash_summary = {"error": str(e)}
    result["bletester_streaming_hash_summary"] = bletester_hash_summary

    if bletester_hash_summary.get("sha256") != EXPECTED_SOURCE_SHA256 or not bletester_hash_summary.get("valid"):
        return result, "BLETESTER_STREAMING_HASH_MISMATCH_OR_INVALID"

    # ---- Boot ID must be unchanged ----
    boot_id_after = read_boot_id(client)
    result["holterlev_boot_id_before_this_run"] = boot_id_before_this_run
    result["holterlev_boot_id_after_this_run"] = boot_id_after
    if boot_id_after != boot_id_before_this_run:
        return result, "BOOT_ID_CHANGED"

    # ---- Connection generation unchanged (still the SAME BLE connection) ----
    conn_status = client.send("controller_status", timeout=5)
    result["connection_generation"] = conn_status["connection_generation"]

    return result, None


def connect_and_prepare(client):
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


def main():
    watcher = ConsoleWatcher(HOLTERLEV_PORT)
    watcher.start()
    time.sleep(0.5)

    # ================= Preflight (one controlled reboot/reset of HolterLEV
    # already performed manually right before this script -- see the task
    # report's own record; the reset_controller call below is BLE_TESTER's
    # own equivalent one-time reset) =================
    client = HolterBLEClient(BLETESTER_PORT, baud=115200, default_timeout=15, raw_capture_path=RAW_CAPTURE_PATH)
    client.send("reset_controller", timeout=5)  # the ONE BLE_TESTER-side reset for this whole 3-run session
    bletester_boot0 = client.send("diag_boot", timeout=5)
    log(f"BLE_TESTER boot_generation_before={bletester_boot0['boot_generation']} (informational only)")

    base_txn = connect_and_prepare(client)
    log("connected + authenticated -- this ONE connection/session is reused for all 3 transfers")

    result = {"runs": []}

    status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    status = decode_status_bytes(status_raw["data_hex"])
    log(f"GET_STATUS: {status}")
    result["preflight_status"] = status
    if status["session_state"] != 1 or not status["sd_ready"]:
        log("FATAL: not READY/sd_ready. ABORTING.")
        watcher.stop(); client.close(); sys.exit(2)

    list_msg = encode_hlv_message(HLV_OPCODE_LIST_FILES, base_txn + 2, (0).to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=list_msg.hex())
    cr = wait_for_command_response(client, base_txn + 2, window_s=5)
    listing = decode_list_files_response(cr["inner"])
    log(f"LIST_FILES: {listing}")
    result["preflight_list_files"] = listing
    sizes = sorted(e["size_bytes"] for e in listing["entries"])
    if listing["count_returned"] != 3 or sizes != EXISTING_SIZES:
        log(f"FATAL: expected exactly 3 files with sizes {EXISTING_SIZES}, found {sizes}. ABORTING.")
        watcher.stop(); client.close(); sys.exit(3)

    info_msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, base_txn + 3, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=info_msg.hex())
    cr = wait_for_command_response(client, base_txn + 3, window_s=5)
    info = decode_get_file_info_response(cr["inner"])
    log(f"GET_FILE_INFO(file_index={FILE_INDEX}): {info}")
    result["preflight_get_file_info"] = info
    if not info["exists"] or info["size_bytes"] != EXPECTED_SIZE:
        log("FATAL: GET_FILE_INFO mismatch. ABORTING.")
        watcher.stop(); client.close(); sys.exit(4)

    del_txn = base_txn + 4
    del_msg = encode_hlv_message(HLV_OPCODE_DIAG_DELETE_INVALID_TEST_FILE, del_txn, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=del_msg.hex())
    accepted_del = wait_for_command_response(client, del_txn, window_s=10)
    if accepted_del is None or accepted_del["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
        log(f"FATAL: DIAG_DELETE_INVALID_TEST_FILE (reopen-hash preflight) not accepted: {accepted_del}. ABORTING.")
        watcher.stop(); client.close(); sys.exit(5)
    # Worker task reopens+hashes the full 12.3MB file from SD before replying COMPLETED -- generous bound.
    terminal = wait_for_command_response(client, del_txn, window_s=90)
    del_err = terminal["inner"][0] if terminal and terminal.get("inner") else None
    log(f"DIAG_DELETE_INVALID_TEST_FILE(preflight reopen-hash): {DELETE_ERROR_NAMES.get(del_err, del_err)}")
    result["preflight_reopen_validate_error"] = del_err
    if del_err != 8:
        log("FATAL: source file did not refuse deletion as VALID. ABORTING.")
        watcher.stop(); client.close(); sys.exit(5)

    time.sleep(0.3)
    reopened_sha256 = None
    try:
        with open(CONSOLE_LOG_PATH, "rb") as f:
            text = f.read().decode(errors="replace")
        m = re.search(
            r"File_index=" + str(FILE_INDEX) + r".*?reopened-SD SHA-256=([0-9a-fA-F]{64}) is_valid=(\d)", text
        )
        if m:
            reopened_sha256 = m.group(1).lower()
    except Exception as e:
        log(f"could not parse reopened SHA-256: {e}")
    result["preflight_reopened_sd_sha256"] = reopened_sha256
    result["preflight_reopened_sd_sha256_matches"] = (reopened_sha256 == EXPECTED_SOURCE_SHA256)
    log(f"reopened_sd_sha256={reopened_sha256} matches={result['preflight_reopened_sd_sha256_matches']}")
    if not result["preflight_reopened_sd_sha256_matches"]:
        log("FATAL: source SHA-256 mismatch. ABORTING.")
        watcher.stop(); client.close(); sys.exit(6)

    holterlev_boot_id = read_boot_id(client)
    log(f"HolterLEV boot_id (baseline)={holterlev_boot_id}")
    result["holterlev_boot_id_baseline"] = holterlev_boot_id

    if watcher.reboot_event.is_set():
        log("FATAL: a reset marker was already seen before Transfer 1 even started. ABORTING.")
        watcher.stop(); client.close(); sys.exit(7)

    # Baseline diagnostic snapshot (device has just rebooted -- expect all zero).
    try:
        baseline_diag = collect_full_diagnostics(client, base_txn + 500)
        log(f"Baseline diagnostic snapshot: {baseline_diag}")
        result["baseline_diagnostics"] = baseline_diag
    except Exception as e:
        log(f"Baseline diagnostic query failed (non-fatal, recorded): {e}")
        result["baseline_diagnostics"] = {"error": str(e)}

    client.send("diag_file_hash_mode", timeout=5, enable=True)
    log("BLE_TESTER O(1) streaming hash forwarding enabled for the whole 3-run session")

    # ================= Three consecutive transfers, same connection/session =================
    boot_id_for_this_run = holterlev_boot_id
    overall_stop_reason = None

    for run_number in (1, 2, 3):
        idle_start = time.time()
        log(f"=== Starting Run {run_number}/3 (idle gap before this run began at script start + "
            f"{idle_start - TEST_START_TIME:.1f}s) ===")

        if watcher.reboot_event.is_set():
            overall_stop_reason = f"RESET_DETECTED_BEFORE_RUN_{run_number}"
            break

        run_result, stop_reason = run_one_transfer(client, watcher, run_number, base_txn, boot_id_for_this_run)
        if run_result is not None:
            run_result["idle_gap_before_run_s"] = idle_start - TEST_START_TIME if run_number == 1 else (
                idle_start - result["runs"][-1]["_run_end_time"]
            )
            run_result["_run_end_time"] = time.time()
            result["runs"].append(run_result)

        if stop_reason is not None:
            log(f"*** STOPPED at Run {run_number}: {stop_reason} -- preserving evidence, "
                f"NOT proceeding to any further run. ***")
            overall_stop_reason = f"RUN_{run_number}: {stop_reason}"
            break

        log(f"Run {run_number}/3 PASSED: elapsed={run_result['elapsed_s']:.1f}s "
            f"throughput={run_result['throughput_bytes_per_s']:.1f} B/s "
            f"transient_failures={run_result['diagnostics']['aggregate']['failure_count_total']}")
        boot_id_for_this_run = run_result["holterlev_boot_id_after_this_run"]

    result["overall_stop_reason"] = overall_stop_reason
    result["total_elapsed_s"] = time.time() - TEST_START_TIME

    client.send("diag_file_hash_mode", timeout=5, enable=False)
    try:
        client.send("disconnect", timeout=5)
    except ControllerError:
        pass
    watcher.stop()
    client.close()

    # Strip internal bookkeeping key before dumping.
    for r in result["runs"]:
        r.pop("_run_end_time", None)

    with open(RESULT_PATH, "w") as f:
        json.dump(result, f, indent=2, default=str)

    print()
    print("=== BLE-D4.0R RESULT ===")
    print(json.dumps(result, indent=2, default=str))

    if overall_stop_reason is not None:
        sys.exit(1)


if __name__ == "__main__":
    main()
