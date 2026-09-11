"""
BLE-D4.0I Phase 2/5 -- the Python host is the canonical, offset-addressed
file reconstruction owner. BLE_TESTER never holds a full-file buffer (its
own memory is bounded regardless of file size -- see ble_central.c's
notify_queue_item_t queue and notify_worker_task); this module holds the
canonical byte buffer in PC RAM instead, which has no comparable
constraint (Phase 5 explicitly allows an in-memory buffer + compact
bitmap for a file of this scale).

Two classes:
  - FileReconstructor: pure offset-addressed reconstruction logic (no
    file I/O), independently testable without hardware or a real file.
  - FileDownloadSession: wraps a FileReconstructor with the .part-file
    finalization protocol (flush, fsync, close, reopen, hash, compare,
    rename) Phase 5 requires -- a reconstructed-but-unverified result
    must never silently become the final, apparently-successful file.
"""
import hashlib
import os
import zlib


class ReconstructionError(Exception):
    """Raised when reconstruction/finalization is asked to do something
    that would misrepresent an incomplete or unverified result as done."""


class FileReconstructor:
    """Absolute-offset canonical reconstruction with byte-level duplicate/
    conflict detection. Every byte is addressed by its true file position,
    so a retry re-delivering an already-received range never disturbs the
    canonical result, and a conflicting redelivery (same offset, different
    byte) is counted and the FIRST-seen byte kept -- never silently
    overwritten by whichever copy arrived more recently."""

    def __init__(self, expected_size):
        if expected_size <= 0:
            raise ValueError("expected_size must be a positive integer")
        self.expected_size = expected_size
        self.buf = bytearray(expected_size)
        self.received = bytearray((expected_size + 7) // 8)  # 1 bit per byte
        self.unique_bytes = 0
        self.identical_duplicate_bytes = 0
        self.conflicting_duplicate_bytes = 0
        self.out_of_range_chunks = 0
        self.zero_length_chunks = 0
        self.chunks_ingested = 0
        self.first_conflict = None
        self._finalized = False

    def _is_received(self, i):
        return (self.received[i >> 3] >> (i & 7)) & 1 == 1

    def _mark_received(self, i):
        self.received[i >> 3] |= 1 << (i & 7)

    def ingest(self, offset, data):
        """Ingests one chunk's payload at its absolute file offset.
        Returns a dict describing the outcome -- never raises for a
        malformed/out-of-range/zero-length chunk, since a stream of BLE
        notifications routinely contains all three and must not abort
        the whole session over one bad chunk."""
        if self._finalized:
            raise ReconstructionError("cannot ingest after finalize() has produced a result")

        length = len(data)
        self.chunks_ingested += 1

        if length == 0:
            self.zero_length_chunks += 1
            return {"outcome": "zero_length"}

        # Explicit, overflow-safe bounds check. Python integers never wrap
        # (unlike the uint32_t offset/length on the wire), so this is safe
        # even for an offset/length pair that WOULD overflow a 32-bit sum
        # in C -- the check below still rejects it correctly as
        # out-of-range rather than silently accepting a wrapped value.
        if offset < 0 or length < 0 or offset >= self.expected_size or offset + length > self.expected_size:
            self.out_of_range_chunks += 1
            return {"outcome": "out_of_range"}

        identical = 0
        conflicting = 0
        newly_unique = 0
        for i in range(length):
            pos = offset + i
            byte = data[i]
            if not self._is_received(pos):
                self.buf[pos] = byte
                self._mark_received(pos)
                newly_unique += 1
            elif self.buf[pos] == byte:
                identical += 1
            else:
                conflicting += 1
                if self.first_conflict is None:
                    self.first_conflict = {"offset": pos, "old_byte": self.buf[pos], "new_byte": byte}

        self.unique_bytes += newly_unique
        self.identical_duplicate_bytes += identical
        self.conflicting_duplicate_bytes += conflicting
        return {
            "outcome": "accepted",
            "newly_unique_bytes": newly_unique,
            "identical_duplicate_bytes": identical,
            "conflicting_duplicate_bytes": conflicting,
        }

    def is_complete(self):
        return self.unique_bytes >= self.expected_size

    def missing_ranges(self):
        """Every contiguous missing (start, length) range, in order."""
        ranges = []
        i = 0
        n = self.expected_size
        while i < n:
            if not self._is_received(i):
                start = i
                while i < n and not self._is_received(i):
                    i += 1
                ranges.append((start, i - start))
            else:
                i += 1
        return ranges

    def canonical_bytes(self):
        if not self.is_complete():
            raise ReconstructionError(
                f"cannot produce canonical bytes: {len(self.missing_ranges())} missing range(s)"
            )
        return bytes(self.buf)

    def canonical_sha256(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def canonical_crc32(self):
        return format(zlib.crc32(self.canonical_bytes()) & 0xFFFFFFFF, "08x")


class FileDownloadSession:
    """Wraps a FileReconstructor with the .part-file finalization protocol:
    flush, fsync, close, reopen from disk, verify size + hash against the
    canonical in-memory result, and ONLY THEN rename to the final path.
    An incomplete or verification-failed attempt never produces (or
    leaves behind, under the final name) something that looks like a
    successfully downloaded file."""

    def __init__(self, target_path, expected_size):
        self.target_path = target_path
        self.part_path = target_path + ".part"
        self.expected_size = expected_size
        self.reconstructor = FileReconstructor(expected_size)
        self.reconnect_count = 0
        self.resume_count = 0
        self._finalized_result = None

    def ingest(self, offset, data):
        return self.reconstructor.ingest(offset, data)

    def is_complete(self):
        return self.reconstructor.is_complete()

    def _write_part_file(self, data):
        """Isolated as its own method so a test can simulate a disk-level
        write fault (data diverging from what was intended) without
        touching the verification logic in finalize() itself."""
        with open(self.part_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def finalize(self):
        """Idempotent: a second call after a successful finalize() returns
        the same cached result without touching the filesystem again,
        rather than risking a second write/rename race against the file
        that already exists under its final name."""
        if self._finalized_result is not None:
            return self._finalized_result

        if not self.reconstructor.is_complete():
            missing = self.reconstructor.missing_ranges()
            raise ReconstructionError(
                f"cannot finalize: incomplete, {len(missing)} missing range(s), "
                f"first={missing[0] if missing else None}"
            )

        canonical_bytes = self.reconstructor.canonical_bytes()
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        canonical_crc32 = format(zlib.crc32(canonical_bytes) & 0xFFFFFFFF, "08x")

        self._write_part_file(canonical_bytes)

        # Independently reopen from disk -- never trust the write path
        # that just ran; a real bug (partial write, wrong buffer, a stale
        # handle) must be caught here, not assumed away.
        with open(self.part_path, "rb") as f:
            saved_bytes = f.read()
        saved_size = len(saved_bytes)
        saved_sha256 = hashlib.sha256(saved_bytes).hexdigest()

        if saved_size != self.expected_size:
            raise ReconstructionError(
                f"saved-file size {saved_size} != expected {self.expected_size} -- refusing to finalize"
            )
        if saved_sha256 != canonical_sha256:
            raise ReconstructionError(
                f"saved-file SHA-256 {saved_sha256} != canonical SHA-256 {canonical_sha256} -- refusing to finalize"
            )

        os.replace(self.part_path, self.target_path)

        self._finalized_result = {
            "canonical_sha256": canonical_sha256,
            "canonical_crc32": canonical_crc32,
            "saved_sha256": saved_sha256,
            "saved_size": saved_size,
            "final_path": self.target_path,
        }
        return self._finalized_result
