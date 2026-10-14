import hashlib

import pytest

from scripts.check_tabular_skill_chat import assess, PACKAGE_ID


def evidence():
    sample = b"sample"
    release = {"version": "1.0.0", "package_sha256": "a" * 64}
    events = [
        {"event": "tool", "data": {"function": "skill_read_script", "status": "called", "args": {"script_filename": "profile_csv.py"}}},
        {"event": "tool", "data": {"function": "shell_run", "status": "called"}},
        {"event": "done", "data": {}},
    ]
    runs = [{"status": "succeeded", "history_complete": True, "skills": [
        {"package_id": PACKAGE_ID, "version": "1.0.0", "sha256": "a" * 64}]}]
    report = {"schema_version": 1, "input_sha256": hashlib.sha256(sample).hexdigest(),
              "row_count": 4, "column_count": 3, "duplicate_rows": 1,
              "columns": [{"name": "id", "missing_count": 0}, {"name": "value", "missing_count": 2}, {"name": "label", "missing_count": 1}],
              "method": {"missing": "empty_or_whitespace", "duplicates": "exact_parsed_row_after_first", "dialect": "comma", "encoding": "utf-8-sig"}}
    return events, runs, report, release, sample


def test_complete_evidence_passes():
    assert assess(*evidence())["status"] == "passed"


def test_front_controller_rejection_followed_by_done_is_blocked():
    events, _, _, release, sample = evidence()
    events = [{"event": "message", "data": {"metadata": {"front_controller_error": {"reason": "unavailable"}}}}, events[-1]]
    result = assess(events, [], None, release, sample)
    assert result["status"] == "blocked"
    assert result["checks"]["stream_done"] is True
    assert result["checks"]["run_succeeded"] is False


@pytest.mark.parametrize("change", ["running", "wrong-release", "wrong-input", "wrong-count", "no-report", "no-tools", "no-done", "incomplete-history"])
def test_partial_or_mismatched_evidence_never_passes(change):
    events, runs, report, release, sample = evidence()
    if change == "running": runs[0]["status"] = "running"
    if change == "wrong-release": runs[0]["skills"][0]["sha256"] = "b" * 64
    if change == "wrong-input": report["input_sha256"] = "b" * 64
    if change == "wrong-count": report["duplicate_rows"] = 0
    if change == "no-report": report = None
    if change == "no-tools": events = events[-1:]
    if change == "no-done": events.pop()
    if change == "incomplete-history": runs[0]["history_complete"] = False
    assert assess(events, runs, report, release, sample)["status"] == "incomplete"
