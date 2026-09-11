#!/usr/bin/env python3
"""
BLE-D4.0G Step 6 -- exactly ONE bounded file-transfer attempt over the new
dedicated protocol channel (native USB-Serial/JTAG), while UART0's raw
output is captured independently and concurrently by a separate process
(_raw_uart0_capture.py on COM28). This is a direct adaptation of
_test_integrity_boundary.py (BLE-D4.0F) -- same file, same absolute-offset
dual-sided comparison -- but capped at one attempt, per this task's
explicit "exactly one bounded attempt" constraint, and pointed at the new
channel instead of the old shared UART0 wire.
"""
import sys, os, time, hashlib, zlib, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID,
    HLV_OPCODE_GET_FILE_INFO, HLV_OPCODE_START_FILE_DOWNLOAD, HLV_OPCODE_ABORT_FILE_DOWNLOAD,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_COMPLETED, HLV_RESULT_OK,
    encode_hlv_message, decode_command_response, decode_get_file_info_response, decode_file_chunk_header,
    wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM17"
FILE_INDEX = int(sys.argv[2]) if len(sys.argv) > 2 else 1
EXPECTED_SIZE_ARG = int(sys.argv[3]) if len(sys.argv) > 3 else 17280
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
EVIDENCE_DIR = os.path.join(os.path.dirname(__file__), "_evidence")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class PyIntegrityBuffer:
    def __init__(self, expected_size):
        self.expected_size = expected_size
        self.buf = bytearray(expected_size)
        self.received = bytearray(expected_size)
        self.gen_at_offset = [0] * expected_size
        self.notifications = 0
        self.unique_bytes = 0
        self.identical_duplicates = 0
        self.conflicting_duplicates = 0
        self.out_of_range_chunks = 0
        self.first_conflict = None

    def ingest(self, chunk_offset, fragment, connection_generation):
        self.notifications += 1
        if chunk_offset < 0 or chunk_offset + len(fragment) > self.expected_size:
            self.out_of_range_chunks += 1
            return
        for i, b in enumerate(fragment):
            off = chunk_offset + i
            if not self.received[off]:
                self.buf[off] = b
                self.received[off] = 1
                self.gen_at_offset[off] = connection_generation
                self.unique_bytes += 1
            elif self.buf[off] == b:
                self.identical_duplicates += 1
            else:
                self.conflicting_duplicates += 1
                if self.first_conflict is None:
                    self.first_conflict = {
                        "offset": off, "old_byte": self.buf[off], "new_byte": b,
                        "old_generation": self.gen_at_offset[off], "new_generation": connection_generation,
                    }

    def summary(self):
        complete = self.unique_bytes >= self.expected_size
        first_missing = None
        if not complete:
            for i in range(self.expected_size):
                if not self.received[i]:
                    first_missing = i
                    break
        out = {
            "expected_size": self.expected_size,
            "notifications": self.notifications,
            "unique_bytes": self.unique_bytes,
            "identical_duplicates": self.identical_duplicates,
            "conflicting_duplicates": self.conflicting_duplicates,
            "out_of_range_chunks": self.out_of_range_chunks,
            "complete": complete,
            "first_missing_offset": first_missing,
            "first_conflict": self.first_conflict,
        }
        if complete:
            out["crc32"] = format(zlib.crc32(bytes(self.buf)) & 0xFFFFFFFF, "08x")
            out["sha256"] = hashlib.sha256(bytes(self.buf)).hexdigest()
        return out


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


def run_attempt(client_holder, file_index, expected_size):
    client = client_holder["client"]
    result = {}

    boot0 = client.send("diag_boot", timeout=5)
    result["boot_generation_before"] = boot0["boot_generation"]
    health0 = client.send("diag_health", timeout=5)
    result["pending_active_before"] = health0["pending_operation_active"]

    start_resp = client.send("diag_integrity_start", timeout=5, expected_size=expected_size)
    log(f"  diag_integrity_start: {start_resp}")

    base_txn = connect_and_prepare(client)
    status = client.send("controller_status", timeout=5)
    conn_gen = status["connection_generation"]
    log(f"  connected, connection_generation={conn_gen}")

    py_buf = PyIntegrityBuffer(expected_size)
    received = bytearray()
    reconnect_count = 0
    resume_count = 0
    gap_count = 0
    first_gap_offset = None
    txn = base_txn
    attempt = 0
    max_retries = 300
    connection_generations_seen = {conn_gen}
    max_queue_depth_seen = 0
    max_queue_highwater_seen = 0
    queue_full_events_seen = 0
    max_notify_cb_duration_us = 0

    def reconnect():
        nonlocal conn_gen
        c = client_holder["client"]
        for i in range(5):
            try:
                new_base = connect_and_prepare(c)
                st = c.send("controller_status", timeout=5)
                conn_gen = st["connection_generation"]
                connection_generations_seen.add(conn_gen)
                return new_base
            except ControllerError as e:
                log(f"    reconnect attempt {i+1}/5 failed: {e}")
                time.sleep(2.0)
        raise RuntimeError("could not reconnect")

    while len(received) < expected_size:
        attempt += 1
        resume_count = attempt - 1
        if attempt > max_retries:
            log(f"  giving up after {max_retries} attempts")
            break
        client.drain_events()
        try:
            txn += 1
            start_msg = encode_hlv_message(HLV_OPCODE_START_FILE_DOWNLOAD, txn,
                                            file_index.to_bytes(2, "little") + len(received).to_bytes(4, "little"))
            client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
            accepted = wait_for_command_response(client, txn, window_s=5)
        except ControllerError as e:
            log(f"  attempt {attempt}: connection issue starting ({e}); reconnecting")
            reconnect_count += 1
            base_txn = reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
            time.sleep(1.0)
            continue

        expected_offset = len(received)
        transfer_id = None
        chunk_deadline = time.time() + 30
        disconnected = False
        try:
            while time.time() < chunk_deadline:
                remaining = chunk_deadline - time.time()
                ev = client.wait_for_event(timeout=remaining)
                if ev is None:
                    break
                if ev.get("event") == "disconnected":
                    disconnected = True
                    break
                if ev.get("event") != "notification":
                    continue
                if ev.get("characteristic") == HLV_FILE_TRANSFER_DATA_UUID:
                    chunk = decode_file_chunk_header(ev["data_hex"])
                    py_buf.ingest(chunk["chunk_offset"], chunk["fragment"], conn_gen)

                    if transfer_id is None:
                        transfer_id = chunk["transfer_id"]
                    if chunk["transfer_id"] != transfer_id or chunk["chunk_offset"] < expected_offset:
                        continue
                    if chunk["chunk_offset"] != expected_offset:
                        gap_count += 1
                        if first_gap_offset is None:
                            first_gap_offset = chunk["chunk_offset"]
                        try_abort(client, txn + 1)
                        break
                    received.extend(chunk["fragment"])
                    expected_offset = len(received)
                    if len(received) >= chunk["total_length"]:
                        break
                elif ev.get("characteristic") == HLV_COMMAND_RESPONSE_UUID:
                    decoded = decode_command_response(ev["data_hex"])
                    if decoded["transaction_id"] == txn:
                        break
        except ControllerError as e:
            log(f"  attempt {attempt}: connection issue mid-transfer ({e}); reconnecting")
            reconnect_count += 1
            base_txn = reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if disconnected:
            reconnect_count += 1
            base_txn = reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF

        # BLE-D4.0G: sample the notify-queue/worker counters as we go so we
        # catch the peak even if the transfer finishes before a final read.
        try:
            h = client.send("diag_health", timeout=5)
            max_queue_depth_seen = max(max_queue_depth_seen, h.get("notify_queue_depth", 0))
            max_queue_highwater_seen = max(max_queue_highwater_seen, h.get("notify_queue_highwater", 0))
            queue_full_events_seen = max(queue_full_events_seen, h.get("notify_queue_full_events", 0))
            max_notify_cb_duration_us = max(max_notify_cb_duration_us, h.get("notify_callback_max_duration_us", 0))
        except ControllerError:
            pass

    py_summary = py_buf.summary()

    try:
        bt_summary = client.send("diag_integrity_summary", timeout=5)
    except ControllerError as e:
        bt_summary = {"error": str(e)}
    log(f"  BLE_TESTER integrity summary: {bt_summary}")

    try:
        boot1 = client.send("diag_boot", timeout=5)
        health1 = client.send("diag_health", timeout=5)
    except ControllerError as e:
        boot1 = {"boot_generation": None, "reset_reason": str(e)}
        health1 = {"pending_operation_active": None}

    max_queue_depth_seen = max(max_queue_depth_seen, health1.get("notify_queue_depth", 0) or 0)
    max_queue_highwater_seen = max(max_queue_highwater_seen, health1.get("notify_queue_highwater", 0) or 0)
    queue_full_events_seen = max(queue_full_events_seen, health1.get("notify_queue_full_events", 0) or 0)
    max_notify_cb_duration_us = max(max_notify_cb_duration_us, health1.get("notify_callback_max_duration_us", 0) or 0)

    saved_path = None
    saved_hash = None
    if len(received) == expected_size:
        saved_path = os.path.join(OUTPUT_DIR, f"ECG{file_index:05d}_d4_0g_step6.TLV")
        with open(saved_path, "wb") as f:
            f.write(bytes(received))
        saved_bytes = open(saved_path, "rb").read()
        saved_hash = {
            "crc32": format(zlib.crc32(saved_bytes) & 0xFFFFFFFF, "08x"),
            "sha256": hashlib.sha256(saved_bytes).hexdigest(),
        }

    result.update({
        "notifications_received_python": py_summary["notifications"],
        "bytes_received_arrival_order": len(received),
        "unique_bytes_python": py_summary["unique_bytes"],
        "identical_duplicates_python": py_summary["identical_duplicates"],
        "conflicting_duplicates_python": py_summary["conflicting_duplicates"],
        "gaps": gap_count,
        "first_gap_offset": first_gap_offset,
        "first_missing_offset_python": py_summary["first_missing_offset"],
        "first_conflict_python": py_summary["first_conflict"],
        "out_of_range_chunks_python": py_summary["out_of_range_chunks"],
        "reconnect_count": reconnect_count,
        "resume_count": resume_count,
        "connection_generations_seen": sorted(connection_generations_seen),
        "python_complete": py_summary["complete"],
        "python_canonical_crc32": py_summary.get("crc32"),
        "python_canonical_sha256": py_summary.get("sha256"),
        "bletester_complete": bt_summary.get("complete"),
        "bletester_canonical_crc32": format(bt_summary["crc32"], "08x") if bt_summary.get("complete") else None,
        "bletester_canonical_sha256": bt_summary.get("sha256"),
        "bletester_conflicting_duplicates": bt_summary.get("conflicting_duplicate_bytes"),
        "bletester_first_conflict": bt_summary.get("first_conflict"),
        "bletester_out_of_range_chunks": bt_summary.get("out_of_range_chunks"),
        "saved_path": saved_path,
        "saved_hash": saved_hash,
        "boot_generation_after": boot1["boot_generation"],
        "reset_reason_after": boot1.get("reset_reason"),
        "pending_active_after": health1["pending_operation_active"],
        "panicked": boot1["boot_generation"] != boot0["boot_generation"],
        "max_notify_queue_depth_seen": max_queue_depth_seen,
        "max_notify_queue_highwater_seen": max_queue_highwater_seen,
        "notify_queue_full_events_seen": queue_full_events_seen,
        "max_notify_callback_duration_us_seen": max_notify_cb_duration_us,
    })
    return result


def main():
    client_holder = {"client": HolterBLEClient(PORT, baud=115200, default_timeout=15)}
    client = client_holder["client"]

    base_txn = connect_and_prepare(client)
    msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, base_txn + 1, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, base_txn + 1, window_s=5)
    info = decode_get_file_info_response(cr["inner"])
    log(f"file_index={FILE_INDEX} info: {info}")
    if not info["exists"]:
        log("FATAL: file does not exist")
        sys.exit(2)
    expected_size = info["size_bytes"]
    if expected_size != EXPECTED_SIZE_ARG:
        log(f"NOTE: reported size {expected_size} != expected {EXPECTED_SIZE_ARG}; using the reported size")

    log("=== SINGLE BOUNDED ATTEMPT (BLE-D4.0G Step 6) ===")
    r = run_attempt(client_holder, FILE_INDEX, expected_size)
    log("attempt summary: panicked=%s bt_hash=%s py_hash=%s" % (
        r["panicked"], r["bletester_canonical_sha256"], r["python_canonical_sha256"]))

    try:
        client.close()
    except Exception:
        pass

    print()
    print("=== BLE-D4.0G STEP 6 RESULT ===")
    print(json.dumps(r, indent=2, default=str))

    print()
    print("=== COMPARISON ===")
    match_bt_py = (r["bletester_canonical_sha256"] == r["python_canonical_sha256"])
    match_py_saved = (r["saved_hash"]["sha256"] == r["python_canonical_sha256"]) if r["saved_hash"] else None
    print(f"BLE_TESTER==Python: {match_bt_py}, Python==saved_file: {match_py_saved}")


if __name__ == "__main__":
    main()
