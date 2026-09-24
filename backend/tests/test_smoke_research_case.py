"""Acceptance runner cannot hide failure or modify the user's CASE."""
import json
from pathlib import Path

import httpx
import pytest

from scripts import smoke_research_case as smoke
from test_smoke_analysis_task_matrix import message, outcome


def fixture_client(monkeypatch, fault=None):
    state = {"created": 0, "requests": [], "events": []}

    def ok(data):
        return httpx.Response(200, json={"code": 0, "data": data})

    def handle(request):
        path = request.url.path
        if request.method == "PUT" and path.endswith("/sessions"):
            state["created"] += 1
            return ok({"session_id": "new-iris-only"})
        if request.method == "PATCH" and path.endswith("/title"):
            return ok({})
        if request.method == "POST" and path.endswith("/chat"):
            body = json.loads(request.content)
            state["requests"].append(body)
            user = message("user", text=body["message"])
            user["data"]["metadata"]["client_message_id"] = body["client_message_id"]
            result = outcome(status="partial" if fault == "partial" else "succeeded")
            attached = [{"file_id": "plot", "filename": "extra.png"}] if fault == "plot" else []
            answer = message(text="原始数据核对结果，单位与重复标记的完整科学说明。" * 12,
                             result=result, attachments=attached)
            if fault == "missing_outcome":
                answer["data"]["metadata"] = {}
            state["events"] = [user, answer]
            if fault == "transport":
                raise httpx.ReadTimeout("PRIVATE ERROR", request=request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=b'event: done\ndata: {}\n\n')
        if request.method == "GET" and path.endswith("/sessions/new-iris-only"):
            return ok({"status": "completed", "events": state["events"]})
        raise AssertionError(f"Unexpected request {request.method} {path}")

    client = httpx.Client(base_url="http://127.0.0.1:7001/api/v1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kwargs: client)
    return state


def test_oracle_compares_all_source_observations_and_duplicate_group_members():
    oracle = smoke.source_oracle()
    assert oracle["same_multiset"] and oracle["same_order"]
    assert oracle["rows_csv"] == oracle["rows_raw"] == 150
    assert sorted(oracle["species_counts"].values()) == [50, 50, 50]
    assert oracle["duplicate_extra_rows"] == 3
    assert oracle["duplicate_member_rows"] == 5
    assert [group["one_based_rows"] for group in oracle["duplicate_groups"]] == [[10, 35, 38], [102, 143]]


def test_exact_question_matches_user_case_document():
    document = Path(__file__).resolve().parents[2] / "outputs/dataseek-research-test-instructions-2026-09-18.md"
    if document.exists():
        step = document.read_text().split('<a id="task-001"></a>', 1)[1].split("```text\n", 1)[1].split("```", 1)[0].strip()
        assert smoke.MESSAGE == step


def test_one_new_session_one_exact_submission_no_coaching_or_old_session(tmp_path, monkeypatch):
    state = fixture_client(monkeypatch)
    report = smoke.run("http://127.0.0.1:7001", tmp_path / "fresh.json")
    assert state["created"] == 1 and len(state["requests"]) == 1
    assert state["requests"][0] == {"message": smoke.MESSAGE, "dataset_ids": ["open-uci-iris"],
                                    "client_message_id": report["client_message_id"]}
    assert report["automated_checks_passed"]
    assert report["scientific_scope_and_facts_review"] == "requires_independent_review"
    assert "原始数据核对结果" not in (tmp_path / "fresh.json").read_text()


@pytest.mark.parametrize("fault,code", [("partial", "authoritative_outcome_not_succeeded"),
    ("missing_outcome", "authoritative_outcome_not_succeeded"),
    ("plot", "unrequested_chart_or_report_delivered"),
    ("transport", "acceptance_transport_or_execution_failed")])
def test_failures_are_retained_and_not_resubmitted(tmp_path, monkeypatch, fault, code):
    state = fixture_client(monkeypatch, fault)
    path = tmp_path / "fresh.json"
    report = smoke.run("http://127.0.0.1:7001", path)
    assert not report["automated_checks_passed"] and code in report["errors"]
    assert len(state["requests"]) == 1 and report["automatic_retries"] == 0
    assert json.loads(path.read_text()) == report
    assert "PRIVATE ERROR" not in path.read_text()


def test_existing_report_is_never_reused(tmp_path, monkeypatch):
    path = tmp_path / "existing.json"
    path.write_text("previous failure")
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kwargs: pytest.fail("must not access network"))
    with pytest.raises(smoke.matrix.CheckFailure, match="use_new_absolute_report_path"):
        smoke.run("http://127.0.0.1:7001", path)
    assert path.read_text() == "previous failure"
