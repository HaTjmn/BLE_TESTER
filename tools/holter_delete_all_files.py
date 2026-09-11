#!/usr/bin/env python3
"""
BLE-D4.0J -- delete-all-recordings client (docs/ble/PROTOCOL.md Section
18). Drives HolterLEV's two-phase PREPARE/CONFIRM/GET_DELETE_STATUS
maintenance opcodes through BLE_TESTER's existing generic BLE commands
(write/subscribe/wait_event) over the dedicated native USB-Serial/JTAG
channel -- no BLE_TESTER firmware change needed for this feature.

Safe by default: running this script with no flags only calls PREPARE,
prints the exact live file count/total bytes, and stops -- it never
deletes anything unless the caller passes --confirm-delete-all
explicitly. There is no way to trigger deletion through this script by
accident.

Usage:
    python tools/holter_delete_all_files.py --port COM17
        (prepare only -- prints the inventory and exits, deletes nothing)

    python tools/holter_delete_all_files.py --port COM17 --confirm-delete-all
        (prepare, then actually execute the deletion, polling status to
        completion, then verify the final inventory with LIST_FILES)
"""
import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402
from holter_hlv_protocol import (  # noqa: E402
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_OPCODE_LIST_FILES, HLV_OPCODE_PREPARE_DELETE_ALL_FILES,
    HLV_OPCODE_CONFIRM_DELETE_ALL_FILES, HLV_OPCODE_GET_DELETE_STATUS,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_COMPLETED,
    HLV_DELETE_STATE_DELETING, HLV_DELETE_STATE_COMPLETE, HLV_DELETE_STATE_FAILED,
    encode_hlv_message, decode_command_response, decode_list_files_response,
    decode_prepare_delete_all_files_response, encode_confirm_delete_all_files_request,
    decode_get_delete_status_response,
    wait_for_command_response, authenticate_raw,
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_and_prepare(client):
    client.send("reset_controller", timeout=5)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    if not status["authorized"]:
        raise ControllerError("AUTH_FAILED", "device rejected the default dev access code")
    return base_txn


def list_all_files(client, base_txn):
    """Full LIST_FILES walk -- used only for the final post-deletion
    verification, where we want the ground truth, not a fast estimate."""
    files = []
    start_index = 0
    txn = base_txn
    while True:
        txn += 1
        msg = encode_hlv_message(HLV_OPCODE_LIST_FILES, txn, start_index.to_bytes(2, "little"))
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        cr = wait_for_command_response(client, txn, window_s=10)
        if cr is None or cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
            raise ControllerError("LIST_FILES_FAILED", f"unexpected response: {cr}")
        page = decode_list_files_response(cr["inner"])
        files.extend(page["entries"])
        if not page["has_more"] or not page["entries"]:
            break
        start_index = page["entries"][-1]["file_index"] + 1
    return files, txn


def prepare_delete_all(client, txn):
    msg = encode_hlv_message(HLV_OPCODE_PREPARE_DELETE_ALL_FILES, txn)
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, txn, window_s=15)
    if cr is None or cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
        raise ControllerError("PREPARE_DELETE_ALL_FILES_FAILED", f"unexpected response: {cr}")
    return decode_prepare_delete_all_files_response(cr["inner"])


def confirm_delete_all(client, txn, token, expected_file_count, expected_total_bytes):
    payload = encode_confirm_delete_all_files_request(token, expected_file_count, expected_total_bytes)
    msg = encode_hlv_message(HLV_OPCODE_CONFIRM_DELETE_ALL_FILES, txn, payload)
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    accepted = wait_for_command_response(client, txn, window_s=10)
    if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
        raise ControllerError("CONFIRM_DELETE_ALL_FILES_NOT_ACCEPTED", f"unexpected response: {accepted}")
    return accepted


def get_delete_status(client, txn):
    msg = encode_hlv_message(HLV_OPCODE_GET_DELETE_STATUS, txn)
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, txn, window_s=6)
    if cr is None or cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
        raise ControllerError("GET_DELETE_STATUS_FAILED", f"unexpected response: {cr}")
    return decode_get_delete_status_response(cr["inner"])


def main():
    parser = argparse.ArgumentParser(description="HolterLEV delete-all-recordings client (BLE-D4.0J)")
    parser.add_argument("--port", required=True, help="BLE_TESTER's dedicated native USB-Serial/JTAG port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--confirm-delete-all", action="store_true",
                         help="Actually execute the deletion. Without this flag, only PREPARE runs "
                              "and nothing is deleted -- this is the safe default.")
    parser.add_argument("--poll-interval-s", type=float, default=2.0,
                         help="Conservative interval for polling GET_DELETE_STATUS while DELETING (default: 2s)")
    args = parser.parse_args()

    client = HolterBLEClient(args.port, baud=args.baud, default_timeout=15)
    try:
        base_txn = connect_and_prepare(client)
    except ControllerError as e:
        log(f"FATAL: could not connect/authenticate: {e}")
        client.close()
        return 2

    try:
        txn = base_txn + 1
        log("PREPARE_DELETE_ALL_FILES ...")
        prepared = prepare_delete_all(client, txn)
        file_count = prepared["file_count"]
        total_bytes = prepared["total_bytes"]
        token = prepared["token"]

        log(f"Live recognized inventory: {file_count} file(s), {total_bytes} bytes "
            f"({total_bytes / (1024 * 1024):.2f} MB)")
        log(f"Token expires in {prepared['expires_in_ms']} ms")

        if not args.confirm_delete_all:
            log("--confirm-delete-all was not passed: stopping here. Nothing was deleted.")
            client.close()
            return 0

        log(f"CONFIRM_DELETE_ALL_FILES: deleting {file_count} file(s), {total_bytes} bytes -- executing now")
        txn += 1
        confirm_delete_all(client, txn, token, file_count, total_bytes)
        log("CONFIRM accepted -- deletion is running on HolterLEV's dedicated delete-worker task")

        t_start = time.time()
        last_files_deleted = -1
        while True:
            time.sleep(args.poll_interval_s)
            txn += 1
            status = get_delete_status(client, txn)
            state = status["state_name"]

            if status["files_deleted"] != last_files_deleted:
                log(f"  status={state} files_deleted={status['files_deleted']}/{file_count} "
                    f"bytes_deleted={status['bytes_deleted']}/{total_bytes} failures={status['failures']}")
                last_files_deleted = status["files_deleted"]

            if status["state"] == HLV_DELETE_STATE_FAILED:
                log(f"*** DELETION FAILED: first_error_code={status['first_error_code']} "
                    f"first_failed_path={status['first_failed_path']!r} ***")
                client.close()
                return 1

            if status["state"] == HLV_DELETE_STATE_COMPLETE:
                elapsed = time.time() - t_start
                log(f"DELETION COMPLETE in {elapsed:.1f}s: files_deleted={status['files_deleted']} "
                    f"bytes_deleted={status['bytes_deleted']} failures={status['failures']}")
                if status["failures"] > 0:
                    log(f"  NOTE: {status['failures']} file(s) failed to delete -- "
                        f"first_error_code={status['first_error_code']} "
                        f"first_failed_path={status['first_failed_path']!r}")
                break

            if status["state"] != HLV_DELETE_STATE_DELETING:
                log(f"*** unexpected state during polling: {state} -- stopping ***")
                client.close()
                return 1

        log("Verifying final inventory with LIST_FILES ...")
        txn += 1
        final_files, txn = list_all_files(client, txn)
        log(f"Post-deletion LIST_FILES: {len(final_files)} recognized recording file(s)")

        if len(final_files) == 0:
            log("The HolterLEV recording inventory is empty.")
        else:
            log(f"*** WARNING: {len(final_files)} recognized file(s) remain: {final_files} ***")

    except ControllerError as e:
        log(f"FATAL: {e} (response={getattr(e, 'response', None)})")
        client.close()
        return 2

    try:
        client.send("disconnect", timeout=5)
    except ControllerError:
        pass
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
