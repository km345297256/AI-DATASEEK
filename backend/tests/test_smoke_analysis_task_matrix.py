"""Offline acceptance-oracle tests. These tests never invoke a real model."""
from io import BytesIO
import json
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image, ImageDraw
import pytest

from scripts import smoke_analysis_task_matrix as matrix

FOLLOWUP_ANSWER = json.dumps({"total_count": 150, "species_counts": {
    "Iris-setosa": 50, "Iris-versicolor": 50, "Iris-virginica": 50}, "max_mean_species": "Iris-virginica"})


def outcome(**changes):
    return {"status": "succeeded", "reason_code": "completed", "missing": [],
            "issues": [], "can_resume": False, **changes}


def message(role="assistant", text="finished", attachments=None, result=None, step_id="analysis"):
    return {"event": "message", "data": {"role": role, "content": text,
            "attachments": attachments or [],
            "metadata": {} if result is None else {"step_id": step_id, "analysis_outcome": result}}}


def png_bytes():
    image = Image.new("RGB", (320, 240), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 120, 180), fill="blue")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def csv_bytes(case_name="synthetic"):
    if case_name in {"synthetic", "partial"}:
        return b"count,mean,min,max\n6,7,2,12\n"
    return ("species,count,sepal_length_mean\n"
            "Iris-virginica,50,6.588000000\nIris-setosa,50,5.006000000\n"
            "Iris-versicolor,50,5.936000000\n").encode()


def client_for(case_name="synthetic", result=None, extra=None, payloads=None, answer="finished", history=None):
    calls = []
    case = matrix.CASES[case_name]
    blobs = {name: png_bytes() if name.endswith(".png") else csv_bytes(case_name) for name in case.filenames}
    blobs.update(payloads or {})
    attachments = [{"file_id": name, "filename": name, "size": len(data)} for name, data in blobs.items()]
    if extra:
        attachments.append({"file_id": "extra-source", "filename": extra})
    session = {"status": "completed", "events": [*(history or []), message("user"),
               message(text=answer, attachments=attachments, result=outcome() if result is None else result)]}

    def handle(request):
        calls.append((request.method, request.url.path))
        if "/sessions/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "data": session})
        name = request.url.path.split("/")[-2]
        return httpx.Response(200, content=blobs[name])
    client = httpx.Client(base_url="http://127.0.0.1:7001/api/v1", transport=httpx.MockTransport(handle))
    return client, calls, session


@pytest.mark.parametrize("name", ["synthetic", "iris"])
def test_strict_success_checks_exact_filenames_full_png_and_independent_statistics(name):
    client, calls, _ = client_for(name)
    result = matrix.verify_case(client, matrix.CASES[name], "new-session")
    assert result["passed"], result
    assert len(result["artifacts"]) == 2
    assert all(item["passed"] and len(item["sha256"]) == 64 for item in result["artifacts"])
    assert next(item for item in result["artifacts"] if "image" in item)["image"]["full_decode"]
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("changes", [
    {"status": "partial", "reason_code": "answer_validation_unavailable"},
    {"status": "failed"},
    {"missing": [{"kind": "image", "min_count": 1, "label": "plot"}]},
    {"issues": [{"artifact_name": "chart.png", "reason_code": "invalid_content", "blocking": True}]},
])
def test_valid_downloads_and_completed_transport_never_override_incomplete_authoritative_outcome(changes):
    client, _, _ = client_for(result=outcome(**changes))
    result = matrix.verify_case(client, matrix.CASES["synthetic"], "new-session")
    assert not result["passed"]
    assert "authoritative_outcome_not_succeeded" in result["errors"]
    assert all(item["passed"] for item in result["artifacts"])


def test_correct_requested_files_plus_unrequested_source_code_fails():
    client, _, _ = client_for(extra="analysis.py")
    result = matrix.verify_case(client, matrix.CASES["synthetic"], "new-session")
    assert not result["passed"]
    assert "delivered_filenames_do_not_exactly_match_request" in result["errors"]


@pytest.mark.parametrize("data,reason", [
    (b"count,mean,min,max\n6,8,2,12\n", "csv_wrong_mean"),
    (b"count,mean,min,max\n5,7,2,12\n", "csv_wrong_count"),
    (b"count,mean,min,max\n6,NaN,2,12\n", "csv_nonfinite_number"),
    (b"count,mean,min,max\n6,7,2\n", "csv_ragged_rows"),
    (b"count,mean,min,max\n6,\"7,2,12\n", "csv_not_complete_utf8"),
    (b"count,mean,min,max\n6,7,2,12\n6,7,2,12\n", "csv_wrong_row_count"),
    (b"n,average,min,max\n6,7,2,12\n", "csv_columns_do_not_match_request"),
])
def test_statistical_oracle_rejects_wrong_or_malformed_deliveries(data, reason):
    with pytest.raises(matrix.CheckFailure, match=reason):
        matrix.verify_statistics(matrix.CASES["synthetic"], data)


def test_iris_cannot_pass_with_total_count_only_or_missing_group():
    with pytest.raises(matrix.CheckFailure, match="csv_wrong_row_count"):
        matrix.verify_statistics(matrix.CASES["iris"], b"species,count,sepal_length_mean\nall,150,5.843333333\n")


@pytest.mark.parametrize("data", [b"not an image", png_bytes()[:60]])
def test_png_requires_complete_pixel_decode(data):
    with pytest.raises(matrix.CheckFailure, match="png_cannot_fully_decode"):
        matrix.verify_png(data)


def test_followup_ignores_previous_deliveries_but_requires_its_own_authoritative_outcome():
    history = [message("user"), message(attachments=[{"file_id": "old", "filename": "old.png"}], result=outcome())]
    client, _, session = client_for("followup", answer=FOLLOWUP_ANSWER, history=history)
    assert matrix.verify_case(client, matrix.CASES["followup"], "new-session")["passed"]
    session["events"][-1]["data"]["metadata"] = {}
    result = matrix.verify_case(client, matrix.CASES["followup"], "new-session")
    assert not result["passed"]
    assert "authoritative_outcome_not_succeeded" in result["errors"]


def test_followup_rejects_re_attached_old_files():
    client, _, _ = client_for("followup", extra="iris_species_summary.csv", answer=FOLLOWUP_ANSWER)
    assert not matrix.verify_case(client, matrix.CASES["followup"], "new-session")["passed"]


def test_report_inspection_is_get_only_and_checks_exact_recorded_turn(tmp_path, monkeypatch):
    report = {"run_id": "regression", "base_url": "http://127.0.0.1:7001", "cases": [
        {"case": "followup", "session_id": "one", "request": {"client_message_id": "followup-id"}}]}
    path = tmp_path / "report.json"
    matrix.save_report(path, report)
    client, calls, session = client_for("followup", answer=FOLLOWUP_ANSWER)
    session["events"][0]["data"]["metadata"]["client_message_id"] = "followup-id"
    monkeypatch.setattr(matrix.httpx, "Client", lambda **kwargs: client)
    result = matrix.inspect_report(path)
    assert result["passed"]
    assert len(result["cases"]) == 1
    assert calls == [("GET", "/api/v1/sessions/one")]
    assert json.loads(path.read_text()) == report


@pytest.mark.parametrize("url", ["https://example.com", "http://39.106.98.67:7000", "http://user:password@127.0.0.1:7001", "http://127.0.0.1:7001/private"])
def test_real_matrix_is_restricted_to_existing_local_service(url):
    with pytest.raises(matrix.CheckFailure, match="only_existing_loopback"):
        matrix.validate_base_url(url)


def test_report_redacts_host_paths_and_raw_issue_text():
    result = matrix.safe_outcomes({"/private/task": outcome(issues=[{
        "artifact_name": "/private/data/chart.png", "reason_code": "/private/error detail", "blocking": True}])})
    assert "/private" not in json.dumps(result)
    assert result["unavailable"]["issues"][0]["artifact_name"] == "chart.png"


def test_live_matrix_requires_new_report_and_new_iris_for_followup(tmp_path):
    path = tmp_path / "exists.json"
    path.write_text("{}")
    with pytest.raises(matrix.CheckFailure, match="report_already_exists"):
        matrix.run_matrix("http://127.0.0.1:7001", path)
    with pytest.raises(matrix.CheckFailure, match="followup_requires_new_iris"):
        matrix.run_matrix("http://127.0.0.1:7001", tmp_path / "new.json", ("followup",))


def test_read_only_facts_reject_wrong_group_counts_and_wrong_largest_group():
    assert matrix.verify_followup_facts(f"```json\n{FOLLOWUP_ANSWER}\n```")
    changed = json.loads(FOLLOWUP_ANSWER)
    changed["species_counts"]["Iris-setosa"] = 49
    assert not matrix.verify_followup_facts(json.dumps(changed))
    changed = json.loads(FOLLOWUP_ANSWER)
    changed["max_mean_species"] = "Iris-setosa"
    assert not matrix.verify_followup_facts(json.dumps(changed))


def test_recorded_iris_turn_is_not_replaced_by_later_followup_for_inspection():
    first = message("user")
    first["data"]["metadata"]["client_message_id"] = "iris-id"
    second = message("user")
    second["data"]["metadata"]["client_message_id"] = "followup-id"
    events = [first, message(result=outcome(status="partial")), second, message(result=outcome())]
    session = {"status": "completed", "events": events}
    selected = matrix.requested_turn(session, "iris-id")
    assert selected["events"] == events[:2]
    assert session["events"] == events
    with pytest.raises(matrix.CheckFailure, match="requested_turn_not_uniquely_recorded"):
        matrix.requested_turn(session, "unrecorded-id")


def live_harness(monkeypatch, *, stuck=False, terminal="done"):
    calls, sent, created = [], [], []

    def response(data):
        return httpx.Response(200, json={"code": 0, "data": data})

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/files"):
            return response({"file_id": "synthetic-input"})
        if request.method == "PUT" and request.url.path.endswith("/sessions"):
            session_id = f"new-{len(created) + 1}"
            created.append(session_id)
            return response({"session_id": session_id})
        if request.method == "PATCH" and request.url.path.endswith("/title"):
            assert json.loads(request.content)["title"].startswith("[回归验证]")
            return response({})
        if request.method == "POST" and request.url.path.endswith("/stop"):
            return response({})
        return response({"status": "running" if stuck else "completed"})

    client = httpx.Client(base_url="http://127.0.0.1:7001/api/v1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(matrix.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(matrix, "verify_public_iris_source", lambda client: {"dataset_id": matrix.IRIS_ID})

    def send(client, session_id, request, max_seconds):
        sent.append((session_id, dict(request)))
        if stuck:
            raise httpx.ReadTimeout("private server details must not be recorded")
        return {"terminal_event": terminal}

    def verify(client, case, session_id, client_message_id):
        return {"case": case.name, "session_id": session_id, "errors": [],
                "session_status": "running" if stuck else "completed", "passed": not stuck}

    monkeypatch.setattr(matrix, "send_once", send)
    monkeypatch.setattr(matrix, "verify_case", verify)
    return calls, sent, created


def test_live_matrix_creates_two_new_sessions_sends_three_turns_once_and_persists_requests(tmp_path, monkeypatch):
    _, sent, created = live_harness(monkeypatch)
    path = tmp_path / "matrix.json"
    result = matrix.run_matrix("http://127.0.0.1:7001", path)
    assert result["passed"]
    assert created == ["new-1", "new-2"]
    assert [session_id for session_id, _ in sent] == ["new-1", "new-2", "new-2"]
    assert len({request["client_message_id"] for _, request in sent}) == 3
    assert "attachments" in sent[0][1]
    assert sent[1][1]["dataset_ids"] == [matrix.IRIS_ID]
    assert "attachments" not in sent[2][1] and "dataset_ids" not in sent[2][1]
    assert json.loads(path.read_text()) == result
    assert [entry["request"] for entry in result["cases"]] == [request for _, request in sent]


def test_unconfirmed_stop_halts_matrix_instead_of_overlapping_or_retrying_real_tasks(tmp_path, monkeypatch):
    calls, sent, created = live_harness(monkeypatch, stuck=True)
    path = tmp_path / "failed.json"
    result = matrix.run_matrix("http://127.0.0.1:7001", path)
    assert not result["passed"]
    assert created == ["new-1"] and len(sent) == 1
    assert ("POST", "/api/v1/sessions/new-1/stop") in calls
    assert "matrix_halted_unconfirmed_task_state" in result["cases"][0]["errors"]
    assert "private server details" not in path.read_text()


def test_error_terminal_never_passes_even_if_session_claims_success(tmp_path, monkeypatch):
    live_harness(monkeypatch, terminal="error")
    result = matrix.run_matrix("http://127.0.0.1:7001", tmp_path / "error.json", ("synthetic",))
    assert not result["passed"]
    assert "stream_terminal_not_done" in result["cases"][0]["errors"]


def partial_outcome(**changes):
    return outcome(status="partial", reason_code="artifacts_missing",
                   missing=[{"kind": "table", "label": "task_matrix_missing_summary.csv", "min_count": 1}],
                   issues=[{"artifact_name": "task_matrix_missing_summary.csv",
                            "reason_code": "missing_artifact", "blocking": True}], **changes)


PARTIAL_ANSWER = ("value 已完成，提供 task_matrix_value_summary.csv。missing_measurement 列不存在，"
                  "未生成 task_matrix_missing_summary.csv。")


def test_expected_partial_requires_correct_available_statistics_and_explicit_missing_subset():
    client, _, _ = client_for("partial", result=partial_outcome(), answer=PARTIAL_ANSWER)
    result = matrix.verify_case(client, matrix.CASES["partial"], "new-session")
    assert result["passed"], result
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["statistics"]["values"][0]["mean"] == "7"


@pytest.mark.parametrize("changes", [
    {"status": "succeeded"},
    {"status": "failed"},
    {"missing": [], "issues": []},
])
def test_partial_case_rejects_false_success_total_failure_and_unspecified_missing_item(changes):
    result = partial_outcome()
    result.update(changes)
    client, _, _ = client_for("partial", result=result, answer=PARTIAL_ANSWER)
    checked = matrix.verify_case(client, matrix.CASES["partial"], "new-session")
    assert not checked["passed"]
    assert "authoritative_partial_or_explicit_missing_item_absent" in checked["errors"]


def test_partial_case_rejects_fabricated_missing_column_file_and_wrong_available_statistics():
    client, _, _ = client_for("partial", result=partial_outcome(), answer=PARTIAL_ANSWER,
                            extra="task_matrix_missing_summary.csv",
                            payloads={"task_matrix_value_summary.csv": b"count,mean,min,max\n6,999,2,12\n"})
    result = matrix.verify_case(client, matrix.CASES["partial"], "new-session")
    assert not result["passed"]
    assert "csv_wrong_mean" in result["errors"]
    assert "delivered_filenames_do_not_exactly_match_request" in result["errors"]


def test_expected_partial_does_not_mask_a_blocking_problem_in_the_completable_subset():
    result = partial_outcome()
    result["issues"].append({"artifact_name": "task_matrix_value_summary.csv",
                             "reason_code": "invalid_content", "blocking": True})
    client, _, _ = client_for("partial", result=result, answer=PARTIAL_ANSWER)
    assert not matrix.verify_case(client, matrix.CASES["partial"], "new-session")["passed"]


def test_stream_records_wall_clock_jump_without_replaying_or_changing_monotonic_deadline(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"event: done\ndata: {}\n\n")
        return httpx.Response(200, json={"code": 0, "data": {"status": "completed"}})

    monotonic = iter([0., 1., 2., 3., 4.])
    wall_times = iter([datetime(2026, 9, 17, tzinfo=timezone.utc),
                       datetime(2026, 9, 17, tzinfo=timezone.utc) + timedelta(seconds=64)])

    class Clock:
        @staticmethod
        def now(zone):
            return next(wall_times)

    monkeypatch.setattr(matrix.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(matrix, "datetime", Clock)
    with httpx.Client(base_url="http://local/api/v1", transport=httpx.MockTransport(handle)) as client:
        result = matrix.send_once(client, "new-session", {"message": "once"}, max_seconds=10)
    assert result["elapsed_seconds"] == 4
    assert result["wall_elapsed_seconds"] == 64
    assert result["clock_discontinuity_seconds"] == 60
    assert calls == ["POST", "GET"]
