#!/usr/bin/env python3
"""
BLE-D4.0A Step 6 -- bounded baseline transfer with full timing/counter
breakdown. Deliberately a SEPARATE script from holter_file_download.py:
diagnostic instrumentation must not change production behavior, so this
duplicates that tool's resume logic (faithfully, same semantics) rather
than editing it, and adds the timestamps/counters BLE-D4.0A Step 6 asks
for. Read-only towards the SD card; only ever requests a single small,
already-known-good file (default file_index=1, 17280 bytes).
"""
import sys, os, time, hashlib, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID,
    HLV_OPCODE_START_FILE_DOWNLOAD, HLV_OPCODE_ABORT_FILE_DOWNLOAD,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESULT_OK,
    encode_hlv_message, decode_command_response, decode_file_chunk_header,
    wait_for_command_response, authenticate_raw,
)

FILE_INDEX = int(sys.argv[2]) if len(sys.argv) > 2 else 1
EXPECTED_SIZE = int(sys.argv[3]) if len(sys.argv) > 3 else 17280
PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
MAX_RETRIES = 60

t0 = time.time()
marks = {}


def mark(name, extra=""):
    marks[name] = time.time()
    print(f"[t+{time.time() - t0:8.3f}s] {name}{(' -- ' + extra) if extra else ''}", flush=True)


def connect_and_prepare(client):
    client.send("reset_controller", timeout=5)
    mark("command_issued_connect")
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    mark("connected")
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.send("subscribe", timeout=6, characteristic=HLV_FILE_TRANSFER_DATA_UUID, mode="notify")
    mark("subscription_enabled")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    if not status["authorized"]:
        raise ControllerError("AUTH_FAILED", "device rejected the default dev access code")
    mark("authentication_completed")
    return base_txn


def try_abort(client, txn):
    try:
        msg = encode_hlv_message(HLV_OPCODE_ABORT_FILE_DOWNLOAD, txn)
        client.send("write", timeout=4, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    except ControllerError:
        pass


counters = dict(
    reconnects=0, resumes=0, disconnects=0, duplicates=0, gaps=0,
    chunks_received=0, retry_wait_s=0.0, disconnected_s=0.0,
)
holder = {}


def reconnect():
    # BLE-D4.0B Step 2/7 Test 3: reconnect must reuse the existing UART
    # connection (BLE-level only), matching the fix now in
    # holter_file_download.py -- never reopen the physical COM port here.
    t_start = time.time()
    counters["reconnects"] += 1
    client = holder["client"]
    for i in range(5):
        try:
            base_txn = connect_and_prepare(client)
            counters["disconnected_s"] += time.time() - t_start
            return client, base_txn
        except ControllerError as e:
            print(f"  reconnect attempt {i + 1}/5 failed (BLE-level, same UART connection): {e}", flush=True)
            time.sleep(2.0)
    raise RuntimeError("could not reconnect (BLE-level, same UART connection)")


mark("test_start")
holder["client"] = HolterBLEClient(PORT, baud=115200, default_timeout=15)
base_txn = connect_and_prepare(holder["client"])

received = bytearray()
txn = base_txn
attempt = 0
first_chunk_time = None
last_chunk_time = None
first_missing_offset = None

mark("command_issued_start_file_download")

while len(received) < EXPECTED_SIZE:
    attempt += 1
    counters["resumes"] = attempt - 1
    if attempt > MAX_RETRIES:
        print(f"FAILED: only {len(received)}/{EXPECTED_SIZE} bytes after {MAX_RETRIES} attempts")
        break

    holder["client"].drain_events()
    client = holder["client"]

    try:
        txn += 1
        start_msg = encode_hlv_message(
            HLV_OPCODE_START_FILE_DOWNLOAD, txn,
            FILE_INDEX.to_bytes(2, "little") + len(received).to_bytes(4, "little")
        )
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
        accepted = wait_for_command_response(client, txn, window_s=5)
    except ControllerError as e:
        print(f"  attempt {attempt}: connection issue starting download ({e}); reconnecting", flush=True)
        t_wait = time.time()
        client, base_txn = reconnect()
        txn = int(time.time() * 1000) & 0x7FFFFFFF
        continue

    if attempt == 1:
        mark("download_accepted" if accepted and accepted["phase"] == HLV_RESPONSE_PHASE_ACCEPTED else "download_not_accepted_first_attempt")

    if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
        print(f"  attempt {attempt}: START_FILE_DOWNLOAD not accepted ({accepted}); retrying shortly", flush=True)
        t_wait = time.time()
        time.sleep(1.0)
        counters["retry_wait_s"] += time.time() - t_wait
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
                print(f"  attempt {attempt}: stalled at {len(received)}/{EXPECTED_SIZE} bytes, no event for 30s", flush=True)
                break
            if ev.get("event") == "disconnected":
                print(f"  attempt {attempt}: peer disconnected at {len(received)}/{EXPECTED_SIZE} bytes", flush=True)
                counters["disconnects"] += 1
                disconnected = True
                break
            if ev.get("event") != "notification":
                continue
            if ev.get("characteristic") == HLV_FILE_TRANSFER_DATA_UUID:
                chunk = decode_file_chunk_header(ev["data_hex"])
                counters["chunks_received"] += 1
                if first_chunk_time is None:
                    first_chunk_time = time.time()
                    mark("first_chunk_received", f"offset=0 fragment_len={chunk['fragment_length']}")
                last_chunk_time = time.time()
                if transfer_id is None:
                    transfer_id = chunk["transfer_id"]
                if chunk["transfer_id"] != transfer_id or chunk["chunk_offset"] < expected_offset:
                    counters["duplicates"] += 1
                    continue
                if chunk["chunk_offset"] != expected_offset:
                    print(f"  attempt {attempt}: gap detected (expected offset {expected_offset}, "
                          f"got {chunk['chunk_offset']}) at {len(received)}/{EXPECTED_SIZE} bytes", flush=True)
                    counters["gaps"] += 1
                    if first_missing_offset is None:
                        first_missing_offset = expected_offset
                    try_abort(client, txn + 1)
                    break
                received.extend(chunk["fragment"])
                expected_offset = len(received)
                if len(received) >= chunk["total_length"]:
                    break
            elif ev.get("characteristic") == HLV_COMMAND_RESPONSE_UUID:
                decoded = decode_command_response(ev["data_hex"])
                if decoded["transaction_id"] == txn:
                    if decoded["result_code"] != HLV_RESULT_OK:
                        print(f"  attempt {attempt}: DUT ended transfer early, result_code="
                              f"{decoded['result_code']} at {len(received)}/{EXPECTED_SIZE} bytes", flush=True)
                    break
    except ControllerError as e:
        print(f"  attempt {attempt}: connection issue mid-transfer ({e}) at "
              f"{len(received)}/{EXPECTED_SIZE} bytes; reconnecting", flush=True)
        client, base_txn = reconnect()
        txn = int(time.time() * 1000) & 0x7FFFFFFF
        continue

    if disconnected:
        client, base_txn = reconnect()
        txn = int(time.time() * 1000) & 0x7FFFFFFF

mark("last_chunk_or_stop")

trace_resp = None
disc_resp = None
try:
    trace_resp = holder["client"].send("diag_trace", timeout=5, count=64)
except ControllerError as e:
    print(f"diag_trace failed: {e}", flush=True)
try:
    disc_resp = holder["client"].send("diag_disconnect", timeout=5)
except ControllerError as e:
    print(f"diag_disconnect failed: {e}", flush=True)

try:
    holder["client"].close()
except Exception:
    pass
mark("file_closed")

if disc_resp is not None:
    print("last_disconnect_info:", disc_resp)
if trace_resp is not None:
    print("total_transitions_since_boot:", trace_resp.get("total_transitions_since_boot"))
    print("last 15 transitions (most recent first):")
    for t in trace_resp.get("transitions_most_recent_first", [])[:15]:
        print(" ", t)

total_elapsed = time.time() - t0
connected_transfer_s = total_elapsed - counters["disconnected_s"] - counters["retry_wait_s"]

print()
print("=== BASELINE TEST RESULT (file_index=%d, expected=%d bytes) ===" % (FILE_INDEX, EXPECTED_SIZE))
print("bytes_received:", len(received), "/", EXPECTED_SIZE)
print("chunks_received:", counters["chunks_received"])
print("duplicate_chunks:", counters["duplicates"])
print("gaps_outoforder_chunks:", counters["gaps"])
print("first_missing_offset:", first_missing_offset)
print("reconnect_count:", counters["reconnects"])
print("resume_count(attempts-1):", counters["resumes"])
print("disconnect_events:", counters["disconnects"])
print("total_end_to_end_time_s:", round(total_elapsed, 3))
print("retry_wait_time_s:", round(counters["retry_wait_s"], 3))
print("disconnected_time_s:", round(counters["disconnected_s"], 3))
print("connected_transfer_time_s(derived):", round(connected_transfer_s, 3))
if first_chunk_time and last_chunk_time and last_chunk_time > first_chunk_time:
    span = last_chunk_time - first_chunk_time
    print("raw_throughput_while_receiving_Bps:", round(len(received) / span, 1))
print("effective_end_to_end_throughput_Bps:", round(len(received) / total_elapsed, 2) if total_elapsed > 0 else None)

if len(received) == EXPECTED_SIZE:
    print("crc32:", format(zlib.crc32(bytes(received)) & 0xFFFFFFFF, '08x'))
    print("sha256:", hashlib.sha256(bytes(received)).hexdigest())
    print("INTEGRITY: size verified; content hash computed this run")
    out_path = os.path.join(os.path.dirname(__file__), f"_baseline_run_{int(t0)}.bin")
    with open(out_path, "wb") as f:
        f.write(bytes(received))
    print("saved raw bytes to:", out_path)
else:
    print("INTEGRITY: incomplete (size mismatch)")
