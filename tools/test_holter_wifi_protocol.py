#!/usr/bin/env python3
"""
HLV-WIFI-20261006-008 -- offline tests for tools/holter_wifi_protocol.py,
the credential redaction in holter_ble_client.py and the request path of
tools/holter_wifi_manage.py. No COM port, no BLE_TESTER, no HolterLEV.
Dummy values only. Run: python tools/test_holter_wifi_protocol.py -v

The GOLDEN_* vectors were produced by the HolterLEV firmware's own codec
(src/holter_ble_wifi_codec.c, compiled on the host) -- so these tests check
byte-for-byte agreement with the firmware, not with a re-derivation.
"""
import io
import json
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import holter_wifi_protocol as w  # noqa: E402
import holter_ble_client  # noqa: E402
import holter_wifi_manage as cli  # noqa: E402

GOLDEN_LIST_SNAPSHOT = bytes.fromhex(
    "0101030100001e001c490207000000000103064c61624e657404030201010000074f70656e2d4150")
GOLDEN_LIST_INNER = bytes.fromhex("1e001c49")
GOLDEN_STATUS_SNAPSHOT = bytes.fromhex(
    "010204010000300061d805010203020000000300000004000000020301000b000000030000000200"
    "0000e0930400ec910400c0a8010a09000000")
GOLDEN_CAPS_INNER = bytes.fromhex("02050f05")   # protocol v2 (HLV-WIFI-20261009-013)
# HLV-WIFI-20261009-013: the firmware codec's own answers -- (opcode, auth, encrypted, policy, result) for
# holter_ble_wifi_admit() with a well-formed request per opcode, and (auth, current, encrypted, policy,
# att) for holter_ble_wifi_snapshot_access().
GOLDEN_ADMISSION = [(0x30,0,0,0,2),(0x30,0,0,1,2),(0x30,0,1,0,2),(0x30,0,1,1,2),(0x30,1,0,0,0),(0x30,1,0,1,0),(0x30,1,1,0,0),(0x30,1,1,1,0),(0x31,0,0,0,2),(0x31,0,0,1,2),(0x31,0,1,0,2),(0x31,0,1,1,2),(0x31,1,0,0,0),(0x31,1,0,1,18),(0x31,1,1,0,0),(0x31,1,1,1,0),(0x32,0,0,0,2),(0x32,0,0,1,2),(0x32,0,1,0,2),(0x32,0,1,1,2),(0x32,1,0,0,0),(0x32,1,0,1,18),(0x32,1,1,0,0),(0x32,1,1,1,0),(0x33,0,0,0,2),(0x33,0,0,1,2),(0x33,0,1,0,2),(0x33,0,1,1,2),(0x33,1,0,0,0),(0x33,1,0,1,18),(0x33,1,1,0,0),(0x33,1,1,1,0),(0x34,0,0,0,2),(0x34,0,0,1,2),(0x34,0,1,0,2),(0x34,0,1,1,2),(0x34,1,0,0,0),(0x34,1,0,1,18),(0x34,1,1,0,0),(0x34,1,1,1,0),(0x35,0,0,0,2),(0x35,0,0,1,2),(0x35,0,1,0,2),(0x35,0,1,1,2),(0x35,1,0,0,0),(0x35,1,0,1,18),(0x35,1,1,0,0),(0x35,1,1,1,0),(0x36,0,0,0,2),(0x36,0,0,1,2),(0x36,0,1,0,2),(0x36,0,1,1,2),(0x36,1,0,0,0),(0x36,1,0,1,18),(0x36,1,1,0,0),(0x36,1,1,1,0),(0x37,0,0,0,2),(0x37,0,0,1,2),(0x37,0,1,0,2),(0x37,0,1,1,2),(0x37,1,0,0,0),(0x37,1,0,1,18),(0x37,1,1,0,0),(0x37,1,1,1,0),(0x38,0,0,0,2),(0x38,0,0,1,2),(0x38,0,1,0,2),(0x38,0,1,1,2),(0x38,1,0,0,0),(0x38,1,0,1,18),(0x38,1,1,0,0),(0x38,1,1,1,0),(0x39,0,0,0,2),(0x39,0,0,1,2),(0x39,0,1,0,2),(0x39,0,1,1,2),(0x39,1,0,0,0),(0x39,1,0,1,18),(0x39,1,1,0,0),(0x39,1,1,1,0)]
GOLDEN_SNAPSHOT_ACCESS = [(0,0,0,0,8),(0,0,0,1,15),(0,0,1,0,8),(0,0,1,1,8),(0,1,0,0,8),(0,1,0,1,15),(0,1,1,0,8),(0,1,1,1,8),(1,0,0,0,8),(1,0,0,1,15),(1,0,1,0,8),(1,0,1,1,8),(1,1,0,0,0),(1,1,0,1,15),(1,1,1,0,0),(1,1,1,1,0)]
GOLDEN_ADD_OK_INNER = bytes.fromhex("01000000")
GOLDEN_ADD_FULL_INNER = bytes.fromhex("05020000")
# docs/ble/PROTOCOL.md 19.9 (admitted as OK by the firmware codec)
DOC_ADD_REQUEST = bytes.fromhex(
    "010032000101000000001600" "01060c00" "4c61624e6574" "64756d6d792d706173732d31")
DUMMY_PASSWORD = b"dummy-pass-1"


class CodecTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(w.crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_add_request_matches_protocol_example(self):
        payload = w.encode_wifi_add_payload(b"LabNet", DUMMY_PASSWORD, w.WIFI_AUTH["wpa2"])
        self.assertEqual(w.encode_wifi_request(w.HLV_OPCODE_WIFI_ADD, 0x101, payload), DOC_ADD_REQUEST)

    def test_max_add_is_112_bytes(self):
        payload = w.encode_wifi_add_payload(b"S" * 32, b"0123456789abcdef" * 4, 1)
        self.assertEqual(len(w.encode_wifi_request(w.HLV_OPCODE_WIFI_ADD, 1, payload)), 112)

    def test_update_layout(self):
        p = w.encode_wifi_update_payload(3, b"dummy-new-pw3", 2)
        self.assertEqual(p[:8], bytes([3, 0, 0, 0, 2, 13, 0, 0]))
        self.assertEqual(len(p), 8 + 13)

    def test_invalid_inputs_never_echo_password(self):
        for bad in (b"x" * 65, b"dummy\x01pass"):
            with self.assertRaises(ValueError) as cm:
                w.encode_wifi_add_payload(b"LabNet", bad, 1)
            self.assertNotIn("dummy", str(cm.exception))
            self.assertNotIn("xxxx", str(cm.exception))
        with self.assertRaises(ValueError):
            w.encode_wifi_add_payload(b"", DUMMY_PASSWORD, 1)
        with self.assertRaises(ValueError):
            w.encode_wifi_add_payload(b"S" * 33, DUMMY_PASSWORD, 1)
        with self.assertRaises(ValueError):
            w.encode_wifi_update_payload(0, DUMMY_PASSWORD, 1)

    def test_describe_request_redacts_credentials(self):
        text = w.describe_request(DOC_ADD_REQUEST)
        self.assertIn("22 bytes, credential redacted", text)
        self.assertNotIn("dummy", text)
        self.assertNotIn(DUMMY_PASSWORD.hex(), text)
        delete = w.encode_wifi_request(w.HLV_OPCODE_WIFI_DELETE, 5, w.encode_wifi_id_payload(2))
        self.assertIn("payload=02000000", w.describe_request(delete))

    def test_capabilities_golden(self):
        caps = w.decode_capabilities(GOLDEN_CAPS_INNER)
        self.assertEqual((caps["version"], caps["max_networks"], caps["auth_mask"]), (w.WIFI_BLE_VERSION, 5, 0x0F))
        self.assertEqual(w.WIFI_BLE_VERSION, 2)
        self.assertTrue(caps["requires_encryption"] and caps["snapshot_char"])
        self.assertFalse(caps["cred_encrypted_at_rest"])

    def test_completed_inner_golden(self):
        self.assertEqual(w.decode_wifi_completed(w.HLV_OPCODE_WIFI_ADD, 0, GOLDEN_ADD_OK_INNER), {"ok": True, "id": 1})
        r = w.decode_wifi_completed(w.HLV_OPCODE_WIFI_ADD, w.HLV_RESULT_WIFI_REJECTED, GOLDEN_ADD_FULL_INNER)
        self.assertEqual((r["ok"], r["wifi_result_name"], r["state_name"]), (False, "FULL", "CONNECTING"))

    def test_list_snapshot_golden(self):
        ref = w.decode_snapshot_ref(GOLDEN_LIST_INNER)
        snap = w.decode_snapshot(GOLDEN_LIST_SNAPSHOT, expect_txn=0x103, expect_ref=ref)
        self.assertEqual(snap["kind"], "list")
        a, b = snap["networks"]
        self.assertEqual((a["id"], a["ssid"], a["auth_name"], a["preferred"], a["has_password"], a["order"]),
                         (7, b"LabNet", "wpa2", True, True, 0))
        self.assertEqual((b["id"], b["ssid"], b["auth_name"], b["preferred"], b["has_password"], b["order"]),
                         (0x01020304, b"Open-AP", "open", False, False, 1))

    def test_status_snapshot_golden(self):
        st = w.decode_snapshot(GOLDEN_STATUS_SNAPSHOT, expect_txn=0x104)
        self.assertEqual(st["state_name"], "BACKOFF")
        self.assertEqual((st["configured"], st["preferred_id"], st["current_id"], st["round"]), (3, 2, 3, 4))
        self.assertEqual((st["round_pos"], st["round_size"], st["last_failure"], st["attempts_total"]), (2, 3, 1, 11))
        self.assertEqual((st["backoff_step"], st["backoff_delay_ms"], st["next_retry_in_ms"]), (2, 300000, 299500))
        self.assertTrue(st["associated"] and not st["ip_ready"] and not st["storage_locked"])
        self.assertEqual((st["ip4"], st["stale_events"]), ("192.168.1.10", 9))

    def test_snapshot_integrity_checks(self):
        tampered = bytearray(GOLDEN_LIST_SNAPSHOT)
        tampered[-1] ^= 0x01
        with self.assertRaises(ValueError):
            w.decode_snapshot(bytes(tampered))
        with self.assertRaises(ValueError):
            w.decode_snapshot(GOLDEN_LIST_SNAPSHOT, expect_txn=0x999)
        with self.assertRaises(ValueError):
            w.decode_snapshot(GOLDEN_LIST_SNAPSHOT[:-1])
        with self.assertRaises(ValueError):
            w.decode_snapshot(GOLDEN_LIST_SNAPSHOT, expect_ref=(30, 0))
        self.assertEqual(w.decode_snapshot(bytes([1, 0, 0, 0, 0, 0, 0, 0, 0, 0])), {"kind": "none"})


class PolicyTests(unittest.TestCase):
    """HLV-WIFI-20261009-013 (F2): the client's policy mirror equals the firmware codec, cell for cell."""

    def test_admission_matrix_matches_firmware(self):
        self.assertEqual(len(GOLDEN_ADMISSION), 80)
        for op, auth, enc, pol, fw in GOLDEN_ADMISSION:
            expect = w.expected_admission(0x0000 | op, bool(auth), bool(enc), bool(pol))
            self.assertEqual(fw, 0 if expect is None else expect, (hex(op), auth, enc, pol))

    def test_only_capabilities_is_exempt(self):
        exempt = {op for op in range(0x30, 0x3A) if not w.requires_encrypted_link(op)}
        self.assertEqual(exempt, {w.HLV_OPCODE_WIFI_GET_CAPABILITIES})
        self.assertFalse(any(w.requires_encrypted_link(op, policy_on=False) for op in range(0x30, 0x3A)))

    def test_snapshot_access_matches_firmware(self):
        self.assertEqual(len(GOLDEN_SNAPSHOT_ACCESS), 16)
        for auth, cur, enc, pol, fw in GOLDEN_SNAPSHOT_ACCESS:
            expect = w.expected_snapshot_att_error(bool(auth), bool(cur), bool(enc), bool(pol))
            self.assertEqual(fw, 0 if expect is None else expect, (auth, cur, enc, pol))
        self.assertEqual((w.ATT_INSUFFICIENT_ENCRYPTION, w.ATT_INSUFFICIENT_AUTHORIZATION), (0x0F, 0x08))


class FakeSerial:
    def __init__(self, *a, **k):
        self.written = []

    def read(self, n):
        import time
        time.sleep(0.01)
        return b""

    def write(self, data):
        self.written.append(data)

    def close(self):
        pass


class RedactionTests(unittest.TestCase):
    def test_sensitive_send_logs_length_only(self):
        log = io.StringIO()
        with mock.patch.object(holter_ble_client.serial, "Serial", FakeSerial):
            client = holter_ble_client.HolterBLEClient("FAKE", default_timeout=0.2, log_file=log)
            try:
                with self.assertRaises(holter_ble_client.ControllerError) as cm:
                    client.send("write", sensitive=True, characteristic="x", data_hex=DOC_ADD_REQUEST.hex())
                wire = b"".join(client._ser.written)
            finally:
                client.close()
        self.assertIn(DOC_ADD_REQUEST.hex().encode(), wire)        # the device still receives it
        self.assertNotIn(DUMMY_PASSWORD.hex(), log.getvalue())     # the log never holds it
        self.assertIn("<redacted 34 bytes>", log.getvalue())
        self.assertNotIn(DUMMY_PASSWORD.hex(), json.dumps(cm.exception.request))
        self.assertNotIn(DUMMY_PASSWORD.hex(), str(cm.exception))

    def test_non_sensitive_send_is_unchanged(self):
        log = io.StringIO()
        with mock.patch.object(holter_ble_client.serial, "Serial", FakeSerial):
            client = holter_ble_client.HolterBLEClient("FAKE", default_timeout=0.1, log_file=log)
            try:
                with self.assertRaises(holter_ble_client.ControllerError):
                    client.send("write", characteristic="x", data_hex="abcd")
            finally:
                client.close()
        self.assertIn('"data_hex": "abcd"', log.getvalue())


class ManageToolTests(unittest.TestCase):
    def test_snapshot_att_error_is_reported(self):
        client = mock.Mock()
        client.send.side_effect = holter_ble_client.ControllerError(
            "READ_FAILED", "GATT read error", response={"status": "error", "gatt_status": 0x10F, "att_error": 0x0F})
        self.assertEqual(cli.WifiSession(client).snapshot_att_error(), 0x0F)
        client.send.side_effect = None
        client.send.return_value = {"status": "ok", "data_hex": "01000000000000000000"}
        self.assertIsNone(cli.WifiSession(client).snapshot_att_error())

    def test_plaintext_matrix_covers_every_opcode(self):
        ops = [op for op, _ in cli.plaintext_matrix_requests()]
        self.assertEqual(sorted(ops), list(range(0x30, 0x3A)))

    def test_send_raw_marks_credential_writes_sensitive(self):
        client = mock.Mock()
        s = cli.WifiSession(client)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            s.send_raw(DOC_ADD_REQUEST)
            s.send_raw(w.encode_wifi_request(w.HLV_OPCODE_WIFI_LIST, 9))
        kinds = [c.kwargs["sensitive"] for c in client.send.call_args_list]
        self.assertEqual(kinds, [True, False])
        self.assertNotIn("dummy", out.getvalue())
        self.assertNotIn(DUMMY_PASSWORD.hex(), out.getvalue())

    def test_password_never_from_argv(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", io.StringIO()):
                cli.main(["--port", "X", "add", "--ssid", "LabNet", "--password", "dummy-pass-1"])

    def test_request_waits_past_accepted(self):
        client = mock.Mock()
        s = cli.WifiSession(client)
        txn = s.txn + 1

        def note(phase, result, inner=b""):
            body = bytes([phase, 0xFF]) + struct.pack("<H", len(inner)) + inner
            hdr = bytes([1, 0, 0x32, 0]) + struct.pack("<IHH", txn, result, len(body))
            return {"event": "notification", "characteristic": cli.HLV_COMMAND_RESPONSE_UUID,
                    "data_hex": (hdr + body).hex()}
        client.wait_for_event.side_effect = [note(1, 12), note(3, 0, GOLDEN_ADD_OK_INNER)]
        with mock.patch("sys.stdout", io.StringIO()):
            cr = s.request(w.HLV_OPCODE_WIFI_ADD, w.encode_wifi_add_payload(b"LabNet", DUMMY_PASSWORD, 1))
        self.assertTrue(cr["accepted_seen"])
        self.assertEqual(w.decode_wifi_completed(w.HLV_OPCODE_WIFI_ADD, cr["result_code"], cr["inner"])["id"], 1)


if __name__ == "__main__":
    unittest.main()
