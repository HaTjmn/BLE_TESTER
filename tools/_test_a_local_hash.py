#!/usr/bin/env python3
"""
BLE-D4.0C Step 7, Test A -- local-hash-only reception. BLE_TESTER hashes
the File Transfer Data notification stream locally; nothing is forwarded
over UART for that characteristic. This removes UART serialization and
Python file writing from the data path entirely, to determine whether BLE
notification reception ALONE (inside BLE_TESTER's own firmware) is what
crashes it.

Crash detection is via boot_generation (ble_central.c's RTC-backed
counter): a mid-attempt jump proves a reboot occurred, independent of
whatever the UART link happens to do during/after it.
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID,
    HLV_OPCODE_START_FILE_DOWNLOAD, HLV_RESPONSE_PHASE_ACCEPTED,
    encode_hlv_message, wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
FILE_INDEX = int(sys.argv[2]) if len(sys.argv) > 2 else 1
EXPECTED_SIZE = int(sys.argv[3]) if len(sys.argv) > 3 else 17280
ATTEMPTS = int(sys.argv[4]) if len(sys.argv) > 4 else 3

RAW_CAPTURE = os.path.join(os.path.dirname(__file__), "_evidence", "test_a_raw_capture.jsonl")
os.makedirs(os.path.dirname(RAW_CAPTURE), exist_ok=True)


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


results = []
client = HolterBLEClient(PORT, baud=115200, default_timeout=15, raw_capture_path=RAW_CAPTURE)

for attempt in range(1, ATTEMPTS + 1):
    log(f"=== attempt {attempt}/{ATTEMPTS} ===")
    result = {"attempt": attempt}
    try:
        boot0 = client.send("diag_boot", timeout=5)
        boot_gen_start = boot0["boot_generation"]
        log(f"boot_generation before: {boot_gen_start} (reset_reason={boot0['reset_reason']})")

        base_txn = connect_and_prepare(client)
        health0 = client.send("diag_health", timeout=5)
        log(f"health before: heap={health0['free_internal_heap']} "
            f"min_heap={health0['minimum_free_internal_heap']} "
            f"stack_hwm={health0['nimble_host_task_stack_highwater_words']}")

        client.send("diag_file_hash_mode", timeout=5, enable=True)

        txn = base_txn + 1
        msg = encode_hlv_message(HLV_OPCODE_START_FILE_DOWNLOAD, txn,
                                  FILE_INDEX.to_bytes(2, "little") + (0).to_bytes(4, "little"))
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        accepted = wait_for_command_response(client, txn, window_s=5)
        if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
            raise ControllerError("NOT_ACCEPTED", str(accepted))
        log("START_FILE_DOWNLOAD accepted; polling diag_file_hash_summary + diag_boot")

        deadline = time.time() + 60
        crashed = False
        completed = False
        last_summary = None
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                boot_now = client.send("diag_boot", timeout=4)
                if boot_now["boot_generation"] != boot_gen_start:
                    crashed = True
                    log(f"*** REBOOT DETECTED *** boot_generation {boot_gen_start} -> "
                        f"{boot_now['boot_generation']}, reset_reason={boot_now['reset_reason']}")
                    result["reboot_reset_reason"] = boot_now["reset_reason"]
                    result["reboot_reset_reason_code"] = boot_now["reset_reason_code"]
                    result["boot_generation_after"] = boot_now["boot_generation"]
                    break
                summary = client.send("diag_file_hash_summary", timeout=4)
                last_summary = summary
                log(f"  progress: bytes={summary['bytes_received']}/{EXPECTED_SIZE} "
                    f"notifications={summary['notifications_received']} "
                    f"gaps={summary['gap_count']} dups={summary['duplicate_count']}")
                if summary["bytes_received"] >= EXPECTED_SIZE:
                    completed = True
                    break
            except ControllerError as e:
                log(f"  poll failed ({e}) -- treating as possible reboot-in-progress, retrying")
                time.sleep(1.0)

        if crashed:
            result["outcome"] = "CRASHED"
        elif completed:
            result["outcome"] = "COMPLETED"
            result["summary"] = last_summary
            log(f"COMPLETED: {last_summary}")
        else:
            result["outcome"] = "TIMED_OUT"
            result["summary"] = last_summary
            log(f"TIMED OUT waiting for completion. last summary: {last_summary}")

        try:
            client.send("diag_file_hash_mode", timeout=5, enable=False)
        except ControllerError:
            pass

    except ControllerError as e:
        result["outcome"] = "ERROR"
        result["error"] = str(e)
        log(f"attempt {attempt} ERROR: {e} response={getattr(e, 'response', None)}")
        try:
            client.close()
        except Exception:
            pass
        time.sleep(1.0)
        client = HolterBLEClient(PORT, baud=115200, default_timeout=15, raw_capture_path=RAW_CAPTURE)

    results.append(result)
    if result.get("outcome") == "CRASHED":
        log("Stopping early: a reset occurred, per Step 7's own instruction "
            "('Run at least three bounded attempts unless a reset occurs earlier').")
        break

try:
    client.close()
except Exception:
    pass

print()
print("=== TEST A RESULTS ===")
for r in results:
    print(" ", r)

raw_lines = []
if os.path.exists(RAW_CAPTURE):
    with open(RAW_CAPTURE, encoding="utf-8") as f:
        raw_lines = f.readlines()
print(f"\nraw non-JSON lines captured: {len(raw_lines)} (see {RAW_CAPTURE})")
if raw_lines:
    print("--- raw capture contents ---")
    for line in raw_lines:
        print(line.rstrip())
