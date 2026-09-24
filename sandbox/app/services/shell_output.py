"""Process-local, private shell logs and bounded model/UI projections.

Anonymous files are deliberately not durable artifacts: release, replacement,
or a sandbox restart retires them. No path or file descriptor leaves this class.
"""
from __future__ import annotations

import tempfile
import threading
from functools import wraps
from uuid import uuid4


PREVIEW_BYTES = 32 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024  # Per stream, not a task lifetime quota.
MAX_PAGE_BYTES = 16 * 1024
OMISSION = "\n[... output omitted from preview; read this output_id with a cursor ...]\n"


def _locked(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return invoke


def _close_file(spool):
    try:
        spool.close()
    except OSError:
        pass


def utf8_prefix(value: bytes, limit: int) -> bytes:
    return value[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def utf8_tail(value: bytes, limit: int) -> bytes:
    return value[-limit:].decode("utf-8", errors="ignore").encode("utf-8") if limit else b""


class _AnsiFilter:
    """Bounded state machine, including CSI/OSC sequences split across reads."""

    def __init__(self):
        self.state = "text"

    def feed(self, text: str) -> str:
        kept = []
        for char in text:
            state = self.state
            if state == "text":
                if char == "\x1b":
                    self.state = "escape"
                else:
                    kept.append(char)
            elif state == "escape":
                self.state = ("csi" if char == "[" else "osc" if char == "]"
                              else "string" if char in "PX^_" else "text")
            elif state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif state in {"osc", "string"}:
                if char == "\x1b":
                    self.state = state + "_escape"
                elif char == "\x07" and state == "osc":
                    self.state = "text"
            elif state.endswith("_escape"):
                self.state = "text" if char == "\\" else state.removesuffix("_escape")
        return "".join(kept)


class ShellOutputBuffer:
    def __init__(self, *, preview_bytes: int = PREVIEW_BYTES, max_log_bytes: int = MAX_LOG_BYTES):
        if preview_bytes < 256 or max_log_bytes < 1:
            raise ValueError("Invalid shell output limits")
        self._lock = threading.RLock()
        self.output_id = uuid4().hex
        self.preview_bytes = preview_bytes
        self.max_log_bytes = max_log_bytes
        self.total_bytes = 0
        self.raw_bytes = 0
        self.stream_complete = False
        self.status = "available"
        self.raw_status = "available"
        self._head = b""
        self._head_finished = False
        self._tail = b""
        self._ansi = _AnsiFilter()
        self._spool = self._open()
        self._raw_spool = self._open()
        if self._spool is None:
            self.status = "unavailable"
        if self._raw_spool is None:
            self.raw_status = "unavailable"

    @staticmethod
    def _open():
        try:
            # Fixed system temporary directory, never an execution/dataset path.
            return tempfile.TemporaryFile(mode="w+b", dir="/tmp")
        except OSError:
            return None

    def _write(self, attribute: str, status: str, data: bytes, total: int):
        spool = getattr(self, attribute)
        if spool is None:
            return
        try:
            if total > self.max_log_bytes:
                setattr(self, status, "limit_exceeded")
            else:
                spool.seek(0, 2)
                if spool.write(data) != len(data):
                    raise OSError("Incomplete shell log write")
                return
        except (OSError, ValueError):
            setattr(self, status, "unavailable")
        # An incomplete file must never be advertised as a complete log.
        _close_file(spool)
        setattr(self, attribute, None)

    @_locked
    def append(self, text: str) -> None:
        if self.status == "closed":
            return
        raw = text.encode("utf-8", errors="replace")
        self.raw_bytes += len(raw)
        self._write("_raw_spool", "raw_status", raw, self.raw_bytes)
        data = self._ansi.feed(text).encode("utf-8", errors="replace")
        self.total_bytes += len(data)
        self._write("_spool", "status", data, self.total_bytes)
        half = (self.preview_bytes - len(OMISSION.encode())) // 2
        if not self._head_finished:
            self._head_finished = len(self._head) + len(data) >= half
            self._head = utf8_prefix(self._head + data, half)
        self._tail = utf8_tail(self._tail + data, self.preview_bytes)

    @_locked
    def preview(self) -> str:
        if self.total_bytes <= self.preview_bytes:
            return self._tail.decode("utf-8")
        remaining = self.preview_bytes - len(self._head) - len(OMISSION.encode())
        return (self._head + OMISSION.encode() + utf8_tail(self._tail, remaining)).decode("utf-8")

    @_locked
    def metadata(self) -> dict:
        return {"output_id": self.output_id, "total_bytes": self.total_bytes,
                "preview_truncated": self.total_bytes > self.preview_bytes,
                "log_status": self.status, "raw_log_status": self.raw_status,
                "stream_complete": self.stream_complete}

    @_locked
    def read(self, cursor: int, max_bytes: int) -> tuple[str, dict]:
        if type(cursor) is not int or not 0 <= cursor <= self.total_bytes:
            raise ValueError("Invalid output cursor")
        if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_PAGE_BYTES:
            raise ValueError("Invalid output page size")
        start = cursor
        source = "spool"
        data = None
        if self._spool is not None:
            try:
                self._spool.seek(cursor)
                data = self._spool.read(max_bytes)
            except (OSError, ValueError):
                self.status = "unavailable"
                _close_file(self._spool)
                self._spool = None
        if data is None:
            source = "tail"
            earliest = self.total_bytes - len(self._tail)
            start = max(cursor, earliest)
            data = self._tail[start - earliest:start - earliest + max_bytes]
        # Arbitrary callers may resume inside a code point; advance explicitly
        # and mark the gap rather than emitting replacement characters.
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
            start += 1
        data = utf8_prefix(data, max_bytes)
        next_cursor = start + len(data)
        return data.decode("utf-8"), {"cursor": cursor, "start": start,
            "next_cursor": next_cursor, "lossy": start != cursor, "source": source,
            "eof": self.stream_complete and next_cursor == self.total_bytes}

    @_locked
    def close(self) -> None:
        for attribute in ("_spool", "_raw_spool"):
            spool = getattr(self, attribute, None)
            if spool is not None:
                _close_file(spool)
                setattr(self, attribute, None)
        self.status = self.raw_status = "closed"

    def __del__(self):
        if hasattr(self, "_lock"):
            self.close()
