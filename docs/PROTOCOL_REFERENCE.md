# HolterLEV BLE Test Controller -- Protocol Reference

Document version: 1.0
Date: 2026-09-03

This test controller implements two protocols. Both are documented here
because Claude Code (or any future maintainer) must never guess either
one -- both are frozen contracts owned elsewhere.

## 1. HolterLEV BLE GATT contract (source of truth: the DUT firmware repo)

Mirrored from `docs/ble/PROTOCOL.md` (v1.5) and `src/holter_ble_protocol.h`
in `c:/repos/ECG_ads1293/holterlev_1293-BLE` (branch
`fix/ble-d3-2-connection-ownership` at the time this was written). That
repo is the only source of truth for this section -- if it disagrees with
this file, the firmware repo wins and this file is stale.

- Advertised name: `NOAMED` (confirmed live against real hardware).
- Primary service: `7b8e0000-6a52-4f4d-9a33-71c02e6b4d90`.
- Characteristics: Control Point (`...0040`, Write), Status (`...0041`,
  Read/Notify/Indicate), Data/Telemetry (`...0042`, Notify, no producer
  yet), Protocol/Device Info (`...0043`, Read), Command Response
  (`...0044`, Notify/Indicate), Status v2 (`...0045`, Read/Notify).
- Wire message: 12-byte little-endian header (`protocol_major`,
  `protocol_minor`, `opcode` u16, `transaction_id` u32, `result_code`
  u16, `payload_length` u16) + payload. Used for every Control Point
  write; **not** used for a plain characteristic Read (Status, Status v2,
  and Device Info reads return their raw struct directly, no header).
- Opcodes: `AUTH=0x0001` (payload: 4 ASCII digit bytes), `GET_STATUS=0x0002`,
  `START=0x0010` (payload: `duration_seconds` u32 LE), `STOP=0x0011`.
- Auth: development-only 4-digit code, default `2018`, never encryption,
  cleared on every disconnect, 5 failed attempts trigger a 60s
  device-wide cooldown.
- Status payload (14 bytes): `startup_status, ads1293_ready, sd_ready,
  recording_capable, session_state, authorized` (each u8), then
  `last_command_opcode` (u16), `last_command_transaction_id` (u32),
  `last_command_result` (u16).

**Known simplification**: `subscribe`/`unsubscribe` write the CCCD at
`val_handle + 1` rather than discovering it via `ble_gattc_disc_all_dscs`.
This is correct for every characteristic on this device (confirmed
against real hardware) because NimBLE's own GATT server always places an
auto-generated CCCD immediately after the value attribute when no other
descriptor is declared first -- true here, but not true in general for an
arbitrary peer. If this controller is ever pointed at a different BLE
peripheral, add real descriptor discovery before trusting `subscribe`.

## 2. UART host-automation protocol (owned by this repo)

Newline-delimited JSON, 115200 8N1, one complete object per line. Every
command carries a unique `id`; every response echoes it. Async BLE events
(`device_found`, `scan_complete` is folded into the `scan` command's own
terminal response, `notification`, `disconnected`) are separate lines
with `"type":"event"`, uncorrelated with any `id`. Malformed input or an
unparseable line responds with `{"id":null,...}` since no request id
could be determined.

Design choice: `scan` and `connect` do **not** ack-then-report; their
single terminal response only arrives once the operation actually
finishes (scan timeout/cancel, or connect success/failure/timeout). This
matches the worked example in the original task brief exactly and keeps
"exactly one terminal response per id" simple. `scan_stop` and
`disconnect` are themselves separate, immediately-acked commands -- they
do not block waiting for the original operation's own terminal response.

Only one BLE operation may be in flight at a time (mirrors the real
device's own V1 scope of one active connection/operation). A second
command sent while one is pending gets an immediate `BUSY` or
`INVALID_STATE` error rather than being queued.

### Commands implemented

Raw: `ping`, `version`, `controller_status`, `scan`, `scan_stop`,
`connect` (by `name` or `addr`), `disconnect`, `discover_services`,
`discover_characteristics` (optional `service_uuid` filter), `read`,
`write` (`data_hex`, optional `with_response`), `subscribe`/`unsubscribe`
(`mode`: notify|indicate), `wait_event` (optional `event` filter, blocks
up to `timeout_ms`), `clear_bonds`, `reset_controller`.

Semantic (HolterLEV-specific, thin wrappers over the raw layer):
`holter_connect` (connect + discover services + discover characteristics
in one call), `holter_authenticate` (writes AUTH, then polls Status until
it reflects our own `transaction_id` -- never trusts the ATT write ack
alone, since PROTOCOL.md Section 16 explicitly documents Status timing as
unsynchronized with command completion), `holter_get_status`,
`holter_get_device_info`, `holter_get_session_status`,
`holter_start_session` (`duration_seconds`), `holter_stop_session`,
`holter_wait_state` (`target_state` by name or number, polls until match
or timeout).

Not implemented (no live BLE contract to bind to yet, per the firmware's
own PROTOCOL.md Section 10): telemetry/ECG preview, OTA, bonding-based
reconnection semantics beyond raw `clear_bonds`.

### HLV-WIFI-20261006-008 additions (Wi-Fi management over BLE)

These are all opt-in, and existing commands behave as before:

- **`read`:** `"long": true` performs an ATT long read (Read + Read Blob) of up to 512 bytes. It is needed for values longer than MTU-3, such as HolterLEV's Wi-Fi Snapshot at MTU 23.
- **`write`:** `"long": true` forces an ATT long write (Prepare Write + Execute Write). A write with response that is longer than MTU-3 now uses a long write automatically; before, it failed. The written payload is wiped after the write and is never echoed.
- **`connect`:** `"exchange_mtu": false` skips the MTU exchange for that one connection, so it stays at ATT MTU 23.
- **`security_initiate`:** starts pairing with LE Secure Connections, Just Works, no bonding. `ok` only means the pairing has started. The outcome arrives as the event `enc_change` `{status, encrypted, key_size}` and is mirrored for `wait_event`.
- **`controller_status`:** now also reports `encrypted`.

Host side:
- `tools/holter_wifi_protocol.py`: the codec of HolterLEV PROTOCOL.md Section 19.
- `tools/holter_wifi_manage.py`: the CLI, plus the `selftest` hardware checklist.
- `tools/test_holter_wifi_protocol.py`: offline tests, checked against golden vectors from the firmware codec.

`HolterBLEClient.send(..., sensitive=True)` logs `data_hex` as `<redacted N bytes>` and keeps it out of `ControllerError`. The Wi-Fi tool uses it for every ADD / UPDATE. Passwords are taken from `HLV_WIFI_PASSWORD` or a no-echo prompt, never from the command line.
