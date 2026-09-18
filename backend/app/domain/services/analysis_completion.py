"""Deterministic completion checks, independent of model prose or tool names."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from app.domain.models.analysis_outcome import (
    AnalysisOutcome, ArtifactIssue, DeliverableRequirement, DELIVERABLE_LABELS,
    ARTIFACT_ISSUE_REASONS, safe_artifact_name,
)

KINDS = {
    "image": {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "svg", "avif"},
    "table": {"csv", "tsv", "xlsx", "xls", "parquet"},
    "report": {"md", "markdown", "txt", "json", "html", "htm", "pdf", "docx"},
    "code": {"py", "r", "js", "ts", "sh", "sql", "ipynb"},
}
LABELS = DELIVERABLE_LABELS
REASONS = {
    "artifacts_missing": "部分要求的成果还未完成。",
    "artifact_validation_failed": "部分文件尚未通过内容检查，暂不能计为已完成成果。",
    "answer_validation_unavailable": "本次结果说明尚未完成证据核验，暂不能作为已确认结论。",
    "analytical_requirements_missing": "部分要求的分析内容尚未完成。",
    "validation_unavailable": "暂时无法核验成果内容，完成情况尚未确认。",
    "delivery_failed": "部分成果文件尚未成功交付。",
    "tool_budget_exhausted": "本轮工具执行额度已用尽。",
    "analysis_budget_deadline_exceeded": "本次分析已达到最长执行时间，尚未完成的部分已停止。",
    "analysis_budget_store_unavailable": "暂时无法可靠记录执行额度，系统已安全停止后续操作。",
    "budget_no_progress_loop": "连续操作未产生新的有效进展，系统已停止重复尝试。",
    "tool_arguments_invalid": "工具参数校验未通过。",
    "tool_execution_failed": "工具执行失败。",
    "tool_execution_unknown": "部分操作的执行状态尚未确认，未将其计为已完成。",
    "finalization_timeout": "结果整理超时。",
    "finalization_failed": "结果整理失败。",
    "invalid_final_result": "结果格式未通过验证。",
    "invalid_execution_result": "模型未返回可验证的执行结果。",
    "tool_protocol_error": "模型未能发起有效的工具调用，相关操作未执行。",
    "execution_failed": "分析尚未完成。",
}


def artifact_kind(path: str) -> str | None:
    extension = PurePosixPath(path).suffix.lower().lstrip(".")
    return next((kind for kind, formats in KINDS.items() if extension in formats), None)


def requirements_for_step(step, message) -> list[DeliverableRequirement]:
    from app.domain.services.analysis_request_contract import current_artifact_requirement
    if current_artifact_requirement(message) is False:
        # A later router/model step cannot turn a current read-only request into
        # a new chart obligation, including after earlier turns produced files.
        return []
    declared = list(getattr(step, "deliverables", []) or [])
    requirements = [DeliverableRequirement.model_validate(item) for item in declared]
    intent = (step.inputs or {}).get("dataset_intent")
    policy = (step.inputs or {}).get("artifact_policy")
    # This is the existing authoritative route's minimum, not a filename-based
    # special case. A report or a saved script cannot satisfy visualization.
    if intent == "visualization" and not any(item.kind == "image" for item in requirements):
        requirements.append(DeliverableRequirement(kind="image"))
    elif policy == "required" and not requirements:
        requirements.append(DeliverableRequirement(kind="any"))
    return requirements


DIAGNOSTIC_KEYS = frozenset({
    "row_number", "line_number", "column_number", "byte_offset", "expected_columns", "actual_columns",
    "row_count", "column_count", "size_bytes", "limit_bytes", "max_rows", "max_columns", "max_cells",
    "actual_cells", "width", "height", "frames", "max_pixels", "max_frames", "sheet_count", "max_sheets",
    "archive_entries", "max_archive_entries", "archive_bytes", "max_archive_bytes",
})
UNAVAILABLE_REASONS = frozenset({"validation_unavailable", "validator_unavailable", "validation_deadline"})


def safe_diagnostics(value: Any) -> dict[str, int]:
    """Repair hints may contain bounded counts/positions, never data or paths."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in sorted(DIAGNOSTIC_KEYS) if key in value
            and type(value[key]) is int and 0 <= value[key] <= 2**63 - 1}


def _safe_output_path(path: Any) -> bool:
    return bool(isinstance(path, str) and path.startswith("/home/ubuntu/output/")
                and str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts
                and "\\" not in path and not any(ord(char) < 32 for char in path))


def _value(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _matches(requirement: DeliverableRequirement, kind: str, path: Any) -> bool:
    return bool((kind == requirement.kind or (requirement.kind == "any" and kind != "code"))
                and (not requirement.formats or (isinstance(path, str)
                    and PurePosixPath(path).suffix.lower().lstrip(".") in requirement.formats))
                and (not requirement.output_paths or path in requirement.output_paths
                     or requirement.min_count > len(requirement.output_paths)))


def _inspect_records(records, delivered, validation_available, *, _selected_deliveries=None):
    valid, failures, seen, conflicts = {}, [], {}, set()

    def failed(item, reason):
        item = item if isinstance(item, dict) else {}
        path = item.get("path")
        kind = item.get("expected_kind") or item.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            kind = artifact_kind(path) if isinstance(path, str) else None
        failures.append({"path": path, "kind": kind or "any",
                         "reason": reason if isinstance(reason, str) and reason in ARTIFACT_ISSUE_REASONS else "invalid_content",
                         "diagnostics": safe_diagnostics(item.get("diagnostics"))})

    for item in records:
        if not isinstance(item, dict):
            failed({}, "validation_receipt_invalid")
            continue
        path = item.get("path")
        if _safe_output_path(path):
            if path in seen:
                if seen[path] != item:
                    conflicts.add(path)
                continue
            seen[path] = item
        if item.get("valid") is not True:
            failed(item, item.get("reason", "invalid_content") if item.get("valid") is False else "validation_receipt_invalid")
            continue
        digest, size, kind = item.get("sha256"), item.get("size"), item.get("kind")
        suffix = PurePosixPath(path).suffix.lower().lstrip(".") if isinstance(path, str) else ""
        if (not _safe_output_path(path) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size <= 0 or not isinstance(kind, str) or kind not in KINDS
                or (suffix not in KINDS[kind] and not (kind == "table" and suffix == "json"))):
            failed(item, "validation_receipt_invalid")
            continue
        valid[path] = item
    for path in conflicts:
        valid.pop(path, None)
        failures = [item for item in failures if item["path"] != path]
        failed(seen[path], "validation_receipt_invalid")

    accepted, accepted_ids = [], set()
    for path, record in valid.items():
        matching = next((item for item in delivered if _value(item, "file_path") == path
            and isinstance(_value(item, "file_id"), str) and _value(item, "file_id").strip()
            and _value(item, "file_id") not in accepted_ids and type(_value(item, "size")) is int
            and _value(item, "size") == record["size"] and isinstance(_value(item, "metadata"), dict)
            and _value(item, "metadata").get("artifact_sha256") == record["sha256"]), None)
        if matching is not None:
            accepted.append(record)
            accepted_ids.add(_value(matching, "file_id"))
            if _selected_deliveries is not None:
                _selected_deliveries.append(matching)
        else:
            failed(record, "delivery_failed")
    for info in delivered:
        path = _value(info, "file_path")
        if not isinstance(path, str) or path not in seen:
            failed({"path": path}, "validation_receipt_invalid" if validation_available else "validation_unavailable")
    return accepted, failures


def verified_deliveries(records: list[dict], delivered, *, validation_available: bool = True) -> list[Any]:
    """Select the exact uploaded objects accepted by the completion contract.

    A path alone cannot bind an attachment to checked bytes. Keep receipt
    conflict handling, SHA/size checks and uploaded-ID deduplication in one
    implementation, including non-final event publication paths.
    """
    selected = []
    _inspect_records(records, list(delivered), validation_available, _selected_deliveries=selected)
    return selected


def _missing_requirements(requirements, accepted):
    # Each delivered artifact satisfies at most one count slot. Augmenting
    # paths handle overlaps (e.g. any + image) without greedy false failures.
    slots = [(index, path) for index, requirement in enumerate(requirements)
             for path in [*requirement.output_paths,
                          *([None] * (requirement.min_count - len(requirement.output_paths)))]]
    choices = []
    for index, required_path in slots:
        requirement = requirements[index]
        choices.append([position for position, item in enumerate(accepted)
                        if _matches(requirement, item["kind"], item["path"])
                        and (required_path is None or item["path"] == required_path)])
    assigned = {}
    def assign(slot, visited):
        for position in choices[slot]:
            if position in visited:
                continue
            visited.add(position)
            if position not in assigned or assign(assigned[position], visited):
                assigned[position] = slot
                return True
        return False
    for slot in sorted(range(len(slots)), key=lambda value: len(choices[value])):
        assign(slot, set())
    fulfilled = [0] * len(requirements)
    fulfilled_paths = [set() for _ in requirements]
    for slot in assigned.values():
        index, path = slots[slot]
        fulfilled[index] += 1
        if path is not None:
            fulfilled_paths[index].add(path)
    missing = []
    for index, requirement in enumerate(requirements):
        if fulfilled[index] < requirement.min_count:
            missing.append(requirement.model_copy(update={
                "min_count": requirement.min_count - fulfilled[index], "label": LABELS[requirement.kind],
                "output_paths": [path for path in requirement.output_paths if path not in fulfilled_paths[index]],
            }))
    return missing


def _assessment(requirements, records, delivered, validation_available):
    requirements = [DeliverableRequirement.model_validate(item) for item in requirements]
    if len(requirements) > 16:
        raise ValueError("Too many deliverable requirements")
    accepted, failures = _inspect_records(records, delivered, validation_available)
    missing = _missing_requirements(requirements, accepted)
    # Partial validation failure does not invalidate other exact-byte receipts.
    # Localized unavailable records are preferred to an unidentified batch fault.
    if (not validation_available and (requirements or records or delivered)
            and not any(item["reason"] in UNAVAILABLE_REASONS for item in failures)):
        failures.append({"path": None, "kind": "any", "reason": "validation_unavailable", "diagnostics": {}})
    feedback, seen = [], set()
    for item in failures:
        identity = (item["path"] if isinstance(item["path"], str) else None, item["kind"], item["reason"])
        if identity in seen:
            continue
        seen.add(identity)
        blocking = bool(missing) if item["path"] is None else any(
            _matches(requirement, item["kind"], item["path"]) for requirement in missing)
        issue = ArtifactIssue(artifact_name=safe_artifact_name(item["path"]), kind=item["kind"],
                              reason_code=item["reason"], blocking=blocking)
        feedback.append({**issue.model_dump(), "diagnostics": item["diagnostics"]})
    feedback.sort(key=lambda item: not item["blocking"])
    return accepted, missing, feedback[:64]


def issues_from_records(requirements, records: list[dict], delivered=(), *, validation_available: bool = True) -> list[ArtifactIssue]:
    """Public file diagnostics; never forward receipt paths or parser text."""
    return [ArtifactIssue.model_validate({key: value for key, value in item.items() if key != "diagnostics"})
            for item in _assessment(requirements, records, delivered, validation_available)[2]]


def repair_feedback(requirements, records: list[dict], delivered=(), *, validation_available: bool = True) -> list[dict]:
    """Sanitized repair hints. Exact target paths stay in the caller's private map."""
    return _assessment(requirements, records, delivered, validation_available)[2]


def blocking_receipt_paths(requirements, records: list[dict], delivered=(), *,
                           validation_available: bool = True) -> frozenset[str]:
    """Private exact identities of failed outputs relevant to missing requirements.

    Never match on display basenames: two different directories can contain
    identically named files with different validation and delivery states. This
    untruncated set is for internal repair selection, not public presentation.
    """
    requirements = [DeliverableRequirement.model_validate(item) for item in requirements]
    if len(requirements) > 16:
        raise ValueError("Too many deliverable requirements")
    accepted, failures = _inspect_records(records, delivered, validation_available)
    missing = _missing_requirements(requirements, accepted)
    return frozenset(item["path"] for item in failures if _safe_output_path(item["path"])
                     and any(_matches(requirement, item["kind"], item["path"]) for requirement in missing))


def assess_delivery(requirements, records: list[dict], delivered: list[Any], *,
                    execution_success: bool, stop_code: str = "", validation_available: bool = True) -> AnalysisOutcome:
    accepted, missing, feedback = _assessment(requirements, records, delivered, validation_available)
    issues = [ArtifactIssue.model_validate({key: value for key, value in item.items() if key != "diagnostics"}) for item in feedback]
    if not missing and execution_success:
        return AnalysisOutcome(status="succeeded", reason_code="completed", issues=issues)
    blocking = [item.reason_code for item in issues if item.blocking]
    if not execution_success and stop_code in REASONS:
        reason = stop_code
    elif any(reason in UNAVAILABLE_REASONS for reason in blocking):
        reason = "validation_unavailable"
    elif any(reason not in {"delivery_failed", "missing_artifact"} for reason in blocking):
        reason = "artifact_validation_failed"
    elif "delivery_failed" in blocking:
        reason = "delivery_failed"
    else:
        reason = "artifacts_missing" if missing else "execution_failed"
    return AnalysisOutcome(status="partial" if accepted else "failed", reason_code=reason, missing=missing, issues=issues)


def verified_delivery_counts(delivered_files: list[Any]) -> dict[str, int]:
    """Summarize the caller's content-verified uploads without disclosing paths.

    Accept internal FileInfo instances or equivalent dictionaries, not a model's
    claimed type/count list. Receipt validation still belongs to assess_delivery;
    the identity/digest checks here prevent incomplete upload objects and duplicate
    references from becoming misleading presentation counts.
    """
    counts = {kind: 0 for kind in (*KINDS, "any")}
    seen_ids, seen_paths = set(), set()
    for item in delivered_files:
        get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
        file_id, path, size, metadata = get("file_id"), get("file_path"), get("size"), get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        digest = metadata.get("artifact_sha256")
        if (not isinstance(file_id, str) or not file_id.strip() or file_id in seen_ids
                or not isinstance(path, str) or not path.startswith("/home/ubuntu/output/") or path in seen_paths
                or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts
                or "\\" in path or any(ord(char) < 32 for char in path)
                or type(size) is not int or size <= 0
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            continue
        seen_ids.add(file_id)
        seen_paths.add(path)
        kind = artifact_kind(path)
        # JSON is the one supported extension whose verified content may be a
        # data table or a report. An explicit receipt-derived kind disambiguates it.
        if PurePosixPath(path).suffix.lower() == ".json" and (get("kind") or metadata.get("artifact_kind")) == "table":
            kind = "table"
        counts[kind or "any"] += 1
    return {kind: count for kind, count in counts.items() if count}


def outcome_message(outcome: AnalysisOutcome, *, delivered_count: int = 0,
                    delivered_files: list[Any] | None = None) -> str:
    """A truthful status summary; do not replace verified analysis content with it."""
    complete = outcome.status == "succeeded" and not outcome.missing
    prefix = ("本次分析已完成。" if complete else
              "本次分析部分完成。" if outcome.status in {"partial", "succeeded"} else "本次分析未完成。")
    lines = [prefix]
    if not complete:
        lines.append(REASONS.get(outcome.reason_code, "本次分析尚未完成，具体原因暂未确认。"))
    if delivered_files is not None:
        counts = verified_delivery_counts(delivered_files)
        if counts:
            lines.append("已交付并保留：" + "、".join(f"{LABELS[kind]} × {count}" for kind, count in counts.items()) + "。")
    elif type(delivered_count) is int and delivered_count > 0:
        # Older callers have only a count. Do not invent file types or an
        # explanation about code when the actual delivered content is unknown.
        lines.append(f"已交付并保留 {delivered_count} 个文件。")
    if outcome.missing:
        lines.append("待完成：" + "、".join(f"{LABELS[item.kind]} × {item.min_count}" for item in outcome.missing) + "。")
    missing_names = list(dict.fromkeys(issue.artifact_name for issue in outcome.issues
                                      if issue.blocking and issue.reason_code == "missing_artifact"))
    if missing_names:
        def quoted_name(name):
            fence = "`" * (max((len(match.group()) for match in re.finditer(r"`+", name)), default=0) + 1)
            return f"{fence} {name} {fence}" if "`" in name else f"`{name}`"
        # These names come from exact required-path validation, never from an
        # executor's attachment claims. They are absent outputs, not downloads.
        lines.append("尚未生成的所需文件：" + "、".join(quoted_name(name) for name in missing_names[:8]) + "。")
    if (not complete and outcome.can_resume and isinstance(outcome.resume_from, str)
            and re.fullmatch(r"[a-f0-9]{32}", outcome.resume_from)):
        lines.append("可点击“继续未完成部分”，从本任务的已保存进度继续。")
    return "\n\n".join(lines)
