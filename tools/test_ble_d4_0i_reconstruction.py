#!/usr/bin/env python3
"""
BLE-D4.0I Phase 6 -- deterministic, offline tests for the production
reconstruction (file_reconstructor.py) and serial-framing
(holter_ble_client.NdjsonLineFramer) logic. No hardware, no BLE_TESTER,
no HolterLEV required -- pure logic against synthetic chunk data.

Run: python tools/test_ble_d4_0i_reconstruction.py -v
"""
import hashlib
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from file_reconstructor import FileReconstructor, FileDownloadSession, ReconstructionError
from holter_ble_client import NdjsonLineFramer


def chunks_of(data, size):
    """Yields (offset, fragment) pairs covering `data` in order."""
    for i in range(0, len(data), size):
        yield i, data[i:i + size]


class Test01SequentialChunks(unittest.TestCase):
    def test_sequential_chunks_reconstruct_exactly(self):
        data = bytes(random.Random(1).randbytes(10000)) if hasattr(random.Random(1), "randbytes") \
            else bytes(random.Random(1).getrandbits(8) for _ in range(10000))
        r = FileReconstructor(len(data))
        for off, frag in chunks_of(data, 240):
            outcome = r.ingest(off, frag)
            self.assertEqual(outcome["outcome"], "accepted")
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)
        self.assertEqual(r.canonical_sha256(), hashlib.sha256(data).hexdigest())
        self.assertEqual(r.conflicting_duplicate_bytes, 0)
        self.assertEqual(r.identical_duplicate_bytes, 0)
        self.assertEqual(r.out_of_range_chunks, 0)


class Test02FinalShortChunk(unittest.TestCase):
    def test_final_chunk_shorter_than_the_rest(self):
        data = bytes(range(256)) * 4 + b"\xAB" * 37  # 1061 bytes, last fragment shorter than 240
        r = FileReconstructor(len(data))
        for off, frag in chunks_of(data, 240):
            r.ingest(off, frag)
        self.assertTrue(r.is_complete())
        self.assertEqual(len(r.canonical_bytes()), len(data))
        self.assertEqual(r.canonical_bytes(), data)


class Test03Gap(unittest.TestCase):
    def test_gap_leaves_a_detected_missing_range(self):
        data = b"X" * 1000
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:100])
        r.ingest(200, data[200:300])  # skip [100,200) -- a gap
        self.assertFalse(r.is_complete())
        ranges = r.missing_ranges()
        self.assertIn((100, 100), ranges)
        with self.assertRaises(ReconstructionError):
            r.canonical_bytes()


class Test04IdenticalDuplicate(unittest.TestCase):
    def test_identical_duplicate_counted_not_conflicting(self):
        data = b"Y" * 500
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:100])
        outcome = r.ingest(0, data[0:100])  # exact resend, same bytes
        self.assertEqual(outcome["identical_duplicate_bytes"], 100)
        self.assertEqual(outcome["conflicting_duplicate_bytes"], 0)
        self.assertEqual(r.conflicting_duplicate_bytes, 0)
        self.assertEqual(r.identical_duplicate_bytes, 100)
        self.assertIsNone(r.first_conflict)


class Test05ConflictingDuplicate(unittest.TestCase):
    def test_conflicting_duplicate_detected_and_first_byte_preserved(self):
        r = FileReconstructor(100)
        r.ingest(0, b"\x01" * 10)
        outcome = r.ingest(0, b"\x02" * 10)  # same offset, DIFFERENT bytes
        self.assertEqual(outcome["conflicting_duplicate_bytes"], 10)
        self.assertEqual(r.conflicting_duplicate_bytes, 10)
        self.assertIsNotNone(r.first_conflict)
        self.assertEqual(r.first_conflict["offset"], 0)
        self.assertEqual(r.first_conflict["old_byte"], 0x01)
        self.assertEqual(r.first_conflict["new_byte"], 0x02)
        # The canonical buffer must keep the FIRST-seen byte, never the
        # conflicting redelivery -- fill the rest and check.
        r.ingest(10, b"\x01" * 90)
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), b"\x01" * 100)


class Test06OutOfOrderChunk(unittest.TestCase):
    def test_arrival_order_does_not_matter_for_valid_ranges(self):
        data = bytes(range(256)) * 4  # 1024 bytes
        r = FileReconstructor(len(data))
        pieces = list(chunks_of(data, 256))
        # Ingest in reverse order -- offset-addressing must not care.
        for off, frag in reversed(pieces):
            outcome = r.ingest(off, frag)
            self.assertEqual(outcome["outcome"], "accepted")
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)


class Test07PartialOverlapIdentical(unittest.TestCase):
    def test_partial_overlap_identical_bytes(self):
        data = bytes(range(256)) * 2  # 512 bytes, deterministic content
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:200])
        # [150,300) overlaps [0,200) in [150,200) with IDENTICAL bytes,
        # and extends new data into [200,300).
        outcome = r.ingest(150, data[150:300])
        self.assertEqual(outcome["identical_duplicate_bytes"], 50)
        self.assertEqual(outcome["conflicting_duplicate_bytes"], 0)
        self.assertEqual(outcome["newly_unique_bytes"], 100)
        r.ingest(300, data[300:512])
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)


class Test08PartialOverlapConflicting(unittest.TestCase):
    def test_partial_overlap_conflicting_bytes(self):
        r = FileReconstructor(300)
        r.ingest(0, b"\xAA" * 200)
        conflicting_tail = b"\xBB" * 150  # [150,300): [150,200) conflicts, [200,300) new
        outcome = r.ingest(150, conflicting_tail)
        self.assertEqual(outcome["conflicting_duplicate_bytes"], 50)
        self.assertEqual(outcome["newly_unique_bytes"], 100)
        self.assertTrue(r.is_complete())
        # First-seen bytes at [150,200) must be preserved as 0xAA.
        canon = r.canonical_bytes()
        self.assertEqual(canon[150:200], b"\xAA" * 50)
        self.assertEqual(canon[200:300], b"\xBB" * 100)


class Test09OutOfRangeChunk(unittest.TestCase):
    def test_chunk_extending_past_expected_size_is_rejected(self):
        r = FileReconstructor(100)
        outcome = r.ingest(90, b"Z" * 20)  # 90+20=110 > 100
        self.assertEqual(outcome["outcome"], "out_of_range")
        self.assertEqual(r.out_of_range_chunks, 1)
        self.assertEqual(r.unique_bytes, 0)

    def test_chunk_starting_at_or_past_expected_size_is_rejected(self):
        r = FileReconstructor(100)
        outcome = r.ingest(100, b"Z" * 5)
        self.assertEqual(outcome["outcome"], "out_of_range")


class Test10OffsetLengthOverflow(unittest.TestCase):
    def test_wire_max_u32_offset_does_not_wrap_and_is_rejected(self):
        # A uint32_t chunk_offset of 0xFFFFFFFF combined with a nonzero
        # length would overflow a naive 32-bit sum in C; Python integers
        # never wrap, but the bounds check itself must still reject this
        # explicitly rather than relying on that as an accident.
        r = FileReconstructor(1000)
        outcome = r.ingest(0xFFFFFFFF, b"Z" * 16)
        self.assertEqual(outcome["outcome"], "out_of_range")
        self.assertEqual(r.unique_bytes, 0)


class Test11ZeroLengthChunk(unittest.TestCase):
    def test_zero_length_chunk_is_counted_and_ignored(self):
        r = FileReconstructor(100)
        outcome = r.ingest(50, b"")
        self.assertEqual(outcome["outcome"], "zero_length")
        self.assertEqual(r.zero_length_chunks, 1)
        self.assertEqual(r.unique_bytes, 0)


class Test12DisconnectExactContiguousResume(unittest.TestCase):
    def test_resume_exactly_at_next_expected_offset(self):
        data = b"R" * 1000
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:400])
        # "disconnect" -- nothing to do, reconstruction is session-agnostic
        # "resume" exactly at byte 400
        r.ingest(400, data[400:1000])
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)


class Test13ResumeBeforeRequiredOffset(unittest.TestCase):
    def test_resume_overlapping_backward_is_handled_as_overlap(self):
        data = b"S" * 1000
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:400])
        # Resume asked to start at 300 (before the 400 already received) --
        # e.g. a conservative resume request. Must not corrupt state.
        r.ingest(300, data[300:1000])
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)
        self.assertEqual(r.conflicting_duplicate_bytes, 0)
        self.assertGreater(r.identical_duplicate_bytes, 0)


class Test14ResumeAfterRequiredOffset(unittest.TestCase):
    def test_resume_starting_past_required_offset_leaves_a_gap(self):
        data = b"T" * 1000
        r = FileReconstructor(len(data))
        r.ingest(0, data[0:400])
        # Resume incorrectly jumps ahead to 500, skipping [400,500).
        r.ingest(500, data[500:1000])
        self.assertFalse(r.is_complete())
        self.assertIn((400, 100), r.missing_ranges())


class Test15AbortBeforeCompletion(unittest.TestCase):
    def test_finalize_refuses_an_incomplete_session_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "abort_test.bin")
            session = FileDownloadSession(target, 1000)
            session.ingest(0, b"P" * 400)  # incomplete -- aborted here
            with self.assertRaises(ReconstructionError):
                session.finalize()
            self.assertFalse(os.path.exists(target))
            self.assertFalse(os.path.exists(session.part_path))


class Test16NewTransferAfterAbort(unittest.TestCase):
    def test_a_fresh_reconstructor_shares_no_state_with_an_aborted_one(self):
        r1 = FileReconstructor(500)
        r1.ingest(0, b"A" * 500)
        r1.ingest(0, b"B" * 500)  # force a conflict on r1
        self.assertGreater(r1.conflicting_duplicate_bytes, 0)

        r2 = FileReconstructor(500)  # a fresh attempt, e.g. after abort+restart
        self.assertEqual(r2.conflicting_duplicate_bytes, 0)
        self.assertEqual(r2.unique_bytes, 0)
        self.assertIsNone(r2.first_conflict)
        r2.ingest(0, b"C" * 500)
        self.assertTrue(r2.is_complete())
        self.assertEqual(r2.canonical_bytes(), b"C" * 500)


class Test17AboveDiagLimitStillReconstructsInPython(unittest.TestCase):
    def test_file_above_256kb_diag_cap_reconstructs_fine_in_python(self):
        size = 300 * 1024  # > BLE_TESTER's 256KB file_integrity_diag.c cap
        data = bytes((i * 7 + 3) & 0xFF for i in range(size))
        r = FileReconstructor(size)
        for off, frag in chunks_of(data, 240):
            r.ingest(off, frag)
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_bytes(), data)
        # Confirms Phase 3's policy is purely about BLE_TESTER's on-chip
        # diagnostic buffer -- Python-side reconstruction has no such cap.


class Test18Synthetic12_3MBTransfer(unittest.TestCase):
    def test_synthetic_12_3mb_transfer_matches_reference_hash(self):
        size = int(12.3 * 1024 * 1024)
        rng = random.Random(42)
        # Deterministic pseudo-random content, generated in blocks to
        # avoid materializing a second full copy unnecessarily.
        data = bytearray(size)
        block = 65536
        for i in range(0, size, block):
            n = min(block, size - i)
            data[i:i + n] = bytes(rng.getrandbits(8) for _ in range(n))
        data = bytes(data)
        reference_sha256 = hashlib.sha256(data).hexdigest()

        r = FileReconstructor(size)
        # Realistic fragment size matching what BLE-D4.0G's own hardware
        # run observed (17280 bytes / 72 notifications = 240 bytes each).
        for off, frag in chunks_of(data, 240):
            r.ingest(off, frag)
        self.assertTrue(r.is_complete())
        self.assertEqual(r.canonical_sha256(), reference_sha256)
        self.assertEqual(len(r.canonical_bytes()), size)


class Test19SavedFileCorruptionDetected(unittest.TestCase):
    def test_finalize_refuses_to_rename_if_disk_write_diverges_from_canonical(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "corrupt_test.bin")
            session = FileDownloadSession(target, 100)
            session.ingest(0, b"Q" * 100)
            self.assertTrue(session.is_complete())

            real_write = FileDownloadSession._write_part_file

            def corrupting_write(self, data):
                # Simulate a disk-level fault: what actually lands on disk
                # differs from the canonical bytes finalize() intended.
                real_write(self, bytes(b ^ 0xFF for b in data))

            with mock.patch.object(FileDownloadSession, "_write_part_file", corrupting_write):
                with self.assertRaises(ReconstructionError):
                    session.finalize()

            # The corrupted .part file must not have been promoted to the
            # final name -- no misleadingly "successful" file left behind.
            self.assertFalse(os.path.exists(target))


class Test20MissingOneByte(unittest.TestCase):
    def test_finalize_refuses_with_exactly_one_missing_byte(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "one_byte_missing.bin")
            session = FileDownloadSession(target, 100)
            session.ingest(0, b"M" * 50)
            session.ingest(51, b"M" * 49)  # byte 50 never arrives
            self.assertFalse(session.is_complete())
            self.assertEqual(session.reconstructor.missing_ranges(), [(50, 1)])
            with self.assertRaises(ReconstructionError):
                session.finalize()
            self.assertFalse(os.path.exists(target))


class Test21MissingOneRange(unittest.TestCase):
    def test_finalize_refuses_with_a_missing_range(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "range_missing.bin")
            session = FileDownloadSession(target, 1000)
            session.ingest(0, b"N" * 300)
            session.ingest(500, b"N" * 500)  # [300,500) never arrives
            self.assertFalse(session.is_complete())
            self.assertIn((300, 200), session.reconstructor.missing_ranges())
            with self.assertRaises(ReconstructionError):
                session.finalize()
            self.assertFalse(os.path.exists(target))


class Test22RepeatedFinalization(unittest.TestCase):
    def test_finalize_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "repeat_finalize.bin")
            session = FileDownloadSession(target, 200)
            session.ingest(0, b"K" * 200)
            result1 = session.finalize()
            self.assertTrue(os.path.exists(target))
            result2 = session.finalize()  # must not re-write/re-verify/raise
            self.assertEqual(result1, result2)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"K" * 200)


class Test23SerialParserSplitAcrossReads(unittest.TestCase):
    def test_line_split_across_multiple_feed_calls(self):
        framer = NdjsonLineFramer()
        msg = b'{"type":"response","id":1,"status":"ok"}\n'
        # Feed it one byte at a time -- the pathological worst case for a
        # naive "does this chunk contain a full line" assumption.
        results = []
        for i in range(len(msg)):
            results.extend(framer.feed(msg[i:i + 1]))
        self.assertEqual(len(results), 1)
        kind, obj = results[0]
        self.assertEqual(kind, "json")
        self.assertEqual(obj, {"type": "response", "id": 1, "status": "ok"})

    def test_line_split_at_an_arbitrary_midpoint(self):
        framer = NdjsonLineFramer()
        msg = b'{"type":"event","event":"notification","id":null}\n'
        split_at = 17
        results = framer.feed(msg[:split_at])
        self.assertEqual(results, [])  # no complete line yet
        results = framer.feed(msg[split_at:])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "json")


class Test24MultipleMessagesOneRead(unittest.TestCase):
    def test_multiple_json_lines_in_a_single_feed_call(self):
        framer = NdjsonLineFramer()
        chunk = (
            b'{"type":"response","id":1,"status":"ok"}\n'
            b'{"type":"response","id":2,"status":"ok"}\n'
            b'{"type":"event","event":"notification"}\n'
        )
        results = framer.feed(chunk)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(kind == "json" for kind, _ in results))
        self.assertEqual(results[0][1]["id"], 1)
        self.assertEqual(results[1][1]["id"], 2)
        self.assertEqual(results[2][1]["event"], "notification")


class Test25MalformedJsonNotTreatedAsValidChunk(unittest.TestCase):
    def test_malformed_json_line_is_classified_raw_not_json(self):
        framer = NdjsonLineFramer()
        chunk = (
            b"ESP-ROM:esp32s3-20210327\n"  # boot banner-style raw text
            b'{"type":"response","id":5,"status":"ok"}\n'
            b'{"type":"response","id":6,"status":"ok"\n'  # truncated/malformed JSON (missing closing brace)
        )
        results = framer.feed(chunk)
        kinds = [k for k, _ in results]
        self.assertEqual(kinds.count("raw"), 2)
        self.assertEqual(kinds.count("json"), 1)
        # The malformed line must never be handed to the caller as a
        # parsed object that could be mistaken for a real chunk/response.
        for kind, payload in results:
            if kind == "raw":
                self.assertIsInstance(payload, str)
            else:
                self.assertIsInstance(payload, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
