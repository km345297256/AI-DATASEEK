"""Deny Office-origin access to anonymous-administrator application APIs.

CORS alone cannot prevent simple cross-origin writes. This small ASGI boundary
also covers WebSocket and opaque-origin descendants of a third-party iframe.
"""
from urllib.parse import urlsplit
from starlette.responses import JSONResponse


def office_origin(value: str) -> bool:
    if value == "null":
        return True
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".") == "office.localhost"
    except ValueError:
        return True


class OfficeOriginGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and scope.get("path", "").startswith("/api/"):
            headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
            blocked = (office_origin(headers.get("origin", "")) or office_origin(headers.get("referer", ""))
                or headers.get("host", "").split(":", 1)[0].lower().rstrip(".") == "office.localhost")
            if blocked:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Office origin cannot access application APIs"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
