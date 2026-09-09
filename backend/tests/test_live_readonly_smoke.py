"""The smoke comparator ignores only fresh typed-file-URL signing values."""
import copy
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "dataseek_readonly_smoke", Path(__file__).resolve().parents[1] / "scripts" / "check_live_readonly.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
stable = _module.stable_history_events


def url(signature="a", expires="1700000000"):
    return f"/api/v1/files/opaque-file?download=1&signature={signature * 64}&expires={expires}#preview"


def events():
    return [{"event": "message", "data": {
        "event_id": "event-1", "seq": 1, "version": 1, "content": "kept exact",
        "attachments": [{"file_id": "opaque-file", "filename": "data.nc", "file_url": url()}],
        "metadata": {"signature": "important event data", "expires": "not a URL"},
    }}]


def test_refreshing_known_attachment_signatures_does_not_change_replay_content():
    first = events()
    unchanged = copy.deepcopy(first)
    second = events()
    second[0]["data"]["attachments"][0]["file_url"] = url("b", "1700000002")
    assert first != second
    assert stable(first) == stable(second)
    assert first == unchanged  # Comparator must not rewrite actual responses.


@pytest.mark.parametrize("change", ["host", "path", "query", "query_encoding", "fragment", "file_id", "filename", "body", "seq", "metadata"])
def test_non_signature_changes_are_never_hidden(change):
    first, second = events(), events()
    data = second[0]["data"]
    attachment = data["attachments"][0]
    if change == "host": attachment["file_url"] = "https://other.test" + url("b")
    elif change == "path": attachment["file_url"] = url("b").replace("opaque-file", "other-file")
    elif change == "query": attachment["file_url"] = url("b").replace("download=1", "download=2")
    elif change == "query_encoding": attachment["file_url"] = url("b").replace("download=1", "download=%31")
    elif change == "fragment": attachment["file_url"] = url("b").replace("#preview", "#changed")
    elif change == "file_id": attachment["file_id"] = "other-file"
    elif change == "filename": attachment["filename"] = "other.nc"
    elif change == "body": data["content"] = "changed"
    elif change == "seq": data["seq"] = 2
    elif change == "metadata": data["metadata"]["signature"] = "changed event data"
    assert stable(first) != stable(second)


@pytest.mark.parametrize("suffix", ["&signature=duplicate", "&expires=duplicate"])
def test_duplicate_signing_parameters_are_not_normalized(suffix):
    first, second = events(), events()
    first[0]["data"]["attachments"][0]["file_url"] = url().replace("#preview", suffix + "#preview")
    second[0]["data"]["attachments"][0]["file_url"] = url("b").replace("#preview", suffix + "#preview")
    assert stable(first) != stable(second)


def test_screenshot_shape_is_targeted_not_recursive_metadata_or_tool_results():
    first = [{"event": "tool", "data": {"content": {"screenshot": url()}, "args": {"signature": "important"}}}]
    second = copy.deepcopy(first)
    second[0]["data"]["content"]["screenshot"] = url("b", "1700000002")
    assert stable(first) == stable(second)
    second[0]["data"]["args"]["signature"] = "changed"
    assert stable(first) != stable(second)
    first[0]["data"]["content"] = {"result": {"screenshot": url()}}
    second = copy.deepcopy(first)
    second[0]["data"]["content"]["result"]["screenshot"] = url("b")
    assert stable(first) != stable(second)
