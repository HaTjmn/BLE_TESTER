#!/usr/bin/env python3
"""
HLV-WIFI-20261006-008 (SCRUM-31 / SCRUM-20) -- client codec for HolterLEV's
Wi-Fi network management over BLE: docs/ble/PROTOCOL.md Section 19 of the
HolterLEV repository (ble-wifi-dev firmware only).

Pure functions, no I/O. Mirrors src/holter_ble_wifi_codec.c byte for byte.
Never formats a password into an exception message, repr or log string.
"""
import struct

from holter_hlv_protocol import encode_hlv_message

HLV_WIFI_SNAPSHOT_UUID = "7b8e0047-6a52-4f4d-9a33-71c02e6b4d90"

HLV_OPCODE_WIFI_GET_CAPABILITIES = 0x0030
HLV_OPCODE_WIFI_LIST = 0x0031
HLV_OPCODE_WIFI_ADD = 0x0032
HLV_OPCODE_WIFI_UPDATE = 0x0033
HLV_OPCODE_WIFI_DELETE = 0x0034
HLV_OPCODE_WIFI_CLEAR_ALL = 0x0035
HLV_OPCODE_WIFI_SET_PREFERRED = 0x0036
HLV_OPCODE_WIFI_GET_STATUS = 0x0037
HLV_OPCODE_WIFI_START = 0x0038
HLV_OPCODE_WIFI_STOP = 0x0039

HLV_WIFI_ASYNC_OPCODES = frozenset({
    HLV_OPCODE_WIFI_ADD, HLV_OPCODE_WIFI_UPDATE, HLV_OPCODE_WIFI_DELETE, HLV_OPCODE_WIFI_CLEAR_ALL,
    HLV_OPCODE_WIFI_SET_PREFERRED, HLV_OPCODE_WIFI_START, HLV_OPCODE_WIFI_STOP,
})
HLV_WIFI_CREDENTIAL_OPCODES = frozenset({HLV_OPCODE_WIFI_ADD, HLV_OPCODE_WIFI_UPDATE})

HLV_RESULT_NOT_SUPPORTED = 10
HLV_RESULT_STORAGE_ERROR = 8
HLV_RESULT_ACCEPTED = 12
HLV_RESULT_ENCRYPTION_REQUIRED = 18
HLV_RESULT_WIFI_REJECTED = 19

# 2 (HLV-WIFI-20261009-013): with the device's policy on, every Wi-Fi opcode except
# WIFI_GET_CAPABILITIES (the discovery exception) needs AUTH and an encrypted link, and every
# Snapshot read needs an encrypted link and AUTH (v1: encryption for ADD / UPDATE only).
WIFI_BLE_VERSION = 2
WIFI_MAX_NETWORKS = 5
WIFI_SSID_MAX = 32
WIFI_PASS_MAX = 64
WIFI_CLEAR_CONFIRM = b"WCLR"

WIFI_AUTH = {"open": 0, "wpa2": 1, "wpa3": 2, "wpa2wpa3": 3}
WIFI_AUTH_NAMES = {v: k for k, v in WIFI_AUTH.items()}
WIFI_RESULT_NAMES = ["OK", "INVALID_SSID", "INVALID_PASSWORD", "UNSUPPORTED_AUTH", "DUPLICATE_SSID", "FULL",
                     "NOT_FOUND", "STORAGE_LOCKED", "SAVE_FAILED", "INVALID_ARG"]
WIFI_STATE_NAMES = ["STOPPED", "NO_NETWORKS", "CONNECTING", "WAITING_IP", "CONNECTED", "BACKOFF"]

CAP_REQUIRES_ENCRYPTION = 0x01
# ATT error codes the Wi-Fi Snapshot read can return (Core spec Vol 3 Part F 3.4.1.1).
ATT_INSUFFICIENT_AUTHORIZATION = 0x08
ATT_INSUFFICIENT_ENCRYPTION = 0x0F
CAP_CRED_ENCRYPTED_AT_REST = 0x02
CAP_SNAPSHOT_CHAR = 0x04

SNAPSHOT_HEADER_LEN = 10
SNAPSHOT_KIND_NONE = 0
SNAPSHOT_KIND_LIST = 1
SNAPSHOT_KIND_STATUS = 2
STATUS_BODY_LEN = 48


def _name(table, value):
    return table[value] if 0 <= value < len(table) else f"UNKNOWN({value})"


def _check_password(password):
    """Mirrors the firmware's wire rule: 0..64 printable ASCII bytes. The
    error text never contains the password itself."""
    if not isinstance(password, (bytes, bytearray)):
        raise TypeError("password must be bytes")
    if len(password) > WIFI_PASS_MAX:
        raise ValueError(f"password too long ({len(password)} bytes, max {WIFI_PASS_MAX})")
    if any(c < 0x20 or c > 0x7E for c in password):
        raise ValueError("password contains a non-printable byte")


def requires_encrypted_link(opcode, policy_on=True):
    """Mirrors the firmware admission (PROTOCOL.md 19.2, version 2): every Wi-Fi opcode except
    WIFI_GET_CAPABILITIES needs an encrypted link when the policy is on."""
    return policy_on and opcode != HLV_OPCODE_WIFI_GET_CAPABILITIES


def expected_admission(opcode, authorized, encrypted, policy_on=True):
    """The BLE result the firmware gives before decoding anything: UNAUTHORIZED (2),
    ENCRYPTION_REQUIRED (18), or None (admitted)."""
    if not authorized:
        return 2
    if requires_encrypted_link(opcode, policy_on) and not encrypted:
        return HLV_RESULT_ENCRYPTION_REQUIRED
    return None


def expected_snapshot_att_error(authorized, current_connection, encrypted, policy_on=True):
    """Mirrors holter_ble_wifi_snapshot_access(): 0x0F before 0x08, None = readable."""
    if policy_on and not encrypted:
        return ATT_INSUFFICIENT_ENCRYPTION
    if not authorized or not current_connection:
        return ATT_INSUFFICIENT_AUTHORIZATION
    return None


def encode_wifi_add_payload(ssid, password, auth):
    if not isinstance(ssid, (bytes, bytearray)) or not 1 <= len(ssid) <= WIFI_SSID_MAX:
        raise ValueError(f"ssid must be 1..{WIFI_SSID_MAX} bytes")
    _check_password(password)
    return bytes([auth & 0xFF, len(ssid), len(password), 0]) + bytes(ssid) + bytes(password)


def encode_wifi_update_payload(net_id, password, auth):
    if not 1 <= net_id <= 0xFFFFFFFF:
        raise ValueError("network id must be 1..2^32-1")
    _check_password(password)
    return struct.pack("<IBBH", net_id, auth & 0xFF, len(password), 0) + bytes(password)


def encode_wifi_id_payload(net_id):
    return struct.pack("<I", net_id)


def encode_wifi_request(opcode, txn, payload=b""):
    """The full Control Point message (12-byte header + payload)."""
    return encode_hlv_message(opcode, txn, payload)


def describe_request(message):
    """Log-safe description of a Control Point message: the header fields and,
    for ADD / UPDATE, only the payload LENGTH -- never the password."""
    opcode = message[2] | (message[3] << 8)
    txn = struct.unpack_from("<I", message, 4)[0]
    plen = message[10] | (message[11] << 8)
    if opcode in HLV_WIFI_CREDENTIAL_OPCODES:
        return f"opcode=0x{opcode:04x} txn={txn} payload=<{plen} bytes, credential redacted>"
    return f"opcode=0x{opcode:04x} txn={txn} payload={message[12:].hex()}"


def decode_capabilities(inner):
    if len(inner) != 4:
        raise ValueError("capabilities inner payload must be 4 bytes")
    return {
        "version": inner[0],
        "max_networks": inner[1],
        "auth_mask": inner[2],
        "requires_encryption": bool(inner[3] & CAP_REQUIRES_ENCRYPTION),
        "cred_encrypted_at_rest": bool(inner[3] & CAP_CRED_ENCRYPTED_AT_REST),
        "snapshot_char": bool(inner[3] & CAP_SNAPSHOT_CHAR),
    }


def decode_wifi_completed(opcode, result_code, inner):
    """Decodes the 4-byte COMPLETED inner payload of an async Wi-Fi request."""
    if len(inner) != 4:
        raise ValueError("Wi-Fi COMPLETED inner payload must be 4 bytes")
    if opcode == HLV_OPCODE_WIFI_ADD and result_code == 0:
        return {"ok": True, "id": struct.unpack("<I", inner)[0]}
    return {
        "ok": result_code == 0 and inner[0] == 0,
        "wifi_result": inner[0], "wifi_result_name": _name(WIFI_RESULT_NAMES, inner[0]),
        "state": inner[1], "state_name": _name(WIFI_STATE_NAMES, inner[1]),
        "platform_error_low": inner[2],
    }


def decode_snapshot_ref(inner):
    """LIST / GET_STATUS COMPLETED inner: (body_len, crc16) of the published snapshot."""
    if len(inner) != 4:
        raise ValueError("snapshot reference must be 4 bytes")
    return struct.unpack("<HH", inner)


def crc16_ccitt_false(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def decode_snapshot(value, expect_txn=None, expect_ref=None):
    """Decodes a Wi-Fi Snapshot characteristic value and verifies its CRC
    (and, if given, the producing transaction id and the COMPLETED's
    (body_len, crc16)). Raises ValueError on any mismatch."""
    if len(value) < SNAPSHOT_HEADER_LEN:
        raise ValueError("snapshot shorter than its header")
    version, kind, txn, body_len, crc = struct.unpack_from("<BBIHH", value, 0)
    if version != 1:
        raise ValueError(f"unknown snapshot version {version}")
    if kind == SNAPSHOT_KIND_NONE:
        return {"kind": "none"}
    body = bytes(value[SNAPSHOT_HEADER_LEN:SNAPSHOT_HEADER_LEN + body_len])
    if len(body) != body_len or len(value) != SNAPSHOT_HEADER_LEN + body_len:
        raise ValueError("snapshot length mismatch")
    if crc16_ccitt_false(body) != crc:
        raise ValueError("snapshot CRC mismatch")
    if expect_txn is not None and txn != expect_txn:
        raise ValueError(f"snapshot belongs to txn {txn}, expected {expect_txn} (replaced by a newer request)")
    if expect_ref is not None and tuple(expect_ref) != (body_len, crc):
        raise ValueError("snapshot does not match the COMPLETED reference")
    if kind == SNAPSHOT_KIND_LIST:
        count = body[0]
        nets, k = [], 1
        for _ in range(count):
            net_id, order, auth, flags, ssid_len = struct.unpack_from("<IBBBB", body, k)
            k += 8
            ssid = body[k:k + ssid_len]
            k += ssid_len
            nets.append({"id": net_id, "order": order, "auth": auth, "auth_name": WIFI_AUTH_NAMES.get(auth, "?"),
                         "preferred": bool(flags & 1), "has_password": bool(flags & 2), "ssid": ssid})
        if k != len(body):
            raise ValueError("LIST snapshot has trailing bytes")
        return {"kind": "list", "txn": txn, "networks": nets}
    if kind == SNAPSHOT_KIND_STATUS:
        if body_len != STATUS_BODY_LEN:
            raise ValueError("STATUS snapshot body must be 48 bytes")
        f = struct.unpack_from("<BBBBIIIBBBBIIIIIII", body, 0)
        return {
            "kind": "status", "txn": txn,
            "state": f[0], "state_name": _name(WIFI_STATE_NAMES, f[0]), "load_status": f[1],
            "storage_locked": bool(f[2] & 1), "associated": bool(f[2] & 2), "ip_ready": bool(f[2] & 4),
            "configured": f[3], "preferred_id": f[4], "current_id": f[5], "round": f[6],
            "round_pos": f[7], "round_size": f[8], "last_failure": f[9],
            "attempts_total": f[11], "last_failure_id": f[12], "backoff_step": f[13], "backoff_delay_ms": f[14],
            "next_retry_in_ms": f[15], "ip4": ".".join(str(b) for b in struct.pack("<I", f[16])),
            "stale_events": f[17],
        }
    raise ValueError(f"unknown snapshot kind {kind}")
