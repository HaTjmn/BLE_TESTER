#!/usr/bin/env python3
"""
BLE-D4.0D Steps 5 and 6 -- control test (100x GET_STATUS, no forced
timeouts) then a deterministic forced-timeout test (up to 20 sequential
GATT-read timeouts, one pending operation at a time), tracking the
Timer Service Task's own stack high-water mark (confirmed to be BYTES on
this port -- portmacro.h: StackType_t = uint8_t) at every required
checkpoint.
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_STATUS_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_GET_STATUS, HLV_RESPONSE_PHASE_COMPLETED,
    encode_hlv_message, wait_for_command_response, authenticate_raw,
)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
RAW_CAPTURE = os.path.join(os.path.dirname(__file__), "_evidence", "test56_raw_capture.jsonl")
os.makedirs(os.path.dirname(RAW_CAPTURE), exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def health(client, label):
    h = client.send("diag_health", timeout=5)
    b = client.send("diag_boot", timeout=5)
    log(f"  [{label}] boot_gen={b['boot_generation']} reset_reason={b['reset_reason']} "
        f"heap={h['free_internal_heap']} min_heap={h['minimum_free_internal_heap']} "
        f"nimble_hwm={h['nimble_host_task_stack_highwater_bytes']}B "
        f"timer_hwm={h['timer_task_stack_highwater_bytes']}B "
        f"pending_active={h['pending_operation_active']}")
    return h, b


client = HolterBLEClient(PORT, baud=115200, default_timeout=15, raw_capture_path=RAW_CAPTURE)

h0, b0 = health(client, "after_boot")
boot_gen_start = b0["boot_generation"]

client.send("reset_controller", timeout=5)
client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
health(client, "after_connect")
client.send("discover_services", timeout=10)
client.send("discover_characteristics", timeout=10)
client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
client.drain_events()
base_txn = int(time.time() * 1000) & 0x7FFFFFFF
status = authenticate_raw(client, base_txn)
if not status["authorized"]:
    log("FATAL: auth failed")
    sys.exit(2)
h_auth, b_auth = health(client, "after_authentication")
min_timer_hwm = h_auth["timer_task_stack_highwater_bytes"]

# ---------------------------------------------------------------------
# Step 5: control test -- 100x GET_STATUS, no forced timeouts, no download
# ---------------------------------------------------------------------
log("=== STEP 5: control test (100x GET_STATUS) ===")
txn = base_txn
passed = 0
failed = 0
for i in range(100):
    txn += 1
    try:
        msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn)
        client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        resp = wait_for_command_response(client, txn, window_s=4)
        if resp is not None and resp["phase"] == HLV_RESPONSE_PHASE_COMPLETED:
            passed += 1
        else:
            failed += 1
            log(f"  GET_STATUS #{i+1}: unexpected response {resp}")
    except ControllerError as e:
        failed += 1
        log(f"  GET_STATUS #{i+1}: ControllerError {e}")
    time.sleep(0.2)

h_ctrl, b_ctrl = health(client, "after_control_test")
min_timer_hwm = min(min_timer_hwm, h_ctrl["timer_task_stack_highwater_bytes"])
log(f"STEP 5 RESULT: {passed}/100 passed, {failed} failed, "
    f"boot_generation {boot_gen_start} -> {b_ctrl['boot_generation']}, "
    f"reset_reason={b_ctrl['reset_reason']}")

if b_ctrl["boot_generation"] != boot_gen_start:
    log("*** Step 5 (control) itself triggered a reboot -- stopping before Step 6 ***")
    client.close()
    sys.exit(1)

# ---------------------------------------------------------------------
# Step 6: forced-timeout test -- deterministic GATT read timeouts,
# strictly sequential (one pending operation at a time), max 20 attempts.
# ---------------------------------------------------------------------
# scan_start() (and connect_start()) require BLE_CENTRAL_STATE_IDLE --
# empirically confirmed just now: staying connected/READY from Step 5
# makes every scan attempt fail synchronously with INVALID_STATE before
# ever reaching begin_pending()/the timeout path at all. Disconnect first
# so scan can actually be admitted and run its full timeout_ms.
log("disconnecting before Step 6 (scan requires IDLE, not READY)")
try:
    client.send("disconnect", timeout=6)
except ControllerError as e:
    log(f"  disconnect returned {e} (may already be effectively idle)")
time.sleep(0.5)
health(client, "after_disconnect_before_step6")

log("=== STEP 6: forced-timeout test (up to 20 sequential timeouts) ===")
timeout_results = []
panicked = False

for attempt in range(1, 21):
    entry = {"attempt": attempt}
    t_start = time.time()
    try:
        # A GATT read/write with a very short timeout_ms does NOT reliably
        # force the firmware's own timeout path: the real round trip
        # (tens of ms) usually completes before timeout_timer_cb()'s next
        # 200ms poll even runs, so the operation just succeeds normally
        # (confirmed empirically -- all 20 attempts at timeout_ms=1
        # returned real data). A scan for a name guaranteed not to be
        # advertising nearby has no such race: nothing can possibly
        # complete it early, so it reliably runs its full timeout_ms and
        # is caught by the SAME timeout_timer_cb() -> clear_pending() ->
        # command_dispatcher_on_op_timeout() -> protocol_json_send_error()
        # cascade under suspicion (scan is one of the two ops with
        # explicit handling in that function, alongside connect).
        resp = client.send("scan", timeout=3, timeout_ms=300,
                            name="NO_SUCH_DEVICE_BLE_D4_0D_FORCED_TIMEOUT")
        entry["result"] = "UNEXPECTED_SUCCESS"
        entry["response"] = resp
        log(f"  attempt {attempt}: unexpected success (no timeout fired): {resp}")
    except ControllerError as e:
        t_observed = time.time()
        entry["result"] = e.error_code
        entry["latency_s"] = round(t_observed - t_start, 3)
        entry["response"] = e.response
        log(f"  attempt {attempt}: {e.error_code} after {entry['latency_s']}s -- {e.response}")

    # Confirm exactly one terminal response: drain anything unexpectedly
    # left over (a second response for the same id would show up here).
    leftover = client.drain_raw_lines()
    if leftover:
        entry["raw_lines_during_attempt"] = [l["line"] for l in leftover]

    try:
        h, b = health(client, f"attempt_{attempt}")
        entry["boot_generation"] = b["boot_generation"]
        entry["reset_reason"] = b["reset_reason"]
        entry["timer_task_hwm"] = h["timer_task_stack_highwater_bytes"]
        entry["nimble_task_hwm"] = h["nimble_host_task_stack_highwater_bytes"]
        entry["pending_operation_active"] = h["pending_operation_active"]
        min_timer_hwm = min(min_timer_hwm, h["timer_task_stack_highwater_bytes"])
        if b["boot_generation"] != boot_gen_start:
            panicked = True
            entry["PANIC_DETECTED"] = True
            log(f"*** REBOOT DETECTED at attempt {attempt}: boot_generation "
                f"{boot_gen_start} -> {b['boot_generation']}, reset_reason={b['reset_reason']} ***")
    except ControllerError as e:
        panicked = True
        entry["health_check_failed"] = str(e)
        log(f"*** health check failed after attempt {attempt} (possible mid-reboot): {e} ***")

    timeout_results.append(entry)
    if panicked:
        log("Stopping immediately: a reset occurred (per Step 6's own instruction).")
        break
    time.sleep(0.3)

try:
    client.close()
except Exception:
    pass

print()
print("=== STEP 6 RESULTS ===")
for r in timeout_results:
    print(" ", r)
print()
print(f"lowest observed Timer Task stack high-water mark: {min_timer_hwm} bytes")
print(f"panic occurred during forced-timeout test: {panicked}")

raw_lines = []
if os.path.exists(RAW_CAPTURE):
    with open(RAW_CAPTURE, encoding="utf-8") as f:
        raw_lines = f.readlines()
print(f"\nraw non-JSON lines captured overall: {len(raw_lines)} (see {RAW_CAPTURE})")
if raw_lines:
    print("--- raw capture contents ---")
    for line in raw_lines:
        print(line.rstrip())
