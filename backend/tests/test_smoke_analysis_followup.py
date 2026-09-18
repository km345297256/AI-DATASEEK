"""Offline three-turn acceptance tests; all HTTP/model behavior is synthetic."""
import hashlib
import json
import sys

import httpx
import pytest

from scripts import smoke_analysis_followup as followup
from scripts import smoke_analysis_task_matrix as matrix
from test_smoke_analysis_task_matrix import csv_bytes, message, outcome, png_bytes


FACTS = {"count": 6, "mean": 7, "min": 2, "max": 12}
DETAIL = (
    "这份合成数据包含六个标签，每个标签对应一个数值。count 表示非空数值的观测数量，结果为 6；"
    "mean 表示这些观测的算术平均值，结果为 7；min 为 2，max 为 12。"
    "柱状图展示了各标签的原始数值，统计表则把全部观测汇总为总体描述。"
    "这些指标描述当前六个观测，不应将其解释为总体因果关系或超出样本的推断。"
)
METHOD = DETAIL + "\n\n均值使用总和除以数量，最小值和最大值来自排序后的端点。\n" + json.dumps(FACTS)


def harness(monkeypatch, *, fault=None, fail_turn=0, exposed_review=False):
    state = {"calls": [], "sent": [], "created": [], "events": [], "status": "completed", "stopped": False}
    blobs = {"summary-id": csv_bytes(), "plot-id": png_bytes()}
    names = {"summary-id": "task_matrix_summary.csv", "plot-id": "task_matrix_bar.png"}

    def ok(data):
        return httpx.Response(200, json={"code": 0, "data": data})

    def handle(request):
        path, method = request.url.path, request.method
        state["calls"].append((method, path))
        if method == "PUT" and path.endswith("/sessions"):
            state["created"].append("new-synthetic-session")
            return ok({"session_id": "new-synthetic-session"})
        if method == "PATCH" and path.endswith("/title"):
            assert json.loads(request.content)["title"].startswith("[回归验证]")
            return ok({})
        if method == "POST" and path.endswith("/files"):
            assert matrix.SYNTHETIC_SOURCE in request.content and b"task_matrix_input.csv" in request.content
            state["upload_body"] = request.content
            return ok({"file_id": "new-upload"})
        if method == "POST" and path.endswith("/stop"):
            state["stopped"] = True
            state["status"] = "completed"
            return httpx.Response(503) if fault == "stop_unconfirmed" else ok({})
        if method == "POST" and path.endswith("/chat"):
            body = json.loads(request.content)
            index = len(state["sent"])
            state["sent"].append(body)
            if index == fail_turn and fault in {"timeout", "stop_unconfirmed", "interrupt"}:
                state["status"] = "running"
                if fault == "interrupt":
                    raise KeyboardInterrupt()
                raise httpx.ReadTimeout("PRIVATE_PROVIDER_BODY /private/secret", request=request)
            user = message("user", text=body["message"])
            user["data"]["metadata"]["client_message_id"] = body["client_message_id"]
            attached = [{"file_id": key, "filename": names[key], "size": len(data)} for key, data in blobs.items()] if index == 0 else []
            if index == fail_turn and fault in {"reattached", "reattached_partial", "reattached_new_id",
                                                "reattached_renamed", "reattached_changed", "reattached_same_size", "reattached_extra"}:
                attached = [{"file_id": "summary-id", "filename": names["summary-id"]}]
                if fault == "reattached_new_id":
                    blobs["new-summary-id"] = blobs["summary-id"]
                    attached[0]["file_id"] = "new-summary-id"
                elif fault == "reattached_renamed":
                    attached[0]["filename"] = "different-name.csv"
                elif fault == "reattached_extra":
                    attached.append({"file_id": "extra-file", "filename": "additional.png"})
            if index == fail_turn and fault == "reattached_both":
                attached = [{"file_id": key, "filename": names[key], "size": len(data)} for key, data in blobs.items()]
            actual_outcome = outcome()
            if index == fail_turn and fault in {"partial", "reattached_partial"}:
                actual_outcome = outcome(status="partial", reason_code="answer_validation_unavailable")
            answer = ["count=6, mean=7, min=2, max=12.", DETAIL, METHOD][index]
            if index == fail_turn and fault == "wrong_inline":
                answer = DETAIL + json.dumps({**FACTS, "mean": 99})
            if index == fail_turn and fault == "no_explanation":
                answer = "任务已完成。"
            assistant = message(text=answer, attachments=attached, result=actual_outcome)
            if exposed_review:
                assistant["data"]["metadata"]["answer_review"] = {"status": "verified", "source_count": 2,
                    "prior_review_count": 1, "PRIVATE_RAW_SOURCES": "/private/path"}
            if index == fail_turn and fault == "missing_outcome":
                assistant["data"]["metadata"] = {}
            state["events"].extend([user, assistant])
            if index == fail_turn and fault in {"mutated_output", "reattached_changed"}:
                blobs["summary-id"] = b"count,mean,min,max\n6,999,2,12\n"
            if index == fail_turn and fault == "reattached_same_size":
                blobs["summary-id"] = b"count,mean,min,max\n6,8,2,12\n"
            terminal = "error" if index == fail_turn and fault == "error_terminal" else "done"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=f"event: {terminal}\ndata: {{}}\n\n".encode())
        if method == "GET" and "/sessions/" in path:
            return ok({"status": state["status"], "events": state["events"]})
        if method == "GET" and path.endswith("/download"):
            file_id = path.split("/")[-2]
            return httpx.Response(200, content=blobs[file_id])
        raise AssertionError(f"Unexpected request: {method} {path}")

    client = httpx.Client(base_url="http://127.0.0.1:7001/api/v1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(followup.httpx, "Client", lambda **kwargs: client)
    return state


def test_three_turn_run_uses_one_new_session_exact_followup_and_only_synthetic_upload(tmp_path, monkeypatch):
    state = harness(monkeypatch)
    path = tmp_path / "new-followup.json"
    result = followup.run_followup("http://127.0.0.1:7001", path)
    assert result["passed"], result
    assert state["created"] == ["new-synthetic-session"]
    assert len(state["sent"]) == 3 and len({item["client_message_id"] for item in state["sent"]}) == 3
    assert state["sent"][0]["message"] == matrix.CASES["synthetic"].message
    assert state["sent"][1]["message"] == "把刚才的结果解释得更详细一点"
    assert state["sent"][2]["message"] == followup.METHOD_MESSAGE
    assert state["sent"][0]["attachments"] == [{"file_id": "new-upload", "filename": "task_matrix_input.csv"}]
    assert all(set(item) == {"message", "client_message_id"} for item in state["sent"][1:])
    assert all("/datasets/" not in path for _, path in state["calls"])
    assert not state["stopped"] and result["automatic_retries"] == 0
    assert result["source"]["sha256"] == hashlib.sha256(matrix.SYNTHETIC_SOURCE).hexdigest()
    assert result["source"]["rows"] == 6
    assert result["report_path"] == str(path) and json.loads(path.read_text()) == result
    assert all(turn["outcomes"]["analysis"]["status"] == "succeeded" for turn in result["turns"])
    assert all(not turn["delivered_filenames"] for turn in result["turns"][1:])
    for turn in result["turns"][1:]:
        assert len(turn["original_uploaded_artifacts"]) == 2
        assert all(item["unchanged"] for item in turn["original_uploaded_artifacts"])
        assert turn["answer_review"] == {"available": False, "reviews": [],
                                        "history_source_verification": "not_asserted_from_public_api"}
    assert result["turns"][2]["inline_statistics"]["verified"]
    assert DETAIL not in path.read_text() and METHOD not in path.read_text()


@pytest.mark.parametrize("fault,fail_turn,code", [
    ("partial", 0, "authoritative_outcome_not_succeeded"),
    ("missing_outcome", 1, "authoritative_outcome_not_succeeded"),
    ("partial", 1, "authoritative_outcome_not_succeeded"),
    ("reattached", 2, "delivered_filenames_do_not_exactly_match_request"),
    ("reattached_partial", 1, "authoritative_outcome_not_succeeded"),
    ("reattached_new_id", 1, "followup_attachment_not_original"),
    ("reattached_renamed", 1, "followup_attachment_identity_mismatch"),
    ("reattached_extra", 1, "followup_attachment_not_original"),
    ("reattached_changed", 1, "followup_original_reference_changed"),
    ("reattached_same_size", 1, "followup_original_reference_changed"),
    ("mutated_output", 1, "original_uploaded_artifact_changed"),
    ("no_explanation", 1, "followup_explanation_not_substantive"),
    ("error_terminal", 0, "stream_terminal_not_done"),
    ("wrong_inline", 2, "inline_statistics_wrong_mean"),
])
def test_failure_is_preserved_and_prevents_following_turns_or_automatic_retries(tmp_path, monkeypatch, fault, fail_turn, code):
    state = harness(monkeypatch, fault=fault, fail_turn=fail_turn)
    path = tmp_path / "failed.json"
    result = followup.run_followup("http://127.0.0.1:7001", path)
    assert not result["passed"] and len(state["sent"]) == fail_turn + 1
    assert len(state["created"]) == 1 and result["automatic_retries"] == 0
    assert code in result["turns"][-1]["errors"]
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize("fault,reference_count", [("reattached", 1), ("reattached_both", 2)])
def test_detail_followup_accepts_only_unchanged_original_references_without_counting_new_delivery(
        tmp_path, monkeypatch, fault, reference_count):
    state = harness(monkeypatch, fault=fault, fail_turn=1)
    prior_reports = [tmp_path / f"prior-failure-{number}.json" for number in range(3)]
    for path in prior_reports:
        path.write_text(json.dumps({"passed": False, "errors": ["original_failure"], "identity": path.name}))
    before = {path: path.read_bytes() for path in prior_reports}
    report = followup.run_followup("http://127.0.0.1:7001", tmp_path / "new-reference-report.json")
    assert report["passed"] and len(state["sent"]) == 3
    assert {path: path.read_bytes() for path in prior_reports} == before
    detail = report["turns"][1]
    assert len(detail["referenced_original_artifacts"]) == reference_count
    assert detail["artifacts"] == []  # Existing references do not prove new generation.
    baseline = {item["file_id"]: item for item in report["turns"][0]["artifacts"]}
    for reference in detail["referenced_original_artifacts"]:
        original = baseline[reference["file_id"]]
        assert all(reference[key] == original[key] for key in ("filename", "file_id", "bytes", "sha256"))
        assert reference["origin"] == "unchanged_original_reference" and reference["passed"]
    assert not report["turns"][2]["delivered_filenames"]


def test_reference_check_rejects_conflicting_identity_even_if_later_message_reuses_original_name():
    originals = [{"file_id": "original", "filename": "original.csv", "bytes": 1, "sha256": "a" * 64},
                 {"file_id": "plot", "filename": "plot.png", "bytes": 2, "sha256": "b" * 64}]
    session = {"events": [message(attachments=[{"file_id": "original", "filename": "renamed.csv"}]),
                          message(attachments=[{"file_id": "original", "filename": "original.csv"}])]}
    with pytest.raises(matrix.CheckFailure, match="followup_attachment_identity_mismatch"):
        followup.verify_original_references(None, session, originals)


@pytest.mark.parametrize("fault", ["timeout", "stop_unconfirmed"])
def test_exception_stops_only_own_new_diagnostic_and_never_resubmits(tmp_path, monkeypatch, fault):
    state = harness(monkeypatch, fault=fault, fail_turn=1)
    path = tmp_path / "observation-failed.json"
    result = followup.run_followup("http://127.0.0.1:7001", path)
    assert not result["passed"] and state["stopped"]
    assert len(state["sent"]) == 2 and len(state["created"]) == 1
    assert ("POST", "/api/v1/sessions/new-synthetic-session/stop") in state["calls"]
    assert "ReadTimeout" in result["errors"]
    assert "PRIVATE_PROVIDER_BODY" not in path.read_text() and "/private/secret" not in path.read_text()
    if fault == "stop_unconfirmed":
        assert "diagnostic_stop_unconfirmed" in result["errors"]


def test_keyboard_interrupt_stops_own_session_and_preserves_failed_report(tmp_path, monkeypatch):
    state = harness(monkeypatch, fault="interrupt", fail_turn=0)
    path = tmp_path / "interrupted.json"
    with pytest.raises(KeyboardInterrupt):
        followup.run_followup("http://127.0.0.1:7001", path)
    report = json.loads(path.read_text())
    assert not report["passed"] and report["errors"] == ["KeyboardInterrupt"]
    assert report["finished_at"] and state["stopped"] and len(state["sent"]) == 1


def test_exposed_review_fields_are_read_only_and_private_provenance_is_not_invented_or_logged(tmp_path, monkeypatch):
    harness(monkeypatch, exposed_review=True)
    report = followup.run_followup("http://127.0.0.1:7001", tmp_path / "public-review.json")
    assert report["passed"]
    for turn in report["turns"]:
        assert turn["answer_review"]["available"]
        assert turn["answer_review"]["reviews"] == [{"status": "verified", "source_count": 2, "prior_review_count": 1}]
    assert "PRIVATE_RAW_SOURCES" not in json.dumps(report) and "/private/path" not in json.dumps(report)


@pytest.mark.parametrize("answer", [
    "6 7 2 12", '{"count":6,"mean":8,"min":2,"max":12}',
    '{"count":true,"mean":7,"min":2,"max":12}',
    '{"count":"6","mean":7,"min":2,"max":12}',
    '{"count":6,"mean":NaN,"min":2,"max":12}',
    '{"count":6,"mean":7,"mean":8,"min":2,"max":12}',
    '{"count":6,"mean":7,"min":2}',
    '{"count":6,"mean":7,"min":2,"max":12,"guess":true}',
    json.dumps(FACTS) + "\n" + json.dumps({**FACTS, "mean": 999}),
])
def test_inline_oracle_rejects_isolated_numbers_wrong_values_types_duplicates_or_ambiguous_answers(answer):
    with pytest.raises(matrix.CheckFailure):
        followup.verify_inline_statistics(answer)


def test_inline_oracle_accepts_prose_and_a_single_fenced_json_without_forcing_detail_format():
    result = followup.verify_inline_statistics(DETAIL + "\n```json\n" + json.dumps(FACTS) + "\n```")
    assert result == {"oracle": "stdlib_decimal", "verified": True,
                      "values": {"count": "6", "max": "12", "mean": "7", "min": "2"}}


def test_report_path_must_be_new_absolute_and_live_flag_is_mandatory(tmp_path, monkeypatch):
    path = tmp_path / "exists.json"
    path.write_text("existing report")
    with pytest.raises(matrix.CheckFailure, match="report_already_exists"):
        followup.run_followup("http://127.0.0.1:7001", path)
    assert path.read_text() == "existing report"
    with pytest.raises(matrix.CheckFailure, match="report_path_must_be_absolute"):
        followup.run_followup("http://127.0.0.1:7001", "relative.json")
    monkeypatch.setattr(sys, "argv", ["smoke_analysis_followup.py", "--output", str(tmp_path / "new.json")])
    with pytest.raises(SystemExit) as exc:
        followup.main()
    assert exc.value.code == 2 and not (tmp_path / "new.json").exists()


@pytest.mark.parametrize("url", ["http://39.106.98.67:7000", "https://example.com", "http://127.0.0.1:8000"])
def test_script_never_contacts_other_hosts_or_exposes_another_frontend_port(tmp_path, url):
    with pytest.raises(matrix.CheckFailure, match="only_existing_loopback"):
        followup.run_followup(url, tmp_path / "new.json")
