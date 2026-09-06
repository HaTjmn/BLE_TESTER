#!/usr/bin/env python3
"""
BLE-D4.0B Step 2 -- regression tests proving the corrected serial-port
ownership model: exactly one owner of the COM port, BLE-level reconnect
never reopens it, a timeout doesn't leak or corrupt the handle, and a
port is immediately reusable by a new process after an exception.
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import HLV_DEVICE_NAME, HLV_COMMAND_RESPONSE_UUID, HLV_FILE_TRANSFER_DATA_UUID

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def full_connect(client):
    client.send("reset_controller", timeout=5)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.send("subscribe", timeout=6, characteristic=HLV_FILE_TRANSFER_DATA_UUID, mode="notify")


# --- Test 1: open/close repeatedly ---------------------------------------
try:
    for i in range(5):
        c = HolterBLEClient(PORT, baud=115200, default_timeout=10)
        c.send("ping", timeout=3)
        c.close()
    check("test1_open_close_repeated_5x", True)
except Exception as e:
    check("test1_open_close_repeated_5x", False, str(e))

# --- Test 2: multiple BLE reconnects, ONE persistent serial connection ---
try:
    client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
    ser_handle_id = id(client._ser)
    for i in range(3):
        full_connect(client)
        assert client._ser.is_open, "serial handle closed mid-test"
        assert id(client._ser) == ser_handle_id, "serial handle was replaced (port was reopened)"
    check("test2_three_ble_reconnects_one_serial_connection", True,
          f"same underlying serial.Serial object id={ser_handle_id} across 3 reconnects")
    client.close()
except Exception as e:
    check("test2_three_ble_reconnects_one_serial_connection", False, str(e))
    try:
        client.close()
    except Exception:
        pass

# --- Test 3: inject a timeout, confirm the port remains usable afterward -
try:
    client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
    full_connect(client)
    timed_out = False
    try:
        # 1ms is far too short for any real controller round trip -- guaranteed
        # host-side timeout without touching firmware.
        client.send("read", timeout=0.001, characteristic=HLV_COMMAND_RESPONSE_UUID)
    except ControllerError as e:
        timed_out = "TIMEOUT" in str(e).upper() or "HOST_TIMEOUT" in str(e).upper()
    # Port must still be usable immediately after -- a real command should succeed.
    client.send("ping", timeout=3)
    still_open = client._ser.is_open
    check("test3_timeout_then_port_still_usable", timed_out and still_open,
          f"timed_out={timed_out}, still_open={still_open}")
    client.close()
except Exception as e:
    check("test3_timeout_then_port_still_usable", False, str(e))
    try:
        client.close()
    except Exception:
        pass

# --- Test 4: exit after an exception, confirm immediate reopen by a NEW client
try:
    client = HolterBLEClient(PORT, baud=115200, default_timeout=15)
    raised = False
    try:
        client.send("write", timeout=3, characteristic="not-a-real-uuid", data_hex="00")
    except ControllerError:
        raised = True
    client.close()
    time.sleep(0.3)
    c2 = HolterBLEClient(PORT, baud=115200, default_timeout=10)
    c2.send("ping", timeout=3)
    c2.close()
    check("test4_reopen_immediately_after_exception", raised, "new client opened+pinged successfully")
except Exception as e:
    check("test4_reopen_immediately_after_exception", False, str(e))

passed = sum(1 for _, s, _ in results if s == "PASS")
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
