#!/usr/bin/env python3
"""
BLE-D4.0E hardware validation -- Test 1 (forced timeouts), Test 2
(timeout/completion races), Test 3 (control regression). Keeps
CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH at 2048 throughout (unchanged) --
the point is validating the architecture, not hiding the issue behind a
bigger stack.
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
RAW_CAPTURE = os.path.join(os.path.dirname(__file__), "_evidence", "d4_0e_raw_capture.jsonl")
os.makedirs(os.path.dirname(RAW_CAPTURE), exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def health(client, label):
    h = client.send("diag_health", timeout=5)
    b = client.send("diag_boot", timeout=5)
    log(f"  [{label}] boot_gen={b['boot_generation']} reset_reason={b['reset_reason']} "
        f"timer_hwm={h['timer_task_stack_highwater_bytes']}B nimble_hwm={h['nimble_host_task_stack_highwater_bytes']}B "
        f"pending_active={h['pending_operation_active']} "
        f"queued={h['timeout_events_queued']} full={h['timeout_events_queue_full']} "
        f"processed={h['timeout_events_processed']} late={h['late_completions_ignored']} "
        f"dup_prevented={h['duplicate_completions_prevented']}")
    return h, b


client = HolterBLEClient(PORT, baud=115200, default_timeout=15, raw_capture_path=RAW_CAPTURE)
h0, b0 = health(client, "after_boot")
boot_gen_start = b0["boot_generation"]
min_timer_hwm = h0["timer_task_stack_highwater_bytes"]

# ---------------------------------------------------------------------
# Test 1: 20x diag_force_timeout, sequential
# ---------------------------------------------------------------------
log("=== TEST 1: 20x diag_force_timeout (sequential) ===")
test1_results = []
test1_panicked = False
for i in range(1, 21):
    entry = {"attempt": i}
    try:
        resp = client.send("diag_force_timeout", timeout=3)
        entry["outcome"] = "UNEXPECTED_OK"
        entry["response"] = resp
    except ControllerError as e:
        entry["outcome"] = e.error_code
        entry["response"] = e.response
    h, b = health(client, f"t1_attempt_{i}")
    entry["boot_generation"] = b["boot_generation"]
    entry["timer_hwm"] = h["timer_task_stack_highwater_bytes"]
    entry["pending_active"] = h["pending_operation_active"]
    entry["queue_full"] = h["timeout_events_queue_full"]
    min_timer_hwm = min(min_timer_hwm, h["timer_task_stack_highwater_bytes"])
    if b["boot_generation"] != boot_gen_start:
        test1_panicked = True
        entry["PANIC"] = True
        log(f"*** PANIC at Test 1 attempt {i} ***")
    test1_results.append(entry)
    if test1_panicked:
        break
    time.sleep(0.1)

t1_ok = sum(1 for r in test1_results if r["outcome"] == "TIMEOUT")
log(f"TEST 1 RESULT: {t1_ok}/20 exactly-one-TIMEOUT, panicked={test1_panicked}")

# ---------------------------------------------------------------------
# Test 2: >=30 race cases -- before/near/after deadline, plus disconnect
# ---------------------------------------------------------------------
log("=== TEST 2: timeout/completion races ===")
race_offsets = (
    [(-100, False)] * 8 +   # clearly before timeout
    [(-5, False)] * 8 +     # very close, completion should still usually win
    [(5, False)] * 8 +      # very close, timeout should usually win
    [(100, False)] * 4 +    # clearly after timeout
    [(-100, True)] * 2 +    # disconnect cancellation before
    [(100, True)] * 2       # disconnect cancellation after (should be no-op, already timed out)
)
test2_results = []
test2_panicked = False
for i, (offset_ms, disconnect_mode) in enumerate(race_offsets, 1):
    entry = {"case": i, "offset_ms": offset_ms, "disconnect_mode": disconnect_mode}
    # The terminal race outcome (RACE_TEST_COMPLETED or TIMEOUT) arrives as
    # THIS SAME command's own response (both command_dispatcher_on_op_error()
    # and command_dispatcher_on_op_timeout() address the original request_id
    # via protocol_json_send_error/_ok) -- so client.send() itself blocks
    # until whichever side wins, no separate event wait needed. Timeout
    # budget covers RACE_TEST_DEADLINE_MS(300) + |offset_ms| + margin.
    wait_budget = 0.3 + abs(offset_ms) / 1000.0 + 0.5
    try:
        resp = client.send("diag_race_test", timeout=wait_budget, offset_ms=offset_ms, disconnect=disconnect_mode)
        entry["outcome"] = "UNEXPECTED_OK"
        entry["response"] = resp
    except ControllerError as e:
        entry["outcome"] = e.error_code
        entry["response"] = e.response
    h, b = health(client, f"t2_case_{i}")
    entry["boot_generation"] = b["boot_generation"]
    entry["pending_active"] = h["pending_operation_active"]
    min_timer_hwm = min(min_timer_hwm, h["timer_task_stack_highwater_bytes"])
    if b["boot_generation"] != boot_gen_start:
        test2_panicked = True
        entry["PANIC"] = True
        log(f"*** PANIC at Test 2 case {i} ***")
    test2_results.append(entry)
    if test2_panicked:
        break
    time.sleep(0.1)

race_summary = client.send("diag_race_test_summary", timeout=5)
log(f"race summary: {race_summary}")
log(f"TEST 2 RESULT: {len(test2_results)}/{len(race_offsets)} cases run, panicked={test2_panicked}")

# ---------------------------------------------------------------------
# Test 3: control regression -- 100x GET_STATUS + 20x full cycles
# ---------------------------------------------------------------------
log("=== TEST 3: control regression ===")
client.send("reset_controller", timeout=5)
client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
client.send("discover_services", timeout=10)
client.send("discover_characteristics", timeout=10)
client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
client.drain_events()
base_txn = int(time.time() * 1000) & 0x7FFFFFFF
status = authenticate_raw(client, base_txn)
if not status["authorized"]:
    log("FATAL: auth failed")
    sys.exit(2)

txn = base_txn
t3a_pass = 0
for i in range(100):
    txn += 1
    try:
        msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn)
        client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        resp = wait_for_command_response(client, txn, window_s=4)
        if resp is not None and resp["phase"] == HLV_RESPONSE_PHASE_COMPLETED:
            t3a_pass += 1
    except ControllerError as e:
        log(f"  GET_STATUS #{i+1} failed: {e}")
    time.sleep(0.15)
log(f"Test 3a (100x GET_STATUS): {t3a_pass}/100 passed")
h3a, b3a = health(client, "after_test3a")
min_timer_hwm = min(min_timer_hwm, h3a["timer_task_stack_highwater_bytes"])

t3b_pass = 0
for cycle in range(1, 21):
    try:
        client.send("reset_controller", timeout=5)
        client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
        client.send("discover_services", timeout=10)
        client.send("discover_characteristics", timeout=10)
        client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
        client.drain_events()
        cbase = int(time.time() * 1000) & 0x7FFFFFFF
        cstatus = authenticate_raw(client, cbase)
        if not cstatus["authorized"]:
            raise ControllerError("AUTH_FAILED", "not authorized")
        ctxn = cbase + 1
        cmsg = encode_hlv_message(HLV_OPCODE_GET_STATUS, ctxn)
        client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=cmsg.hex())
        cresp = wait_for_command_response(client, ctxn, window_s=4)
        if cresp is None or cresp["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
            raise ControllerError("GET_STATUS_FAILED", str(cresp))
        client.send("disconnect", timeout=6)
        t3b_pass += 1
    except ControllerError as e:
        log(f"  cycle {cycle} failed: {e}")
        try:
            client.send("reset_controller", timeout=5)
        except ControllerError:
            pass
    time.sleep(0.2)
log(f"Test 3b (20x full cycles): {t3b_pass}/20 passed")
h3b, b3b = health(client, "after_test3b")
min_timer_hwm = min(min_timer_hwm, h3b["timer_task_stack_highwater_bytes"])

try:
    client.close()
except Exception:
    pass

print()
print("=== SUMMARY ===")
print(f"Test 1: {t1_ok}/20 exactly-one-TIMEOUT, panicked={test1_panicked}")
print(f"Test 2: {len(test2_results)}/{len(race_offsets)} cases, panicked={test2_panicked}, race_summary={race_summary}")
print(f"Test 3a: {t3a_pass}/100 GET_STATUS")
print(f"Test 3b: {t3b_pass}/20 full cycles")
print(f"boot_generation: started {boot_gen_start}, ended {b3b['boot_generation']}")
print(f"lowest observed Timer Task stack high-water mark: {min_timer_hwm} bytes")
print()
print("=== TEST 1 DETAIL ===")
for r in test1_results:
    print(" ", r)
print()
print("=== TEST 2 DETAIL ===")
for r in test2_results:
    print(" ", r)

raw_lines = []
if os.path.exists(RAW_CAPTURE):
    with open(RAW_CAPTURE, encoding="utf-8") as f:
        raw_lines = f.readlines()
print(f"\nraw non-JSON lines captured: {len(raw_lines)}")
if raw_lines:
    print("--- raw capture contents ---")
    for line in raw_lines:
        print(line.rstrip())
