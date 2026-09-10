#!/usr/bin/env python3
"""
BLE-D4.0R post-test confirmation ONLY: after all three transfers have
already completed and the main session was cleanly closed, open one
fresh connection solely to re-confirm the SD-side source file (file_index
3) is still exactly what it was before Transfer 1 -- same size, same
reopened-SD SHA-256, still refused for deletion as VALID. This is a
read-only diagnostic query (the DIAG_DELETE_INVALID_TEST_FILE safety gate
never removes a currently-valid file), not a new test and not a mutation.
"""
import sys, os, time, re

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_GET_FILE_INFO, HLV_RESPONSE_PHASE_ACCEPTED,
    encode_hlv_message, decode_get_file_info_response, wait_for_command_response, authenticate_raw,
)

HLV_OPCODE_DIAG_DELETE_INVALID_TEST_FILE = 0x0028
HOLTERLEV_PORT = "COM23"
BLETESTER_PORT = "COM17"
FILE_INDEX = 3
EXPECTED_SIZE = 12301872
EXPECTED_SHA256 = "e898365da5098f6a7680710132ba0335a3ed750e6954101c8555d881bdfcd79c"

SCRATCHPAD_DIR = os.path.dirname(os.path.abspath(__file__))
CONSOLE_LOG_PATH = os.path.join(SCRATCHPAD_DIR, "d4_0r_evidence", "holterlev_console_capture_posttest.txt")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ser = serial.Serial(HOLTERLEV_PORT, 115200, timeout=1.0)
    console_buf = b""

    client = HolterBLEClient(BLETESTER_PORT, baud=115200, default_timeout=15)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    if not status["authorized"]:
        raise ControllerError("AUTH_FAILED", "not authorized")
    log("connected + authenticated (post-test verification connection)")

    info_msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, base_txn + 1, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=info_msg.hex())
    cr = wait_for_command_response(client, base_txn + 1, window_s=5)
    info = decode_get_file_info_response(cr["inner"])
    log(f"GET_FILE_INFO(file_index={FILE_INDEX}) post-test: {info}")
    size_ok = info["exists"] and info["size_bytes"] == EXPECTED_SIZE

    del_txn = base_txn + 2
    del_msg = encode_hlv_message(HLV_OPCODE_DIAG_DELETE_INVALID_TEST_FILE, del_txn, FILE_INDEX.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=del_msg.hex())
    accepted = wait_for_command_response(client, del_txn, window_s=10)
    accepted_ok = accepted is not None and accepted["phase"] == HLV_RESPONSE_PHASE_ACCEPTED
    terminal = wait_for_command_response(client, del_txn, window_s=90)
    del_err = terminal["inner"][0] if terminal and terminal.get("inner") else None
    log(f"DIAG_DELETE_INVALID_TEST_FILE post-test reopen-hash: accepted={accepted_ok} error={del_err} "
        f"(8=FILE_IS_VALID expected)")

    time.sleep(0.3)
    # Drain whatever console text has accumulated since this process's own
    # boot-time watcher wasn't running (main test's own watcher already
    # exited) -- read fresh from the port directly for this one check.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            console_buf += chunk
    text = console_buf.decode(errors="replace")
    with open(CONSOLE_LOG_PATH, "wb") as f:
        f.write(console_buf)
    m = re.search(
        r"File_index=" + str(FILE_INDEX) + r".*?reopened-SD SHA-256=([0-9a-fA-F]{64}) is_valid=(\d)", text
    )
    reopened_sha256 = m.group(1).lower() if m else None
    log(f"post-test reopened_sd_sha256={reopened_sha256} matches={reopened_sha256 == EXPECTED_SHA256}")

    client.send("disconnect", timeout=5)
    client.close()
    ser.close()

    print()
    print("=== POST-TEST SOURCE FILE VERIFICATION ===")
    print(f"size_ok={size_ok} (expected {EXPECTED_SIZE}, got {info['size_bytes']})")
    print(f"delete_gate_refused_as_valid={del_err == 8}")
    print(f"reopened_sd_sha256={reopened_sha256}")
    print(f"reopened_sd_sha256_matches={reopened_sha256 == EXPECTED_SHA256}")
    overall_ok = size_ok and (del_err == 8) and (reopened_sha256 == EXPECTED_SHA256)
    print(f"OVERALL_SOURCE_UNCHANGED={overall_ok}")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
