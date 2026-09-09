#!/usr/bin/env python3
"""
BLE-D4.0H Step 1 -- preflight, using ONE persistent serial handle for the
whole check (per this task's explicit instruction: no repeated ad-hoc
pyserial open/close probes, which were shown in BLE-D4.0G to cause
self-inflicted ESP_RST_USB resets on this board's native USB port).
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_GET_STATUS, HLV_RESPONSE_PHASE_COMPLETED,
    encode_hlv_message, wait_for_command_response, authenticate_raw,
)
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM17"

client = HolterBLEClient(PORT, baud=115200, default_timeout=15)

version = client.send("version", timeout=5)
print("version:", json.dumps(version))
assert version["firmware_version"] == "0.5.1-diag", f"unexpected firmware_version: {version['firmware_version']}"

boot_before = client.send("diag_boot", timeout=5)
print("boot_before:", json.dumps(boot_before))

client.send("reset_controller", timeout=5)
client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
client.send("discover_services", timeout=10)
client.send("discover_characteristics", timeout=10)
client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
client.drain_events()
base_txn = int(time.time() * 1000) & 0x7FFFFFFF
status = authenticate_raw(client, base_txn)
print("authenticated:", status["authorized"])
assert status["authorized"], "authentication failed during preflight control check"

txn = base_txn + 1
msg = encode_hlv_message(HLV_OPCODE_GET_STATUS, txn)
client.send("write", timeout=5, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
resp = wait_for_command_response(client, txn, window_s=4)
print("get_status response phase:", resp["phase"] if resp else None)
assert resp is not None and resp["phase"] == HLV_RESPONSE_PHASE_COMPLETED, f"GET_STATUS failed: {resp}"

client.send("disconnect", timeout=6)

boot_after = client.send("diag_boot", timeout=5)
print("boot_after:", json.dumps(boot_after))

client.close()

print()
print("=== PREFLIGHT RESULT ===")
print("firmware_version:", version["firmware_version"])
print("boot_generation_before_control_check:", boot_before["boot_generation"])
print("boot_generation_after_control_check:", boot_after["boot_generation"])
ok = (boot_before["boot_generation"] == boot_after["boot_generation"])
print("no_reset_during_control_check:", ok)
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
