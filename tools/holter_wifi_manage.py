#!/usr/bin/env python3
"""
HLV-WIFI-20261006-008 (SCRUM-31 / SCRUM-20) -- manage HolterLEV's saved
Wi-Fi networks over BLE through BLE_TESTER (docs/ble/PROTOCOL.md Section 19
of the HolterLEV repository; firmware env esp32-s3-devkitc-1-n32r8v-ble-wifi-dev).

Passwords: never on the command line (shell history / process list). Taken
from the environment variable HLV_WIFI_PASSWORD, or prompted without echo.
Never printed, never written to the --log file (requests that carry one are
sent with sensitive=True and logged as a length only).

Usage:
    python tools/holter_wifi_manage.py --port COM17 caps
    python tools/holter_wifi_manage.py --port COM17 list
    python tools/holter_wifi_manage.py --port COM17 status
    python tools/holter_wifi_manage.py --port COM17 add --ssid LabNet --auth wpa2
    python tools/holter_wifi_manage.py --port COM17 update --id 3 --auth wpa2
    python tools/holter_wifi_manage.py --port COM17 delete --id 3
    python tools/holter_wifi_manage.py --port COM17 prefer --id 3      (0 = no preference)
    python tools/holter_wifi_manage.py --port COM17 clear --confirm
    python tools/holter_wifi_manage.py --port COM17 start | stop
    python tools/holter_wifi_manage.py --port COM17 --mtu23 selftest --erase-wifi-config --log run.jsonl

Options: --mtu23 keeps ATT MTU 23 (long write / long read are then used),
--no-pair skips LE Secure Connections pairing (ADD / UPDATE are then refused
with ENCRYPTION_REQUIRED -- that is the point of the option).

`selftest` is the hardware checklist of HLV-WIFI-008: it ERASES the saved
Wi-Fi networks (only those) and uses dummy networks / passwords only.
"""
import argparse
import getpass
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402
from holter_hlv_protocol import (  # noqa: E402
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_COMPLETED, HLV_RESPONSE_PHASE_REJECTED,
    HLV_RESULT_UNAUTHORIZED, HLV_RESULT_INVALID_ARGUMENT,
    decode_command_response, authenticate_raw,
)
import holter_wifi_protocol as w  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class WifiSession:
    """One BLE connection to HolterLEV, ready for Wi-Fi requests."""

    def __init__(self, client, mtu23=False):
        self.client = client
        self.mtu23 = mtu23
        self.txn = int(time.time() * 1000) & 0x3FFFFFFF

    def next_txn(self):
        self.txn += 1
        return self.txn

    def connect(self, authorize=True, pair=True):
        c = self.client
        c.send("reset_controller", timeout=5)
        c.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000, exchange_mtu=not self.mtu23)
        c.send("discover_services", timeout=10)
        c.send("discover_characteristics", timeout=10)
        c.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
        c.drain_events()
        st = c.send("controller_status", timeout=5)
        log(f"connected: mtu={st.get('mtu')} encrypted={st.get('encrypted')}")
        if authorize:
            if not authenticate_raw(c, self.next_txn())["authorized"]:
                raise ControllerError("AUTH_FAILED", "device rejected the dev access code")
        if pair:
            self.pair()

    def pair(self):
        self.client.send("security_initiate", timeout=5)
        ev = self.client.wait_for_event("enc_change", timeout=15)
        if ev is None or ev.get("status") != 0 or not ev.get("encrypted"):
            raise ControllerError("PAIRING_FAILED", f"enc_change={ev}")
        log(f"link encrypted (key_size={ev.get('key_size')})")

    def disconnect(self):
        try:
            self.client.send("disconnect", timeout=5)
        except ControllerError:
            pass
        self.client.wait_for_event("disconnected", timeout=5)

    def send_raw(self, message):
        sensitive = (message[2] | (message[3] << 8)) in w.HLV_WIFI_CREDENTIAL_OPCODES
        self.client.send("write", timeout=12, sensitive=sensitive, characteristic=HLV_CONTROL_POINT_UUID,
                         data_hex=message.hex(), timeout_ms=10000)
        log(f"-> {w.describe_request(message)}")

    def wait_terminal(self, txn, window_s=20.0):
        """Returns (accepted_seen, terminal_response) for `txn`."""
        deadline = time.time() + window_s
        accepted = False
        while time.time() < deadline:
            ev = self.client.wait_for_event("notification", timeout=deadline - time.time())
            if ev is None:
                break
            if ev.get("characteristic") != HLV_COMMAND_RESPONSE_UUID:
                continue
            cr = decode_command_response(ev["data_hex"])
            if cr["transaction_id"] != txn:
                continue
            if cr["phase"] == HLV_RESPONSE_PHASE_ACCEPTED:
                accepted = True
                continue
            return accepted, cr
        return accepted, None

    def request(self, opcode, payload=b"", txn=None):
        txn = self.next_txn() if txn is None else txn
        self.send_raw(w.encode_wifi_request(opcode, txn, payload))
        accepted, cr = self.wait_terminal(txn)
        if cr is None:
            raise ControllerError("NO_RESPONSE", f"opcode=0x{opcode:04x} txn={txn}")
        cr["accepted_seen"] = accepted
        cr["txn"] = txn
        return cr

    def read_snapshot(self):
        r = self.client.send("read", timeout=10, characteristic=w.HLV_WIFI_SNAPSHOT_UUID, long=True, timeout_ms=8000)
        return bytes.fromhex(r["data_hex"])

    def snapshot_request(self, opcode):
        cr = self.request(opcode)
        if cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED or cr["result_code"] != 0:
            raise ControllerError("SNAPSHOT_REQUEST_FAILED", f"phase={cr['phase']} result={cr['result_code']}")
        ref = w.decode_snapshot_ref(cr["inner"])
        return w.decode_snapshot(self.read_snapshot(), expect_txn=cr["txn"], expect_ref=ref)


def show_result(opcode, cr):
    phase = {1: "ACCEPTED", 2: "REJECTED", 3: "COMPLETED"}.get(cr["phase"], cr["phase"])
    out = {"phase": phase, "result_code": cr["result_code"]}
    if cr["phase"] == HLV_RESPONSE_PHASE_COMPLETED and len(cr["inner"]) == 4 and opcode in w.HLV_WIFI_ASYNC_OPCODES:
        out.update(w.decode_wifi_completed(opcode, cr["result_code"], cr["inner"]))
    return out


def read_password():
    pw = os.environ.get("HLV_WIFI_PASSWORD")
    if pw is None:
        pw = getpass.getpass("Wi-Fi password (not echoed; empty for open): ")
    return pw.encode("ascii")


def printable(obj):
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "backslashreplace")
    if isinstance(obj, dict):
        return {k: printable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [printable(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------------------------
# Hardware self-test (HLV-WIFI-008 checklist). Dummy values only.
# ---------------------------------------------------------------------------------------------
class Checks:
    def __init__(self):
        self.results = []

    def check(self, ok, what):
        self.results.append((bool(ok), what))
        log(f"{'PASS' if ok else 'FAIL'}: {what}")

    @property
    def failed(self):
        return [w_ for ok, w_ in self.results if not ok]


def dummy_networks():
    """Five dummy networks: the first uses the maximum SSID (32 B) and a 64-hex PSK
    (maximum request, 112 B) so the long write at MTU 23 is exercised."""
    nets = [(b"HLV-DUMMY-" + b"X" * 22, b"0123456789abcdef" * 4, w.WIFI_AUTH["wpa2"])]
    for i in range(1, 5):
        nets.append((f"HLV-DUMMY-{i}".encode(), f"dummy-pass-{i:02d}".encode(), w.WIFI_AUTH["wpa2"]))
    return nets


def selftest(client, mtu23, log_path):
    chk = Checks()
    s = WifiSession(client, mtu23=mtu23)
    secrets = [p for _, p, _ in dummy_networks()] + [b"dummy-new-pw-3", b"dummy-pass-6th", b"dummy-pass-retry"]

    # 1. unauthorized
    s.connect(authorize=False, pair=False)
    cr = s.request(w.HLV_OPCODE_WIFI_LIST)
    chk.check(cr["phase"] == HLV_RESPONSE_PHASE_REJECTED and cr["result_code"] == HLV_RESULT_UNAUTHORIZED,
              "LIST before AUTH -> REJECTED UNAUTHORIZED")
    try:
        s.read_snapshot()
        chk.check(False, "Snapshot read before AUTH is refused")
    except ControllerError:
        chk.check(True, "Snapshot read before AUTH is refused")
    authenticate_raw(client, s.next_txn())

    # 2. capabilities, unencrypted credential refused
    cr = s.request(w.HLV_OPCODE_WIFI_GET_CAPABILITIES)
    caps = w.decode_capabilities(cr["inner"]) if cr["phase"] == HLV_RESPONSE_PHASE_COMPLETED else {}
    chk.check(caps.get("version") == 1 and caps.get("max_networks") == 5 and caps.get("snapshot_char"),
              f"capabilities {caps}")
    name, pw, auth = dummy_networks()[1]
    cr = s.request(w.HLV_OPCODE_WIFI_ADD, w.encode_wifi_add_payload(name, pw, auth))
    chk.check(cr["phase"] == HLV_RESPONSE_PHASE_REJECTED and cr["result_code"] == w.HLV_RESULT_ENCRYPTION_REQUIRED,
              "ADD on an unencrypted link -> ENCRYPTION_REQUIRED")
    s.pair()

    # 3. clear, bad input, unknown opcode
    cr = s.request(w.HLV_OPCODE_WIFI_CLEAR_ALL, b"")
    chk.check(cr["result_code"] == HLV_RESULT_INVALID_ARGUMENT, "CLEAR_ALL without confirmation -> INVALID_ARGUMENT")
    cr = s.request(w.HLV_OPCODE_WIFI_CLEAR_ALL, w.WIFI_CLEAR_CONFIRM)
    chk.check(cr["result_code"] == 0 and cr["accepted_seen"], "CLEAR_ALL WCLR -> ACCEPTED then COMPLETED OK")
    bad = bytearray(w.encode_wifi_add_payload(b"HLV-DUMMY-BAD", b"dummy-pass-bad", 1))
    bad[1] += 1
    cr = s.request(w.HLV_OPCODE_WIFI_ADD, bytes(bad))
    chk.check(cr["result_code"] == HLV_RESULT_INVALID_ARGUMENT, "ADD with inconsistent lengths -> INVALID_ARGUMENT")
    cr = s.request(0x003A)
    chk.check(cr["result_code"] == w.HLV_RESULT_NOT_SUPPORTED, "unknown opcode 0x003A -> NOT_SUPPORTED")

    # 4. five networks (max lengths first), a sixth rejected
    ids = []
    for name, pw, auth in dummy_networks():
        cr = s.request(w.HLV_OPCODE_WIFI_ADD, w.encode_wifi_add_payload(name, pw, auth))
        r = w.decode_wifi_completed(w.HLV_OPCODE_WIFI_ADD, cr["result_code"], cr["inner"])
        ids.append(r.get("id"))
        chk.check(cr["accepted_seen"] and r["ok"], f"ADD {name.decode()} -> id {r.get('id')}")
    cr = s.request(w.HLV_OPCODE_WIFI_ADD, w.encode_wifi_add_payload(b"HLV-DUMMY-6", b"dummy-pass-6th", 1))
    r = w.decode_wifi_completed(w.HLV_OPCODE_WIFI_ADD, cr["result_code"], cr["inner"])
    chk.check(cr["result_code"] == w.HLV_RESULT_WIFI_REJECTED and r.get("wifi_result_name") == "FULL",
              "sixth ADD -> WIFI_REJECTED / FULL")
    snap = s.snapshot_request(w.HLV_OPCODE_WIFI_LIST)
    chk.check(len(snap["networks"]) == 5 and [n["id"] for n in snap["networks"]] == ids,
              "LIST: the five networks, insertion order, nothing replaced")

    # 5. update in place, retry, preferred, delete
    if len(ids) == 5 and all(ids):
        cr = s.request(w.HLV_OPCODE_WIFI_UPDATE, w.encode_wifi_update_payload(ids[3], b"dummy-new-pw-3", 1))
        chk.check(cr["result_code"] == 0, "UPDATE (new password) at full capacity -> OK")
        payload = w.encode_wifi_add_payload(b"HLV-DUMMY-RETRY", b"dummy-pass-retry", 1)
        cr = s.request(w.HLV_OPCODE_WIFI_DELETE, w.encode_wifi_id_payload(ids[4]))
        chk.check(cr["result_code"] == 0, "DELETE -> OK")
        cr1 = s.request(w.HLV_OPCODE_WIFI_ADD, payload)
        cr2 = s.request(w.HLV_OPCODE_WIFI_ADD, payload, txn=cr1["txn"])
        chk.check(cr1["result_code"] == 0 and cr2["inner"] == cr1["inner"] and cr2["result_code"] == 0,
                  "identical retry (same txn) -> the cached COMPLETED, same id")
        cr = s.request(w.HLV_OPCODE_WIFI_SET_PREFERRED, w.encode_wifi_id_payload(ids[2]))
        snap = s.snapshot_request(w.HLV_OPCODE_WIFI_LIST)
        chk.check(cr["result_code"] == 0 and snap["networks"][0]["id"] == ids[2] and snap["networks"][0]["preferred"]
                  and len(snap["networks"]) == 5, "SET_PREFERRED -> listed first; retry created no duplicate")

    # 6. start, clear during the attempt, stop
    cr = s.request(w.HLV_OPCODE_WIFI_START)
    chk.check(cr["result_code"] == 0, "START -> OK (dummy networks: the attempts fail and back off)")
    time.sleep(2)
    st = s.snapshot_request(w.HLV_OPCODE_WIFI_GET_STATUS)
    log(f"status: state={st['state_name']} current={st['current_id']} round={st['round']}")
    cr = s.request(w.HLV_OPCODE_WIFI_CLEAR_ALL, w.WIFI_CLEAR_CONFIRM)
    st = s.snapshot_request(w.HLV_OPCODE_WIFI_GET_STATUS)
    chk.check(cr["result_code"] == 0 and st["state_name"] == "NO_NETWORKS" and st["next_retry_in_ms"] == 0
              and st["configured"] == 0, "CLEAR_ALL while started -> NO_NETWORKS, no retry scheduled")
    cr = s.request(w.HLV_OPCODE_WIFI_STOP)
    chk.check(cr["result_code"] == 0, "STOP -> OK")

    # 7. disconnect mid-request, then retry after reconnecting
    payload = w.encode_wifi_add_payload(b"HLV-DUMMY-1", b"dummy-pass-01", 1)
    txn = s.next_txn()
    s.send_raw(w.encode_wifi_request(w.HLV_OPCODE_WIFI_ADD, txn, payload))
    s.disconnect()
    s.connect()
    cr = s.request(w.HLV_OPCODE_WIFI_ADD, payload, txn=txn)
    snap = s.snapshot_request(w.HLV_OPCODE_WIFI_LIST)
    chk.check(cr["result_code"] == 0 and len(snap["networks"]) == 1,
              "disconnect after the write, retry the same txn -> one network, no duplicate")
    s.request(w.HLV_OPCODE_WIFI_CLEAR_ALL, w.WIFI_CLEAR_CONFIRM)
    s.disconnect()

    # 8. no dummy password anywhere in the client log
    if log_path:
        client_log = open(log_path, "rb").read()
        leaked = [p for p in secrets if p in client_log or p.hex().encode() in client_log]
        chk.check(not leaked, f"client log {log_path}: no password (ascii or hex) present")
    log(f"SELFTEST: {len(chk.results) - len(chk.failed)}/{len(chk.results)} passed")
    for f in chk.failed:
        log(f"  FAILED: {f}")
    log("Not covered here (host tests only): save failure, full request queue. "
        "Manual: power-cycle the board and run `list` to check persistence; "
        "search the device console capture for the dummy passwords.")
    return 0 if not chk.failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--mtu23", action="store_true", help="keep ATT MTU 23")
    ap.add_argument("--no-pair", action="store_true", help="do not encrypt the link")
    ap.add_argument("--log", help="JSONL log of the controller traffic (credentials redacted)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("caps", "list", "status", "start", "stop"):
        sub.add_parser(name)
    p = sub.add_parser("add")
    p.add_argument("--ssid", required=True)
    p.add_argument("--auth", choices=sorted(w.WIFI_AUTH), default="wpa2")
    p = sub.add_parser("update")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--auth", choices=sorted(w.WIFI_AUTH), default="wpa2")
    for name in ("delete", "prefer"):
        sub.add_parser(name).add_argument("--id", type=int, required=True)
    sub.add_parser("clear").add_argument("--confirm", action="store_true", required=True)
    sub.add_parser("selftest").add_argument("--erase-wifi-config", action="store_true", required=True)
    args = ap.parse_args(argv)

    log_file = open(args.log, "a", encoding="utf-8") if args.log else None
    try:
        with HolterBLEClient(args.port, log_file=log_file) as client:
            if args.cmd == "selftest":
                return selftest(client, args.mtu23, args.log)
            s = WifiSession(client, mtu23=args.mtu23)
            s.connect(pair=not args.no_pair)
            if args.cmd == "list":
                result = s.snapshot_request(w.HLV_OPCODE_WIFI_LIST)
            elif args.cmd == "status":
                result = s.snapshot_request(w.HLV_OPCODE_WIFI_GET_STATUS)
            elif args.cmd == "caps":
                cr = s.request(w.HLV_OPCODE_WIFI_GET_CAPABILITIES)
                result = w.decode_capabilities(cr["inner"]) if cr["result_code"] == 0 else show_result(0x30, cr)
            else:
                op, payload = {
                    "add": lambda: (w.HLV_OPCODE_WIFI_ADD,
                                    w.encode_wifi_add_payload(args.ssid.encode("utf-8"), read_password(), w.WIFI_AUTH[args.auth])),
                    "update": lambda: (w.HLV_OPCODE_WIFI_UPDATE,
                                       w.encode_wifi_update_payload(args.id, read_password(), w.WIFI_AUTH[args.auth])),
                    "delete": lambda: (w.HLV_OPCODE_WIFI_DELETE, w.encode_wifi_id_payload(args.id)),
                    "prefer": lambda: (w.HLV_OPCODE_WIFI_SET_PREFERRED, w.encode_wifi_id_payload(args.id)),
                    "clear": lambda: (w.HLV_OPCODE_WIFI_CLEAR_ALL, w.WIFI_CLEAR_CONFIRM),
                    "start": lambda: (w.HLV_OPCODE_WIFI_START, b""),
                    "stop": lambda: (w.HLV_OPCODE_WIFI_STOP, b""),
                }[args.cmd]()
                result = show_result(op, s.request(op, payload))

            print(json.dumps(printable(result), indent=2))
            s.disconnect()
            return 0
    except ControllerError as e:
        log(f"ERROR {e.error_code}: {e.detail}")
        return 2
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    sys.exit(main())
