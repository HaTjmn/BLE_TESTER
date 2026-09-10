"""
HolterLEV BLE wire-protocol constants and codec helpers.

Mirrors docs/ble/PROTOCOL.md and src/holter_ble_protocol.h in the
HolterLEV firmware repo (c:/repos/ECG_ads1293/holterlev_1293-BLE) --
kept in ONE place here so tools/holter_ble_test.py and
tools/holter_file_download.py never drift into two independently-typed
copies of the same wire format. If the firmware's own protocol changes,
update PROTOCOL.md there first, then mirror the change here.

This module has no dependency on HolterBLEClient -- it only encodes/
decodes bytes. Callers own the actual BLE traffic.
"""
import time

HLV_SERVICE_UUID = "7b8e0000-6a52-4f4d-9a33-71c02e6b4d90"
HLV_CONTROL_POINT_UUID = "7b8e0040-6a52-4f4d-9a33-71c02e6b4d90"
HLV_STATUS_UUID = "7b8e0041-6a52-4f4d-9a33-71c02e6b4d90"
HLV_DEVICE_INFO_UUID = "7b8e0043-6a52-4f4d-9a33-71c02e6b4d90"
HLV_COMMAND_RESPONSE_UUID = "7b8e0044-6a52-4f4d-9a33-71c02e6b4d90"
HLV_FILE_TRANSFER_DATA_UUID = "7b8e0046-6a52-4f4d-9a33-71c02e6b4d90"
HLV_DEVICE_NAME = "NOAMED"
HLV_DEV_ACCESS_CODE = b"2018"

HLV_OPCODE_AUTH = 0x0001
HLV_OPCODE_GET_STATUS = 0x0002
HLV_OPCODE_START = 0x0010
HLV_OPCODE_STOP = 0x0011
HLV_OPCODE_LIST_FILES = 0x0020
HLV_OPCODE_GET_FILE_INFO = 0x0021
HLV_OPCODE_START_FILE_DOWNLOAD = 0x0022
HLV_OPCODE_ABORT_FILE_DOWNLOAD = 0x0023
# BLE-D4.0J (docs/ble/PROTOCOL.md Section 18) -- two-phase delete-all-recordings.
HLV_OPCODE_PREPARE_DELETE_ALL_FILES = 0x0024
HLV_OPCODE_CONFIRM_DELETE_ALL_FILES = 0x0025
HLV_OPCODE_GET_DELETE_STATUS = 0x0026
# BLE-D4.0K -- diagnostic-only, compile-gated fallback (HOLTER_DIAG_TEST_FILE_GEN_ENABLED).
# Empirically required after the preferred UART0 console path proved unreachable on this
# hardware (write() to the console's COM port consistently timed out).
HLV_OPCODE_DIAG_GENERATE_TEST_FILE = 0x0027

HLV_RESULT_OK = 0
HLV_RESULT_UNAUTHORIZED = 2
HLV_RESULT_INVALID_STATE = 3
HLV_RESULT_INVALID_ARGUMENT = 4
HLV_RESULT_SD_NOT_READY = 5
HLV_RESULT_BUSY = 9
HLV_RESULT_TRANSACTION_CONFLICT = 13
HLV_RESULT_FILE_NOT_FOUND = 14
HLV_RESULT_TRANSFER_BUSY = 15
HLV_RESULT_TOKEN_INVALID = 16
HLV_RESULT_INVENTORY_MISMATCH = 17

HLV_DELETE_STATE_IDLE = 0
HLV_DELETE_STATE_PREPARED = 1
HLV_DELETE_STATE_DELETING = 2
HLV_DELETE_STATE_COMPLETE = 3
HLV_DELETE_STATE_FAILED = 4
HLV_DELETE_STATE_NAMES = {
    HLV_DELETE_STATE_IDLE: "IDLE",
    HLV_DELETE_STATE_PREPARED: "PREPARED",
    HLV_DELETE_STATE_DELETING: "DELETING",
    HLV_DELETE_STATE_COMPLETE: "COMPLETE",
    HLV_DELETE_STATE_FAILED: "FAILED",
}

HLV_SESSION_RECORDING = 3

HLV_RESPONSE_PHASE_ACCEPTED = 1
HLV_RESPONSE_PHASE_REJECTED = 2
HLV_RESPONSE_PHASE_COMPLETED = 3


def encode_hlv_message(opcode, txn_id, payload=b""):
    """Mirrors PROTOCOL.md Section 2 / holter_ble_protocol.h: 12-byte
    little-endian header + payload."""
    header = bytes([
        1, 0,  # protocol_major, protocol_minor
        opcode & 0xFF, (opcode >> 8) & 0xFF,
        txn_id & 0xFF, (txn_id >> 8) & 0xFF, (txn_id >> 16) & 0xFF, (txn_id >> 24) & 0xFF,
        0, 0,  # result_code (unused in a request)
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF,
    ])
    return header + payload


def decode_status_bytes(data_hex):
    """Mirrors PROTOCOL.md Section 6 -- the raw 14-byte Status payload."""
    data = bytes.fromhex(data_hex)
    return {
        "startup_status": data[0],
        "ads1293_ready": bool(data[1]),
        "sd_ready": bool(data[2]),
        "recording_capable": bool(data[3]),
        "session_state": data[4],
        "authorized": bool(data[5]),
        "last_command_opcode": data[6] | (data[7] << 8),
        "last_command_transaction_id": data[8] | (data[9] << 8) | (data[10] << 16) | (data[11] << 24),
        "last_command_result": data[12] | (data[13] << 8),
    }


def decode_command_response(data_hex):
    """Mirrors PROTOCOL.md Section 12.1 -- the Command Response payload
    (phase, resulting_session_state, inner_payload_length, inner_payload),
    delivered as the PAYLOAD of the shared 12-byte message header (Section
    2), so the header must be skipped first. Also surfaces the header's
    own opcode/transaction_id/result_code -- result_code (the actual
    holter_ble_result_t value) lives in the HEADER, not the phase-specific
    inner payload."""
    data = bytes.fromhex(data_hex)
    header = data[:12]
    payload = data[12:]
    inner_len = payload[2] | (payload[3] << 8)
    return {
        "opcode": header[2] | (header[3] << 8),
        "transaction_id": header[4] | (header[5] << 8) | (header[6] << 16) | (header[7] << 24),
        "result_code": header[8] | (header[9] << 8),
        "phase": payload[0],
        "resulting_session_state": payload[1],
        "inner": payload[4:4 + inner_len],
    }


def decode_list_files_response(inner):
    """Mirrors PROTOCOL.md Section 17.2's LIST_FILES response shape."""
    count_returned = inner[0]
    has_more = bool(inner[1])
    entries = []
    pos = 2
    for _ in range(count_returned):
        file_index = inner[pos] | (inner[pos + 1] << 8)
        size_bytes = inner[pos + 2] | (inner[pos + 3] << 8) | (inner[pos + 4] << 16) | (inner[pos + 5] << 24)
        entries.append({"file_index": file_index, "size_bytes": size_bytes})
        pos += 6
    return {"count_returned": count_returned, "has_more": has_more, "entries": entries}


def decode_get_file_info_response(inner):
    """Mirrors PROTOCOL.md Section 17.2's GET_FILE_INFO response shape."""
    exists = bool(inner[0])
    size_bytes = inner[1] | (inner[2] << 8) | (inner[3] << 16) | (inner[4] << 24)
    return {"exists": exists, "size_bytes": size_bytes}


def decode_prepare_delete_all_files_response(inner):
    """Mirrors PROTOCOL.md Section 18.1's PREPARE_DELETE_ALL_FILES response
    shape (21 bytes): token(8) + file_count(4) + total_bytes(4) +
    expires_in_ms(4) + operation_type(1)."""
    token = bytes(inner[0:8])
    file_count = inner[8] | (inner[9] << 8) | (inner[10] << 16) | (inner[11] << 24)
    total_bytes = inner[12] | (inner[13] << 8) | (inner[14] << 16) | (inner[15] << 24)
    expires_in_ms = inner[16] | (inner[17] << 8) | (inner[18] << 16) | (inner[19] << 24)
    operation_type = inner[20]
    return {
        "token": token,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "expires_in_ms": expires_in_ms,
        "operation_type": operation_type,
    }


def encode_confirm_delete_all_files_request(token, expected_file_count, expected_total_bytes):
    """Mirrors PROTOCOL.md Section 18.1's CONFIRM_DELETE_ALL_FILES request
    shape (16 bytes): token(8) + expected_file_count(4) + expected_total_bytes(4)."""
    if len(token) != 8:
        raise ValueError(f"token must be exactly 8 bytes, got {len(token)}")
    return (
        bytes(token)
        + expected_file_count.to_bytes(4, "little")
        + expected_total_bytes.to_bytes(4, "little")
    )


def decode_get_delete_status_response(inner):
    """Mirrors PROTOCOL.md Section 18.1's GET_DELETE_STATUS response shape
    (56 bytes)."""
    state = inner[0]
    files_planned = inner[1] | (inner[2] << 8) | (inner[3] << 16) | (inner[4] << 24)
    bytes_planned = inner[5] | (inner[6] << 8) | (inner[7] << 16) | (inner[8] << 24)
    files_deleted = inner[9] | (inner[10] << 8) | (inner[11] << 16) | (inner[12] << 24)
    bytes_deleted = inner[13] | (inner[14] << 8) | (inner[15] << 16) | (inner[16] << 24)
    failures = inner[17] | (inner[18] << 8) | (inner[19] << 16) | (inner[20] << 24)
    first_error_code = inner[21] | (inner[22] << 8)
    first_failed_path_len = inner[23]
    first_failed_path_bytes = bytes(inner[24:24 + first_failed_path_len])
    return {
        "state": state,
        "state_name": HLV_DELETE_STATE_NAMES.get(state, f"UNKNOWN_{state}"),
        "files_planned": files_planned,
        "bytes_planned": bytes_planned,
        "files_deleted": files_deleted,
        "bytes_deleted": bytes_deleted,
        "failures": failures,
        "first_error_code": first_error_code,
        "first_failed_path": first_failed_path_bytes.decode("utf-8", errors="replace"),
    }


def encode_diag_generate_test_file_request(record_count, frames_per_record):
    """BLE-D4.0K: mirrors HOLTER_BLE_DIAG_GENERATE_TEST_FILE_REQUEST_LEN (3
    bytes): record_count(1) + frames_per_record(2)."""
    return bytes([record_count & 0xFF]) + frames_per_record.to_bytes(2, "little")


def decode_diag_generate_test_file_response(inner):
    """BLE-D4.0K: mirrors the 43-byte DIAG_GENERATE_TEST_FILE response:
    error(1) + records_written(4) + bytes_written(4) + is_valid(1) +
    sha256_valid(1) + sha256(32)."""
    error = inner[0]
    records_written = inner[1] | (inner[2] << 8) | (inner[3] << 16) | (inner[4] << 24)
    bytes_written = inner[5] | (inner[6] << 8) | (inner[7] << 16) | (inner[8] << 24)
    is_valid = bool(inner[9])
    sha256_valid = bool(inner[10])
    sha256 = bytes(inner[11:11 + 32])
    return {
        "error": error,
        "records_written": records_written,
        "bytes_written": bytes_written,
        "is_valid": is_valid,
        "sha256_valid": sha256_valid,
        "sha256_hex": sha256.hex(),
    }


def decode_file_chunk_header(data_hex):
    """Mirrors PROTOCOL.md Section 17.3 -- the File Transfer Data chunk
    header (independent of the Section 2 message header)."""
    data = bytes.fromhex(data_hex)
    transfer_id = data[0] | (data[1] << 8)
    chunk_offset = data[2] | (data[3] << 8) | (data[4] << 16) | (data[5] << 24)
    total_length = data[6] | (data[7] << 8) | (data[8] << 16) | (data[9] << 24)
    fragment_length = data[10] | (data[11] << 8)
    fragment = data[12:12 + fragment_length]
    return {
        "transfer_id": transfer_id,
        "chunk_offset": chunk_offset,
        "total_length": total_length,
        "fragment_length": fragment_length,
        "fragment": fragment,
    }


def poll_status_until(client, predicate, timeout=8, interval=0.3):
    """Repeatedly reads the raw Status characteristic until `predicate(status_dict)`
    is true. Never trusts a single read -- PROTOCOL.md Section 16 explicitly
    documents Status timing as unsynchronized with command completion."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status_raw = client.send("read", timeout=6, characteristic=HLV_STATUS_UUID)
        last = decode_status_bytes(status_raw["data_hex"])
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"status never satisfied predicate within {timeout}s; last={last}")


def wait_for_command_response(client, transaction_id, window_s):
    """Drains 'notification' events on Command Response for `window_s` seconds looking for the
    one matching `transaction_id`. Returns the decoded response dict, or None on timeout."""
    deadline = time.time() + window_s
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        ev = client.wait_for_event("notification", timeout=remaining)
        if ev is None:
            return None
        if ev.get("characteristic") == HLV_COMMAND_RESPONSE_UUID:
            decoded = decode_command_response(ev["data_hex"])
            if decoded["transaction_id"] == transaction_id:
                return decoded


def authenticate_raw(client, base_txn, code=HLV_DEV_ACCESS_CODE):
    """Raw AUTH via Control Point write + Status poll (precise transaction_id control).
    Returns the Status snapshot once the AUTH command has settled (check ["authorized"])."""
    auth_msg = encode_hlv_message(HLV_OPCODE_AUTH, base_txn, code)
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=auth_msg.hex())
    return poll_status_until(client, lambda st: st["last_command_transaction_id"] == base_txn, timeout=5)
