#!/usr/bin/env python3
"""
Download HolterLEV session files from the SD card over BLE.

Usage:
    python tools/holter_file_download.py --port COM28
    python tools/holter_file_download.py --port COM28 --file-index 1
    python tools/holter_file_download.py --port COM28 --output-dir downloads

Known limitation (docs/ble/PROTOCOL.md Section 17.3 in the HolterLEV
firmware repo): the underlying chunk transport is plain BLE NOTIFY with
no acknowledgment or retry, so a single dropped radio packet can corrupt
an in-progress transfer -- confirmed on real hardware to happen. This
tool works around that, not fixes it: on any gap, disconnect, or stall,
it re-establishes the connection if needed and resumes the SAME file
from the last successfully received byte (START_FILE_DOWNLOAD's own
`offset` parameter), up to --max-retries attempts. A file that never
completes after that many attempts is reported as failed and left out
of --output-dir.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError  # noqa: E402
from holter_hlv_protocol import (  # noqa: E402
    HLV_DEVICE_NAME, HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID,
    HLV_FILE_TRANSFER_DATA_UUID,
    HLV_OPCODE_LIST_FILES, HLV_OPCODE_GET_FILE_INFO, HLV_OPCODE_START_FILE_DOWNLOAD,
    HLV_OPCODE_ABORT_FILE_DOWNLOAD,
    HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_COMPLETED, HLV_RESULT_OK,
    encode_hlv_message, decode_command_response, decode_list_files_response,
    decode_get_file_info_response, decode_file_chunk_header,
    wait_for_command_response, authenticate_raw,
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_and_prepare(client):
    """Connect, discover, subscribe, authenticate. Raises ControllerError on failure."""
    client.send("reset_controller", timeout=5)
    client.send("connect", timeout=12, name=HLV_DEVICE_NAME, timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.send("subscribe", timeout=6, characteristic=HLV_FILE_TRANSFER_DATA_UUID, mode="notify")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    if not status["authorized"]:
        raise ControllerError("AUTH_FAILED", "device rejected the default dev access code")
    return base_txn


def get_file_info(client, base_txn, file_index):
    """Single GET_FILE_INFO round trip -- much cheaper than list_all_files() when the caller
    already knows exactly which file_index(es) it wants, since LIST_FILES re-walks the whole
    SD directory fresh every call (docs/ble/PROTOCOL.md Section 17.5) and can take minutes on
    a card with a long testing history. Returns {"file_index", "size_bytes"} or None if the
    file does not exist."""
    txn = base_txn + 1
    msg = encode_hlv_message(HLV_OPCODE_GET_FILE_INFO, txn, file_index.to_bytes(2, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    cr = wait_for_command_response(client, txn, window_s=5)
    if cr is None or cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
        raise ControllerError("GET_FILE_INFO_FAILED", f"unexpected response: {cr}")
    info = decode_get_file_info_response(cr["inner"])
    if not info["exists"]:
        return None
    return {"file_index": file_index, "size_bytes": info["size_bytes"]}


def list_all_files(client, base_txn):
    """Pages through LIST_FILES until has_more is false. Slow on a card with many historical
    files (a fresh directory walk every call, Section 17.5) -- prefer get_file_info() when the
    caller already knows which file_index(es) it wants."""
    files = []
    start_index = 0
    txn = base_txn
    while True:
        txn += 1
        msg = encode_hlv_message(HLV_OPCODE_LIST_FILES, txn, start_index.to_bytes(2, "little"))
        client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
        cr = wait_for_command_response(client, txn, window_s=5)
        if cr is None or cr["phase"] != HLV_RESPONSE_PHASE_COMPLETED:
            raise ControllerError("LIST_FILES_FAILED", f"unexpected response: {cr}")
        page = decode_list_files_response(cr["inner"])
        files.extend(page["entries"])
        if not page["has_more"] or not page["entries"]:
            break
        start_index = page["entries"][-1]["file_index"] + 1
    return files, txn


def try_abort(client, txn):
    """Best-effort: tell the DUT to drop a stale in-progress transfer before resuming."""
    try:
        abort_msg = encode_hlv_message(HLV_OPCODE_ABORT_FILE_DOWNLOAD, txn)
        client.send("write", timeout=4, characteristic=HLV_CONTROL_POINT_UUID, data_hex=abort_msg.hex())
    except ControllerError:
        pass


def download_file(client, base_txn, file_index, expected_size, max_retries, on_reconnect):
    """Downloads one file, resuming from the last received byte on any gap, stall, or
    disconnect. `on_reconnect()` re-establishes a session and returns a fresh base_txn --
    called only when the current connection is found to be unusable.

    Returns the complete bytes, or raises RuntimeError after max_retries attempts.
    """
    received = bytearray()
    txn = base_txn
    attempt = 0

    while len(received) < expected_size:
        attempt += 1
        if attempt > max_retries:
            raise RuntimeError(
                f"only got {len(received)}/{expected_size} bytes after {max_retries} attempts"
            )

        # Discard any leftover events from a previous attempt (e.g. late chunks still in
        # flight when we aborted/timed out) -- otherwise a stale chunk can be misread as
        # belonging to this new attempt (observed: offset going BACKWARDS).
        client.drain_events()

        try:
            txn += 1
            start_msg = encode_hlv_message(
                HLV_OPCODE_START_FILE_DOWNLOAD, txn,
                file_index.to_bytes(2, "little") + len(received).to_bytes(4, "little")
            )
            client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=start_msg.hex())
            accepted = wait_for_command_response(client, txn, window_s=5)
        except ControllerError as e:
            log(f"  attempt {attempt}: connection issue starting download ({e}); reconnecting")
            client = on_reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if accepted is None or accepted["phase"] != HLV_RESPONSE_PHASE_ACCEPTED:
            log(f"  attempt {attempt}: START_FILE_DOWNLOAD not accepted ({accepted}); retrying shortly")
            time.sleep(1.0)
            continue

        expected_offset = len(received)
        transfer_id = None
        chunk_deadline = time.time() + 30
        disconnected = False

        try:
            while time.time() < chunk_deadline:
                remaining = chunk_deadline - time.time()
                # No event_name filter here -- a "disconnected" event must be seen too, not
                # just "notification"; filtering by name earlier made this loop blind to a
                # mid-wait disconnect, misreporting it as an indistinguishable 30s stall.
                ev = client.wait_for_event(timeout=remaining)
                if ev is None:
                    log(f"  attempt {attempt}: stalled at {len(received)}/{expected_size} bytes, no event for 30s")
                    break
                if ev.get("event") == "disconnected":
                    log(f"  attempt {attempt}: peer disconnected at {len(received)}/{expected_size} bytes")
                    disconnected = True
                    break
                if ev.get("event") != "notification":
                    continue
                if ev.get("characteristic") == HLV_FILE_TRANSFER_DATA_UUID:
                    chunk = decode_file_chunk_header(ev["data_hex"])
                    if transfer_id is None:
                        transfer_id = chunk["transfer_id"]
                    # A chunk from a different (stale/earlier) transfer_id, or one whose
                    # offset falls at or before data we already have, is a harmless
                    # duplicate/late redelivery -- plain NOTIFY has no dedup at the ATT
                    # layer (PROTOCOL.md Section 17.3), and this was observed live:
                    # e.g. "expected offset 241, got 0" right after offset 241 was
                    # already reached. Only a chunk strictly AHEAD of what we expect
                    # represents real data loss worth aborting/retrying for.
                    if chunk["transfer_id"] != transfer_id or chunk["chunk_offset"] < expected_offset:
                        continue
                    if chunk["chunk_offset"] != expected_offset:
                        log(f"  attempt {attempt}: gap detected (expected offset {expected_offset}, "
                            f"got {chunk['chunk_offset']}) at {len(received)}/{expected_size} bytes")
                        try_abort(client, txn + 1)
                        break
                    received.extend(chunk["fragment"])
                    expected_offset = len(received)
                    if len(received) >= chunk["total_length"]:
                        break
                elif ev.get("characteristic") == HLV_COMMAND_RESPONSE_UUID:
                    decoded = decode_command_response(ev["data_hex"])
                    if decoded["transaction_id"] == txn:
                        if decoded["result_code"] != HLV_RESULT_OK:
                            # The DUT itself ended the transfer early (e.g. a failed
                            # ble_gatts_notify_custom() aborts rather than retrying --
                            # PROTOCOL.md Section 17.3). Previously this was swallowed
                            # silently, making an early server-side abort look identical
                            # to a plain stall/gap in the log.
                            log(f"  attempt {attempt}: DUT ended transfer early, result_code="
                                f"{decoded['result_code']} at {len(received)}/{expected_size} bytes")
                        break
        except ControllerError as e:
            log(f"  attempt {attempt}: connection issue mid-transfer ({e}) at "
                f"{len(received)}/{expected_size} bytes; reconnecting")
            client = on_reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF
            continue

        if disconnected:
            client = on_reconnect()
            txn = int(time.time() * 1000) & 0x7FFFFFFF

    return bytes(received)


def main():
    parser = argparse.ArgumentParser(description="Download HolterLEV session files over BLE")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--file-index", type=int, action="append",
                         help="download only this file_index (repeatable); default: all files")
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "..", "downloads"))
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument("--reconnect-attempts", type=int, default=5)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    holder = {}  # boxed client so on_reconnect() can rebind it for the caller too

    def reconnect():
        # BLE-D4.0B Step 2: a BLE-level reconnect must not require reopening the
        # physical COM port. connect_and_prepare() already starts with
        # reset_controller, which (ble_central_reset(), ble_central.c) fully
        # resets BLE_TESTER's own central state machine -- clears any pending
        # op, terminates an existing connection, cancels an in-progress scan,
        # forces state back to IDLE -- entirely over the existing UART link.
        # Reopening serial.Serial() here was never necessary for a BLE-level
        # disconnect/reconnect, and doing so unnecessarily is exactly what
        # caused the earlier "Access is denied" crash (the previous instance's
        # handle was still open when a new one tried to claim the same port).
        client = holder["client"]
        for i in range(args.reconnect_attempts):
            try:
                base_txn = connect_and_prepare(client)
                holder["base_txn"] = base_txn
                return client
            except ControllerError as e:
                log(f"  reconnect attempt {i + 1}/{args.reconnect_attempts} failed "
                    f"(BLE-level, same UART connection): {e}")
                time.sleep(2.0)

        # BLE-level reconnect exhausted -- the controller itself may be wedged
        # (e.g. it never answers reset_controller at all). Only as a last
        # resort does this fall back to closing and reopening the physical
        # port, and only once the old handle is confirmed closed first.
        log("  BLE-level reconnect exhausted; falling back to a full serial-port reopen (last resort)")
        try:
            client.close()
        except Exception:
            pass
        time.sleep(1.0)
        c = None
        try:
            c = HolterBLEClient(args.port, baud=args.baud, default_timeout=15)
            base_txn = connect_and_prepare(c)
            holder["client"] = c
            holder["base_txn"] = base_txn
            return c
        except Exception as e:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            raise RuntimeError(f"could not reconnect to the DUT even after a full serial-port reopen: {e}")

    holder["client"] = HolterBLEClient(args.port, baud=args.baud, default_timeout=15)
    try:
        holder["base_txn"] = connect_and_prepare(holder["client"])
    except ControllerError as e:
        log(f"FATAL: could not connect/authenticate: {e}")
        return 2

    client = holder["client"]
    base_txn = holder["base_txn"]

    try:
        if args.file_index:
            # Specific indices requested -- a direct GET_FILE_INFO per index is far cheaper
            # than a full LIST_FILES scan (which re-walks the whole SD directory every call).
            files = []
            for idx in args.file_index:
                info = get_file_info(client, base_txn, idx)
                base_txn += 1
                if info is None:
                    log(f"file_index={idx} not found on the device -- skipping")
                else:
                    files.append(info)
        else:
            log("listing session files (this re-walks the whole SD directory -- can take a "
                "while on a card with a long testing history)...")
            files, base_txn = list_all_files(client, base_txn)
    except ControllerError as e:
        log(f"FATAL: could not determine which files to download: {e}")
        return 2

    if not files:
        log("no files to download")
        return 0

    log(f"{len(files)} file(s) to download: " +
        ", ".join(f"ECG{f['file_index']:05d}.TLV ({f['size_bytes']} bytes)" for f in files))

    succeeded, failed = [], []

    for f in files:
        file_index, size_bytes = f["file_index"], f["size_bytes"]
        out_path = os.path.join(args.output_dir, f"ECG{file_index:05d}.TLV")

        # This batch can legitimately run for hours (some files are 100+ MB over an
        # unreliable BLE NOTIFY transport) -- a crash or restart must resume, not
        # re-download everything already saved from scratch.
        if os.path.exists(out_path) and os.path.getsize(out_path) == size_bytes:
            log(f"skipping file_index={file_index}: already downloaded ({size_bytes} bytes) -> {out_path}")
            succeeded.append(file_index)
            continue

        log(f"downloading file_index={file_index} ({size_bytes} bytes) -> {out_path}")
        t0 = time.time()
        # A fresh seed per file (not the shared, possibly-stale connection-setup base_txn) --
        # download_file() advances its own local counter from here.
        file_base_txn = (int(time.time() * 1000) + file_index) & 0x7FFFFFFF
        try:
            data = download_file(
                holder["client"], file_base_txn, file_index, size_bytes,
                args.max_retries, reconnect
            )
        except RuntimeError as e:
            log(f"FAILED file_index={file_index}: {e}")
            failed.append(file_index)
            continue

        with open(out_path, "wb") as out:
            out.write(data)

        elapsed = time.time() - t0
        log(f"OK file_index={file_index}: {len(data)} bytes in {elapsed:.1f}s -> {out_path}")
        succeeded.append(file_index)

    try:
        holder["client"].send("disconnect", timeout=5)
    except ControllerError:
        pass
    holder["client"].close()

    log(f"done: {len(succeeded)} succeeded, {len(failed)} failed"
        + (f" (failed: {failed})" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
