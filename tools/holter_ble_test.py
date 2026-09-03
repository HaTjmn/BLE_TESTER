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
HLV_DEVICE_NAME = "NOAMED"

HLV_OPCODE_GET_STATUS = 0x0002
HLV_RESULT_UNAUTHORIZED = 2


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
