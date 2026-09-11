#!/usr/bin/env python3
"""
BLE-D4.0T -- deterministic, offline tests for diag_generation_monitor.py.

No hardware, no serial port, no BLE_TESTER, no HolterLEV, and no
wall-clock sleep anywhere: every scenario feeds explicit `now` values
(plain floats) directly to the state machine, so a "5 minutes pass" or
"580 seconds of silence" scenario runs in milliseconds of real test time.

Run: python tools/test_diag_generation_monitor.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diag_generation_monitor import (
    GenerationPhase,
    GenerationMonitorStateMachine,
    GenerationMonitorError,
    compute_post_write_validation_timeout_s,
    POST_WRITE_TIMEOUT_MIN_S,
    POST_WRITE_TIMEOUT_MAX_S,
    CONSERVATIVE_VALIDATION_BYTES_PER_SEC,
)

VALID_SHA = "e898365da5098f6a7680710132ba0335a3ed750e6954101c8555d881bdfcd79c"  # 64 hex chars, real D4.0Q value
D4_0S_SHA = "8e158b1b83fafb30ec49e7d6ba6d5b3971e49df482c51f4cbda5c46b105390ee"   # 64 hex chars, real D4.0S value


def make_sm(records, size, now=0.0, **kwargs):
    return GenerationMonitorStateMachine(requested_records=records, expected_bytes=size, now=now, **kwargs)


# ---- 1..3: normal happy-path generations at three real, previously-used scales ----
class Test01SmallFileNormalGeneration(unittest.TestCase):
    def test_45984_byte_file_completes(self):
        sm = make_sm(7, 45984, now=0.0)
        sm.on_accepted(0.05)
        sm.on_progress(0.3, 7, 45984)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.on_terminal_success(0.9, 7, 45984, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)
        ev = sm.evidence(0.9)
        self.assertEqual(ev.last_records_written, 7)
        self.assertEqual(ev.sha256_hex, VALID_SHA)


class Test02MediumFile12_3MB(unittest.TestCase):
    def test_1873_records_completes_after_realistic_validation_window(self):
        sm = make_sm(1873, 12301872, now=0.0)
        sm.on_accepted(0.2)
        t = 0.2
        for i in range(1, 11):
            t += 22.0
            records = min(1873, i * 190)
            bytes_ = min(12301872, records * (12301872 // 1873))
            sm.on_progress(t, records, bytes_ if records < 1873 else 12301872)
        sm.on_progress(t + 1, 1873, 12301872)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.check_timeouts(t + 30)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION, "58s validation must not time out")
        sm.on_terminal_success(t + 58, 1873, 12301872, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)


class Test03LargeFile122_6MB(unittest.TestCase):
    def test_18667_records_completes_after_realistic_validation_window(self):
        sm = make_sm(18667, 122604864, now=0.0)
        sm.on_accepted(0.2)
        sm.on_progress(1770.0, 18667, 122604864)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.check_timeouts(1770.0 + 580.0)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.on_terminal_success(1770.0 + 580.5, 18667, 122604864, True, True, D4_0S_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)


# ---- 4: brief pause then resume ----
class Test04BriefPauseThenResume(unittest.TestCase):
    def test_60s_pause_under_the_5min_stall_timeout_then_resumes(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 400, 40000)
        sm.check_timeouts(10.0 + 60.0)  # well under 300s
        self.assertEqual(sm.phase, GenerationPhase.WRITING)
        sm.on_progress(10.0 + 60.0, 1000, 100000)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.on_terminal_success(10.0 + 65.0, 1000, 100000, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)


# ---- 5: genuine write stall (>5 min, below requested count) ----
class Test05GenuineWriteStall(unittest.TestCase):
    def test_more_than_5_minutes_no_progress_below_target_times_out(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(5.0, 400, 40000)
        sm.check_timeouts(5.0 + 301.0)
        self.assertEqual(sm.phase, GenerationPhase.TIMED_OUT)
        self.assertIn("no write progress", sm.reason)
        ev = sm.evidence(5.0 + 301.0)
        self.assertEqual(ev.last_records_written, 400)
        self.assertEqual(ev.requested_records, 1000)


# ---- 6: the actual regression -- 100% written, 10 minutes quiet, then succeeds ----
class Test06TenMinutesQuietAfterWritingCompletesThenSucceeds(unittest.TestCase):
    def test_10_minutes_of_silence_after_100_percent_write_is_not_a_stall(self):
        sm = make_sm(5000, 500000, now=0.0, post_write_timeout_s=1800)
        sm.on_accepted(0.1)
        sm.on_progress(100.0, 5000, 500000)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.check_timeouts(100.0 + 600.0)  # 10 minutes of silence
        self.assertEqual(
            sm.phase, GenerationPhase.POST_WRITE_VALIDATION,
            "the OLD heuristic would have fired at 300s; the fixed monitor must still be waiting at 600s",
        )
        sm.on_terminal_success(100.0 + 610.0, 5000, 500000, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)


# ---- 7: exact BLE-D4.0S reproduction (see also Test28ReplayD40SEvidence below) ----
class Test07ExactD4_0SReproduction(unittest.TestCase):
    def test_18667_records_580s_validation_then_success(self):
        sm = make_sm(18667, 122604864, now=0.0)
        sm.on_accepted(0.2)
        sm.on_progress(1772.6, 18667, 122604864)  # real write-phase duration observed in BLE-D4.0S
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        # The exact moment (309s) the OLD heuristic incorrectly fired:
        sm.check_timeouts(1772.6 + 309.0)
        self.assertEqual(
            sm.phase, GenerationPhase.POST_WRITE_VALIDATION,
            "this is the exact BLE-D4.0S false-stall point -- the fix must still be validating here",
        )
        sm.on_terminal_success(1772.6 + 580.5, 18667, 122604864, True, True, D4_0S_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)
        self.assertEqual(sm.evidence(1772.6 + 580.5).sha256_hex, D4_0S_SHA)


# ---- 8: post-write validation exceeds ITS OWN timeout ----
class Test08PostWriteValidationTimeout(unittest.TestCase):
    def test_post_write_exceeding_its_dedicated_bound_times_out(self):
        sm = make_sm(1000, 100000, now=0.0, post_write_timeout_s=120)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 100000)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.check_timeouts(10.0 + 121.0)
        self.assertEqual(sm.phase, GenerationPhase.TIMED_OUT)
        self.assertIn("post-write validation exceeded", sm.reason)


# ---- 9/10: terminal validation vs hashing failure ----
class Test09TerminalValidationFailure(unittest.TestCase):
    def test_terminal_reports_is_valid_false(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 100000)
        sm.on_terminal_success(15.0, 1000, 100000, False, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("is_valid=False", sm.reason)


class Test10TerminalHashingFailure(unittest.TestCase):
    def test_terminal_reports_sha256_valid_false(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 100000)
        sm.on_terminal_success(15.0, 1000, 100000, True, False, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("sha256_valid=False", sm.reason)


# ---- 11/12: decreasing counters ----
class Test11RecordCounterDecreases(unittest.TestCase):
    def test_records_written_decrease_fails(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 500, 50000)
        sm.on_progress(11.0, 400, 50000)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("counters decreased", sm.reason)


class Test12ByteCounterDecreases(unittest.TestCase):
    def test_bytes_written_decrease_fails(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 500, 50000)
        sm.on_progress(11.0, 500, 40000)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("counters decreased", sm.reason)


# ---- 13/14: overshoot / size mismatch ----
class Test13RecordCounterExceedsRequest(unittest.TestCase):
    def test_records_written_above_requested_fails(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1001, 100100)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("exceeds requested", sm.reason)


class Test14ByteCountDiffersFromExpectedAtFullRecordCount(unittest.TestCase):
    def test_full_record_count_but_wrong_byte_total_fails(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 99000)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("bytes_written=99000", sm.reason)


# ---- 15: terminal success before writing complete ----
class Test15TerminalSuccessBeforeWritingComplete(unittest.TestCase):
    def test_terminal_success_with_incomplete_totals_fails(self):
        sm = make_sm(18667, 122604864, now=0.0)
        sm.on_accepted(0.1)
        # No progress ever observed (plausible for a very fast/small file
        # whose generation finishes before any periodic telemetry line
        # fires) -- what matters is the TERMINAL response's own reported
        # totals, which here are short of the requested amount.
        sm.on_terminal_success(1.0, 10000, 65680000, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("before writing is complete", sm.reason)


# ---- 16/17: duplicate / malformed progress events ----
class Test16DuplicateProgressEvents(unittest.TestCase):
    def test_identical_repeated_progress_is_a_harmless_no_op(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 500, 50000)
        sm.on_progress(10.5, 500, 50000)
        sm.on_progress(11.0, 500, 50000)
        self.assertEqual(sm.phase, GenerationPhase.WRITING)
        ev = sm.evidence(11.0)
        self.assertEqual(ev.last_records_written, 500)
        self.assertAlmostEqual(ev.seconds_since_last_progress, 1.0, places=3)


class Test17MalformedProgressEvent(unittest.TestCase):
    def test_negative_counters_are_ignored_not_a_crash(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 500, 50000)
        sm.on_progress(11.0, -1, -1)  # malformed -- must not raise, must not corrupt state
        self.assertEqual(sm.phase, GenerationPhase.WRITING)
        self.assertEqual(sm.evidence(11.0).last_records_written, 500)


# ---- 18/19: serial read timeouts during each phase ----
class Test18SerialReadTimeoutDuringWriting(unittest.TestCase):
    def test_read_timeout_during_writing_does_not_fail_and_deadline_still_applies(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(5.0, 400, 40000)
        sm.on_serial_read_timeout(10.0)
        self.assertEqual(sm.phase, GenerationPhase.WRITING, "a read timeout alone is not a failure")
        sm.check_timeouts(5.0 + 301.0)
        self.assertEqual(sm.phase, GenerationPhase.TIMED_OUT, "the phase's own deadline still applies")


class Test19SerialReadTimeoutDuringValidation(unittest.TestCase):
    def test_read_timeout_during_validation_does_not_fail(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 100000)
        sm.on_serial_read_timeout(50.0)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)
        sm.on_terminal_success(60.0, 1000, 100000, True, True, VALID_SHA)
        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)


# ---- 20: connection loss -- never auto-reconnect ----
class Test20SerialConnectionLoss(unittest.TestCase):
    def test_connection_loss_is_a_terminal_failure_not_a_reconnect(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 400, 40000)
        sm.on_connection_lost(20.0, detail="port closed")
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("not reconnecting", sm.reason)


# ---- 21: user cancellation ----
class Test21UserCancellation(unittest.TestCase):
    def test_cancel_is_a_clean_terminal_failure(self):
        sm = make_sm(1000, 100000, now=0.0)
        sm.on_accepted(0.1)
        sm.on_cancel(5.0)
        self.assertEqual(sm.phase, GenerationPhase.FAILED)
        self.assertIn("cancelled", sm.reason)


# ---- 22/23/24: cleanup fires on every terminal outcome (state-machine
#      side: is_terminal() flips true; GenerationRunner's own finally
#      block, exercised structurally here via a fake port double, proves
#      close() runs exactly once for success/failure/timeout alike) ----
class _FakePort:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False
        self.open_count = 1

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class Test22CleanupOnSuccess(unittest.TestCase):
    def test_runner_closes_the_port_exactly_once_on_success(self):
        from diag_generation_monitor import GenerationRunner

        sm = make_sm(2, 20, now=0.0)
        port = _FakePort([b"SD_WRITER_SUMMARY session=x, open=1, attempted=2, written=2, bytes=20\n"])
        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 1.0
            return clock["t"]

        call_count = {"n": 0}

        def fake_wait(client, txn, window_s):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return {"result_code": 0, "inner": b"ok"}
            return None

        def fake_decode(inner):
            return {"error": 0, "records_written": 2, "bytes_written": 20, "is_valid": True,
                    "sha256_valid": True, "sha256_hex": VALID_SHA}

        runner = GenerationRunner(console_port_opener=lambda: port)
        evidence = runner.run(sm, client=object(), gen_txn=1,
                               wait_for_command_response=fake_wait, decode_response=fake_decode, now_fn=now_fn)
        self.assertTrue(port.closed)
        self.assertEqual(evidence.serial_ports_opened, 1)
        self.assertEqual(evidence.serial_ports_closed, 1)
        self.assertEqual(evidence.phase, GenerationPhase.COMPLETED)


class Test23CleanupOnFailure(unittest.TestCase):
    def test_runner_closes_the_port_exactly_once_on_failure(self):
        from diag_generation_monitor import GenerationRunner

        sm = make_sm(2, 20, now=0.0)
        port = _FakePort([])
        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 1.0
            return clock["t"]

        def fake_wait(client, txn, window_s):
            return {"result_code": 0, "inner": b"err"}

        def fake_decode(inner):
            return {"error": 7, "records_written": 0, "bytes_written": 0, "is_valid": False,
                     "sha256_valid": False, "sha256_hex": ""}

        runner = GenerationRunner(console_port_opener=lambda: port)
        evidence = runner.run(sm, client=object(), gen_txn=1,
                               wait_for_command_response=fake_wait, decode_response=fake_decode, now_fn=now_fn)
        self.assertTrue(port.closed)
        self.assertEqual(evidence.serial_ports_opened, 1)
        self.assertEqual(evidence.serial_ports_closed, 1)
        self.assertEqual(evidence.phase, GenerationPhase.FAILED)
        self.assertEqual(evidence.terminal_error_code, 7)


class Test24CleanupOnTimeout(unittest.TestCase):
    def test_runner_closes_the_port_exactly_once_on_timeout(self):
        from diag_generation_monitor import GenerationRunner

        sm = make_sm(1000, 100000, now=0.0, acceptance_timeout_s=15)
        port = _FakePort([])  # never accepted, never any progress
        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 10.0
            return clock["t"]

        def fake_wait(client, txn, window_s):
            return None

        def fake_decode(inner):
            raise AssertionError("should never be called -- no terminal response is ever produced")

        runner = GenerationRunner(console_port_opener=lambda: port)
        evidence = runner.run(sm, client=object(), gen_txn=1,
                               wait_for_command_response=fake_wait, decode_response=fake_decode, now_fn=now_fn)
        self.assertTrue(port.closed)
        self.assertEqual(evidence.serial_ports_opened, 1)
        self.assertEqual(evidence.serial_ports_closed, 1)
        self.assertEqual(evidence.phase, GenerationPhase.TIMED_OUT)


# ---- 25/26: exactly one generation command, no auto-retry ----
class Test25ExactlyOneGenerationCommandSent(unittest.TestCase):
    def test_constructor_records_exactly_one_command_sent(self):
        sm = make_sm(1000, 100000, now=0.0)
        self.assertEqual(sm.generation_commands_sent, 1)
        sm.on_accepted(0.1)
        sm.on_progress(10.0, 1000, 100000)
        sm.on_terminal_success(15.0, 1000, 100000, True, True, VALID_SHA)
        self.assertEqual(sm.generation_commands_sent, 1, "no code path in this class ever increments this")


class Test26NoAutomaticGenerationRetry(unittest.TestCase):
    def test_state_machine_never_resends_after_any_terminal_outcome(self):
        for outcome_setup in (
            lambda sm: sm.on_terminal_failure(5.0, 3),
            lambda sm: sm.on_cancel(5.0),
            lambda sm: sm.check_timeouts(400.0),
        ):
            sm = make_sm(1000, 100000, now=0.0)
            sm.on_accepted(0.1)
            outcome_setup(sm)
            self.assertTrue(sm.is_terminal())
            self.assertEqual(sm.generation_commands_sent, 1)
            with self.assertRaises(GenerationMonitorError):
                sm.on_progress(500.0, 999, 99900)  # feeding another event must not silently resume


# ---- 27: no serial-port reopening during an active operation ----
class Test27NoSerialPortReopeningDuringActiveOperation(unittest.TestCase):
    def test_runner_opens_the_console_port_exactly_once_across_a_multi_poll_run(self):
        from diag_generation_monitor import GenerationRunner

        sm = make_sm(2, 20, now=0.0)
        port = _FakePort([b"", b"", b"SD_WRITER_SUMMARY session=x, open=1, attempted=2, written=2, bytes=20\n"])
        open_calls = {"n": 0}

        def opener():
            open_calls["n"] += 1
            return port

        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 1.0
            return clock["t"]

        call_count = {"n": 0}

        def fake_wait(client, txn, window_s):
            call_count["n"] += 1
            return {"result_code": 0, "inner": b"ok"} if call_count["n"] >= 4 else None

        def fake_decode(inner):
            return {"error": 0, "records_written": 2, "bytes_written": 20, "is_valid": True,
                    "sha256_valid": True, "sha256_hex": VALID_SHA}

        runner = GenerationRunner(console_port_opener=opener)
        runner.run(sm, client=object(), gen_txn=1,
                   wait_for_command_response=fake_wait, decode_response=fake_decode, now_fn=now_fn)
        self.assertEqual(open_calls["n"], 1, "the console port must be opened exactly once for the whole run")


# ---- 28: overflow-safe timeout calculation for maximum supported file size ----
class Test28OverflowSafeTimeoutAtMaximumSize(unittest.TestCase):
    def test_wire_ceiling_sized_file_clamps_to_the_hard_maximum(self):
        # 65535 records (this protocol's own uint16_t record_count wire
        # ceiling) at the largest possible per-record size (168+12*534).
        max_wire_records = 65535
        max_record_size = 168 + 12 * 534
        huge_size = max_wire_records * max_record_size
        self.assertEqual(huge_size, 430958160)

        timeout = compute_post_write_validation_timeout_s(huge_size)
        self.assertEqual(timeout, POST_WRITE_TIMEOUT_MAX_S, "must clamp to the hard cap, never grow unbounded")
        self.assertIsInstance(timeout, int)

    def test_zero_and_negative_inputs_never_raise_or_go_negative(self):
        self.assertEqual(compute_post_write_validation_timeout_s(0), POST_WRITE_TIMEOUT_MIN_S)
        self.assertEqual(compute_post_write_validation_timeout_s(-5), POST_WRITE_TIMEOUT_MIN_S)
        self.assertEqual(compute_post_write_validation_timeout_s(1000, bytes_per_sec=0), POST_WRITE_TIMEOUT_MIN_S)
        self.assertEqual(compute_post_write_validation_timeout_s(1000, bytes_per_sec=-1), POST_WRITE_TIMEOUT_MIN_S)

    def test_small_file_clamps_to_the_minimum(self):
        self.assertEqual(compute_post_write_validation_timeout_s(45984), POST_WRITE_TIMEOUT_MIN_S)

    def test_122_6mb_computed_timeout_is_conservative_but_bounded(self):
        timeout = compute_post_write_validation_timeout_s(122604864)
        self.assertGreater(timeout, 580.5, "must exceed the real measured duration with real margin")
        self.assertLessEqual(timeout, POST_WRITE_TIMEOUT_MAX_S)
        # Documents the actual safety margin this policy gives the real BLE-D4.0S file.
        self.assertGreater(timeout / 580.5, 2.5, "expected roughly 3x margin over the real measured duration")


# ---- Replay of preserved BLE-D4.0S evidence -----------------------------
class Test29ReplayPreservedD4_0SEvidence(unittest.TestCase):
    """Reconstructs the exact timeline from
    d4_0s_evidence/generate_result.json and the console capture's own
    SD_WRITER_SUMMARY timestamps (see the BLE-D4.0T report for the byte-
    for-byte derivation): write phase from t=0 to t=1772.6s (matching the
    real 23:19:45-23:49:18 window), then silence until the real terminal
    success at t=1772.6+580.5=2353.1s (the SAME 580.5s this task's own
    read-only re-verification independently measured twice)."""

    WRITE_PHASE_ELAPSED_S = 1772.6
    REAL_VALIDATION_S = 580.5
    OLD_HEURISTIC_TIMEOUT_S = 5 * 60  # the exact defective constant from d4_0s_generate_122mb.py

    def _feed_real_progress_sequence(self, sm):
        # A representative down-sample of the real ~10s-cadence
        # SD_WRITER_SUMMARY progression actually observed in
        # d4_0s_evidence/holterlev_console_capture_generate.txt.
        checkpoints = [
            (5.0, 43, 275856), (100.0, 947, 6213336), (400.0, 3990, 26199756),
            (700.0, 6810, 44721516), (1000.0, 9940, 65279352), (1300.0, 13135, 86264112),
            (1600.0, 16208, 106447584), (self.WRITE_PHASE_ELAPSED_S, 18667, 122604864),
        ]
        for t, records, bts in checkpoints:
            sm.on_progress(t, records, bts)

    def test_old_heuristic_would_have_produced_a_false_stall(self):
        """Reimplements ONLY the old, defective "seconds since counters
        last changed > 300" rule inline (not by importing removed code)
        to concretely demonstrate the regression this task fixes."""
        last_change_t = 0.0
        last_seen = (0, 0)
        fired_at = None
        for t, records, bts in [
            (5.0, 43, 275856), (1000.0, 9940, 65279352), (self.WRITE_PHASE_ELAPSED_S, 18667, 122604864),
            (self.WRITE_PHASE_ELAPSED_S + 309.0, 18667, 122604864),  # the exact BLE-D4.0S disconnect point
        ]:
            if (records, bts) != last_seen:
                last_change_t = t
                last_seen = (records, bts)
            since = t - last_change_t
            if since > self.OLD_HEURISTIC_TIMEOUT_S:
                fired_at = t
                break
        self.assertIsNotNone(fired_at, "the OLD heuristic must (incorrectly) fire in this replay")
        self.assertAlmostEqual(fired_at, self.WRITE_PHASE_ELAPSED_S + 309.0, delta=0.01)

    def test_corrected_state_machine_transitions_and_accepts_the_quiet_period(self):
        sm = make_sm(18667, 122604864, now=0.0)
        sm.on_accepted(0.2)
        self._feed_real_progress_sequence(sm)

        self.assertEqual(
            sm.phase, GenerationPhase.POST_WRITE_VALIDATION,
            "must have transitioned out of WRITING the instant the exact target was reached",
        )

        # The exact moment the OLD tool disconnected -- the fixed monitor must not even flinch.
        false_stall_point = self.WRITE_PHASE_ELAPSED_S + 309.0
        sm.check_timeouts(false_stall_point)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION)

        # The real terminal success, at the real measured 580.5s validation duration.
        terminal_t = self.WRITE_PHASE_ELAPSED_S + self.REAL_VALIDATION_S
        sm.check_timeouts(terminal_t - 0.1)
        self.assertEqual(sm.phase, GenerationPhase.POST_WRITE_VALIDATION, "still validating one instant before success")
        sm.on_terminal_success(terminal_t, 18667, 122604864, True, True, D4_0S_SHA)

        self.assertEqual(sm.phase, GenerationPhase.COMPLETED)
        ev = sm.evidence(terminal_t)
        self.assertEqual(ev.sha256_hex, D4_0S_SHA)
        self.assertEqual(ev.last_records_written, 18667)
        self.assertEqual(ev.last_bytes_written, 122604864)
        # No reconnect, no second command -- both structurally impossible
        # here since GenerationMonitorStateMachine has no reconnect path
        # and generation_commands_sent is set once at construction and
        # never incremented anywhere in this class.
        self.assertEqual(sm.generation_commands_sent, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
