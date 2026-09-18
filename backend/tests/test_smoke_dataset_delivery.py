"""Pure public-event extraction and in-memory download checks; no API calls."""
from copy import deepcopy
from io import BytesIO

import httpx
from PIL import Image
import pytest

from scripts.smoke_dataset_delivery import (
    delivery_outcomes_complete,
    extract_delivery,
    verify_existing,
)


def outcome(**changes):
    return {"status": "succeeded", "reason_code": "completed", "missing": [],
            "issues": [], "can_resume": False, **changes}


def message(role="assistant", metadata=None, attachments=None):
    return {"event": "message", "data": {
        "role": role, "content": "not authoritative", "metadata": metadata,
        "attachments": attachments or [],
    }}


def session(*events):
    return {"status": "completed", "events": list(events)}


def test_extracts_message_metadata_and_flat_step_events_without_mutating_input():
    source = session(
        message("user"),
        {"event": "step", "data": {"id": "s1", "status": "completed", "description": "plot"}},
        message(metadata={"step_id": "s1", "analysis_outcome": outcome()},
                attachments=[{"file_id": "f1", "filename": "chart.png"}]),
        message(),
    )
    original = deepcopy(source)
    result = extract_delivery(source)
    assert result["outcomes"] == {"s1": outcome()}
    assert result["step_statuses"] == {"s1": "completed"}
    assert result["attachments"]["f1"]["filename"] == "chart.png"
    assert delivery_outcomes_complete(result["outcomes"])
    assert source == original


def test_progress_or_prose_is_not_an_outcome():
    source = session(
        {"event": "step", "data": {"id": "s1", "status": "completed",
                                    "step": {"outcome": outcome()}}},
        message(metadata={"analysis_progress": {"stage": "complete"}}),
        message("user", metadata={"analysis_outcome": outcome()}),
        message(),
    )
    assert extract_delivery(source)["outcomes"] == {}
    assert not delivery_outcomes_complete({})


def test_only_latest_user_turn_can_supply_results():
    result = extract_delivery(session(
        message(metadata={"analysis_outcome": outcome()},
                attachments=[{"file_id": "old", "filename": "old.png"}]),
        message("user"),
        message(metadata={"analysis_outcome": outcome(status="partial")}),
    ))
    assert not result["attachments"]
    assert not delivery_outcomes_complete(result["outcomes"])


def test_later_revision_replaces_only_its_own_step():
    result = extract_delivery(session(
        message(metadata={"step_id": "s1", "analysis_outcome": outcome(status="partial")}),
        message(metadata={"step_id": "s1", "analysis_outcome": outcome()}),
        message(metadata={"step_id": "s2", "analysis_outcome": outcome(status="failed")}),
    ))
    assert result["outcomes"]["s1"]["status"] == "succeeded"
    assert not delivery_outcomes_complete(result["outcomes"])


@pytest.mark.parametrize("changes", [
    {"status": "partial"},
    {"missing": [{"kind": "image", "label": "图表", "min_count": 1}]},
    {"issues": [{"blocking": True, "reason_code": "missing_artifact"}]},
])
def test_succeeded_text_does_not_override_missing_or_blocking_results(changes):
    result = extract_delivery(session(message(metadata={"analysis_outcome": outcome(**changes)})))
    assert not delivery_outcomes_complete(result["outcomes"])


def test_nonblocking_additional_issue_does_not_invalidate_required_results():
    result = extract_delivery(session(message(metadata={"analysis_outcome": outcome(
        issues=[{"blocking": False, "reason_code": "missing_artifact"}],
    )})))
    assert delivery_outcomes_complete(result["outcomes"])


@pytest.mark.parametrize("changes", [{"missing": None}, {"issues": [{"blocking": "false"}]}])
def test_malformed_outcome_does_not_become_success(changes):
    with pytest.raises(AssertionError):
        extract_delivery(session(message(metadata={"analysis_outcome": outcome(**changes)})))


def test_verify_existing_only_gets_session_and_real_png_bytes(capsys):
    buffer = BytesIO()
    Image.new("RGB", (3, 4), color="green").save(buffer, format="PNG")
    data = session(message(metadata={"step_id": "s1", "analysis_outcome": outcome()},
                           attachments=[{"file_id": "png1", "filename": "chart.png"}]))

    class ReadOnlyClient:
        def __init__(self):
            self.paths = []

        def get(self, path):
            self.paths.append(path)
            request = httpx.Request("GET", "http://unused.invalid" + path)
            if path == "/sessions/diagnostic":
                return httpx.Response(200, request=request, json={"code": 0, "data": data})
            assert path == "/files/png1/download"
            return httpx.Response(200, request=request, content=buffer.getvalue())

    client = ReadOnlyClient()
    result = verify_existing(client, "diagnostic")
    assert result["passed"] is True
    assert result["downloaded_verified_images"][0]["dimensions"] == (3, 4)
    assert client.paths == ["/sessions/diagnostic", "/files/png1/download"]
    assert '"passed": true' in capsys.readouterr().out
