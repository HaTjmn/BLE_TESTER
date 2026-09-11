# HolterLEV diagnostic generation CLI

`holter_diag_generate.py` is the official, tracked tool for generating one
deterministic diagnostic TLIV file on HolterLEV over BLE and monitoring
its progress with the phase-aware state machine introduced in BLE-D4.0T
(`diag_generation_monitor.py`). It replaces the untracked, one-off scratch
scripts used for this purpose in BLE-D4.0K/M/Q/S.

## What this generates, and what it does not

This tool drives HolterLEV's `DIAG_GENERATE_TEST_FILE` opcode (`0x0027`).
That opcode:

- **Only exists in diagnostic builds** (`esp32-s3-devkitc-1-n32r8v-ble-diag-dev`).
  Production HolterLEV firmware does not compile this code path in at
  all — pointing this tool at a production build produces a clear,
  documented "not ACCEPTED" error, not a crash.
- Produces **synthetic, deterministic** TLIV content using fixed,
  reproducible per-frame values. It **never exercises ADS1293
  acquisition** and must never be mistaken for, or substituted for, a
  real patient recording.
- Uses the **same production TLIV encoder and storage pipeline** a real
  recording uses — it is a genuine end-to-end exercise of SD write, BLE
  transfer readiness, and file-integrity validation, just with
  synthetic input.

## Installation / dependencies

Same as every other tool in `tools/`: Python 3.9+, `pyserial`. No
additional dependencies.

## Usage

### PowerShell

```powershell
python tools/holter_diag_generate.py `
    --tester-port COM17 --console-port COM23 `
    --records 1873 --output-dir tools/_evidence/generation_12mb
```

### Bash

```bash
python tools/holter_diag_generate.py \
    --tester-port /dev/ttyUSB0 --console-port /dev/ttyUSB1 \
    --records 1873 --output-dir tools/_evidence/generation_12mb
```

`tools/_evidence/` is this repo's existing, gitignored convention for
diagnostic run output — point `--output-dir` at a fresh subdirectory
under it unless you have a reason to put evidence elsewhere.

### Authentication

The device access code is resolved in this order: `--auth-code` >
`$HOLTER_DEV_ACCESS_CODE` environment variable > this project's own
existing default dev code. **Never pass a real secret on the command
line in a shared/logged shell** — prefer the environment variable. The
resolved code is never printed, logged, or written to any evidence file
under any circumstance.

## Argument reference

| Argument | Required | Default | Meaning |
|---|---|---|---|
| `--tester-port` | yes | — | BLE_TESTER control serial port |
| `--console-port` | yes | — | HolterLEV's own UART0 console/boot port |
| `--records` | yes | — | record count, 1-20,000 (current diagnostic firmware cap) |
| `--output-dir` | yes | — | evidence directory; must NOT already exist |
| `--baud` | no | 115200 | baud rate for both ports |
| `--device-name` | no | `NOAMED` | BLE device name to connect to |
| `--device-addr` | no | none | BLE address (alternative to `--device-name`) |
| `--frames-per-record` | no | 512 | device-side sanity value only; does not change the actual per-record frame count |
| `--auth-code` | no | see above | device access code; never logged |
| `--host-timeout` | no | 15.0s | host-side wire-level wait ceiling |
| `--acceptance-timeout` | no | BLE-D4.0T default (15s) | wait for the ACCEPTED response |
| `--write-stall-timeout` | no | BLE-D4.0T default (300s) | no-progress bound, applies only below the requested record count |
| `--post-write-timeout` | no | computed from `--records` | bound for the reopen/validate/hash phase |
| `--overall-timeout` | no | BLE-D4.0T default (5400s) | belt-and-suspenders overall cap |

Run `python tools/holter_diag_generate.py --help` for the full,
always-current list.

## Phases

The tool prints and logs six explicit phases (defined in
`diag_generation_monitor.py`, never reimplemented here):

1. **STARTING** — request sent, awaiting ACCEPTED.
2. **WRITING** — HolterLEV is submitting and writing records; the
   5-minute write-stall timeout applies here, and *only* while
   `records_written` is still below the requested count.
3. **POST_WRITE_VALIDATION** — writing reached the exact requested
   totals; HolterLEV is now closing, reopening, validating, and
   hashing the file. **Unchanged write counters here are expected, not
   a stall** — this phase has its own separate, size-proportional
   timeout. For a 122.6MB file this legitimately takes ~10 minutes.
4. **COMPLETED** — terminal success, with matching record count, byte
   count, `is_valid=true`, `sha256_valid=true`, and a well-formed
   64-character SHA-256.
5. **FAILED** — a rejected/inconsistent event, a lost connection, a
   terminal firmware error, or a validation mismatch. Never
   auto-retried.
6. **TIMED_OUT** — a phase's own deadline was exceeded.

## Evidence package

Every run's `--output-dir` contains:

| File | Contents |
|---|---|
| `run_manifest.json` | tool version, timestamps, sanitized args, timeouts, phase-transition list, open/close counts, final status/exit code — written incrementally, finalized in a `finally` path |
| `result.json` | the terminal `GenerationEvidence` snapshot |
| `console_raw.log` | every raw byte read from the HolterLEV console port |
| `tester_raw.jsonl` | BLE_TESTER's own raw NDJSON transcript (the existing `HolterBLEClient(raw_capture_path=...)` mechanism) |
| `events.jsonl` | one JSON line per phase transition and notable event, flushed immediately |
| `pre_generation_inventory.json` | `LIST_FILES` snapshot before the command is sent |
| `post_generation_inventory.json` | `LIST_FILES` snapshot after success |

The output directory must not already exist — this tool never
overwrites or cleans prior evidence.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | invalid arguments or unsafe output directory |
| 3 | serial-port ownership/open failure |
| 4 | BLE connection/authentication/preflight failure |
| 5 | generation timeout (`TIMED_OUT`) |
| 6 | firmware reported a terminal generation failure |
| 7 | result validation or evidence-integrity failure |
| 8 | unexpected tool failure |
| 130 | cancelled by user (Ctrl+C) |

Every non-zero exit still attempts orderly cleanup and evidence
finalization.

## Safety behavior

- Exactly one `DIAG_GENERATE_TEST_FILE` command per invocation, ever.
- No automatic reconnect, rescan, re-authentication, or generation
  retry under any circumstance, including Ctrl+C and serial connection
  loss.
- Each serial port is opened exactly once and closed exactly once.
- Never deletes an incomplete file, never invokes delete-all, never
  starts a file transfer — cleanup of a failed generation attempt is
  the firmware's own existing responsibility.

## Not a substitute for real recording

This generator produces synthetic content for exercising SD write, BLE
transfer readiness, and file-integrity validation. It does not exercise
ADS1293 acquisition and must never be confused with, or used in place
of, a real patient recording.
