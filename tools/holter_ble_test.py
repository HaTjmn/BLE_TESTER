#!/usr/bin/env python3
"""
HolterLEV BLE automated test runner.

Drives the ESP32 test controller (this repo's firmware) against a real
HolterLEV DUT over BLE, records structured PASS/FAIL evidence for each
test, and exits 0 only if every test passed.

Usage:
    python tools/holter_ble_test.py --port COM28
    python tools/holter_ble_test.py --port COM28 --test test_discovery_of_noamed
    python tools/holter_ble_test.py --port COM28 --json-report results.json

Safety: the end-to-end session test checks the DUT's own reported
recording_capable flag before ever attempting START, and always attempts
a STOP during cleanup if a recording may have been started.
"""
import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402

HLV_SERVICE_UUID = "7b8e0000-6a52-4f4d-9a33-71c02e6b4d90"
HLV_CONTROL_POINT_UUID = "7b8e0040-6a52-4f4d-9a33-71c02e6b4d90"
HLV_STATUS_UUID = "7b8e0041-6a52-4f4d-9a33-71c02e6b4d90"
HLV_DEVICE_INFO_UUID = "7b8e0043-6a52-4f4d-9a33-71c02e6b4d90"
HLV_COMMAND_RESPONSE_UUID = "7b8e0044-6a52-4f4d-9a33-71c02e6b4d90"
HLV_DEVICE_NAME = "NOAMED"

HLV_OPCODE_AUTH = 0x0001
HLV_OPCODE_GET_STATUS = 0x0002
HLV_OPCODE_START = 0x0010
HLV_OPCODE_STOP = 0x0011

HLV_RESULT_UNAUTHORIZED = 2
HLV_RESULT_TRANSACTION_CONFLICT = 13

HLV_SESSION_RECORDING = 3

HLV_RESPONSE_PHASE_ACCEPTED = 1
HLV_RESPONSE_PHASE_REJECTED = 2
HLV_RESPONSE_PHASE_COMPLETED = 3


def encode_hlv_message(opcode, txn_id, payload=b""):
    """Mirrors docs/ble/PROTOCOL.md Section 2 / src/holter_ble_protocol.h
    in the HolterLEV firmware repo: 12-byte little-endian header + payload."""
    header = bytes([
        1, 0,  # protocol_major, protocol_minor
        opcode & 0xFF, (opcode >> 8) & 0xFF,
        txn_id & 0xFF, (txn_id >> 8) & 0xFF, (txn_id >> 16) & 0xFF, (txn_id >> 24) & 0xFF,
        0, 0,  # result_code (unused in a request)
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF,
    ])
    return header + payload


def decode_status_bytes(data_hex):
    """Mirrors PROTOCOL.md Section 6 -- the raw 14-byte Status payload."""
    data = bytes.fromhex(data_hex)
    return {
        "startup_status": data[0],
        "ads1293_ready": bool(data[1]),
        "sd_ready": bool(data[2]),
        "recording_capable": bool(data[3]),
        "session_state": data[4],
        "authorized": bool(data[5]),
        "last_command_opcode": data[6] | (data[7] << 8),
        "last_command_transaction_id": data[8] | (data[9] << 8) | (data[10] << 16) | (data[11] << 24),
        "last_command_result": data[12] | (data[13] << 8),
    }


def decode_command_response(data_hex):
    """Mirrors PROTOCOL.md Section 12.1 -- the Command Response payload
    (phase, resulting_session_state, inner_payload_length, inner_payload),
    delivered as the PAYLOAD of the shared 12-byte message header (Section
    2), so the header must be skipped first -- confirmed against a real
    notification: full bytes were the header (echoing protocol version,
    opcode, transaction_id) followed by the actual 4-byte phase payload."""
    data = bytes.fromhex(data_hex)
    payload = data[12:]
    inner_len = payload[2] | (payload[3] << 8)
    return {
        "phase": payload[0],
        "resulting_session_state": payload[1],
        "inner": payload[4:4 + inner_len],
    }


def poll_status_until(client, predicate, timeout=8, interval=0.3):
    """Repeatedly reads the raw Status characteristic until `predicate(status_dict)`
    is true. Never trusts a single read -- PROTOCOL.md Section 16 explicitly
    documents Status timing as unsynchronized with command completion."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
        last = decode_status_bytes(status_raw["data_hex"])
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"status never satisfied predicate within {timeout}s; last={last}")


def collect_command_response_phases(client, window_s):
    """Drains 'notification' events for `window_s` seconds and returns the
    ordered list of Command Response phase values seen (ignoring
    notifications from any other subscribed characteristic)."""
    phases = []
    deadline = time.time() + window_s
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        ev = client.wait_for_event("notification", timeout=remaining)
        if ev is None:
            break
        if ev.get("characteristic") == HLV_COMMAND_RESPONSE_UUID:
            phases.append(decode_command_response(ev["data_hex"])["phase"])
    return phases


def cleanup_stop_if_recording(client, rec, txn_id):
    status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    st = decode_status_bytes(status_raw["data_hex"])
    if st["session_state"] == HLV_SESSION_RECORDING:
        stop_msg = encode_hlv_message(HLV_OPCODE_STOP, txn_id)
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID,
                    data_hex=stop_msg.hex(), with_response=True)
        note(rec, "DUT was RECORDING after test command -- issued STOP for cleanup")


class TestRecord:
    def __init__(self, name):
        self.name = name
        self.start_time = None
        self.duration = None
        self.commands = []
        self.passed = None
        self.failure_reason = None
        self.skipped = False

    def to_dict(self):
        return {
            "name": self.name,
            "start_time": self.start_time,
            "duration_s": self.duration,
            "commands": self.commands,
            "result": "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL"),
            "failure_reason": self.failure_reason,
        }


class Runner:
    def __init__(self, client):
        self.client = client
        self.records = []

    def run(self, name, fn):
        rec = TestRecord(name)
        rec.start_time = time.strftime("%Y-%m-%dT%H:%M:%S")
        t0 = time.time()
        try:
            fn(self.client, rec)
            rec.passed = True
        except SkipTest as e:
            rec.skipped = True
            rec.failure_reason = str(e)
        except (ControllerError, AssertionError) as e:
            rec.passed = False
            rec.failure_reason = str(e)
        except Exception:
            rec.passed = False
            rec.failure_reason = "unexpected exception:\n" + traceback.format_exc()
        rec.duration = round(time.time() - t0, 3)
        self.records.append(rec)
        status = "SKIP" if rec.skipped else ("PASS" if rec.passed else "FAIL")
        print(f"[{status}] {name} ({rec.duration}s)" + (f" -- {rec.failure_reason}" if rec.failure_reason and not rec.passed else ""))
        return rec


class SkipTest(Exception):
    pass


def note(rec, description):
    rec.commands.append(description)


def safe_disconnect(client, rec):
    try:
        client.send("disconnect", timeout=5)
        note(rec, "disconnect -> ok")
    except ControllerError:
        pass


def reset(client, rec):
    client.send("reset_controller", timeout=5)
    note(rec, "reset_controller -> ok")


# ---------------------------------------------------------------------
# Deterministic tests
# ---------------------------------------------------------------------

def test_controller_ping(client, rec):
    resp = client.send("ping")
    note(rec, f"ping -> {resp}")
    assert resp.get("pong") is True


def test_malformed_uart_json(client, rec):
    client.send_raw_line("this is not { valid json at all")
    resp = client.wait_for_unmatched_response(timeout=5)
    note(rec, f"raw malformed line -> {resp}")
    assert resp is not None, "controller did not respond to malformed input"
    assert resp.get("status") == "error"
    assert resp.get("error") == "PARSE_ERROR"
    # controller must still be alive afterward
    assert client.send("ping").get("pong") is True


def test_unknown_command(client, rec):
    try:
        client.send("this_command_does_not_exist")
        assert False, "expected an error"
    except ControllerError as e:
        note(rec, f"unknown command -> {e.error_code}")
        assert e.error_code == "UNKNOWN_COMMAND"


def test_missing_required_field(client, rec):
    try:
        client.send("scan")  # timeout_ms omitted
        assert False, "expected MISSING_FIELD"
    except ControllerError as e:
        note(rec, f"scan w/o timeout_ms -> {e.error_code}: {e.detail}")
        assert e.error_code == "MISSING_FIELD"
        assert e.detail == "timeout_ms"


def test_scan_timeout_when_absent(client, rec):
    reset(client, rec)
    resp = client.send("scan", timeout=8, timeout_ms=3000, name="DEFINITELY_NOT_A_REAL_DEVICE_XYZ")
    note(rec, f"scan (bogus name filter) -> devices_found={resp.get('devices_found')}")
    assert resp.get("devices_found") == 0


def test_discovery_of_noamed(client, rec):
    reset(client, rec)
    resp = client.send("scan", timeout=8, timeout_ms=5000, name=HLV_DEVICE_NAME)
    note(rec, f"scan (name={HLV_DEVICE_NAME}) -> devices_found={resp.get('devices_found')}")
    assert resp.get("devices_found", 0) >= 1, "real HolterLEV DUT (NOAMED) was not found -- is it powered and advertising?"


def test_successful_connection(client, rec):
    reset(client, rec)
    resp = client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    note(rec, f"connect(name={HLV_DEVICE_NAME}) -> {resp}")
    assert resp.get("name") == HLV_DEVICE_NAME
    safe_disconnect(client, rec)


def test_connect_to_nonexistent_address(client, rec):
    reset(client, rec)
    try:
        client.send("connect", timeout=8, addr="aa:bb:cc:dd:ee:ff", timeout_ms=3000)
        assert False, "expected connect to a nonexistent address to fail"
    except ControllerError as e:
        note(rec, f"connect(addr=aa:bb:cc:dd:ee:ff) -> {e.error_code}")
        assert e.error_code in ("TIMEOUT", "CONNECT_FAILED")
    finally:
        client.send("reset_controller", timeout=5)


def test_service_discovery(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    resp = client.send("discover_services", timeout=10)
    note(rec, f"discover_services -> {resp}")
    uuids = [s["uuid"] for s in resp.get("services", [])]
    assert HLV_SERVICE_UUID in uuids, f"HolterLEV primary service not found among {uuids}"
    safe_disconnect(client, rec)


def test_missing_service_handling(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    try:
        client.send("discover_characteristics", timeout=10,
                     service_uuid="deadbeef-0000-0000-0000-000000000000")
        assert False, "expected an error for a nonexistent service_uuid"
    except ControllerError as e:
        note(rec, f"discover_characteristics(bogus service) -> {e.error_code}")
        assert e.error_code == "INVALID_STATE"
    finally:
        safe_disconnect(client, rec)


def test_raw_characteristic_read(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    resp = client.send("read", timeout=6, characteristic=HLV_DEVICE_INFO_UUID)
    note(rec, f"read(Device Info) -> {resp}")
    data = bytes.fromhex(resp["data_hex"])
    name_len = data[3 + data[2]]
    name = data[4 + data[2]: 4 + data[2] + name_len].decode("ascii")
    assert name == HLV_DEVICE_NAME, f"Device Info reported name {name!r}, expected {HLV_DEVICE_NAME!r}"
    safe_disconnect(client, rec)


def test_raw_characteristic_write(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)

    msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn_id=1)
    resp = client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID,
                        data_hex=msg.hex(), with_response=True)
    note(rec, f"write(Control Point, GET_STATUS, unauthorized) -> {resp}")

    time.sleep(0.3)  # let the command task process before we read Status
    status = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    data = bytes.fromhex(status["data_hex"])
    last_result = data[12] | (data[13] << 8)
    note(rec, f"read(Status) after unauthorized GET_STATUS -> last_command_result={last_result}")
    assert last_result == HLV_RESULT_UNAUTHORIZED, \
        f"expected UNAUTHORIZED({HLV_RESULT_UNAUTHORIZED}) for an unauthenticated connection, got {last_result}"
    safe_disconnect(client, rec)


def test_notification_subscription(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    resp = client.send("subscribe", timeout=6, characteristic=HLV_STATUS_UUID, mode="notify")
    note(rec, f"subscribe(Status, notify) -> {resp}")
    assert resp.get("subscribed") is True
    safe_disconnect(client, rec)


def test_notification_timeout(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_STATUS_UUID, mode="notify")
    ev = client.wait_for_event("notification", timeout=2.0)
    note(rec, f"wait 2s with no command issued -> notification event = {ev}")
    assert ev is None, "a Status notification arrived with no command issued to trigger one"
    safe_disconnect(client, rec)


def test_clean_disconnect(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    resp = client.send("disconnect", timeout=6)
    note(rec, f"disconnect -> {resp}")
    status = client.send("controller_status")
    note(rec, f"controller_status -> {status}")
    assert status.get("state") == "IDLE"
    assert status.get("connected") is False


def test_reconnect(client, rec):
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("disconnect", timeout=6)
    resp = client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    note(rec, f"reconnect -> {resp}")
    assert resp.get("name") == HLV_DEVICE_NAME
    safe_disconnect(client, rec)


def test_repeated_connect_disconnect_cycles(client, rec, cycles=5):
    reset(client, rec)
    for i in range(cycles):
        client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
        client.send("disconnect", timeout=6)
        note(rec, f"cycle {i + 1}/{cycles} ok")
    status = client.send("controller_status")
    assert status.get("state") == "IDLE"


def test_controller_recovery_after_errors(client, rec):
    for _ in range(3):
        try:
            client.send("nonexistent_command_xyz")
        except ControllerError:
            pass
    client.send_raw_line("{{{ not json")
    client.wait_for_unmatched_response(timeout=3)
    resp = client.send("ping")
    note(rec, f"ping after a burst of errors -> {resp}")
    assert resp.get("pong") is True
    resp = client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    note(rec, f"connect after error burst -> {resp}")
    assert resp.get("name") == HLV_DEVICE_NAME
    safe_disconnect(client, rec)


# ---------------------------------------------------------------------
# Regression tests for the BLE-D2/D3/D3.1/D3.2/D4 correctness fixes made
# in the HolterLEV firmware repo (holterlev_1293-BLE,
# fix/ble-d3-2-connection-ownership). These use raw writes with explicit
# transaction_ids (bypassing holterlev_client's own auto-incrementing
# counter) since precise transaction_id control is the whole point.
# ---------------------------------------------------------------------

def test_command_response_ordering(client, rec):
    """BLE-D3 (Command Response) / BLE-D3.1 (ACCEPTED always precedes
    COMPLETED for the same transaction). AUTH is synchronous (PROTOCOL.md
    Section 13): exactly one COMPLETED, never preceded by ACCEPTED. START
    is asynchronous: if it reaches the Executor, ACCEPTED must precede
    COMPLETED; if it fails before reaching the Executor (this DUT's
    current recording_capable=false state is exactly the fast-rejection
    scenario BLE-D3.1's own bug report used as its motivating example),
    only REJECTED may appear, and COMPLETED must never appear without a
    preceding ACCEPTED for the same transaction."""
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.drain_events()

    base_txn = int(time.time()) & 0x7FFFFFFF

    auth_msg = encode_hlv_message(HLV_OPCODE_AUTH, base_txn, b"2018")
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID,
                data_hex=auth_msg.hex(), with_response=True)
    phases = collect_command_response_phases(client, window_s=3)
    note(rec, f"AUTH (txn={base_txn}) Command Response phases: {phases}")
    assert HLV_RESPONSE_PHASE_ACCEPTED not in phases, \
        f"AUTH (a synchronous command) must never produce an ACCEPTED phase, got {phases}"
    assert phases.count(HLV_RESPONSE_PHASE_COMPLETED) == 1, \
        f"AUTH must produce exactly one COMPLETED phase, got {phases}"

    start_txn = base_txn + 1
    start_msg = encode_hlv_message(HLV_OPCODE_START, start_txn, (30).to_bytes(4, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID,
                data_hex=start_msg.hex(), with_response=True)
    phases2 = collect_command_response_phases(client, window_s=8)
    note(rec, f"START (txn={start_txn}, duration=30s) Command Response phases: {phases2}")
    assert phases2, "START produced no Command Response message at all"
    if HLV_RESPONSE_PHASE_ACCEPTED in phases2:
        assert phases2.count(HLV_RESPONSE_PHASE_ACCEPTED) == 1
        if HLV_RESPONSE_PHASE_COMPLETED in phases2:
            assert phases2.index(HLV_RESPONSE_PHASE_ACCEPTED) < phases2.index(HLV_RESPONSE_PHASE_COMPLETED), \
                f"ACCEPTED must precede COMPLETED for the same transaction, got order {phases2}"
    else:
        assert HLV_RESPONSE_PHASE_REJECTED in phases2, \
            f"a fast-rejected START without ACCEPTED must show REJECTED, got {phases2}"
        assert HLV_RESPONSE_PHASE_COMPLETED not in phases2, \
            f"COMPLETED must never appear without a preceding ACCEPTED for the same transaction, got {phases2}"

    cleanup_stop_if_recording(client, rec, start_txn + 1)
    safe_disconnect(client, rec)


def test_transaction_ledger_conflict(client, rec):
    """BLE-D2/BLE-D4's transaction ledger (src/holter_ble_transaction_policy.h,
    HOLTER_BLE_TXN_DECISION_CONFLICT): resending the same transaction_id
    with a DIFFERENT payload (same opcode, different duration_seconds)
    must be rejected TRANSACTION_CONFLICT(13) before any side effect --
    true whether the original entry is still active or already completed."""
    reset(client, rec)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)

    auth_txn = int(time.time()) & 0x7FFFFFFF
    auth_msg = encode_hlv_message(HLV_OPCODE_AUTH, auth_txn, b"2018")
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=auth_msg.hex())
    poll_status_until(client, lambda st: st["last_command_transaction_id"] == auth_txn, timeout=5)

    txn = auth_txn + 100  # distinct namespace, unique per run

    msg_a = encode_hlv_message(HLV_OPCODE_START, txn, (30).to_bytes(4, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg_a.hex())
    st1 = poll_status_until(client, lambda st: st["last_command_transaction_id"] == txn, timeout=8)
    note(rec, f"first START(txn={txn}, duration=30) settled -> last_command_result={st1['last_command_result']}")
    assert st1["last_command_result"] != HLV_RESULT_TRANSACTION_CONFLICT, \
        "a never-before-seen transaction_id must not itself be reported as a conflict"

    msg_b = encode_hlv_message(HLV_OPCODE_START, txn, (99).to_bytes(4, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg_b.hex())
    time.sleep(0.5)  # CONFLICT is rejected before any side effect -- should be near-instant
    status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    st2 = decode_status_bytes(status_raw["data_hex"])
    note(rec, f"resend START(txn={txn}, duration=99, same id different payload) -> "
              f"last_command_result={st2['last_command_result']}")
    assert st2["last_command_transaction_id"] == txn
    assert st2["last_command_result"] == HLV_RESULT_TRANSACTION_CONFLICT, \
        (f"reusing transaction_id={txn} with a different payload must yield "
         f"TRANSACTION_CONFLICT(13), got {st2['last_command_result']}")

    cleanup_stop_if_recording(client, rec, txn + 1)
    safe_disconnect(client, rec)


def test_reconnect_ownership_stress(client, rec):
    """Best-effort regression check related to BLE-D3.1/D3.2's connection-
    generation guard (a stale response must never reach a reused
    conn_handle after a disconnect/reconnect). IMPORTANT LIMITATION: a
    single BLE central (this tool) cannot directly observe cross-
    connection delivery on the DUT side -- proving the internal race is
    fixed would need two simultaneous connections or DUT-side
    instrumentation, neither of which this tool has. This test instead
    stresses the externally observable contract: disconnecting
    immediately after a command's ATT write ack (before the Executor's
    result is necessarily known to have settled), then reconnecting
    promptly, must never crash/hang, must always leave the NEW connection
    unauthorized (PROTOCOL.md Section 5), and the controller must recover
    cleanly every time."""
    reset(client, rec)
    last_txn = None
    for i in range(3):
        client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
        client.send("discover_services", timeout=10)
        client.send("discover_characteristics", timeout=10)

        txn = (int(time.time() * 1000) + i) & 0x7FFFFFFF
        auth_msg = encode_hlv_message(HLV_OPCODE_AUTH, txn, b"2018")
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=auth_msg.hex())

        start_msg = encode_hlv_message(HLV_OPCODE_START, txn + 1, (30).to_bytes(4, "little"))
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
        client.send("disconnect", timeout=6)
        last_txn = txn + 1
        note(rec, f"cycle {i + 1}/3: connect+auth+start+disconnect ok (no wait for START's own settlement)")

    client.send("reset_controller", timeout=5)
    resp = client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    note(rec, f"final reconnect after stress cycles -> {resp}")
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)

    status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    st = decode_status_bytes(status_raw["data_hex"])
    note(rec, f"post-stress Status on fresh connection -> {st}")
    assert st["authorized"] is False, \
        "a fresh connection must start unauthorized even immediately after reconnecting"

    still_alive = client.send("holter_get_status", timeout=6)
    note(rec, f"controller fully responsive after stress cycles -> {still_alive is not None}")

    cleanup_stop_if_recording(client, rec, last_txn + 1000)
    safe_disconnect(client, rec)


# ---------------------------------------------------------------------
# Main end-to-end scenario
# ---------------------------------------------------------------------

def test_end_to_end_session(client, rec):
    reset(client, rec)

    ping = client.send("ping"); note(rec, f"ping -> {ping}")
    assert ping.get("pong") is True

    scan = client.send("scan", timeout=8, timeout_ms=5000, name=HLV_DEVICE_NAME)
    note(rec, f"scan -> {scan}")
    assert scan.get("devices_found", 0) >= 1

    conn = client.send("holter_connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    note(rec, f"holter_connect -> {conn}")
    assert conn.get("status_found") and conn.get("control_point_found")

    sub = client.send("subscribe", timeout=6, characteristic=HLV_STATUS_UUID, mode="notify")
    note(rec, f"subscribe(Status) -> {sub}")

    status = client.send("holter_get_status", timeout=6)
    note(rec, f"holter_get_status (pre-auth) -> {status}")

    auth = client.send("holter_authenticate", timeout=6)
    note(rec, f"holter_authenticate -> {auth}")
    assert auth.get("authorized") is True, "AUTH with the firmware's default dev code was rejected"

    status = client.send("holter_get_status", timeout=6)
    note(rec, f"holter_get_status (post-auth) -> {status}")

    if not status.get("recording_capable"):
        note(rec, "DUT reports recording_capable=false -- skipping START/RECORDING/STOP per "
                   "the spec's own safety rule (do not start a recording unless the DUT says "
                   "it is recording-capable and storage is available)")
        safe_disconnect(client, rec)
        raise SkipTest("DUT not recording_capable at test time -- START/STOP not exercised")

    try:
        start = client.send("holter_start_session", timeout=6, duration_seconds=60)
        note(rec, f"holter_start_session(60s) -> {start}")
        assert start.get("last_command_result") in (0, 12)  # OK or ACCEPTED

        wait = client.send("holter_wait_state", timeout=15, target_state="RECORDING", timeout_ms=10000)
        note(rec, f"holter_wait_state(RECORDING) -> {wait}")
        assert wait.get("session_state") == 3

        status = client.send("holter_get_status", timeout=6)
        note(rec, f"holter_get_status (during recording) -> {status}")
    finally:
        stop = client.send("holter_stop_session", timeout=10)
        note(rec, f"holter_stop_session -> {stop}")
        wait = client.send("holter_wait_state", timeout=15, target_state="READY", timeout_ms=10000)
        note(rec, f"holter_wait_state(READY) -> {wait}")

    safe_disconnect(client, rec)


ALL_TESTS = [
    test_controller_ping,
    test_malformed_uart_json,
    test_unknown_command,
    test_missing_required_field,
    test_scan_timeout_when_absent,
    test_discovery_of_noamed,
    test_successful_connection,
    test_connect_to_nonexistent_address,
    test_service_discovery,
    test_missing_service_handling,
    test_raw_characteristic_read,
    test_raw_characteristic_write,
    test_notification_subscription,
    test_notification_timeout,
    test_clean_disconnect,
    test_reconnect,
    test_repeated_connect_disconnect_cycles,
    test_controller_recovery_after_errors,
    test_command_response_ordering,
    test_transaction_ledger_conflict,
    test_reconnect_ownership_stress,
    test_end_to_end_session,
]


def main():
    parser = argparse.ArgumentParser(description="HolterLEV BLE automated test runner")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--test", action="append", help="run only this test (repeatable); default: all")
    parser.add_argument("--log-dir", default=os.path.join(os.path.dirname(__file__), "..", "test", "host", "logs"))
    parser.add_argument("--json-report", help="path to write the full structured report")
    args = parser.parse_args()

    os.makedirs(args.log_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    transcript_path = os.path.join(args.log_dir, f"run_{ts}.jsonl")

    tests = ALL_TESTS
    if args.test:
        tests = [t for t in ALL_TESTS if t.__name__ in args.test]
        if not tests:
            print(f"No matching tests for {args.test}", file=sys.stderr)
            return 2

    with open(transcript_path, "a", encoding="utf-8") as log_file:
        client = HolterBLEClient(args.port, baud=args.baud, log_file=log_file)
        runner = Runner(client)
        try:
            for t in tests:
                runner.run(t.__name__, t)
        finally:
            client.close()

    passed = sum(1 for r in runner.records if r.passed and not r.skipped)
    failed = sum(1 for r in runner.records if r.passed is False)
    skipped = sum(1 for r in runner.records if r.skipped)

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped -- transcript: {transcript_path}")

    report = {
        "timestamp": ts,
        "port": args.port,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "transcript": transcript_path,
        "tests": [r.to_dict() for r in runner.records],
    }
    report_path = args.json_report or os.path.join(args.log_dir, f"report_{ts}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"report: {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
