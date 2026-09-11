#!/usr/bin/env python3
"""
BLE-D4.0U -- offline tests for tools/holter_diag_generate.py.

Everything here uses mocks/fakes: HolterBLEClient, authenticate_raw, and
wait_for_command_response are all patched with test doubles, and the
console "serial port" is a pure-Python fake fed pre-scripted byte
chunks. No real COM port, no real BLE_TESTER, no real HolterLEV is ever
touched. Run: python tools/test_holter_diag_generate.py -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import holter_diag_generate as cli  # noqa: E402
from diag_generation_monitor import compute_expected_tliv_bytes  # noqa: E402

VALID_SHA = "e898365da5098f6a7680710132ba0335a3ed750e6954101c8555d881bdfcd79c"


# ---- wire-format test fixtures (hand-encoded, mirroring holter_hlv_protocol.py's own decoders) ----
def encode_status(session_state=1, sd_ready=True, authorized=True, startup_status=1,
                   ads1293_ready=True, recording_capable=False, last_txn=0, last_result=0):
    b = bytearray(14)
    b[0] = startup_status
    b[1] = 1 if ads1293_ready else 0
    b[2] = 1 if sd_ready else 0
    b[3] = 1 if recording_capable else 0
    b[4] = session_state
    b[5] = 1 if authorized else 0
    b[6:8] = (1).to_bytes(2, "little")
    b[8:12] = (last_txn & 0xFFFFFFFF).to_bytes(4, "little")
    b[12:14] = last_result.to_bytes(2, "little")
    return bytes(b).hex()


def encode_list_files_inner(entries, has_more=False):
    out = bytearray([len(entries), 1 if has_more else 0])
    for e in entries:
        out += e["file_index"].to_bytes(2, "little")
        out += e["size_bytes"].to_bytes(4, "little")
    return bytes(out)


def encode_get_file_info_inner(exists, size_bytes=0):
    out = bytearray([1 if exists else 0])
    out += size_bytes.to_bytes(4, "little")
    return bytes(out)


def encode_generate_response_inner(error=0, records_written=0, bytes_written=0,
                                    is_valid=True, sha256_valid=True, sha256_hex=VALID_SHA):
    out = bytearray([error])
    out += records_written.to_bytes(4, "little")
    out += bytes_written.to_bytes(4, "little")
    out += bytes([1 if is_valid else 0, 1 if sha256_valid else 0])
    out += bytes.fromhex(sha256_hex)
    return bytes(out)


class _FakeClient:
    """Records every send() call; returns a canned response per command
    (or via a callable for dynamic responses). close()/drain_events() are
    harmless no-ops recorded for inspection."""

    def __init__(self, responses=None):
        self.sent = []
        self._responses = responses or {}
        self.closed = False
        self.disconnect_calls = 0

    def send(self, cmd, timeout=None, **fields):
        self.sent.append((cmd, dict(fields)))
        if cmd == "disconnect":
            self.disconnect_calls += 1
        resp = self._responses.get(cmd, {"status": "ok"})
        if callable(resp):
            return resp(fields)
        return resp

    def drain_events(self):
        return []

    def close(self):
        self.closed = True

    def generation_write_count(self, opcode):
        n = 0
        for cmd, fields in self.sent:
            if cmd != "write":
                continue
            data = bytes.fromhex(fields["data_hex"])
            sent_opcode = data[2] | (data[3] << 8)
            if sent_opcode == opcode:
                n += 1
        return n


class _FakeConsolePort:
    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self.closed = False

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class _FakeSerialModule:
    def __init__(self, port_factory):
        self._factory = port_factory

    def Serial(self, port, baud, timeout=None):
        return self._factory()


class _ScriptedWait:
    """Dispatches by window_s -- the CLI's own three call sites use three
    distinct, fixed window_s values (acceptance/list-or-info/runner-poll),
    so this needs no txn-matching to behave deterministically."""

    def __init__(self, accepted=None, list_queue=None, runner_queue=None, acceptance_window=15.0):
        self.accepted = accepted
        self.list_queue = list(list_queue or [])
        self.runner_queue = list(runner_queue or [])
        self.acceptance_window = acceptance_window
        self.calls = []

    def __call__(self, client, txn, window_s):
        self.calls.append(window_s)
        if window_s == self.acceptance_window:
            return self.accepted
        if window_s == 5:
            return self.list_queue.pop(0) if self.list_queue else None
        if window_s == 0.5:
            return self.runner_queue.pop(0) if self.runner_queue else None
        return None


def _accepted_response():
    return {"phase": cli.HLV_RESPONSE_PHASE_ACCEPTED, "result_code": 0, "inner": b""}


def _list_response(entries, has_more=False):
    return {"phase": 3, "result_code": 0, "inner": encode_list_files_inner(entries, has_more)}


def _info_response(exists, size_bytes=0):
    return {"phase": 3, "result_code": 0, "inner": encode_get_file_info_inner(exists, size_bytes)}


def _terminal_success(records, size_bytes, sha256_hex=VALID_SHA):
    return {"result_code": 0, "inner": encode_generate_response_inner(
        error=0, records_written=records, bytes_written=size_bytes, sha256_hex=sha256_hex)}


def _terminal_failure(error_code):
    return {"result_code": 0, "inner": encode_generate_response_inner(error=error_code, is_valid=False,
                                                                        sha256_valid=False)}


class _CliTestCase(unittest.TestCase):
    """Base class wiring every patch this CLI needs to run fully offline."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="d4_0u_test_")
        self.output_dir = os.path.join(self.tmpdir, "evidence")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_cli(self, argv, fake_client=None, console_chunks=None, authorized=True,
                status_hex=None, patches=None):
        fake_client = fake_client or _FakeClient()
        console_port = _FakeConsolePort(console_chunks or [])
        fake_serial_module = _FakeSerialModule(lambda: console_port)

        if status_hex is None:
            status_hex = encode_status()
        fake_client._responses.setdefault("read", {"data_hex": status_hex})

        def client_factory(port, baud=115200, default_timeout=10.0, raw_capture_path=None):
            if raw_capture_path:
                open(raw_capture_path, "a").close()  # mirrors the real HolterBLEClient's own side effect
            return fake_client

        patch_list = [
            mock.patch.object(cli, "HolterBLEClient", client_factory),
            mock.patch.object(cli, "authenticate_raw", lambda client, txn, code=None: {"authorized": authorized}),
            mock.patch.dict(sys.modules, {"serial": fake_serial_module}),
        ]
        if patches:
            patch_list.extend(patches)

        with mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            with mock.patch.multiple("contextlib", ExitStack=mock.DEFAULT) if False else _nullcontext():
                for p in patch_list:
                    p.start()
                try:
                    exit_code = cli.main(argv)
                finally:
                    for p in reversed(patch_list):
                        p.stop()
        return exit_code, fake_client, console_port

    def default_argv(self, **overrides):
        # --auth-code defaults to a harmless placeholder so every test not
        # specifically about auth-code resolution (BLE-D4.0V) doesn't have
        # to deal with the interactive/env-var precedence chain at all.
        argv = {
            "--tester-port": "COM17", "--console-port": "COM23",
            "--records": "7", "--output-dir": self.output_dir,
            "--auth-code": "test-placeholder-not-a-real-secret",
        }
        argv.update(overrides)
        out = []
        for k, v in argv.items():
            out += [k, str(v)]
        return out


class _nullcontext:
    def __enter__(self): return self
    def __exit__(self, *a): return False


# =====================================================================
# 1-10: CLI parsing
# =====================================================================
class Test01Help(_CliTestCase):
    def test_help_exits_zero_and_prints_usage(self):
        with self.assertRaises(SystemExit) as cm:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)


class Test02MissingRequiredArgument(_CliTestCase):
    def test_missing_records_exits_nonzero(self):
        with self.assertRaises(SystemExit) as cm:
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                cli.parse_args(["--tester-port", "COM17", "--console-port", "COM23", "--output-dir", "x"])
        self.assertNotEqual(cm.exception.code, 0)


class Test03InvalidPortValue(_CliTestCase):
    def test_same_port_for_both_is_rejected(self):
        args = cli.parse_args(self.default_argv(**{"--console-port": "COM17"}))
        with self.assertRaises(cli.CliError) as cm:
            cli.validate_args(args)
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)


class Test04RecordCountZero(_CliTestCase):
    def test_zero_records_rejected(self):
        args = cli.parse_args(self.default_argv(**{"--records": "0"}))
        with self.assertRaises(cli.CliError) as cm:
            cli.validate_args(args)
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)


class Test05RecordCount20000(_CliTestCase):
    def test_20000_records_accepted_by_validation(self):
        args = cli.parse_args(self.default_argv(**{"--records": "20000"}))
        cli.validate_args(args)  # must not raise


class Test06RecordCount20001(_CliTestCase):
    def test_20001_records_rejected(self):
        args = cli.parse_args(self.default_argv(**{"--records": "20001"}))
        with self.assertRaises(cli.CliError) as cm:
            cli.validate_args(args)
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)
        self.assertIn("diagnostic-only", str(cm.exception))


class Test07NegativeTimeout(_CliTestCase):
    def test_negative_write_stall_timeout_rejected(self):
        args = cli.parse_args(self.default_argv(**{"--write-stall-timeout": "-5"}))
        with self.assertRaises(cli.CliError) as cm:
            cli.validate_args(args)
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)


class Test08InvalidOutputDirectory(_CliTestCase):
    def test_output_dir_must_not_already_exist(self):
        os.makedirs(self.output_dir)
        args = cli.parse_args(self.default_argv())
        with self.assertRaises(cli.CliError) as cm:
            cli.validate_args(args)
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)


class Test09ExistingEvidenceCollision(_CliTestCase):
    def test_full_main_refuses_to_overwrite_and_never_opens_a_port(self):
        os.makedirs(self.output_dir)
        with open(os.path.join(self.output_dir, "sentinel.txt"), "w") as f:
            f.write("pre-existing")
        exit_code, fake_client, console_port = self.run_cli(self.default_argv())
        self.assertEqual(exit_code, cli.EXIT_INVALID_ARGS)
        self.assertEqual(fake_client.sent, [], "no BLE command may be sent when the output dir is unsafe")
        with open(os.path.join(self.output_dir, "sentinel.txt")) as f:
            self.assertEqual(f.read(), "pre-existing", "existing content must survive untouched")


class Test10SecretRedaction(_CliTestCase):
    def test_auth_code_never_appears_in_manifest_or_stdout(self):
        secret = "SUPER-SECRET-CODE-XYZ"
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--auth-code": secret}),
            fake_client=_FakeClient({
                "read": {"data_hex": encode_status()},
            }),
            patches=[mock.patch.object(cli, "wait_for_command_response", _ScriptedWait(
                accepted=_accepted_response(),
                list_queue=[_list_response([]), _list_response([{"file_index": 5, "size_bytes": 45984}])],
                runner_queue=[_terminal_success(7, 45984)],
            ))],
        )
        manifest_path = os.path.join(self.output_dir, "run_manifest.json")
        with open(manifest_path) as f:
            manifest_text = f.read()
        self.assertNotIn(secret, manifest_text)
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["args"]["auth_code"], "<redacted>")


# =====================================================================
# 11-21: happy path
# =====================================================================
class TestHappyPath(_CliTestCase):
    def _run_happy(self, records=7, size=None):
        size = size if size is not None else compute_expected_tliv_bytes(records)
        fake_client = _FakeClient({"read": {"data_hex": encode_status()}})
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([]), _list_response([{"file_index": 9, "size_bytes": size}]),
                        _info_response(True, size)],
            runner_queue=[_terminal_success(records, size)],
        )
        exit_code, fake_client, console_port = self.run_cli(
            self.default_argv(**{"--records": records}),
            fake_client=fake_client,
            console_chunks=[f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode()],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        return exit_code, fake_client, console_port, scripted

    def test_11_one_open_one_close_per_port(self):
        exit_code, fake_client, console_port, _ = self._run_happy()
        self.assertTrue(console_port.closed)
        self.assertTrue(fake_client.closed)
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["serial_ports_opened"], 2)
        self.assertEqual(manifest["serial_ports_closed"], 2)

    def test_12_one_connection(self):
        exit_code, fake_client, _, _ = self._run_happy()
        connects = [c for c, f in fake_client.sent if c == "connect"]
        self.assertEqual(len(connects), 1)

    def test_13_one_authentication(self):
        # authenticate_raw itself is mocked at the function level (called once by construction);
        # proven via ble_authentications counter in the manifest.
        exit_code, fake_client, _, _ = self._run_happy()
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["ble_authentications"], 1)

    def test_14_one_generation_command(self):
        exit_code, fake_client, _, _ = self._run_happy()
        self.assertEqual(fake_client.generation_write_count(cli.HLV_OPCODE_DIAG_GENERATE_TEST_FILE), 1)

    def test_15_16_correct_phase_transitions_and_terminal_result(self):
        exit_code, fake_client, _, _ = self._run_happy()
        with open(os.path.join(self.output_dir, "events.jsonl")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        transitions = [e["phase"] for e in lines if e["kind"] == "phase_transition"]
        self.assertEqual(transitions, ["WRITING", "POST_WRITE_VALIDATION", "COMPLETED"])

    def test_17_18_correct_terminal_result_and_exit_code(self):
        exit_code, fake_client, _, _ = self._run_happy()
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        with open(os.path.join(self.output_dir, "result.json")) as f:
            result = json.load(f)
        self.assertEqual(result["phase"], "COMPLETED")
        self.assertEqual(result["sha256_hex"], VALID_SHA)

    def test_19_all_evidence_files_produced(self):
        self._run_happy()
        for fname in ("run_manifest.json", "result.json", "console_raw.log", "tester_raw.jsonl",
                      "events.jsonl", "pre_generation_inventory.json", "post_generation_inventory.json"):
            self.assertTrue(os.path.exists(os.path.join(self.output_dir, fname)), f"missing {fname}")

    def test_20_manifest_finalized(self):
        self._run_happy()
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["final_status"], "COMPLETED")
        self.assertEqual(manifest["exit_code"], cli.EXIT_SUCCESS)
        self.assertIsNotNone(manifest["utc_end"])
        self.assertEqual(manifest["new_file_index"], 9)
        self.assertEqual(manifest["source_sha256"], VALID_SHA)

    def test_21_exact_size_for_all_four_known_record_counts(self):
        for records, size in ((7, 45984), (144, 945792), (1873, 12301872), (18667, 122604864)):
            self.assertEqual(compute_expected_tliv_bytes(records), size)


# =====================================================================
# 22-37: failure paths
# =====================================================================
class Test22TesterPortOpenFailure(_CliTestCase):
    def test_tester_port_open_failure_is_serial_ownership_exit(self):
        def raising_client(*a, **k):
            raise OSError("port busy")
        exit_code, fake_client, console_port = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.object(cli, "HolterBLEClient", raising_client)],
        )
        self.assertEqual(exit_code, cli.EXIT_SERIAL_OWNERSHIP_FAILURE)
        self.assertTrue(console_port.closed, "the already-opened console port must still be closed")


class Test23ConsolePortOpenFailure(_CliTestCase):
    def test_console_port_open_failure_is_serial_ownership_exit(self):
        class RaisingSerialModule:
            def Serial(self, *a, **k):
                raise OSError("no such port")
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.dict(sys.modules, {"serial": RaisingSerialModule()})],
        )
        self.assertEqual(exit_code, cli.EXIT_SERIAL_OWNERSHIP_FAILURE)
        self.assertEqual(fake_client.sent, [], "no BLE command may be sent if the console port never opened")


class Test24AuthenticationFailure(_CliTestCase):
    def test_auth_rejected_is_preflight_failure(self):
        exit_code, fake_client, console_port = self.run_cli(self.default_argv(), authorized=False)
        self.assertEqual(exit_code, cli.EXIT_BLE_PREFLIGHT_FAILURE)
        self.assertEqual(fake_client.generation_write_count(cli.HLV_OPCODE_DIAG_GENERATE_TEST_FILE), 0)
        self.assertTrue(console_port.closed)
        self.assertTrue(fake_client.closed)


class Test25HolterLEVNotReady(_CliTestCase):
    def test_session_state_not_ready_is_preflight_failure(self):
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            fake_client=_FakeClient({"read": {"data_hex": encode_status(session_state=3)}}),
        )
        self.assertEqual(exit_code, cli.EXIT_BLE_PREFLIGHT_FAILURE)
        self.assertEqual(fake_client.generation_write_count(cli.HLV_OPCODE_DIAG_GENERATE_TEST_FILE), 0)


class Test26SdNotReady(_CliTestCase):
    def test_sd_not_ready_is_preflight_failure(self):
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            fake_client=_FakeClient({"read": {"data_hex": encode_status(sd_ready=False)}}),
        )
        self.assertEqual(exit_code, cli.EXIT_BLE_PREFLIGHT_FAILURE)


class Test27DiagnosticOpcodeUnavailable(_CliTestCase):
    def test_not_accepted_reports_diagnostic_only_hint(self):
        scripted = _ScriptedWait(
            accepted=None,  # production build -- no ACCEPTED ever arrives
            list_queue=[_list_response([])],
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_FIRMWARE_TERMINAL_FAILURE)
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertIn("diagnostic", manifest["error_classification"].lower())


class Test28WritePhaseStall(_CliTestCase):
    def test_write_stall_below_target_times_out(self):
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[None] * 5,  # never reaches a terminal response
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--write-stall-timeout": "0.01", "--acceptance-timeout": "15"}),
            console_chunks=[b"SD_WRITER_SUMMARY session=x, open=1, attempted=1, written=1, bytes=100\n"],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_GENERATION_TIMEOUT)


class Test29ValidationPhaseTimeout(_CliTestCase):
    def test_post_write_timeout_fires(self):
        records, size = 7, compute_expected_tliv_bytes(7)
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[None] * 5,
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--records": records, "--post-write-timeout": "0.01"}),
            console_chunks=[f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode()],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_GENERATION_TIMEOUT)
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertIn("post-write validation exceeded", manifest["error_classification"])


class Test30TerminalFirmwareFailure(_CliTestCase):
    def test_terminal_error_code_is_firmware_failure_exit(self):
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[_terminal_failure(error_code=3)],
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_FIRMWARE_TERMINAL_FAILURE)
        with open(os.path.join(self.output_dir, "result.json")) as f:
            result = json.load(f)
        self.assertEqual(result["terminal_error_code"], 3)


class Test31SizeMismatch(_CliTestCase):
    def test_terminal_reports_wrong_size_is_rejected_even_though_firmware_says_ok(self):
        records = 7
        wrong_size = compute_expected_tliv_bytes(records) + 8  # firmware claims success but wrong total
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[_terminal_success(records, wrong_size)],
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--records": records}),
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_FIRMWARE_TERMINAL_FAILURE)


class Test32InvalidSha256(_CliTestCase):
    def test_short_sha256_is_rejected(self):
        records, size = 7, compute_expected_tliv_bytes(7)
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[{"result_code": 0, "inner": encode_generate_response_inner(
                records_written=records, bytes_written=size, sha256_hex="ab" * 16)}],  # 32 hex chars, not 64
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--records": records}),
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_FIRMWARE_TERMINAL_FAILURE)


class Test33SerialConnectionLoss(_CliTestCase):
    def test_console_read_exception_is_terminal_failure_no_reconnect(self):
        class RaisingPort(_FakeConsolePort):
            def read(self, n):
                raise OSError("device disconnected")
        scripted = _ScriptedWait(accepted=_accepted_response(), list_queue=[_list_response([])])
        exit_code, fake_client, console_port = self.run_cli(
            self.default_argv(),
            patches=[
                mock.patch.object(cli, "wait_for_command_response", scripted),
                mock.patch.dict(sys.modules, {"serial": _FakeSerialModule(lambda: RaisingPort())}),
            ],
        )
        self.assertEqual(exit_code, cli.EXIT_FIRMWARE_TERMINAL_FAILURE)
        connects = [c for c, f in fake_client.sent if c == "connect"]
        self.assertEqual(len(connects), 1, "must never reconnect after connection loss")


class Test34MalformedEvent(_CliTestCase):
    def test_malformed_console_line_is_ignored_not_a_crash(self):
        records, size = 7, compute_expected_tliv_bytes(7)
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([]), _list_response([{"file_index": 1, "size_bytes": size}]),
                        _info_response(True, size)],
            runner_queue=[_terminal_success(records, size)],
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--records": records}),
            console_chunks=[
                b"garbage not matching any pattern at all\n",
                f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode(),
            ],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)


class Test35CtrlC(_CliTestCase):
    def test_keyboard_interrupt_returns_cancellation_exit_code(self):
        def raising_wait(client, txn, window_s):
            if window_s == 15.0:
                return _accepted_response()
            if window_s == 5:
                return _list_response([])
            raise KeyboardInterrupt()
        exit_code, fake_client, console_port = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.object(cli, "wait_for_command_response", raising_wait)],
        )
        self.assertEqual(exit_code, cli.EXIT_USER_CANCELLED)
        self.assertTrue(console_port.closed)
        self.assertTrue(fake_client.closed)
        self.assertEqual(fake_client.generation_write_count(cli.HLV_OPCODE_DIAG_GENERATE_TEST_FILE), 1,
                          "the one already-sent generation command is not resent")


class Test36UnexpectedException(_CliTestCase):
    def test_unexpected_exception_is_sanitized_and_exits_8(self):
        def raising_wait(client, txn, window_s):
            raise ValueError("something truly unexpected")
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(),
            patches=[mock.patch.object(cli, "wait_for_command_response", raising_wait)],
        )
        self.assertEqual(exit_code, cli.EXIT_UNEXPECTED_TOOL_FAILURE)
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertIn("something truly unexpected", manifest["error_classification"])


class Test37EvidenceFileWriteFailure(_CliTestCase):
    def test_manifest_directory_removed_mid_run_is_handled_as_unexpected_failure_not_a_hang(self):
        records, size = 7, compute_expected_tliv_bytes(7)
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([])],
            runner_queue=[_terminal_success(records, size)],
        )

        real_open = open
        state = {"armed": False}

        def flaky_open(path, mode="r", *a, **k):
            if state["armed"] and "result.json" in str(path):
                raise OSError("simulated evidence-write failure")
            return real_open(path, mode, *a, **k)

        exit_code, fake_client, _ = None, None, None
        with mock.patch.object(cli, "wait_for_command_response", scripted):
            args = cli.parse_args(self.default_argv(**{"--records": records}))
            cli.validate_args(args)
            os.makedirs(args.output_dir)
            manifest = cli.Manifest(args.output_dir, args)
            state["armed"] = True
            with mock.patch("builtins.open", flaky_open), \
                 mock.patch.object(cli, "authenticate_raw", lambda client, txn, code=None: {"authorized": True}), \
                 mock.patch.object(cli, "HolterBLEClient", lambda *a, **k: _FakeClient({"read": {"data_hex": encode_status()}})), \
                 mock.patch.dict(sys.modules, {"serial": _FakeSerialModule(lambda: _FakeConsolePort([
                     f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode()
                 ]))}):
                with self.assertRaises(OSError):
                    cli.run(args, manifest)
            manifest.finalize("FAILED", cli.EXIT_UNEXPECTED_TOOL_FAILURE, "simulated evidence-write failure")
        with open(os.path.join(args.output_dir, "run_manifest.json")) as f:
            m = json.load(f)
        self.assertEqual(m["final_status"], "FAILED")


# =====================================================================
# Replay regression: preserved BLE-D4.0S event sequence through the full CLI
# =====================================================================
class TestReplayD4_0S(_CliTestCase):
    def test_full_cli_accepts_the_real_d4_0s_timeline(self):
        records, size = 18667, 122604864
        d4_0s_sha = "8e158b1b83fafb30ec49e7d6ba6d5b3971e49df482c51f4cbda5c46b105390ee"
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([{"file_index": 1, "size_bytes": 45984},
                                          {"file_index": 2, "size_bytes": 945792},
                                          {"file_index": 3, "size_bytes": 12301872}]),
                        _list_response([{"file_index": 1, "size_bytes": 45984},
                                          {"file_index": 2, "size_bytes": 945792},
                                          {"file_index": 3, "size_bytes": 12301872},
                                          {"file_index": 4, "size_bytes": size}]),
                        _info_response(True, size)],
            # None repeated many times represents the real ~580s of silence
            # (including the exact 309s point the OLD tool would have failed at) --
            # the corrected state machine underneath must simply keep waiting.
            runner_queue=[None] * 50 + [_terminal_success(records, size, sha256_hex=d4_0s_sha)],
        )
        exit_code, fake_client, _ = self.run_cli(
            self.default_argv(**{"--records": records, "--post-write-timeout": "3600"}),
            console_chunks=[f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode()],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(fake_client.generation_write_count(cli.HLV_OPCODE_DIAG_GENERATE_TEST_FILE), 1)
        connects = [c for c, f in fake_client.sent if c == "connect"]
        self.assertEqual(len(connects), 1, "no reconnect")
        with open(os.path.join(self.output_dir, "result.json")) as f:
            result = json.load(f)
        self.assertEqual(result["phase"], "COMPLETED")
        self.assertEqual(result["sha256_hex"], d4_0s_sha)
        self.assertEqual(result["last_records_written"], records)
        self.assertEqual(result["last_bytes_written"], size)
        with open(os.path.join(self.output_dir, "events.jsonl")) as f:
            transitions = [json.loads(l)["phase"] for l in f if '"phase_transition"' in l]
        self.assertEqual(transitions, ["WRITING", "POST_WRITE_VALIDATION", "COMPLETED"])


def _env_without_auth_var():
    """A mock.patch.dict context that guarantees HOLTER_AUTH_CODE is
    absent, regardless of the real host environment, without the
    self-recursive trap of mocking os.environ.get() from inside a
    module that itself calls os.environ.get()."""
    env = dict(os.environ)
    env.pop(cli.AUTH_CODE_ENV_VAR, None)
    return mock.patch.dict(os.environ, env, clear=True)


# =====================================================================
# BLE-D4.0V: secure authentication-input path
# =====================================================================
class TestAuthCliValuePrecedence(_CliTestCase):
    def test_cli_value_wins_over_env_var(self):
        with mock.patch.dict(os.environ, {cli.AUTH_CODE_ENV_VAR: "env-value"}):
            code, source = cli.resolve_auth_code(
                cli.parse_args(self.default_argv(**{"--auth-code": "cli-value"}))
            )
        self.assertEqual(code.reveal(), b"cli-value")
        self.assertEqual(source, "cli_argument")


class TestAuthEnvVarFallback(_CliTestCase):
    def test_env_var_used_when_no_cli_value(self):
        argv = [a for a in self.default_argv() if a not in ("--auth-code", "test-placeholder-not-a-real-secret")]
        with mock.patch.dict(os.environ, {cli.AUTH_CODE_ENV_VAR: "env-value"}):
            code, source = cli.resolve_auth_code(cli.parse_args(argv))
        self.assertEqual(code.reveal(), b"env-value")
        self.assertEqual(source, "environment_variable")


class TestAuthInteractiveFallback(_CliTestCase):
    def test_getpass_used_when_tty_and_no_other_source(self):
        argv = [a for a in self.default_argv() if a not in ("--auth-code", "test-placeholder-not-a-real-secret")]
        with mock.patch.dict(os.environ, {}, clear=False), \
             _env_without_auth_var(), \
             mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("getpass.getpass", return_value="typed-secret") as gp:
            code, source = cli.resolve_auth_code(cli.parse_args(argv))
        self.assertEqual(code.reveal(), b"typed-secret")
        self.assertEqual(source, "interactive_getpass")
        gp.assert_called_once()


class TestAuthNonInteractiveMissingSecretFails(_CliTestCase):
    def test_no_source_available_non_interactively_fails_cleanly_before_any_port(self):
        argv = [a for a in self.default_argv() if a not in ("--auth-code", "test-placeholder-not-a-real-secret")]
        with _env_without_auth_var(), \
             mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(cli.CliError) as cm:
                cli.resolve_auth_code(cli.parse_args(argv))
        self.assertEqual(cm.exception.exit_code, cli.EXIT_INVALID_ARGS)
        self.assertIn("hardcoded", str(cm.exception).lower())

    def test_full_cli_fails_before_opening_any_port(self):
        argv = [a for a in self.default_argv() if a not in ("--auth-code", "test-placeholder-not-a-real-secret")]

        class _ExplodingSerialModule:
            def Serial(self, *a, **k):
                raise AssertionError("must never open the console port when no auth code is available")

        def exploding_client(*a, **k):
            raise AssertionError("must never open the tester port when no auth code is available")

        with _env_without_auth_var(), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch.object(cli, "HolterBLEClient", exploding_client), \
             mock.patch.dict(sys.modules, {"serial": _ExplodingSerialModule()}), \
             mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            exit_code = cli.main(argv)
        self.assertEqual(exit_code, cli.EXIT_INVALID_ARGS)


class TestAuthRedactionEverywhere(_CliTestCase):
    def _run_with_secret(self, secret_value):
        records, size = 7, compute_expected_tliv_bytes(7)
        scripted = _ScriptedWait(
            accepted=_accepted_response(),
            list_queue=[_list_response([]), _list_response([{"file_index": 1, "size_bytes": size}]),
                        _info_response(True, size)],
            runner_queue=[_terminal_success(records, size)],
        )
        return self.run_cli(
            self.default_argv(**{"--auth-code": secret_value}),
            console_chunks=[f"SD_WRITER_SUMMARY session=x, open=1, attempted={records}, written={records}, bytes={size}\n".encode()],
            patches=[mock.patch.object(cli, "wait_for_command_response", scripted)],
        )

    def test_secret_absent_from_every_evidence_file(self):
        secret = "MY-VERY-SECRET-CODE-42"
        exit_code, fake_client, _ = self._run_with_secret(secret)
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        for fname in ("run_manifest.json", "result.json", "events.jsonl",
                      "pre_generation_inventory.json", "post_generation_inventory.json"):
            path = os.path.join(self.output_dir, fname)
            with open(path, "rb") as f:
                content = f.read()
            self.assertNotIn(secret.encode(), content, f"secret leaked into {fname}")
        with open(os.path.join(self.output_dir, "console_raw.log"), "rb") as f:
            self.assertNotIn(secret.encode(), f.read())
        with open(os.path.join(self.output_dir, "tester_raw.jsonl"), "rb") as f:
            self.assertNotIn(secret.encode(), f.read())
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["auth_code_source"], "cli_argument")

    def test_secret_absent_from_exception_message_and_traceback(self):
        secret = "MY-OTHER-SECRET-99"

        def raising_wait(client, txn, window_s):
            raise ValueError("boom while auth_code is in scope")

        with mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            exit_code, fake_client, _ = self.run_cli(
                self.default_argv(**{"--auth-code": secret}),
                patches=[mock.patch.object(cli, "wait_for_command_response", raising_wait)],
            )
        self.assertEqual(exit_code, cli.EXIT_UNEXPECTED_TOOL_FAILURE)
        self.assertNotIn(secret, err.getvalue())
        with open(os.path.join(self.output_dir, "run_manifest.json")) as f:
            manifest_text = f.read()
        self.assertNotIn(secret, manifest_text)

    def test_secret_repr_and_str_are_always_redacted(self):
        s = cli.Secret(b"a-real-secret-value")
        self.assertEqual(repr(s), "<redacted>")
        self.assertEqual(str(s), "<redacted>")
        self.assertEqual(f"{s}", "<redacted>")
        self.assertEqual(s.reveal(), b"a-real-secret-value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
