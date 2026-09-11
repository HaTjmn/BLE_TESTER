# BLE_TESTER User Guide

- **Product/project:** BLE_TESTER
- **Document title:** BLE_TESTER User Guide — BLE Core v1.0
- **Document version:** 1.0.0
- **Date:** 2026-09-11
- **Firmware version:** 0.6.0 (BLE_TESTER firmware); CLI tool version 1.0.0 (`tools/holter_diag_generate.py`) — these are two independent version numbers, tracked separately (see "Versioning" below)
- **Applicable commit/tag:** `main` @ `release/ble-tester-v1.0-integration`; tag `ble-tester-v0.6.0`
- **Status:** Released

## 1. Hardware architecture

BLE_TESTER is an ESP32-S3 devkit running as a **BLE Central**, connecting to HolterLEV (the BLE Peripheral) and exposing a JSON command/event interface to a PC over its own USB link. A full test session normally involves **two independent serial connections**:

| Connection | Physical link | Purpose |
|---|---|---|
| BLE_TESTER control | ESP32-S3's native USB-Serial/JTAG | JSON command/event interface — scan, connect, read/write/subscribe, and every diagnostic command |
| HolterLEV console | HolterLEV's own UART0-to-USB bridge | Boot banner, panic/watchdog/backtrace dumps, and periodic diagnostic telemetry — **not** part of the JSON protocol |

These are genuinely separate devices/cables, not two roles of the same port — BLE_TESTER never touches HolterLEV's UART0, and HolterLEV's console never carries the JSON protocol (this separation was a deliberate BLE-D4.0G fix; UART0 previously carried the JSON protocol and was moved off it precisely so a JSON-channel silence could never be confused with a genuine device crash).

## 2. Identifying the correct ports

On Windows, both appear as separate `COM*` ports in Device Manager. There is no universal fixed assignment — identify them by:
- Unplug/replug one device at a time and note which `COM*` port appears/disappears.
- BLE_TESTER's control port responds to a `version`/`ping` JSON command (see §4); HolterLEV's console port only ever emits plain text (ESP-IDF log lines), never JSON.

**Safety note**: opening some serial ports can trigger the attached device's own auto-reset circuit (a well-known USB-serial-bridge behavior, not a HolterLEV/BLE_TESTER firmware defect). Keep a port open continuously for the duration of one monitored operation rather than repeatedly opening and closing it — repeated open/close cycles have been observed to cause spurious resets between separate script invocations (see the BLE Core v1.0 verification report's "important defects" section).

## 3. Installation

Python 3.9+, `pyserial`. No other dependencies for the tools in `tools/`.

## 4. JSON command interface

Every command is a single-line JSON object with an `id` and a `cmd`; every response/event is a single-line JSON object correlated by that `id`. See `docs/PROTOCOL_REFERENCE.md` for the full command list; `tools/holter_ble_cli.py` is a thin interactive/scriptable wrapper over it:

```bash
python tools/holter_ble_cli.py --port COM17 ping
python tools/holter_ble_cli.py --port COM17 scan --name NOAMED
python tools/holter_ble_cli.py --port COM17 connect --name NOAMED
python tools/holter_ble_cli.py --port COM17 discover
python tools/holter_ble_cli.py --port COM17 holter-authenticate
python tools/holter_ble_cli.py --port COM17 read --characteristic 7b8e0041-6a52-4f4d-9a33-71c02e6b4d90
```

## 5. Python CLI usage

### File download

```bash
python tools/holter_file_download.py --port COM17 --output-dir downloads
python tools/holter_file_download.py --port COM17 --file-index 3 --output-dir downloads
```

### Official diagnostic generation CLI

```powershell
python tools/holter_diag_generate.py `
    --tester-port COM17 --console-port COM23 `
    --records 1873 --output-dir tools/_evidence/generation_12mb
```

See `tools/README.md` for the complete argument/exit-code/evidence reference.

## 6. Authentication-secret handling

`holter_diag_generate.py` resolves the device access code with this precedence, and **never** falls back to a hardcoded development code:

1. `--auth-code` — supported for backward compatibility, but least secure (visible in shell history/process listings).
2. `$HOLTER_AUTH_CODE` environment variable — **recommended for automation/CI**.
3. Interactive `getpass` prompt (hidden input), offered only when stdin is a real terminal — **recommended for manual use**.
4. Clean failure, before any port is opened, if none of the above is available non-interactively.

The resolved value is never printed, logged, written to any evidence file, or embedded in an exception message/traceback under any circumstance. Other tools in this repo (`holter_ble_cli.py`, `holter_file_download.py`) still use the project's original default development access code directly — this safer precedence is currently specific to the official generation CLI; do not assume it applies elsewhere without checking that tool's own source.

## 7. Scan / connect / discover / auth / status example

```bash
python tools/holter_ble_cli.py --port COM17 scan --name NOAMED --timeout 5
python tools/holter_ble_cli.py --port COM17 connect --name NOAMED
python tools/holter_ble_cli.py --port COM17 discover
python tools/holter_ble_cli.py --port COM17 holter-authenticate
python tools/holter_ble_cli.py --port COM17 read --characteristic 7b8e0041-6a52-4f4d-9a33-71c02e6b4d90
```

## 8. File-list and download examples

```bash
python tools/holter_ble_cli.py --port COM17 write --characteristic 7b8e0040-6a52-4f4d-9a33-71c02e6b4d90 --data-hex <encoded LIST_FILES request>
python tools/holter_file_download.py --port COM17 --output-dir downloads
```

(`holter_file_download.py` builds and sends the wire messages for you — the raw `write` example above is only for direct protocol experimentation.)

## 9. Generation CLI examples

See §5 above and `tools/README.md`.

## 10. Evidence directories

- `tools/_evidence/` — this repo's existing, `.gitignore`d convention for diagnostic run output (raw captures, downloaded/reconstructed files, ELF snapshots). Point `--output-dir` at a fresh subdirectory under it.
- `holter_diag_generate.py`'s own evidence package (`run_manifest.json`, `result.json`, `console_raw.log`, `tester_raw.jsonl`, `events.jsonl`, pre/post-generation inventories) is documented in `tools/README.md`.

## 11. Exit codes

`holter_diag_generate.py`'s exit codes (0/2/3/4/5/6/7/8/130) are documented in `tools/README.md`. Other tools in this repo use simpler ad-hoc conventions (0=success, nonzero=failure) — check each tool's own `--help`/docstring.

## 12. Troubleshooting

- **"Access is denied" opening a COM port**: another process (a previous script instance, a serial monitor) still has it open — close that first rather than forcing a reopen.
- **JSON command times out**: confirm you're on BLE_TESTER's control port, not HolterLEV's console port (the console port never responds to JSON).
- **Unexpected device reset between two script runs**: see §2's safety note — this is a known serial-port-reopen side effect, not necessarily a firmware fault. Keep one connection open for the whole operation where possible.
- **`DIAG_GENERATE_TEST_FILE` not accepted**: you are likely pointed at a production HolterLEV build, which does not include this diagnostic-only opcode by design.

## 13. Diagnostic vs. production usage

`holter_diag_generate.py` and every diagnostic opcode it uses exist to exercise HolterLEV's SD-write, storage-pipeline, and BLE-transfer paths with synthetic, deterministic content — **never** a real ADS1293 recording, and unavailable against production HolterLEV firmware by design. Do not use it as a substitute for real-patient-data validation.

## Versioning

BLE_TESTER separates two independent version numbers:
- **Firmware version** (`FIRMWARE_VERSION` in `src/command_dispatcher.c`, reported via the `version`/`diag_boot` JSON commands): `0.6.0` as of this release.
- **`holter_diag_generate.py` CLI tool version** (`TOOL_VERSION` in that file, recorded in its own evidence manifests): `1.0.0`, versioned independently of the firmware since it is a host-side Python script, not something flashed onto the device.

Do not assume these move together.
