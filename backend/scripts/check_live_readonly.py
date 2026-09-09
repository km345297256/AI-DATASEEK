"""Read-only HTTP smoke check. No chat, uploads, updates or model requests."""
import asyncio
import copy
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx


def _stable_signed_file_url(value, *, file_id=None):
    """Normalize only time-dependent signing values on known file URLs.

    FileInfoResponse.from_file_info and BrowserToolContent issue fresh URLs on
    every HTTP read. Their expiry/signature are not persisted event content.
    Preserve the host, path, fragment, query ordering and every other query
    component verbatim; malformed or unknown URLs are not normalized at all.
    """
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if not re.fullmatch(r"/api/v1/files/[A-Za-z0-9_-]+", parsed.path):
        return value
    if file_id is not None and parsed.path != f"/api/v1/files/{file_id}":
        return value
    components = parsed.query.split("&")
    pairs = [component.partition("=") for component in components]
    signatures = [item[2] for item in pairs if item[0] == "signature"]
    expiries = [item[2] for item in pairs if item[0] == "expires"]
    if (len(signatures) != 1 or len(expiries) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", signatures[0])
            or not re.fullmatch(r"[0-9]+", expiries[0])):
        return value
    stable_query = "&".join(
        f"{key}=<transient>" if key in {"signature", "expires"} else component
        for component, (key, _, _) in zip(components, pairs)
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, stable_query, parsed.fragment))


def stable_history_events(events):
    """Compare effective replay content, never erase arbitrary signature keys."""
    result = copy.deepcopy(events)
    for event in result:
        data = event.get("data", {})
        if event.get("event") == "message":
            for attachment in data.get("attachments") or []:
                if isinstance(attachment, dict) and isinstance(attachment.get("file_id"), str):
                    if "file_url" in attachment:
                        attachment["file_url"] = _stable_signed_file_url(
                            attachment["file_url"], file_id=attachment["file_id"],
                        )
        elif event.get("event") == "tool":
            content = data.get("content")
            # This is the exact BrowserToolContent shape. Nested tool results,
            # args, arbitrary metadata and similarly named fields stay exact.
            if isinstance(content, dict) and set(content) == {"screenshot"}:
                content["screenshot"] = _stable_signed_file_url(content["screenshot"])
    return result


async def main():
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        async def get(path, **kwargs):
            response = await client.get(path, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        runtime = await get("/api/v1/plugins/runtime")
        assert runtime["status"] == "healthy"
        items = (await get("/api/v1/sessions"))["sessions"]
        histories_checked = 0
        older_pages = 0
        previews = {"text": 0, "csv": 0}
        for session in items:
            if session["status"] != "completed":
                continue  # Never attach to an active stream.
            sid = session["session_id"]
            full = await get(f"/api/v1/sessions/{sid}")
            page = await get(f"/api/v1/sessions/{sid}/history", params={"turns": 2})
            events = page["events"]
            previous = None
            while page["has_more"]:
                before = page["next_before_seq"]
                assert before is not None and (previous is None or before < previous)
                previous = before
                page = await get(f"/api/v1/sessions/{sid}/history", params={"turns": 2, "before_seq": before})
                events = page["events"] + events
                older_pages += 1
            assert stable_history_events(events) == stable_history_events(full["events"]), "History paging changed existing replay content"
            histories_checked += 1
            files = await get(f"/api/v1/sessions/{sid}/files")
            for file in files:
                suffix = (file.get("filename") or "").lower().rsplit(".", 1)[-1]
                mode = "csv" if suffix in {"csv", "tsv"} else "text" if suffix in {"txt", "py", "json", "md"} else None
                if mode is None or previews[mode] >= 2:
                    continue
                result = await get(f"/api/v1/files/{file['file_id']}/preview", params={"mode": mode})
                assert result["bytes_read"] <= (128 * 1024 if mode == "csv" else 64 * 1024)
                assert len(result["rows"]) <= 100 and len(result["headers"]) <= 50
                if result["next_offset"] is not None:
                    next_page = await get(f"/api/v1/files/{file['file_id']}/preview", params={
                        "mode": mode, "offset": result["next_offset"], "version": result["version"],
                        **({"delimiter": result["delimiter"]} if result.get("delimiter") else {}),
                        "header_pending": result.get("header_pending", False),
                    })
                    assert next_page["version"] == result["version"]
                previews[mode] += 1
            if histories_checked >= 5 and previews["csv"] and previews["text"]:
                break
        assert histories_checked > 0
        missing = await client.get("/api/v1/files/dataseek-readonly-nonexistent/preview")
        assert missing.status_code == 404
        frontend_routes = []
        for route in ("/", "/datasets", "/plugins?tab=runtime"):
            response = await client.get(route)
            assert response.status_code == 200 and "text/html" in response.headers.get("content-type", "")
            frontend_routes.append(route)
        print(json.dumps({"runtime": {key: runtime.get(key) for key in ("engine", "status", "plugin_count", "tool_count")},
            "existing_histories_content_equivalent": histories_checked,
            "history_comparison": "All event content exact except refreshed expires/signature values in typed attachment/screenshot file URLs",
            "older_pages": older_pages,
            "bounded_previews": previews, "missing_file_404": True, "frontend_routes_200": frontend_routes,
            "writes_or_model_requests": 0}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
