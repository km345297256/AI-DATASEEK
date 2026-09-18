"""Opt-in, three-turn synthetic analysis/follow-up acceptance on localhost:7001.

--live is mandatory. One invocation uploads only the six hard-coded synthetic
rows and creates exactly one NEW [回归验证] session. It never uses dataset IDs,
existing sessions, old reports, or automatic model retries. An observation
failure stops only its own newly created session. The observation timeout is a
test-run bound, not an analysis-runtime quota.

The detailed follow-up is the user's exact sentence, without format coaching.
It may reference an unchanged original artifact under the same file identity;
such a reference is not evidence that a new file was produced.
The last follow-up asks for explanations plus inline JSON, independently checked
against Decimal statistics. Original uploaded delivery bytes are re-downloaded
and hashed after each follow-up; this does not attest sandbox working-file bytes.
Reports omit raw tool output/model text. Private answer-review provenance is not
invented when the public API does not expose it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

try:
    from . import smoke_analysis_task_matrix as matrix
except ImportError:
    import smoke_analysis_task_matrix as matrix


DETAIL_MESSAGE = "把刚才的结果解释得更详细一点"
METHOD_MESSAGE = (
    "刚才的 count、mean、min、max 分别表示什么？均值、最小值和最大值的统计含义及计算方法有什么区别，"
    "它们与柱状图中每个标签的原始 value 又有什么区别？"
    "不要生成文件，也不要重新附上上一轮的文件。请先用普通文字解释，"
    "再在正文中直接给出一个 JSON 对象，键为 count、mean、min、max，值为本会话数据的实际统计值。"
    "不要把 JSON 写入文件。"
)
TURNS = (
    matrix.CASES["synthetic"],
    matrix.Case("detailed-followup", DETAIL_MESSAGE, ()),
    matrix.Case("methods-followup", METHOD_MESSAGE, ()),
)
STAT_KEYS = frozenset({"count", "mean", "min", "max"})


def _unique_object(pairs):
    value = {}
    for key, child in pairs:
        matrix.require(key not in value, "inline_json_duplicate_key")
        value[key] = child
    return value


def _invalid_constant(_value):
    raise matrix.CheckFailure("inline_json_nonfinite_number")


def verify_inline_statistics(answer):
    """Validate a single whole statistics object, not four isolated numbers."""
    decoder = json.JSONDecoder(parse_int=Decimal, parse_float=Decimal,
        parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
    objects, position = [], 0
    while position < len(answer):
        start = answer.find("{", position)
        if start < 0:
            break
        try:
            value, length = decoder.raw_decode(answer[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        position = start + length
        if isinstance(value, dict) and STAT_KEYS.intersection(value):
            objects.append(value)
    matrix.require(len(objects) == 1, "inline_statistics_object_not_unique")
    actual = objects[0]
    matrix.require(set(actual) == STAT_KEYS, "inline_statistics_keys_mismatch")
    expected = matrix.oracle_rows("synthetic")[0]
    for key, value in actual.items():
        matrix.require(isinstance(value, Decimal) and value.is_finite(), "inline_statistics_not_numeric")
        matrix.require(value == expected[key], "inline_statistics_wrong_" + key)
    return {"oracle": "stdlib_decimal", "verified": True,
            "values": {key: str(expected[key]) for key in sorted(STAT_KEYS)}}


def public_review_metadata(session):
    """Report only actually exposed metadata, without guessing private history."""
    reviews = []
    for event in session.get("events", []):
        data = event.get("data") or {}
        metadata = data.get("metadata") or {}
        candidate = metadata.get("answer_review")
        if not isinstance(candidate, dict):
            continue
        item = {"status": matrix.safe_code(candidate.get("status"))}
        if isinstance(candidate.get("reason"), str):
            item["reason"] = matrix.safe_code(candidate["reason"])
        for key in ("source_count", "file_count", "paragraph_count", "historical_source_count", "prior_review_count"):
            if type(candidate.get(key)) is int and 0 <= candidate[key] <= 1_000_000:
                item[key] = candidate[key]
        reviews.append(item)
    return {"available": bool(reviews), "reviews": reviews,
            "history_source_verification": "not_asserted_from_public_api"}


def verify_original_artifacts(client, originals):
    checks = []
    for original in originals:
        data = matrix.download_bytes(client, original["file_id"])
        digest = hashlib.sha256(data).hexdigest()
        matrix.require(digest == original["sha256"] and len(data) == original["bytes"],
                       "original_uploaded_artifact_changed")
        checks.append({"filename": original["filename"], "file_id": original["file_id"],
                       "sha256": digest, "bytes": len(data), "unchanged": True})
    matrix.require(len(checks) == 2, "original_artifact_baseline_incomplete")
    return checks


def verify_original_references(client, session, originals):
    """Allow only byte-identical references, not new files with familiar names.

    Inspect every public attachment before deduplication so a conflicting name
    cannot be hidden by a later message carrying the same opaque file id.
    """
    baseline = {item["file_id"]: item for item in originals}
    matrix.require(len(originals) == len(baseline) == 2, "original_artifact_baseline_incomplete")
    references = {}
    for event in session.get("events", []):
        data = event.get("data") or {}
        if event.get("event") != "message" or data.get("role") != "assistant":
            continue
        attached = data.get("attachments") or []
        matrix.require(isinstance(attached, list), "followup_attachment_malformed")
        for item in attached:
            matrix.require(isinstance(item, dict) and isinstance(item.get("file_id"), str)
                           and isinstance(item.get("filename"), str), "followup_attachment_malformed")
            original = baseline.get(item["file_id"])
            matrix.require(original is not None, "followup_attachment_not_original")
            matrix.require(item["filename"] == original["filename"], "followup_attachment_identity_mismatch")
            if item["file_id"] in references:
                continue
            content = matrix.download_bytes(client, item["file_id"])
            digest = hashlib.sha256(content).hexdigest()
            matrix.require(len(content) == original["bytes"] and digest == original["sha256"],
                           "followup_original_reference_changed")
            if isinstance(item.get("size"), int):
                matrix.require(item["size"] == len(content), "download_differs_from_declared_size")
            references[item["file_id"]] = {"file_id": item["file_id"], "filename": original["filename"],
                "bytes": len(content), "sha256": digest, "origin": "unchanged_original_reference", "passed": True}
    return list(references.values())


def verify_turn(client, case, session_id, client_message_id, originals):
    result = matrix.verify_case(client, case, session_id, client_message_id)
    session = matrix.api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
    session = matrix.requested_turn(session, client_message_id)
    answer = matrix.latest_answer(session)
    result["answer_summary"] = {"characters": len(answer),
                                "sha256": hashlib.sha256(answer.encode()).hexdigest()}
    result["answer_review"] = public_review_metadata(session)
    if any(item["status"] == "unavailable" for item in result["answer_review"]["reviews"]):
        result["errors"].append("exposed_answer_review_unavailable")
    if case.name == "detailed-followup":
        # The exact second-turn request does not prohibit existing references.
        # Replace only the matrix's empty-filename expectation after stronger
        # identity + byte checks; every outcome/review error remains blocking.
        try:
            references = verify_original_references(client, session, originals)
            result["referenced_original_artifacts"] = references
            result["errors"] = [error for error in result["errors"]
                                if error != "delivered_filenames_do_not_exactly_match_request"]
        except matrix.CheckFailure as exc:
            result["errors"].append(str(exc))
        except httpx.HTTPError:
            result["errors"].append("artifact_download_failed")
    if case.name != "synthetic":
        # No required prose layout/JSON is imposed on the exact second-turn
        # question. A status-only one-liner is not a detailed explanation.
        if len(answer.strip()) < 80:
            result["errors"].append("followup_explanation_not_substantive")
        try:
            result["original_uploaded_artifacts"] = verify_original_artifacts(client, originals)
        except matrix.CheckFailure as exc:
            result["errors"].append(str(exc))
        if case.name == "methods-followup":
            try:
                result["inline_statistics"] = verify_inline_statistics(answer)
            except matrix.CheckFailure as exc:
                result["errors"].append(str(exc))
    result["passed"] = not result["errors"]
    return result


def _stop_own_session(client, session_id, report):
    if session_id is None:
        return
    try:
        matrix.api_data(client.post(f"/sessions/{quote(session_id, safe='')}/stop", timeout=15))
        state = matrix.api_data(client.get(f"/sessions/{quote(session_id, safe='')}", timeout=15))
        matrix.require(state.get("status") == "completed", "diagnostic_stop_unconfirmed")
        report["stopped_own_diagnostic_after_failure"] = True
    except Exception:
        report["errors"].append("diagnostic_stop_unconfirmed")


def run_followup(base_url, output, max_seconds=600):
    api_base = matrix.validate_base_url(base_url)
    output = Path(output)
    matrix.require(output.is_absolute(), "report_path_must_be_absolute")
    matrix.require(not output.exists() and not output.is_symlink(), "report_already_exists_use_new_path")
    matrix.require(type(max_seconds) is int and 30 <= max_seconds <= 900, "invalid_observation_timeout")
    report = {"schema_version": 1, "run_id": uuid4().hex, "report_path": str(output),
        "started_at": datetime.now(timezone.utc).isoformat(), "base_url": base_url,
        "scope": "new_synthetic_upload_only", "automatic_retries": 0, "turns": [], "errors": [], "passed": False}
    matrix.save_report(output, report)
    matrix.emit({"report_path": str(output), "state": "creating_new_synthetic_session"})
    session_id, originals = None, []
    with httpx.Client(base_url=api_base, timeout=30, trust_env=False) as client:
        try:
            created = matrix.api_data(client.put("/sessions", json={}))
            session_id = created["session_id"]
            matrix.require(isinstance(session_id, str) and bool(session_id), "new_session_identity_missing")
            report["session_id"] = session_id
            matrix.save_report(output, report)
            matrix.api_data(client.patch(f"/sessions/{quote(session_id, safe='')}/title",
                json={"title": "[回归验证] 合成数据连续追问验收"}))
            uploaded = matrix.api_data(client.post("/files", files={
                "file": ("task_matrix_input.csv", matrix.SYNTHETIC_SOURCE, "text/csv")}))
            report["source"] = {"filename": "task_matrix_input.csv", "file_id": uploaded["file_id"],
                "rows": 6, "bytes": len(matrix.SYNTHETIC_SOURCE),
                "sha256": hashlib.sha256(matrix.SYNTHETIC_SOURCE).hexdigest()}
            for index, case in enumerate(TURNS):
                request = {"message": case.message, "client_message_id": uuid4().hex}
                if index == 0:
                    request["attachments"] = [{"file_id": uploaded["file_id"], "filename": "task_matrix_input.csv"}]
                entry = {"case": case.name, "request": request, "passed": False, "errors": [], "submitted_once": True}
                report["turns"].append(entry)
                matrix.save_report(output, report)  # Freeze request identity before any chargeable invocation.
                matrix.emit({"session_id": session_id, "turn": index + 1, "state": "submitting_once"})
                entry["stream"] = matrix.send_once(client, session_id, request, max_seconds)
                checked = verify_turn(client, case, session_id, request["client_message_id"], originals)
                entry.update(checked)
                if entry["stream"]["terminal_event"] != "done":
                    entry["errors"].append("stream_terminal_not_done")
                    entry["passed"] = False
                if index == 0 and entry["passed"]:
                    originals = [dict(item) for item in entry["artifacts"]]
                matrix.save_report(output, report)
                matrix.emit({"turn": index + 1, "passed": entry["passed"], "errors": entry["errors"]})
                if not entry["passed"]:
                    if entry.get("session_status") != "completed":
                        _stop_own_session(client, session_id, report)
                    break  # Never hide a failed round by continuing or rerunning it.
            report["passed"] = len(report["turns"]) == 3 and all(item["passed"] for item in report["turns"])
        except BaseException as exc:
            code = matrix.safe_code(str(exc) if isinstance(exc, matrix.CheckFailure) else type(exc).__name__)
            report["errors"].append(code)
            if report["turns"]:
                report["turns"][-1]["passed"] = False
                report["turns"][-1]["errors"].append(code)
            _stop_own_session(client, session_id, report)
            if not isinstance(exc, Exception):
                raise
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            matrix.save_report(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-seconds", type=int, default=600)
    options = parser.parse_args()
    result = run_followup(options.base_url, options.output, options.max_seconds)
    matrix.emit(result)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
