"""Small bounded HTTP/SSE client; a submitted analysis is never auto-replayed."""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TransportError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError("redirect_refused")


def parse_sse(lines, max_event_bytes=2 * 1024 * 1024):
    """Decode multiline SSE without storing comments/heartbeats or unbounded data."""
    event, data, identifier, size = "message", [], None, 0
    for raw in lines:
        size += len(raw)
        if size > max_event_bytes:
            raise TransportError("event_too_large")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = json.loads("\n".join(data))
                yield {"event": event, "id": identifier, "data": payload}
            event, data, identifier, size = "message", [], None, 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id" and "\x00" not in value:
            identifier = value
    # An incomplete last event is not terminal evidence.


class DataSeekClient:
    def __init__(self, base_url, timeout=30):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be http(s)")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("base_url must not contain credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def _open(self, method, path, body=None):
        if not path.startswith("/api/v1/") or "\r" in path or "\n" in path:
            raise ValueError("only fixed API paths are accepted")
        content = None if body is None else json.dumps(body, allow_nan=False).encode()
        req = Request(self.base_url + path, data=content, method=method,
                      headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            return self.opener.open(req, timeout=self.timeout)
        except HTTPError as exc:
            raise TransportError("http_%s" % exc.code) from None

    def api(self, method, path, body=None, max_bytes=8 * 1024 * 1024):
        with self._open(method, path, body) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TransportError("response_too_large")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("code") != 0:
            raise TransportError("api_error")
        return value.get("data")

    def stream(self, path, body):
        with self._open("POST", path, body) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise TransportError("expected_sse")
            # Bound each read as well as the assembled event.
            def lines():
                while True:
                    line = response.readline(2 * 1024 * 1024 + 1)
                    if not line:
                        return
                    yield line
            yield from parse_sse(lines())

    def download(self, file_id, max_bytes=4 * 1024 * 1024):
        from urllib.parse import quote
        with self._open("GET", "/api/v1/files/%s/download" % quote(file_id, safe="")) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TransportError("artifact_too_large")
        return raw
