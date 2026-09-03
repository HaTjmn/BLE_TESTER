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


class ControllerError(Exception):
    """Raised when the controller returns status:"error", or when a
    host-side timeout elapses waiting for a response."""

    def __init__(self, error_code, detail=None, request=None):
        self.error_code = error_code
        self.detail = detail
        self.request = request
        super().__init__(f"{error_code}: {detail}" if detail else error_code)


class HolterBLEClient:
    """Opens one serial port and speaks the newline-delimited JSON
    automation protocol. Thread-safe for concurrent send/event-drain.
    """

    def __init__(self, port, baud=115200, default_timeout=10.0, log_file=None):
        self._ser = serial.Serial(port, baud, timeout=0.2)
        self._default_timeout = default_timeout
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending = {}  # id -> queue.Queue for that response
        self._events = queue.Queue()
        self._unmatched = queue.Queue()  # responses whose id matched no pending send() (e.g. id:null parse errors)
        self._log_file = log_file
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
        buf = b""
        while not self._stop:
            try:
                chunk = self._ser.read(4096)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
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

    def send(self, cmd, timeout=None, **fields):
        """Sends one command and blocks for its terminal response.
        Raises ControllerError on status:"error" or on host-side timeout.
        Returns the response dict (status:"ok") on success.
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
            raise ControllerError(resp.get("error", "UNKNOWN_ERROR"), resp.get("detail"), request=request)
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
