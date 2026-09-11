"""
BLE-D4.0T -- phase-aware replacement for the single generic "no progress
for N seconds" heuristic that caused a false stall during BLE-D4.0S.

Root cause (see the BLE-D4.0T task report for the full audit): the
BLE-D4.0S monitoring script (d4_0s_generate_122mb.py, an untracked
scratch script -- never previously committed to either repo) tracked
"seconds since records_written/bytes_written last CHANGED" as its only
progress signal, and applied a single flat 5-minute timeout to that
signal for the ENTIRE generation operation. Once writing reached its
target (18,667/18,667 records, 122,604,864/122,604,864 bytes) the
device legitimately spent ~580 more seconds reopening, validating, and
hashing the file -- a read-only pass that never touches
records_written/bytes_written again. The monitor could not tell "still
writing, stuck" apart from "done writing, now legitimately validating a
10x-larger file than ever exercised before", so it disconnected at
309 seconds into that valid quiet period.

This module is pure logic: no serial port, no BLE client, no wall-clock
sleep. A caller feeds it discrete events (on_*() methods) and calls
check_timeouts(now) periodically; `now` and every timeout are ordinary
floats/ints, making every scenario in test_diag_generation_monitor.py
fully deterministic under a fake clock. GenerationRunner (bottom of this
file) is the thin, NOT-unit-tested wiring layer that drives this state
machine from a real HolterBLEClient + ConsoleWatcher -- mirroring this
project's own established FileReconstructor/FileDownloadSession split
(file_reconstructor.py): pure logic tested exhaustively, I/O wiring kept
deliberately thin.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GenerationPhase(Enum):
    STARTING = "STARTING"
    WRITING = "WRITING"
    POST_WRITE_VALIDATION = "POST_WRITE_VALIDATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


_TERMINAL_PHASES = frozenset(
    (GenerationPhase.COMPLETED, GenerationPhase.FAILED, GenerationPhase.TIMED_OUT)
)

# ---- Timeout policy -------------------------------------------------
# Acceptance (STARTING -> WRITING): unrelated to the BLE-D4.0S defect,
# kept at the same value the original script already used successfully
# across BLE-D4.0K/M/Q/S.
DEFAULT_ACCEPTANCE_TIMEOUT_S = 15

# Write-stall timeout: the ORIGINAL, still-valid 5-minute heuristic --
# but per Section 3, it may now fire ONLY while records_written is
# strictly below the requested count. Once the target is reached this
# timeout is retired for the remainder of the operation.
DEFAULT_WRITE_STALL_TIMEOUT_S = 5 * 60

# Post-write validation timeout: computed from file size using a
# deliberately CONSERVATIVE lower-bound throughput, not hard-coded from
# the single 580.5s BLE-D4.0S measurement. The real measured rate was
# ~211,290 bytes/s (122,604,864 / 580.5s); 70,000 bytes/s is roughly a
# 3x safety margin under that, so a genuine size-proportional slowdown
# (e.g. real SD wear-leveling stalls, already documented elsewhere in
# this project's own history) has real headroom before this fires.
CONSERVATIVE_VALIDATION_BYTES_PER_SEC = 70_000
POST_WRITE_TIMEOUT_MIN_S = 120          # 2 minutes -- generous even for a near-empty file
POST_WRITE_TIMEOUT_MAX_S = 60 * 60      # 1 hour hard cap, regardless of file size

# Overall belt-and-suspenders cap, independent of any single phase's own
# timeout -- defense in depth only; the phase-specific timeouts above are
# the actual fix. Matches BLE-D4.0S's own 90-minute generation bound.
DEFAULT_MAX_TOTAL_DURATION_S = 90 * 60


def compute_expected_tliv_bytes(record_count: int) -> int:
    """BLE-D4.0U -- the ONE authoritative host-side formula for this
    diagnostic generator's own deterministic TLIV file size, given a
    record count. Callers (the CLI, its tests, and this module's own
    tests) must call this rather than re-deriving the formula.

    Mirrors the real generator's fixed 3-channel/[534,533,533]-frame
    cycling pattern (holter_diag_test_file_generator.c) and the
    168 + 12*frame_count per-record formula derived from
    HOLTER_TLIV_HEADER_SIZE_BYTES/HOLTER_TLIV_ECG_DATA_HEADER_SIZE_BYTES
    -- cross-validated byte-for-byte against four independent hardware
    reference points: 7->45,984 (BLE-D4.0K), 144->945,792 (BLE-D4.0M),
    1,873->12,301,872 (BLE-D4.0Q), 18,667->122,604,864 (BLE-D4.0S).

    This relationship is NOT automatically kept in sync with the
    firmware: if HOLTER_TLIV_HEADER_SIZE_BYTES,
    HOLTER_TLIV_ECG_DATA_HEADER_SIZE_BYTES, or this generator's own
    fixed phase pattern ever changes on the firmware side, this
    function's own reference points above must be re-derived and
    re-validated against a fresh hardware measurement -- it is a
    mirrored constant, not a live query of the device.

    Overflow-safe: Python integers are arbitrary-precision, and every
    intermediate value here stays far below any fixed-width limit even
    at the protocol's own 65,535-record wire ceiling (~431MB) -- this is
    still validated defensively (a non-positive record_count raises)
    rather than trusted blindly.
    """
    if record_count <= 0:
        raise ValueError(f"record_count must be positive, got {record_count}")

    per_record_bytes_phase0 = 168 + 12 * 534
    per_record_bytes_other = 168 + 12 * 533

    full_cycles, remainder = divmod(record_count, 3)
    total = full_cycles * (per_record_bytes_phase0 + 2 * per_record_bytes_other)
    for i in range(remainder):
        total += per_record_bytes_phase0 if i == 0 else per_record_bytes_other
    return total


def compute_post_write_validation_timeout_s(
    expected_bytes: int,
    bytes_per_sec: int = CONSERVATIVE_VALIDATION_BYTES_PER_SEC,
    min_s: int = POST_WRITE_TIMEOUT_MIN_S,
    max_s: int = POST_WRITE_TIMEOUT_MAX_S,
) -> int:
    """Overflow-safe (Python ints are arbitrary-precision -- this still
    guards against a nonsensical/adversarial expected_bytes rather than
    trusting the caller): a conservative timeout that grows with file
    size but is always clamped to [min_s, max_s]. `bytes_per_sec` must
    be positive; if not, the minimum is returned rather than raising, so
    a bad constant can never turn into an unbounded/negative timeout."""
    if expected_bytes <= 0 or bytes_per_sec <= 0:
        return min_s
    raw_s = math.ceil(expected_bytes / bytes_per_sec)
    return max(min_s, min(raw_s, max_s))


@dataclass
class GenerationEvidence:
    """The structured result FAILED/TIMED_OUT/COMPLETED all carry --
    Section 3's own required fields, always populated, so a caller never
    has to guess which are meaningful for a given terminal phase."""
    phase: GenerationPhase
    reason: str
    last_records_written: int
    last_bytes_written: int
    seconds_since_last_progress: float
    total_elapsed_s: float
    requested_records: int
    expected_bytes: int
    terminal_error_code: Optional[int] = None
    sha256_hex: Optional[str] = None
    serial_ports_opened: int = 0
    serial_ports_closed: int = 0


class GenerationMonitorError(Exception):
    """Raised only for programmer misuse (e.g. feeding an event after a
    terminal phase) -- never for a bad/malformed DEVICE event, which is
    always handled as data (rejected or ignored), never an exception."""


class GenerationMonitorStateMachine:
    """Pure, I/O-free phase tracker for one diagnostic-generator
    operation. Every method that represents time passing takes an
    explicit `now` (seconds, any monotonic-ish float) -- there is no
    internal call to time.time() anywhere in this class, which is what
    makes every scenario in the test suite deterministic without a real
    sleep."""

    def __init__(
        self,
        requested_records: int,
        expected_bytes: int,
        now: float,
        acceptance_timeout_s: int = DEFAULT_ACCEPTANCE_TIMEOUT_S,
        write_stall_timeout_s: int = DEFAULT_WRITE_STALL_TIMEOUT_S,
        post_write_timeout_s: Optional[int] = None,
        max_total_duration_s: int = DEFAULT_MAX_TOTAL_DURATION_S,
    ):
        if requested_records <= 0:
            raise GenerationMonitorError("requested_records must be positive")
        if expected_bytes <= 0:
            raise GenerationMonitorError("expected_bytes must be positive")

        self.requested_records = requested_records
        self.expected_bytes = expected_bytes
        self.acceptance_timeout_s = acceptance_timeout_s
        self.write_stall_timeout_s = write_stall_timeout_s
        self.post_write_timeout_s = (
            post_write_timeout_s
            if post_write_timeout_s is not None
            else compute_post_write_validation_timeout_s(expected_bytes)
        )
        self.max_total_duration_s = max_total_duration_s

        self.phase = GenerationPhase.STARTING
        self.reason = "awaiting ACCEPTED response"
        self.terminal_error_code: Optional[int] = None
        self.sha256_hex: Optional[str] = None

        self._start_time = now
        self._phase_entered_time = now
        self._last_progress_time = now
        self._last_records_written = 0
        self._last_bytes_written = 0

        self.generation_commands_sent = 1  # the ONE request the caller already sent before construction
        self.serial_ports_opened = 0
        self.serial_ports_closed = 0

    # ---- internal helpers ---------------------------------------------------
    def _enter(self, phase: GenerationPhase, now: float, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        self._phase_entered_time = now

    def _fail(self, now: float, reason: str) -> None:
        self._enter(GenerationPhase.FAILED, now, reason)

    def _timeout(self, now: float, reason: str) -> None:
        self._enter(GenerationPhase.TIMED_OUT, now, reason)

    def _require_not_terminal(self) -> None:
        if self.phase in _TERMINAL_PHASES:
            raise GenerationMonitorError(
                f"cannot feed an event -- already in terminal phase {self.phase.value}"
            )

    # ---- events ---------------------------------------------------------
    def on_accepted(self, now: float) -> None:
        self._require_not_terminal()
        if self.phase != GenerationPhase.STARTING:
            return  # a duplicate/late ACCEPTED after we've already moved on -- ignored, not an error
        self._enter(GenerationPhase.WRITING, now, "generation request accepted; writing in progress")
        self._last_progress_time = now

    def on_progress(self, now: float, records_written: int, bytes_written: int) -> None:
        """Feed one SD_WRITER_SUMMARY-style progress reading. Malformed
        (negative) values are rejected as a no-op, never crash the
        monitor and never advance state -- a stream of console telemetry
        can legitimately contain a torn/partial line."""
        self._require_not_terminal()
        if self.phase not in (GenerationPhase.STARTING, GenerationPhase.WRITING):
            return  # progress after validation has begun (or after success) carries no new information

        if records_written < 0 or bytes_written < 0:
            return  # malformed event -- ignored, per this project's own "never crash on one bad line" convention

        if self.phase == GenerationPhase.STARTING:
            # The first valid progress event is itself proof the request
            # was accepted, even if the ACCEPTED response was missed.
            self.on_accepted(now)

        if records_written < self._last_records_written or bytes_written < self._last_bytes_written:
            self._fail(
                now,
                f"counters decreased: records {self._last_records_written}->{records_written}, "
                f"bytes {self._last_bytes_written}->{bytes_written}",
            )
            return

        if records_written > self.requested_records:
            self._fail(
                now,
                f"records_written {records_written} exceeds requested {self.requested_records}",
            )
            return

        if records_written == self.requested_records and bytes_written != self.expected_bytes:
            # Reached the requested record count but the byte total
            # disagrees with the exact expected size -- a conflicting
            # progress state, not silently accepted as "close enough".
            self._fail(
                now,
                f"records_written reached {records_written} but bytes_written={bytes_written} "
                f"!= expected {self.expected_bytes}",
            )
            return

        progressed = (records_written > self._last_records_written) or (bytes_written > self._last_bytes_written)
        self._last_records_written = records_written
        self._last_bytes_written = bytes_written
        if progressed:
            self._last_progress_time = now

        if records_written == self.requested_records and bytes_written == self.expected_bytes:
            self._enter(
                now=now,
                phase=GenerationPhase.POST_WRITE_VALIDATION,
                reason=(
                    f"writing reached the exact target ({records_written} records, {bytes_written} bytes) "
                    f"-- write-stall timeout retired, post-write timeout ({self.post_write_timeout_s}s) now active"
                ),
            )
            # The write-stall clock is no longer relevant; the post-write
            # phase's own deadline is measured from THIS transition.
            self._last_progress_time = now

    def on_terminal_success(
        self,
        now: float,
        records_written: int,
        bytes_written: int,
        is_valid: bool,
        sha256_valid: bool,
        sha256_hex: str,
    ) -> None:
        self._require_not_terminal()

        if records_written != self.requested_records or bytes_written != self.expected_bytes:
            self._fail(
                now,
                f"terminal success arrived before writing is complete: reported records="
                f"{records_written}/{self.requested_records} bytes={bytes_written}/{self.expected_bytes} "
                f"-- required totals not met",
            )
            return
        if not is_valid or not sha256_valid:
            self._fail(now, f"terminal response reports is_valid={is_valid} sha256_valid={sha256_valid}")
            return
        if not isinstance(sha256_hex, str) or len(sha256_hex) != 64:
            self._fail(now, f"sha256_hex is not a well-formed 64-character hex string: {sha256_hex!r}")
            return

        self.sha256_hex = sha256_hex.lower()
        self._last_records_written = records_written
        self._last_bytes_written = bytes_written
        self._enter(GenerationPhase.COMPLETED, now, "terminal success: all required totals and hashes confirmed")

    def on_terminal_failure(self, now: float, error_code: int) -> None:
        self._require_not_terminal()
        self.terminal_error_code = error_code
        self._fail(now, f"device reported terminal generation error_code={error_code}")

    def on_serial_read_timeout(self, now: float) -> None:
        """A normal, expected serial-read timeout (nothing arrived this
        poll) is NOT itself a failure signal -- Section 5. It exists so a
        caller's poll loop has an explicit event to feed even when
        nothing happened; check_timeouts() is what actually evaluates
        the active phase's own deadline."""
        self._require_not_terminal()

    def on_connection_lost(self, now: float, detail: str = "") -> None:
        """Never auto-reconnects (Section 5): reconnecting could itself
        reset the device and would invalidate the observation window, so
        this is always a terminal FAILED, not a retry trigger."""
        self._require_not_terminal()
        self._fail(now, f"serial/BLE connection lost -- not reconnecting: {detail}".rstrip(": "))

    def on_cancel(self, now: float) -> None:
        self._require_not_terminal()
        self._fail(now, "cancelled by caller")

    def check_timeouts(self, now: float) -> None:
        """Call periodically (e.g. once per poll iteration). Evaluates
        ONLY the currently-active phase's own deadline -- this is the
        entire fix: the write-stall deadline is evaluated only in
        WRITING (and only while below the requested count), the
        post-write deadline only in POST_WRITE_VALIDATION, and an
        overall belt-and-suspenders cap is checked in both."""
        if self.phase in _TERMINAL_PHASES:
            return

        total_elapsed = now - self._start_time
        if total_elapsed > self.max_total_duration_s:
            self._timeout(now, f"overall {self.max_total_duration_s}s cap exceeded (elapsed={total_elapsed:.1f}s)")
            return

        if self.phase == GenerationPhase.STARTING:
            if (now - self._phase_entered_time) > self.acceptance_timeout_s:
                self._timeout(now, f"no ACCEPTED response within {self.acceptance_timeout_s}s")
            return

        if self.phase == GenerationPhase.WRITING:
            # The write-stall timeout applies ONLY while genuinely still
            # below the requested totals -- Section 3's central rule.
            if self._last_records_written < self.requested_records:
                since_progress = now - self._last_progress_time
                if since_progress > self.write_stall_timeout_s:
                    self._timeout(
                        now,
                        f"no write progress for {since_progress:.0f}s "
                        f"(records {self._last_records_written}/{self.requested_records}, "
                        f"bytes {self._last_bytes_written}/{self.expected_bytes})",
                    )
            return

        if self.phase == GenerationPhase.POST_WRITE_VALIDATION:
            since_phase_entry = now - self._phase_entered_time
            if since_phase_entry > self.post_write_timeout_s:
                self._timeout(
                    now,
                    f"post-write validation exceeded its {self.post_write_timeout_s}s bound "
                    f"(elapsed in this phase={since_phase_entry:.1f}s)",
                )
            return

    # ---- reporting --------------------------------------------------------
    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    def evidence(self, now: float) -> GenerationEvidence:
        return GenerationEvidence(
            phase=self.phase,
            reason=self.reason,
            last_records_written=self._last_records_written,
            last_bytes_written=self._last_bytes_written,
            seconds_since_last_progress=now - self._last_progress_time,
            total_elapsed_s=now - self._start_time,
            requested_records=self.requested_records,
            expected_bytes=self.expected_bytes,
            terminal_error_code=self.terminal_error_code,
            sha256_hex=self.sha256_hex,
            serial_ports_opened=self.serial_ports_opened,
            serial_ports_closed=self.serial_ports_closed,
        )


# =============================================================================
# Thin I/O wiring layer -- exercised in tests only via injected fakes (a
# fake port double, a fake now_fn, fake wait/decode callables); it holds
# no phase-decision logic of its own for those tests to duplicate.
# GenerationMonitorStateMachine above carries the entire tested DECISION
# contract. Kept deliberately small on purpose, mirroring
# file_reconstructor.py's own FileReconstructor/FileDownloadSession split.
# =============================================================================
class GenerationRunner:
    """Drives one GenerationMonitorStateMachine from a real
    HolterBLEClient + a console-reading thread. Opens each required
    serial port EXACTLY ONCE for the whole operation (Section 4): the
    BLE_TESTER control port is owned by the caller-supplied `client`
    (already open before this class is constructed); this class owns
    only the HolterLEV console port, opening it once in `run()` and
    closing it exactly once in a `finally` block that always executes --
    on success, failure, cancellation, and timeout alike. Never reopens
    a port as a progress check, never launches a follow-up probe
    process, and never auto-reconnects on connection loss."""

    def __init__(self, console_port_opener, poll_interval_s: float = 5.0):
        """`console_port_opener` is a zero-arg callable returning an
        object with a blocking-with-timeout `.read(n)` and a `.close()`
        -- normally `lambda: serial.Serial(port, 115200, timeout=0.5)`.
        Injected (not hardcoded to pyserial) so this class stays testable
        with a fake port double if ever needed, without pulling pyserial
        into the pure unit tests."""
        self._console_port_opener = console_port_opener
        self.poll_interval_s = poll_interval_s
        self._console = None

    def run(self, sm: GenerationMonitorStateMachine, client, gen_txn: int,
             wait_for_command_response, decode_response, now_fn=time.time, on_tick=None):
        """Runs sm to a terminal phase. `wait_for_command_response` and
        `decode_response` are injected exactly like the console opener --
        this keeps GenerationRunner a pure wiring shim with no import-time
        dependency on holter_hlv_protocol's own decode functions, so a
        protocol change never requires touching this class.

        `on_tick`, if given, is called once per poll iteration (and once
        more after the final terminal transition) with the current
        GenerationEvidence -- a pure OBSERVATION hook for a caller's own
        live progress display or event log, never a decision point: it
        cannot influence phase transitions, and this loop's own control
        flow is identical whether or not one is supplied."""
        import re

        sd_writer_pattern = re.compile(
            r"SD_WRITER_SUMMARY session=\S*, open=\d+, attempted=\d+, written=(\d+), bytes=(\d+)"
        )

        self._console = self._console_port_opener()
        sm.serial_ports_opened += 1
        try:
            buf = b""
            while not sm.is_terminal():
                sm.check_timeouts(now_fn())
                if sm.is_terminal():
                    break

                try:
                    chunk = self._console.read(4096)
                except Exception as e:
                    sm.on_connection_lost(now_fn(), detail=str(e))
                    break

                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode(errors="replace")
                        m = sd_writer_pattern.search(text)
                        if m:
                            sm.on_progress(now_fn(), int(m.group(1)), int(m.group(2)))
                            if on_tick is not None:
                                on_tick(sm.evidence(now_fn()))
                            if sm.is_terminal():
                                break
                else:
                    sm.on_serial_read_timeout(now_fn())

                if sm.is_terminal():
                    break

                terminal = wait_for_command_response(client, gen_txn, window_s=0.5)
                if terminal is not None:
                    result = decode_response(terminal["inner"])
                    if terminal["result_code"] != 0 or result.get("error", 0) != 0:
                        sm.on_terminal_failure(now_fn(), result.get("error", terminal["result_code"]))
                    else:
                        sm.on_terminal_success(
                            now_fn(),
                            records_written=result["records_written"],
                            bytes_written=result["bytes_written"],
                            is_valid=result["is_valid"],
                            sha256_valid=result["sha256_valid"],
                            sha256_hex=result["sha256_hex"],
                        )
                    if on_tick is not None:
                        on_tick(sm.evidence(now_fn()))
                elif on_tick is not None:
                    on_tick(sm.evidence(now_fn()))
        finally:
            try:
                self._console.close()
            finally:
                sm.serial_ports_closed += 1

        final_evidence = sm.evidence(now_fn())
        if on_tick is not None:
            on_tick(final_evidence)
        return final_evidence
