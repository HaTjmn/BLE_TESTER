#!/usr/bin/env python3
"""
tools/holter_diag_generate.py -- official, tracked CLI for HolterLEV's
diagnostic-only TLIV file generator (DIAG_GENERATE_TEST_FILE opcode
0x0027, HOLTER_DIAG_TEST_FILE_GEN_ENABLED builds only).

This is the productized replacement (BLE-D4.0U) for the untracked,
one-off scratch scripts used in BLE-D4.0K/M/Q/S. It performs exactly one
generation attempt per invocation and delegates ALL phase/timeout
decisions to the BLE-D4.0T state machine (diag_generation_monitor.py) --
this file contains no progress-timeout or phase-transition logic of its
own.

IMPORTANT: this generator produces deterministic, synthetic TLIV
content. It never touches ADS1293 acquisition and must never be
confused with a real recording. Production HolterLEV firmware does not
build this opcode in at all -- it exists ONLY in diagnostic builds
(esp32-s3-devkitc-1-n32r8v-ble-diag-dev), and this tool will report a
clear ACCEPTED-refused error (not a crash) if pointed at a production
build that lacks it.

PowerShell example:
    python tools/holter_diag_generate.py `
        --tester-port COM17 --console-port COM23 `
        --records 1873 --output-dir tools/_evidence/generation_12mb

Bash example:
    python tools/holter_diag_generate.py \\
        --tester-port /dev/ttyUSB0 --console-port /dev/ttyUSB1 \\
        --records 1873 --output-dir tools/_evidence/generation_12mb

See docs/GENERATION_CLI.md for the full argument reference, phase
descriptions, evidence-package format, and exit-code table.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402
from holter_hlv_protocol import (  # noqa: E402
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_STATUS_UUID,
    HLV_OPCODE_LIST_FILES, HLV_OPCODE_GET_FILE_INFO, HLV_OPCODE_DIAG_GENERATE_TEST_FILE,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_DIAG_TEST_FILE_MAX_RECORDS,
    encode_hlv_message, decode_status_bytes, decode_list_files_response, decode_get_file_info_response,
    decode_diag_generate_test_file_response, encode_diag_generate_test_file_request_v2,
    wait_for_command_response, authenticate_raw,
)
from diag_generation_monitor import (  # noqa: E402
    GenerationMonitorStateMachine, GenerationRunner, GenerationPhase,
    compute_expected_tliv_bytes, compute_post_write_validation_timeout_s,
    DEFAULT_ACCEPTANCE_TIMEOUT_S, DEFAULT_WRITE_STALL_TIMEOUT_S, DEFAULT_MAX_TOTAL_DURATION_S,
)

TOOL_VERSION = "1.0.0"  # BLE-D4.0U
MIN_RECORDS = 1
MAX_RECORDS = HLV_DIAG_TEST_FILE_MAX_RECORDS  # 20000 -- the current diagnostic firmware's own compile-time cap
DEFAULT_FRAMES_PER_RECORD = 512  # sanity-checked device-side only; the real per-record frame count is fixed

# ---- Exit codes (BLE-D4.0U's own first formal convention for this repo --
# no prior tool defined one; documented in docs/GENERATION_CLI.md). -------
EXIT_SUCCESS = 0
EXIT_INVALID_ARGS = 2
EXIT_SERIAL_OWNERSHIP_FAILURE = 3
EXIT_BLE_PREFLIGHT_FAILURE = 4
EXIT_GENERATION_TIMEOUT = 5
EXIT_FIRMWARE_TERMINAL_FAILURE = 6
EXIT_RESULT_VALIDATION_FAILURE = 7
EXIT_UNEXPECTED_TOOL_FAILURE = 8
EXIT_USER_CANCELLED = 130

EXIT_CODE_MEANINGS = {
    EXIT_SUCCESS: "success",
    EXIT_INVALID_ARGS: "invalid arguments or unsafe output directory",
    EXIT_SERIAL_OWNERSHIP_FAILURE: "serial-port ownership/open failure",
    EXIT_BLE_PREFLIGHT_FAILURE: "BLE connection/authentication/preflight failure",
    EXIT_GENERATION_TIMEOUT: "generation timeout (TIMED_OUT phase)",
    EXIT_FIRMWARE_TERMINAL_FAILURE: "firmware reported a terminal generation failure",
    EXIT_RESULT_VALIDATION_FAILURE: "result validation or evidence-integrity failure",
    EXIT_UNEXPECTED_TOOL_FAILURE: "unexpected tool failure (see traceback in console_raw.log/stderr)",
    EXIT_USER_CANCELLED: "cancelled by user (Ctrl+C)",
}

_SD_WRITER_PATTERN = re.compile(
    r"SD_WRITER_SUMMARY session=\S*, open=\d+, attempted=\d+, written=(\d+), bytes=(\d+)"
)
_AUTH_ARG_NAMES = {"auth_code"}  # never written to the manifest or printed


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


class CliError(Exception):
    """Raised for any expected, structured failure -- carries the exit
    code that produced it so main() never has to re-derive it."""
    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="holter_diag_generate.py",
        description=(
            "Generate one deterministic diagnostic TLIV file on HolterLEV over BLE "
            "(DIAG_GENERATE_TEST_FILE, diagnostic builds only) and monitor it with the "
            "BLE-D4.0T phase-aware state machine. Never touches ADS1293 acquisition -- "
            "this is NOT a real recording."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--tester-port", required=True, help="serial port of the BLE_TESTER control link (never the DUT)")
    p.add_argument("--console-port", required=True, help="HolterLEV's own UART0 console/boot port")
    p.add_argument("--records", type=int, required=True,
                   help=f"record count to request ({MIN_RECORDS}-{MAX_RECORDS}; diagnostic-only opcode, "
                        f"unavailable in production firmware)")
    p.add_argument("--output-dir", required=True, help="evidence directory; must NOT already exist")
    p.add_argument("--baud", type=int, default=115200, help="baud rate for both serial ports")
    p.add_argument("--device-name", default=HLV_DEVICE_NAME, help="BLE device name to connect to")
    p.add_argument("--device-addr", default=None, help="BLE address to connect to (alternative to --device-name)")
    p.add_argument("--frames-per-record", type=int, default=DEFAULT_FRAMES_PER_RECORD,
                   help="device-side sanity value only; does not change actual per-record frame count")
    p.add_argument("--auth-code", default=None,
                   help="device auth code -- supported for backward compatibility, but LESS SECURE "
                        "(visible in shell history/process listings). Prefer $HOLTER_AUTH_CODE for "
                        "automation, or omit both and answer the interactive prompt for manual use. "
                        "NEVER logged or written to evidence; this tool never falls back to a "
                        "hardcoded development code.")
    p.add_argument("--host-timeout", type=float, default=15.0, help="host-side wire-level wait ceiling, seconds")

    p.add_argument("--acceptance-timeout", type=float, default=DEFAULT_ACCEPTANCE_TIMEOUT_S,
                   help="seconds to wait for the ACCEPTED response (BLE-D4.0T default)")
    p.add_argument("--write-stall-timeout", type=float, default=DEFAULT_WRITE_STALL_TIMEOUT_S,
                   help="seconds of no write progress (below the requested count) before TIMED_OUT "
                        "(BLE-D4.0T default)")
    p.add_argument("--post-write-timeout", type=float, default=None,
                   help="seconds allowed for POST_WRITE_VALIDATION; default: computed conservatively "
                        "from --records by diag_generation_monitor.compute_post_write_validation_timeout_s()")
    p.add_argument("--overall-timeout", type=float, default=DEFAULT_MAX_TOTAL_DURATION_S,
                   help="belt-and-suspenders overall cap, seconds (BLE-D4.0T default)")
    return p.parse_args(argv)


def validate_args(args):
    if not (MIN_RECORDS <= args.records <= MAX_RECORDS):
        raise CliError(
            EXIT_INVALID_ARGS,
            f"--records={args.records} is outside the supported diagnostic range "
            f"[{MIN_RECORDS}, {MAX_RECORDS}]. This opcode is diagnostic-only and does not exist "
            f"in production HolterLEV firmware; {MAX_RECORDS} is the current diagnostic build's own "
            f"compile-time HOLTER_DIAG_TEST_FILE_MAX_RECORDS cap.",
        )
    if args.frames_per_record <= 0:
        raise CliError(EXIT_INVALID_ARGS, f"--frames-per-record must be positive, got {args.frames_per_record}")
    for name in ("acceptance_timeout", "write_stall_timeout", "overall_timeout"):
        v = getattr(args, name)
        if v <= 0:
            raise CliError(EXIT_INVALID_ARGS, f"--{name.replace('_', '-')} must be positive, got {v}")
    if args.post_write_timeout is not None and args.post_write_timeout <= 0:
        raise CliError(EXIT_INVALID_ARGS, f"--post-write-timeout must be positive, got {args.post_write_timeout}")
    if args.tester_port == args.console_port:
        raise CliError(EXIT_INVALID_ARGS, "--tester-port and --console-port must not be the same port")

    if os.path.exists(args.output_dir):
        raise CliError(
            EXIT_INVALID_ARGS,
            f"--output-dir already exists: {args.output_dir!r} -- refusing to reuse or overwrite prior "
            f"evidence. Choose a new directory (this tool never deletes or cleans an existing one).",
        )


def sanitize_args_for_evidence(args):
    """Never persists --auth-code (or any future secret-bearing option) --
    Section 3/7's own explicit requirement."""
    d = dict(vars(args))
    for k in _AUTH_ARG_NAMES:
        if k in d and d[k] is not None:
            d[k] = "<redacted>"
    return d


class Secret:
    """BLE-D4.0V: wraps a resolved auth-code so an accidental str()/repr()/
    f-string interpolation anywhere (a log line, an exception message, a
    debugger, a future code change) can never reveal the value. Only
    .reveal() returns the real bytes -- exactly one call site (the
    authenticate_raw() call in connect_and_prepare()) is permitted to
    use it."""
    __slots__ = ("_value",)

    def __init__(self, value: bytes):
        self._value = value

    def reveal(self) -> bytes:
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


AUTH_CODE_ENV_VAR = "HOLTER_AUTH_CODE"


def resolve_auth_code(args):
    """BLE-D4.0V: safer auth-code precedence, replacing BLE-D4.0U's
    silent hardcoded-development-code fallback (removed -- this tool
    must never fall back to HLV_DEV_ACCESS_CODE on its own):

      1. --auth-code       (backward compatible, but least secure --
                             visible in shell history/process listings)
      2. $HOLTER_AUTH_CODE (recommended for automation/CI)
      3. interactive getpass, only when stdin is a real TTY
                            (recommended for manual/interactive use)
      4. clean failure -- never a silent fallback

    Returns (Secret, source_name) where source_name is safe,
    non-secret metadata ("cli_argument"/"environment_variable"/
    "interactive_getpass") suitable for the manifest. Raises CliError
    (EXIT_INVALID_ARGS) before any port is opened if nothing is
    available non-interactively.
    """
    if args.auth_code:
        return Secret(args.auth_code.encode("utf-8")), "cli_argument"

    env = os.environ.get(AUTH_CODE_ENV_VAR)
    if env:
        return Secret(env.encode("utf-8")), "environment_variable"

    if sys.stdin.isatty():
        import getpass
        code = getpass.getpass("HolterLEV device auth code (input hidden): ")
        if code:
            return Secret(code.encode("utf-8")), "interactive_getpass"

    raise CliError(
        EXIT_INVALID_ARGS,
        f"no auth code available: pass --auth-code, set ${AUTH_CODE_ENV_VAR} "
        f"(recommended for automation), or run this tool interactively so it can "
        f"prompt securely (recommended for manual use). This tool never falls back "
        f"to a hardcoded development access code.",
    )


class Manifest:
    """Incrementally-written run manifest + append-only event log.
    Section 7: "Evidence files should be written incrementally so that
    useful information remains after failure or cancellation" -- every
    update below flushes to disk immediately, and finalize() is always
    called from main()'s own finally path."""

    def __init__(self, output_dir, args):
        self.output_dir = output_dir
        self.manifest_path = os.path.join(output_dir, "run_manifest.json")
        self.events_path = os.path.join(output_dir, "events.jsonl")
        self._events_fh = open(self.events_path, "a", encoding="utf-8")
        self.data = {
            "tool": "holter_diag_generate.py",
            "tool_version": TOOL_VERSION,
            "utc_start": utc_now_iso(),
            "utc_end": None,
            "args": sanitize_args_for_evidence(args),
            "tester_port": args.tester_port,
            "console_port": args.console_port,
            "device_name": args.device_name,
            "device_addr": args.device_addr,
            "requested_records": args.records,
            "expected_bytes": None,
            "timeouts": {
                "acceptance_s": args.acceptance_timeout,
                "write_stall_s": args.write_stall_timeout,
                "post_write_s": args.post_write_timeout,
                "overall_s": args.overall_timeout,
            },
            "phase_transitions": [],
            "serial_ports_opened": 0,
            "serial_ports_closed": 0,
            "ble_connections": 0,
            "ble_authentications": 0,
            "generation_commands_sent": 0,
            "final_status": None,
            "exit_code": None,
            "error_classification": None,
        }
        self._flush()

    def _flush(self):
        tmp_path = self.manifest_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)
        os.replace(tmp_path, self.manifest_path)

    def update(self, **kwargs):
        self.data.update(kwargs)
        self._flush()

    def event(self, kind, **fields):
        record = {"utc_time": utc_now_iso(), "kind": kind, **fields}
        self._events_fh.write(json.dumps(record, default=str) + "\n")
        self._events_fh.flush()
        if kind == "phase_transition":
            self.data["phase_transitions"].append(record)
            self._flush()

    def finalize(self, final_status, exit_code, error_classification=None):
        self.data["utc_end"] = utc_now_iso()
        self.data["final_status"] = final_status
        self.data["exit_code"] = exit_code
        self.data["error_classification"] = error_classification
        self._flush()
        try:
            self._events_fh.close()
        except Exception:
            pass


class _TeeConsolePort:
    """Transparent proxy around a real console-port object: every byte
    read is also appended to console_raw.log. GenerationRunner sees an
    object identical in interface to the raw port -- this class adds
    zero phase/decision logic, purely evidence capture."""

    def __init__(self, real_port, log_path):
        self._real = real_port
        self._log = open(log_path, "ab", buffering=0)

    def read(self, n):
        chunk = self._real.read(n)
        if chunk:
            self._log.write(chunk)
        return chunk

    def close(self):
        try:
            self._real.close()
        finally:
            self._log.close()


def list_all_files(client, base_txn):
    """Pages through LIST_FILES until has_more is false -- the same
    pagination shape holter_file_download.py's own list_all_files()
    already established; kept local here (not extracted into a shared
    module) consistently with how every prior tool in this project
    inlines its own copy of this small orchestration loop."""
    files = []
    start_index = 0
    txn = base_txn
    while True:
        txn += 1
        msg = encode_hlv_message(HLV_OPCODE_LIST_FILES, txn, start_index.to_bytes(2, "little"))
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        cr = wait_for_command_response(client, txn, window_s=5)
        if cr is None:
            raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, "LIST_FILES: no Command Response received")
        page = decode_list_files_response(cr["inner"])
        files.extend(page["entries"])
        if not page["has_more"] or not page["entries"]:
            break
        start_index = page["entries"][-1]["file_index"] + 1
    return files, txn


def get_file_info(client, base_txn, file_index):
    txn = base_txn + 1
    msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, txn, file_index.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, txn, window_s=5)
    if cr is None:
        raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, f"GET_FILE_INFO(file_index={file_index}): no Command Response")
    return decode_get_file_info_response(cr["inner"]), txn


def connect_and_prepare(client, args, manifest, auth_code):
    """Steps 5-9: version/health (best-effort), connect once (BLE_TESTER's
    own 'connect' command performs scan+connect together when given a
    device name or address -- there is no separate standalone scan step
    for HolterLEV in this project's own established convention),
    discover once, subscribe once, authenticate once."""
    try:
        version = client.send("version", timeout=args.host_timeout)
        manifest.event("bletester_version", version=version)
    except ControllerError as e:
        manifest.event("bletester_version_unavailable", detail=str(e))
    try:
        health = client.send("diag_health", timeout=args.host_timeout)
        manifest.event("bletester_health_before", health=health)
    except ControllerError as e:
        manifest.event("bletester_health_unavailable", detail=str(e))

    client.send("reset_controller", timeout=5)

    connect_fields = {"timeout_ms": 8000}
    if args.device_addr:
        connect_fields["addr"] = args.device_addr
    else:
        connect_fields["name"] = args.device_name
    try:
        client.send("connect", timeout=12, **connect_fields)
        manifest.data["ble_connections"] += 1
        client.send("discover_services", timeout=args.host_timeout)
        client.send("discover_characteristics", timeout=args.host_timeout)
        client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
        client.drain_events()
    except ControllerError as e:
        raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, f"connect/discover/subscribe failed: {e}")

    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn, code=auth_code.reveal())
    manifest.data["ble_authentications"] += 1
    manifest._flush()
    if not status["authorized"]:
        raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, "device rejected authentication")
    return base_txn


def preflight_status_check(client, manifest):
    """Step 10-11: READY + sd_ready. GET_STATUS exposes no separate
    'recording active'/'file transfer active' booleans -- session_state
    itself already distinguishes READY from the recording state
    (HLV_SESSION_RECORDING), and a concurrent file-transfer/generation/
    delete operation is authoritatively caught by the device's own
    request-time rejection (DOWNLOAD_ACTIVE/DELETE_ACTIVE/BUSY), handled
    as a clean FAILED classification below rather than assumed away
    here."""
    status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
    status = decode_status_bytes(status_raw["data_hex"])
    manifest.event("preflight_status", status=status)
    if status["session_state"] != 1:
        raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, f"session_state={status['session_state']} is not READY(1)")
    if not status["sd_ready"]:
        raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, "sd_ready=false")
    return status


def run(args, manifest):
    expected_bytes = compute_expected_tliv_bytes(args.records)
    manifest.update(expected_bytes=expected_bytes)

    # Resolved BEFORE any port is opened: a non-interactive environment
    # with no credential available fails immediately, with no port-open
    # (and no consequent device-reset risk) ever attempted.
    auth_code, auth_code_source = resolve_auth_code(args)
    manifest.update(auth_code_source=auth_code_source)

    # ---- open both required ports, exactly once each ----
    try:
        import serial
        real_console = serial.Serial(args.console_port, args.baud, timeout=0.5)
    except Exception as e:
        raise CliError(EXIT_SERIAL_OWNERSHIP_FAILURE, f"could not open console port {args.console_port!r}: {e}")
    console_log_path = os.path.join(manifest.output_dir, "console_raw.log")
    console_port = _TeeConsolePort(real_console, console_log_path)
    manifest.data["serial_ports_opened"] += 1
    manifest._flush()
    console_port_closed = [False]

    def close_console_once():
        if not console_port_closed[0]:
            console_port_closed[0] = True
            try:
                console_port.close()
            finally:
                manifest.data["serial_ports_closed"] += 1
                manifest._flush()

    try:
        tester_raw_path = os.path.join(manifest.output_dir, "tester_raw.jsonl")
        client = HolterBLEClient(args.tester_port, baud=args.baud, default_timeout=args.host_timeout,
                                  raw_capture_path=tester_raw_path)
    except Exception as e:
        close_console_once()
        raise CliError(EXIT_SERIAL_OWNERSHIP_FAILURE, f"could not open tester port {args.tester_port!r}: {e}")
    manifest.data["serial_ports_opened"] += 1
    manifest._flush()

    try:
        try:
            base_txn = connect_and_prepare(client, args, manifest, auth_code)
            preflight_status_check(client, manifest)

            pre_files, base_txn = list_all_files(client, base_txn)
            with open(os.path.join(manifest.output_dir, "pre_generation_inventory.json"), "w") as f:
                json.dump({"entries": pre_files}, f, indent=2)
            pre_indices = {e["file_index"] for e in pre_files}

            # ---- exactly one DIAG_GENERATE_TEST_FILE command ----
            gen_txn = base_txn + 1000
            payload = encode_diag_generate_test_file_request_v2(args.records, args.frames_per_record)
            msg = encode_hlv_message(HLV_OPCODE_DIAG_GENERATE_TEST_FILE, gen_txn, payload)
            client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
            manifest.data["generation_commands_sent"] += 1
            manifest._flush()
            manifest.event("generation_command_sent", records=args.records,
                            frames_per_record=args.frames_per_record, expected_bytes=expected_bytes)

            accepted = wait_for_command_response(client, gen_txn, window_s=args.acceptance_timeout)
            if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
                raise CliError(
                    EXIT_FIRMWARE_TERMINAL_FAILURE,
                    f"DIAG_GENERATE_TEST_FILE was not ACCEPTED ({accepted!r}). If this is a production "
                    f"build, this opcode does not exist there by design -- diagnostic builds only.",
                )
        except ControllerError as e:
            raise CliError(EXIT_BLE_PREFLIGHT_FAILURE, str(e))

        sm = GenerationMonitorStateMachine(
            requested_records=args.records,
            expected_bytes=expected_bytes,
            now=time.time(),
            acceptance_timeout_s=args.acceptance_timeout,
            write_stall_timeout_s=args.write_stall_timeout,
            post_write_timeout_s=args.post_write_timeout,  # None -> computed default inside the state machine
            max_total_duration_s=args.overall_timeout,
        )
        sm.on_accepted(time.time())
        manifest.event("phase_transition", phase=sm.phase.value, reason=sm.reason)

        last_phase = [sm.phase]
        last_print = [0.0]

        def on_tick(evidence):
            now = time.time()
            if evidence.phase != last_phase[0]:
                manifest.event("phase_transition", phase=evidence.phase.value, reason=evidence.reason,
                                records_written=evidence.last_records_written, bytes_written=evidence.last_bytes_written)
                last_phase[0] = evidence.phase
                print(f"[{time.strftime('%H:%M:%S')}] phase -> {evidence.phase.value}: {evidence.reason}", flush=True)
                if evidence.phase == GenerationPhase.POST_WRITE_VALIDATION:
                    print(f"[{time.strftime('%H:%M:%S')}] writing complete -- HolterLEV is now reopening, "
                          f"validating, and hashing the file. Unchanged write counters from here on are "
                          f"EXPECTED, not a stall.", flush=True)
                last_print[0] = now
                return

            interval = 30.0 if evidence.phase == GenerationPhase.POST_WRITE_VALIDATION else 7.0
            if now - last_print[0] >= interval:
                last_print[0] = now
                print(
                    f"[{time.strftime('%H:%M:%S')}] {evidence.phase.value}: "
                    f"records {evidence.last_records_written}/{evidence.requested_records}, "
                    f"bytes {evidence.last_bytes_written}/{evidence.expected_bytes}, "
                    f"elapsed={evidence.total_elapsed_s:.0f}s, "
                    f"since_progress={evidence.seconds_since_last_progress:.0f}s",
                    flush=True,
                )

        runner = GenerationRunner(console_port_opener=lambda: console_port, poll_interval_s=1.0)
        evidence = runner.run(
            sm, client, gen_txn,
            wait_for_command_response=wait_for_command_response,
            decode_response=decode_diag_generate_test_file_response,
            on_tick=on_tick,
        )
        # GenerationRunner's own finally block has already physically
        # closed console_port by this point; close_console_once() in the
        # outer finally below records that in the manifest exactly once
        # (its own guard makes the resulting second close() call here a
        # harmless no-op on an already-closed handle).
        with open(os.path.join(manifest.output_dir, "result.json"), "w") as f:
            json.dump({
                "phase": evidence.phase.value,
                "reason": evidence.reason,
                "requested_records": evidence.requested_records,
                "expected_bytes": evidence.expected_bytes,
                "last_records_written": evidence.last_records_written,
                "last_bytes_written": evidence.last_bytes_written,
                "sha256_hex": evidence.sha256_hex,
                "terminal_error_code": evidence.terminal_error_code,
                "total_elapsed_s": evidence.total_elapsed_s,
                "serial_ports_opened": evidence.serial_ports_opened,
                "serial_ports_closed": evidence.serial_ports_closed,
            }, f, indent=2)

        if evidence.phase != GenerationPhase.COMPLETED:
            if evidence.phase == GenerationPhase.TIMED_OUT:
                raise CliError(EXIT_GENERATION_TIMEOUT, evidence.reason)
            raise CliError(EXIT_FIRMWARE_TERMINAL_FAILURE, evidence.reason)

        # ---- post-generation inventory (step 18) ----
        post_files, base_txn = list_all_files(client, gen_txn + 100)
        with open(os.path.join(manifest.output_dir, "post_generation_inventory.json"), "w") as f:
            json.dump({"entries": post_files}, f, indent=2)

        new_entries = [e for e in post_files if e["file_index"] not in pre_indices]
        if len(new_entries) != 1 or new_entries[0]["size_bytes"] != expected_bytes:
            raise CliError(
                EXIT_RESULT_VALIDATION_FAILURE,
                f"post-generation inventory does not show exactly one new file of the expected size: "
                f"new_entries={new_entries}, expected_bytes={expected_bytes}",
            )
        new_file_index = new_entries[0]["file_index"]
        info, _ = get_file_info(client, gen_txn + 200, new_file_index)
        if not info["exists"] or info["size_bytes"] != expected_bytes:
            raise CliError(EXIT_RESULT_VALIDATION_FAILURE, f"GET_FILE_INFO disagrees with LIST_FILES: {info}")

        manifest.update(
            new_file_index=new_file_index,
            new_file_size=info["size_bytes"],
            source_sha256=evidence.sha256_hex,
        )

        try:
            client.send("disconnect", timeout=5)
        except ControllerError:
            pass

        print(f"[{time.strftime('%H:%M:%S')}] COMPLETED: file_index={new_file_index} "
              f"size={info['size_bytes']} sha256={evidence.sha256_hex}", flush=True)
        return EXIT_SUCCESS, "COMPLETED", None

    finally:
        try:
            close_console_once()
        finally:
            try:
                client.close()
            finally:
                manifest.data["serial_ports_closed"] += 1
                manifest._flush()


def main(argv=None):
    args = parse_args(argv)
    try:
        validate_args(args)
    except CliError as e:
        print(f"ERROR: {e.message}", file=sys.stderr)
        return e.exit_code

    os.makedirs(args.output_dir)  # already proven not to exist by validate_args -- no race window closed here,
                                   # but this project's own single-operator hardware-in-the-loop usage makes
                                   # that an accepted, documented limitation (see the BLE-D4.0U report)
    manifest = Manifest(args.output_dir, args)

    exit_code = EXIT_UNEXPECTED_TOOL_FAILURE
    final_status = "UNKNOWN"
    error_classification = None
    try:
        exit_code, final_status, error_classification = run(args, manifest)
    except CliError as e:
        exit_code = e.exit_code
        final_status = "FAILED"
        error_classification = e.message
        print(f"ERROR: {e.message}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = EXIT_USER_CANCELLED
        final_status = "CANCELLED"
        error_classification = "user cancellation (Ctrl+C)"
        print("\nCancelled by user -- closing resources, no further commands will be sent.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- deliberately broad: this IS the tool's own top-level safety net
        exit_code = EXIT_UNEXPECTED_TOOL_FAILURE
        final_status = "FAILED"
        error_classification = f"unexpected exception: {e}"
        traceback.print_exc()
    finally:
        manifest.finalize(final_status, exit_code, error_classification)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
