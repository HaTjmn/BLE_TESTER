#!/usr/bin/env python3
"""
BLE-D4.0B Step 7, Test 2 -- 20 bounded cycles of
scan -> connect -> discover -> authenticate -> GET_STATUS -> disconnect,
using ONE persistent UART connection throughout (only the BLE link itself
cycles). Records the real captured disconnect reason each time.
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_GET_STATUS, HLV_RESPONSE_PHASE_COMPLETED,
    encode_hlv_message, wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
CYCLES = int(sys.argv[2]) if len(sys.argv) > 2 else 20

t0 = time.time()
def log(msg):
    print(f"[t+{time.time()-t0:7.3f}s] {msg}", flush=True)

client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
completed = 0
cycle_results = []

for cycle in range(1, CYCLES + 1):
    try:
        client.send("reset_controller", timeout=5)
        client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
        client.send("discover_services", timeout=10)
        client.send("discover_characteristics", timeout=10)
        client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
        client.drain_events()
        base_txn = int(time.time() * 1000) & 0x7FFFFFFF
        status = authenticate_raw(client, base_txn)
        if not status["authorized"]:
            raise ControllerError("AUTH_FAILED", "not authorized")

        txn = base_txn + 1
        msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn)
        client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        resp = wait_for_command_response(client, txn, window_s=4)
        if resp is None or resp["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
            raise ControllerError("GET_STATUS_FAILED", str(resp))

        client.send("disconnect", timeout=6)
        disc = client.send("diag_disconnect", timeout=5)
        reason = disc.get("reason_name") if disc.get("valid") else "NOT_CAPTURED"
        locally = disc.get("locally_initiated")
        completed += 1
        cycle_results.append((cycle, "OK", reason, locally))
        log(f"cycle {cycle}: OK, disconnect_reason={reason}, locally_initiated={locally}")
    except ControllerError as e:
        cycle_results.append((cycle, "FAIL", str(e), None))
        log(f"cycle {cycle}: FAIL -- {e} -- response={getattr(e, 'response', None)}")
        try:
            client.send("reset_controller", timeout=5)
        except ControllerError:
            pass
    time.sleep(0.5)

client.close()

print()
print("=== TEST 2 RESULT ===")
print(f"completed: {completed}/{CYCLES}")
for c in cycle_results:
    print(" ", c)

acceptance = completed == CYCLES
print()
print("ACCEPTANCE:", "PASS" if acceptance else "FAIL")
sys.exit(0 if acceptance else 1)
