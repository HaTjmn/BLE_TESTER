#!/usr/bin/env python3
"""
BLE-D4.0B Step 7, Test 1 -- stable control connection: one persistent UART
connection, one BLE connection, 100x GET_STATUS at a controlled interval,
NO file download. Records state transitions, timeouts, disconnects, and
the real captured HCI disconnect reason (if any).
"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_GET_STATUS, HLV_RESPONSE_PHASE_COMPLETED,
    encode_hlv_message, wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 100
INTERVAL_S = 0.3

t0 = time.time()
def log(msg):
    print(f"[t+{time.time()-t0:7.3f}s] {msg}", flush=True)

client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
log("command_issued_connect")
client.send("reset_controller", timeout=5)
client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
log("connected")
client.send("discover_services", timeout=10)
client.send("discover_characteristics", timeout=10)
client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
log("subscription_enabled")
client.drain_events()
base_txn = int(time.time() * 1000) & 0x7FFFFFFF
status = authenticate_raw(client, base_txn)
log(f"authentication_completed authorized={status['authorized']}")

valid = 0
timeouts = 0
errors = []
exception_raised = False
txn = base_txn

try:
    for i in range(N):
        txn += 1
        try:
            msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn)
            client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
            resp = wait_for_command_response(client, txn, window_s=4)
            if resp is not None and resp["phase"] == HLV_RESPONSE_PHASE_COMPLETED:
                valid += 1
            else:
                timeouts += 1
                errors.append((i, "no_completed_response", str(resp)))
                log(f"  GET_STATUS #{i+1}: no COMPLETED response ({resp})")
        except ControllerError as e:
            timeouts += 1
            errors.append((i, e.error_code, str(e.response)))
            log(f"  GET_STATUS #{i+1}: ControllerError {e.error_code} -- {e.response}")
        time.sleep(INTERVAL_S)
except Exception as e:
    exception_raised = True
    log(f"UNEXPECTED EXCEPTION: {type(e).__name__}: {e}")

log(f"loop finished: {valid}/{N} valid responses, {timeouts} failures")

# Pull diagnostics before closing.
trace_resp = None
disc_resp = None
try:
    trace_resp = client.send("diag_trace", timeout=5, count=64)
except ControllerError as e:
    log(f"diag_trace failed: {e}")
try:
    disc_resp = client.send("diag_disconnect", timeout=5)
except ControllerError as e:
    log(f"diag_disconnect failed: {e}")
try:
    status_resp = client.send("controller_status", timeout=5)
    log(f"final controller_status: {status_resp}")
except ControllerError as e:
    log(f"controller_status failed: {e}")

client.close()

print()
print("=== TEST 1 RESULT ===")
print(f"valid_responses: {valid}/{N}")
print(f"failures: {timeouts}")
print(f"exception_raised: {exception_raised}")
print(f"errors: {errors}")
if disc_resp is not None:
    print(f"disconnect_info: {disc_resp}")
if trace_resp is not None:
    print(f"total_transitions_since_boot: {trace_resp.get('total_transitions_since_boot')}")
    print("last transitions (most recent first):")
    for t in trace_resp.get("transitions_most_recent_first", []):
        print(" ", t)

acceptance = (valid == N) and not exception_raised
print()
print("ACCEPTANCE:", "PASS" if acceptance else "FAIL")
sys.exit(0 if acceptance else 1)
