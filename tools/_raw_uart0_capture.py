#!/usr/bin/env python3
"""BLE-D4.0G Steps 4-6: passive raw capture of UART0. Since the JSON
protocol no longer touches this wire at all (it moved to the dedicated
USB-Serial/JTAG channel), every line seen here is boot/panic/libbt.a
diagnostic text by construction -- there is nothing left to parse as
JSON, so every line lands in the raw-capture file."""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM28"
DURATION_S = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
CAPTURE_PATH = sys.argv[3] if len(sys.argv) > 3 else "_evidence/d4_0g_raw_capture.jsonl"

client = HolterBLEClient(PORT, baud=115200, default_timeout=5, raw_capture_path=CAPTURE_PATH)
print(f"[{time.strftime('%H:%M:%S')}] listening on {PORT} for {DURATION_S}s -> {CAPTURE_PATH}", flush=True)
time.sleep(DURATION_S)
client.close()
print(f"[{time.strftime('%H:%M:%S')}] capture window done", flush=True)
