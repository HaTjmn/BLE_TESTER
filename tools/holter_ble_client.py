"""
Shared NDJSON serial client for the HolterLEV BLE Test Controller.

Used by both holter_ble_cli.py (interactive/scripted single commands) and
holter_ble_test.py (the automated test runner) so the request/response
correlation and event handling logic exists in exactly one place.
"""
import json
import threading
import time
import queue

import serial


def redact_request(request):
    """A copy of a controller request with every payload field replaced by
    "<redacted N bytes>" (HLV-WIFI-20261006-008: never log a credential)."""
    out = dict(request)
    for key in ("data_hex",):
        if key in out and isinstance(out[key], str):
            out[key] = f"<redacted {len(out[key]) // 2} bytes>"
    return out


class ControllerError(Exception):
    """Raised when the controller returns status:"error", or when a
    host-side timeout elapses waiting for a response."""

    def __init__(self, error_code, detail=None, request=None, response=None):
        self.error_code = error_code
        self.detail = detail
        self.request = request
        # BLE-D4.0B Step 3: the full raw response, so a caller can read the
        # structured diagnostic fields (central_state, conn_handle,
        # pending_operation_type, etc.) a specific error code carries,
        # not just the generic error_code/detail strings.
        self.response = response
        super().__init__(f"{error_code}: {detail}" if detail else error_code)


class NdjsonLineFramer:
    """BLE-D4.0I Phase 6: reassembles newline-delimited lines from
    arbitrary byte chunks and classifies each as parsed JSON or a raw
    (non-JSON / malformed) line. Pure logic, no I/O -- extracted from
    HolterBLEClient._read_loop() so the split-across-reads and
    multiple-messages-per-read cases can be unit-tested without a real
    or mocked serial port. Behavior is unchanged from the inline version
    this replaces: an internal buffer persists across feed() calls, and
    every complete line found in one feed() is returned, in order."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk):
        """Returns a list of ("json", obj) / ("raw", decoded_line) tuples
        for every complete line now available -- zero, one, or many."""
        self._buf += chunk
        results = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            decoded_line = line.decode("utf-8", errors="replace")
            try:
                obj = json.loads(decoded_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                results.append(("raw", decoded_line))
                continue
            results.append(("json", obj))
        return results


class HolterBLEClient:
    """Opens one serial port and speaks the newline-delimited JSON
    automation protocol. Thread-safe for concurrent send/event-drain.
    """

    def __init__(self, port, baud=115200, default_timeout=10.0, log_file=None, raw_capture_path=None):
        self._ser = serial.Serial(port, baud, timeout=0.2)
        self._default_timeout = default_timeout
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending = {}  # id -> queue.Queue for that response
        self._events = queue.Queue()
        self._unmatched = queue.Queue()  # responses whose id matched no pending send() (e.g. id:null parse errors)
        self._log_file = log_file
        # BLE-D4.0C Step 2: any line that is NOT valid JSON (ESP-IDF boot
        # banner, a panic/backtrace, a watchdog dump, a brownout message)
        # used to be silently discarded here. Now captured with both a
        # wall-clock and a monotonic timestamp -- monotonic because a panic
        # can happen mid-reboot when the wall clock is meaningless for
        # ordering, and because it survives NTP/timezone weirdness.
        self._raw_lines = queue.Queue()
        self._raw_capture_file = None
        if raw_capture_path is not None:
            self._raw_capture_file = open(raw_capture_path, "a", encoding="utf-8", buffering=1)
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Let the ESP32's boot/UART settle before the first command.
        time.sleep(0.3)

    def close(self):
        self._stop = True
        time.sleep(0.05)
        try:
            self._ser.close()
        except Exception:
            pass
        if self._raw_capture_file is not None:
            try:
                self._raw_capture_file.close()
            except Exception:
                pass

    def drain_raw_lines(self):
        """Returns all raw non-JSON lines received so far (boot banners,
        panics, watchdog/brownout messages), each as
        {"wall_time", "monotonic", "line"}, and clears the internal queue."""
        out = []
        while True:
            try:
                out.append(self._raw_lines.get_nowait())
            except queue.Empty:
                break
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _log(self, direction, obj):
        if self._log_file is None:
            return
        record = {"t": time.time(), "dir": direction, "msg": obj}
        self._log_file.write(json.dumps(record) + "\n")
        self._log_file.flush()

    def _read_loop(self):
        framer = NdjsonLineFramer()
        while not self._stop:
            try:
                chunk = self._ser.read(4096)
            except Exception:
                break
            if not chunk:
                continue
            for kind, payload in framer.feed(chunk):
                if kind == "raw":
                    record = {"wall_time": time.time(), "monotonic": time.monotonic(), "line": payload}
                    self._raw_lines.put(record)
                    if self._raw_capture_file is not None:
                        self._raw_capture_file.write(json.dumps(record) + "\n")
                    continue
                obj = payload
                self._log("recv", obj)
                if obj.get("type") == "response":
                    rid = obj.get("id")
                    with self._lock:
                        q = self._pending.get(rid)
                    if q is not None:
                        q.put(obj)
                    else:
                        self._unmatched.put(obj)
                elif obj.get("type") == "event":
                    self._events.put(obj)

    def send(self, cmd, timeout=None, sensitive=False, **fields):
        """Sends one command and blocks for its terminal response.
        Raises ControllerError on status:"error" or on host-side timeout.
        Returns the response dict (status:"ok") on success.

        sensitive=True (HLV-WIFI-20261006-008): the request carries a
        credential (e.g. a WIFI_ADD / WIFI_UPDATE Control Point write). Its
        data_hex is replaced by a length-only placeholder in the log file
        and in any ControllerError, so a password never lands in a log,
        traceback or report. The serial line itself must carry it.
        """
        timeout = self._default_timeout if timeout is None else timeout
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            q = queue.Queue()
            self._pending[req_id] = q

        request = {"id": req_id, "cmd": cmd}
        request.update(fields)
        line = json.dumps(request)
        if sensitive:
            request = redact_request(request)
        self._log("send", request)
        self._ser.write((line + "\n").encode("utf-8"))

        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise ControllerError("HOST_TIMEOUT",
                                   f"no response to id={req_id} ({cmd}) within {timeout}s",
                                   request=request)
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

        if resp.get("status") != "ok":
            raise ControllerError(resp.get("error", "UNKNOWN_ERROR"), resp.get("detail"),
                                   request=request, response=resp)
        return resp

    def send_raw_line(self, text):
        """Writes an arbitrary raw line (no id-tracking) -- used only to
        test malformed-input handling."""
        self._log("send_raw", text)
        self._ser.write((text + "\n").encode("utf-8"))

    def wait_for_unmatched_response(self, timeout=5.0):
        """Blocks for a response whose id matched no pending send() --
        specifically the id:null PARSE_ERROR response to malformed input,
        or a MISSING_FIELD response to a line with no usable id."""
        try:
            return self._unmatched.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_events(self):
        """Returns and clears all events received so far (non-blocking)."""
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def wait_for_event(self, event_name=None, timeout=5.0):
        """Blocks (host-side) for the next event, optionally filtered by
        name. Returns None on timeout rather than raising -- absence of
        an event is frequently the expected/tested outcome."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                ev = self._events.get(timeout=remaining)
            except queue.Empty:
                return None
            if event_name is None or ev.get("event") == event_name:
                return ev
