#!/usr/bin/env python3
"""
HolterLEV BLE Test Controller -- Python host CLI.

Talks NDJSON over USB UART to the ESP32 test-controller firmware in this
repository, which is the BLE Central/GATT Client against the real
HolterLEV DUT (BLE Peripheral/GATT Server). See docs/PROTOCOL_REFERENCE.md
for the wire protocol and docs/UART_AUTOMATION_PROTOCOL.md-equivalent
section in the repo root task brief.

Examples:
    python tools/holter_ble_cli.py list-ports
    python tools/holter_ble_cli.py --port COM28 ping
    python tools/holter_ble_cli.py --port COM28 scan --name NOAMED --timeout 5
    python tools/holter_ble_cli.py --port COM28 connect --name NOAMED
    python tools/holter_ble_cli.py --port COM28 discover-services
    python tools/holter_ble_cli.py --port COM28 discover-characteristics
    python tools/holter_ble_cli.py --port COM28 holter-connect --name NOAMED
    python tools/holter_ble_cli.py --port COM28 holter-authenticate
    python tools/holter_ble_cli.py --port COM28 holter-get-status
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402


def cmd_list_ports(args):
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    if args.json:
        print(json.dumps([
            {"device": p.device, "description": p.description, "hwid": p.hwid,
             "vid": p.vid, "pid": p.pid, "serial_number": p.serial_number}
            for p in ports
        ], indent=2))
    else:
        if not ports:
            print("No serial ports found.")
        for p in ports:
            print(f"{p.device}\t{p.description}\t{p.hwid}")
    return 0


def open_client(args):
    if not args.port:
        print("error: --port is required (use 'list-ports' to find it; "
              "never guess which port is the test controller vs. the DUT)", file=sys.stderr)
        sys.exit(2)
    log_file = None
    if args.log_dir:
        os.makedirs(args.log_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(args.log_dir, f"cli_{ts}.jsonl")
        log_file = open(log_path, "a", encoding="utf-8")
    return HolterBLEClient(args.port, baud=args.baud, default_timeout=args.host_timeout, log_file=log_file)


def _print_result(args, resp):
    if args.json:
        print(json.dumps(resp))
    else:
        for k, v in resp.items():
            if k in ("type", "id", "status"):
                continue
            print(f"{k}: {v}")


def run_simple(args, cmd_name, fields, wire_timeout_field="timeout_ms"):
    client = open_client(args)
    try:
        kw = dict(fields)
        if args.timeout is not None and wire_timeout_field is not None:
            kw[wire_timeout_field] = int(args.timeout * 1000)
        resp = client.send(cmd_name, timeout=args.host_timeout, **kw)
        _print_result(args, resp)
        return 0
    except ControllerError as e:
        if args.json:
            print(json.dumps({"status": "error", "error": e.error_code, "detail": e.detail}))
        else:
            print(f"ERROR {e.error_code}: {e.detail}", file=sys.stderr)
        return 1
    finally:
        client.close()


def cmd_scan(args):
    client = open_client(args)
    try:
        fields = {"timeout_ms": int(args.timeout * 1000) if args.timeout else 5000}
        if args.name:
            fields["name"] = args.name
        if args.addr:
            fields["addr"] = args.addr
        if args.service_uuid:
            fields["service_uuid"] = args.service_uuid

        devices = []

        def collect_events():
            for ev in client.drain_events():
                if ev.get("event") == "device_found":
                    devices.append(ev)
                    if not args.json:
                        print(f"  found: {ev.get('addr')}  name={ev.get('name')!r}  rssi={ev.get('rssi')}")

        resp = client.send("scan", timeout=(fields["timeout_ms"] / 1000.0) + 5, **fields)
        collect_events()
        if args.json:
            print(json.dumps({"devices_found": resp.get("devices_found"), "devices": devices}))
        else:
            print(f"devices_found: {resp.get('devices_found')}")
        return 0
    except ControllerError as e:
        print(f"ERROR {e.error_code}: {e.detail}", file=sys.stderr)
        return 1
    finally:
        client.close()


def cmd_discover(args):
    """Convenience: discover_services then discover_characteristics."""
    client = open_client(args)
    try:
        svc = client.send("discover_services", timeout=args.host_timeout)
        _print_result(args, svc)
        chr_ = client.send("discover_characteristics", timeout=args.host_timeout)
        _print_result(args, chr_)
        return 0
    except ControllerError as e:
        print(f"ERROR {e.error_code}: {e.detail}", file=sys.stderr)
        return 1
    finally:
        client.close()


def add_common_args(p):
    p.add_argument("--timeout", type=float, default=None, help="wire-level timeout_ms, in seconds")


def main():
    parser = argparse.ArgumentParser(description="HolterLEV BLE Test Controller CLI")
    parser.add_argument("--port", help="serial port of the TEST CONTROLLER (never the DUT)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--host-timeout", type=float, default=15.0, help="host-side wait ceiling, seconds")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--log-dir", default=None, help="directory to save a timestamped NDJSON transcript")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-ports").set_defaults(func=cmd_list_ports)

    p = sub.add_parser("ping"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "ping", {}, wire_timeout_field=None))

    p = sub.add_parser("version"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "version", {}, wire_timeout_field=None))

    p = sub.add_parser("controller-status"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "controller_status", {}, wire_timeout_field=None))

    p = sub.add_parser("scan")
    p.add_argument("--name")
    p.add_argument("--addr")
    p.add_argument("--service-uuid")
    add_common_args(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("scan-stop")
    p.set_defaults(func=lambda a: run_simple(a, "scan_stop", {}, wire_timeout_field=None))

    p = sub.add_parser("connect")
    p.add_argument("--name")
    p.add_argument("--addr")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "connect",
                    {k: v for k, v in (("name", a.name), ("addr", a.addr)) if v}))

    p = sub.add_parser("disconnect")
    p.set_defaults(func=lambda a: run_simple(a, "disconnect", {}, wire_timeout_field=None))

    p = sub.add_parser("discover-services"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "discover_services", {}))

    p = sub.add_parser("discover-characteristics")
    p.add_argument("--service-uuid")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "discover_characteristics",
                    {k: v for k, v in (("service_uuid", a.service_uuid),) if v}))

    p = sub.add_parser("discover"); add_common_args(p)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("read")
    p.add_argument("--characteristic", required=True)
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "read", {"characteristic": a.characteristic}))

    p = sub.add_parser("write")
    p.add_argument("--characteristic", required=True)
    p.add_argument("--data-hex", required=True)
    p.add_argument("--no-response", action="store_true")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "write", {
        "characteristic": a.characteristic, "data_hex": a.data_hex,
        "with_response": not a.no_response}))

    p = sub.add_parser("subscribe")
    p.add_argument("--characteristic", required=True)
    p.add_argument("--mode", choices=["notify", "indicate"], default="notify")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "subscribe",
                    {"characteristic": a.characteristic, "mode": a.mode}))

    p = sub.add_parser("unsubscribe")
    p.add_argument("--characteristic", required=True)
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "unsubscribe", {"characteristic": a.characteristic}))

    p = sub.add_parser("wait-event")
    p.add_argument("--event")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "wait_event",
                    {k: v for k, v in (("event", a.event),) if v}))

    sub.add_parser("clear-bonds").set_defaults(func=lambda a: run_simple(a, "clear_bonds", {}, wire_timeout_field=None))
    sub.add_parser("reset-controller").set_defaults(func=lambda a: run_simple(a, "reset_controller", {}, wire_timeout_field=None))

    p = sub.add_parser("holter-connect")
    p.add_argument("--name")
    p.add_argument("--addr")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_connect",
                    {k: v for k, v in (("name", a.name), ("addr", a.addr)) if v}))

    p = sub.add_parser("holter-authenticate")
    p.add_argument("--code", help="4-digit dev access code (default: firmware's built-in default, '2018')")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_authenticate",
                    {k: v for k, v in (("code", a.code),) if v}))

    p = sub.add_parser("holter-get-status"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_get_status", {}))

    p = sub.add_parser("holter-get-device-info"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_get_device_info", {}))

    p = sub.add_parser("holter-get-session-status"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_get_session_status", {}))

    p = sub.add_parser("holter-start-session")
    p.add_argument("--duration-seconds", type=int, required=True)
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_start_session",
                    {"duration_seconds": a.duration_seconds}))

    p = sub.add_parser("holter-stop-session"); add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_stop_session", {}))

    p = sub.add_parser("holter-wait-state")
    p.add_argument("--target-state", required=True,
                   help="INITIALIZING|READY|PREPARING|RECORDING|STOPPING|ERROR")
    add_common_args(p)
    p.set_defaults(func=lambda a: run_simple(a, "holter_wait_state", {"target_state": a.target_state}))

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
