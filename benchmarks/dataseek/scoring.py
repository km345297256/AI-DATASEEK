"""Independent artifact scoring and repeat/source/domain macro aggregation.

No model calls, product completion flags, or gold-generating tools are used.
The evaluator checks JSON and (when required) Python syntax; it never executes
submitted code. Code correctness on a clean rerun is a separate future metric.
"""

from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path
from typing import Any

from .protocol import (
    DEFAULT_TOLERANCE, ProtocolError, finite_number, loads_json,
    validate_comparison_options, validate_json_value, validate_run, validate_task, validate_tasks,
)

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def verify_answer(expected: Any, actual: Any, tolerance: dict | None = None,
                  field_tolerances: dict | None = None, comparison: dict | None = None) -> dict:
    """Compare every required field; reject extra keys, wrong types and NaN.

    Integer gold is exact (1.0 is acceptable; True is not). Float gold uses
    abs(pred-gold) <= atol + rtol*abs(gold). Overrides use JSON Pointer paths
    such as /answer/mean. Lists are ordered unless marked as exact scalar sets.
    """
    validate_comparison_options(expected, tolerance, field_tolerances, comparison)
    rules = DEFAULT_TOLERANCE if tolerance is None else tolerance
    overrides = {} if field_tolerances is None else field_tolerances
    modes = {} if comparison is None else comparison
    checks: list[dict] = []

    def check(path: str, passed: bool, reason: str) -> None:
        checks.append({"path": path or "/", "passed": passed, "reason": reason})

    try:
        validate_json_value(actual)
    except ProtocolError:
        return {"passed": False, "checks": [{"path": "/", "passed": False, "reason": "non_json_or_non_finite_prediction"}]}

    def visit(gold: Any, pred: Any, path: str) -> None:
        if isinstance(gold, dict):
            if not isinstance(pred, dict):
                check(path, False, "expected_object")
                return
            missing, extra = gold.keys() - pred.keys(), pred.keys() - gold.keys()
            for key in sorted(missing):
                escaped = key.replace("~", "~0").replace("/", "~1")
                check(f"{path}/{escaped}", False, "missing_field")
            for key in sorted(extra):
                escaped = key.replace("~", "~0").replace("/", "~1")
                check(f"{path}/{escaped}", False, "unexpected_field")
            if not gold and not pred:
                check(path, True, "empty_object_equal")
            for key in gold:
                if key not in pred:
                    continue
                escaped = key.replace("~", "~0").replace("/", "~1")
                visit(gold[key], pred[key], f"{path}/{escaped}")
            return
        if isinstance(gold, list):
            if not isinstance(pred, list):
                check(path, False, "expected_array")
                return
            if modes.get(path) == "set":
                if not all(type(item) in {int, str} for item in pred):
                    check(path, False, "set_requires_strings_or_integers")
                    return
                typed_pred = {(type(item).__name__, item) for item in pred}
                typed_gold = {(type(item).__name__, item) for item in gold}
                passed = len(typed_pred) == len(pred) and typed_pred == typed_gold
                check(path, passed, "set_equal" if passed else "set_mismatch_or_duplicates")
                return
            if len(gold) != len(pred):
                check(path, False, "array_length_mismatch")
            if not gold and not pred:
                check(path, True, "empty_array_equal")
            for index, (wanted, found) in enumerate(zip(gold, pred)):
                visit(wanted, found, f"{path}/{index}")
            return
        if finite_number(gold):
            if not finite_number(pred):
                check(path, False, "expected_finite_number")
                return
            if type(gold) is int:
                passed = gold == pred
                check(path, passed, "integer_equal" if passed else "integer_mismatch")
                return
            local = overrides.get(path, rules)
            try:
                distance = abs(pred - gold)
                limit = local["atol"] + local["rtol"] * abs(gold)
                passed = math.isfinite(distance) and distance <= limit
            except OverflowError:
                passed = False
            check(path, passed, "numeric_within_tolerance" if passed else "numeric_outside_tolerance")
            return
        passed = isinstance(pred, str) and gold == pred
        check(path, passed, "string_equal" if passed else "string_mismatch")

    visit(expected, actual, "")
    return {"passed": bool(checks) and all(item["passed"] for item in checks), "checks": checks}


def _artifact_bytes(root: Path, name: str) -> bytes:
    base = root.resolve(strict=True)
    path = (base / name).resolve(strict=True)
    if not path.is_relative_to(base) or not path.is_file():
        raise ProtocolError("Artifact must be a regular file inside its run directory")
    if not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES:
        raise ProtocolError("Artifact is empty or exceeds the evaluator size limit")
    with path.open("rb") as stream:
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
    if not 0 < len(data) <= MAX_ARTIFACT_BYTES:
        raise ProtocolError("Artifact is empty or exceeds the evaluator size limit")
    return data


def score_run(task: dict, run: dict, artifact_root: str | Path) -> dict:
    """Score one finished run against its private task gold and real artifacts."""
    validate_task(task)
    validate_run(run)
    if task["id"] != run["task_id"]:
        raise ProtocolError("Run task_id does not match the scoring task")
    artifact_checks = []
    answer = None
    hashes = {}
    for artifact in task["required_artifacts"]:
        name, kind = artifact["name"], artifact["kind"]
        try:
            data = _artifact_bytes(Path(artifact_root), name)
            text = data.decode("utf-8")
            if kind == "json":
                parsed = loads_json(text)
                if name == "answer.json":
                    answer = parsed
            else:
                tree = ast.parse(text, filename=name)
                if not tree.body:
                    raise ProtocolError("Code artifact has no statements")
            hashes[name] = hashlib.sha256(data).hexdigest()
            artifact_checks.append({"name": name, "kind": kind, "passed": True, "reason": "valid"})
        except (OSError, ValueError, SyntaxError, RecursionError, RuntimeError):
            # Do not persist exceptions containing private host paths or gold.
            artifact_checks.append({"name": name, "kind": kind, "passed": False, "reason": "missing_unsafe_or_invalid_artifact"})
    answer_result = verify_answer(task["expected"], answer, task.get("tolerance"),
                                  task.get("field_tolerances"), task.get("comparison"))
    artifacts_ok = all(item["passed"] for item in artifact_checks)
    success = run["status"] == "completed" and artifacts_ok and answer_result["passed"]
    return {
        **run,
        "source_family_id": task["source_family_id"], "domain": task["domain"],
        "independent_success": success, "answer_passed": answer_result["passed"],
        "artifacts_passed": artifacts_ok, "criterion_checks": answer_result["checks"],
        "artifact_checks": artifact_checks, "artifact_hashes": hashes,
        "scorer_version": "pilot-objective-v1",
    }


_SCORE_FIELDS = {
    "source_family_id", "domain", "independent_success", "answer_passed", "artifacts_passed",
    "criterion_checks", "artifact_checks", "artifact_hashes", "scorer_version",
}


def _run_part(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in _SCORE_FIELDS}


def _identity(row: dict) -> tuple:
    return (row["method"], row["backbone_version"], row["task_id"], row["run_index"])


def _unique_rows(rows: list[dict], *, scored: bool) -> tuple[dict[str, dict], int]:
    by_id: dict[str, dict] = {}
    slots: dict[tuple, str] = {}
    duplicates = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ProtocolError("Run records must be objects")
        validate_run(_run_part(row) if scored else row)
        if scored:
            if type(row.get("independent_success")) is not bool:
                raise ProtocolError("Scored runs require boolean independent_success")
            if row["independent_success"] and row["status"] != "completed":
                raise ProtocolError("Non-completed runs cannot have independent_success")
            validate_json_value(row)
        run_id = row["run_id"]
        if run_id in by_id:
            if by_id[run_id] != row:
                raise ProtocolError("Conflicting records for the same run_id; refusing best-run selection")
            duplicates += 1
            continue
        slot = _identity(row)
        if slot in slots:
            raise ProtocolError("Two run_ids occupy the same method/model/task/repeat slot")
        by_id[run_id] = row
        slots[slot] = run_id
    return by_id, duplicates


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def aggregate_results(tasks: list[dict], runs: list[dict], expected_runs: list[dict] | None = None) -> dict:
    """Deduplicate scored rows and aggregate repeat -> task -> source -> domain.

    Pass the frozen run plan as expected_runs to count absent outputs as failures.
    With no plan, only observed rows are knowable; denominator_scope says so.
    Conflicting duplicates and unplanned runs are errors, never best-of-N choices.
    All failed/timeout/unavailable rows remain in all run denominators.
    """
    validate_tasks(tasks)
    if not isinstance(runs, list) or (expected_runs is not None and not isinstance(expected_runs, list)):
        raise ProtocolError("runs and expected_runs must be lists")
    task_by_id = {task["id"]: task for task in tasks}
    observed, duplicates = _unique_rows(runs, scored=True)
    rows = dict(observed)
    planned_duplicates = 0
    missing_ids = set()
    if expected_runs is not None:
        planned, planned_duplicates = _unique_rows(expected_runs, scored=False)
        for run_id, row in observed.items():
            if run_id not in planned or _identity(row) != _identity(planned[run_id]):
                raise ProtocolError("Observed run is not present in the frozen run plan")
        for run_id, plan in planned.items():
            if run_id not in rows:
                rows[run_id] = {**plan, "status": "missing", "independent_success": False,
                                "cost_usd": None, "actual_input_tokens": None, "actual_output_tokens": None,
                                "estimated_tokens": None, "model_call_count": None,
                                "elapsed_seconds": None, "claimed_complete": None}
                missing_ids.add(run_id)
    grouped: dict[tuple, list[dict]] = {}
    for row in rows.values():
        if row["task_id"] not in task_by_id:
            raise ProtocolError("Run refers to an unknown task")
        task = task_by_id[row["task_id"]]
        for key in ("source_family_id", "domain"):
            if key in row and row[key] != task[key]:
                raise ProtocolError("Scored run metadata does not match the frozen task")
        grouped.setdefault((row["method"], row["backbone_version"]), []).append(row)

    groups = []
    for (method, backbone), records in sorted(grouped.items()):
        task_runs: dict[str, list[int]] = {}
        for row in records:
            task_runs.setdefault(row["task_id"], []).append(int(row["independent_success"]))
        task_scores = {key: _mean(values) for key, values in sorted(task_runs.items())}
        source_tasks: dict[str, dict[str, list[float]]] = {}
        for task_id, score in task_scores.items():
            task = task_by_id[task_id]
            source_tasks.setdefault(task["domain"], {}).setdefault(task["source_family_id"], []).append(score)
        source_scores = {domain: {source: _mean(values) for source, values in sorted(sources.items())}
                         for domain, sources in sorted(source_tasks.items())}
        domain_scores = {domain: _mean(list(sources.values())) for domain, sources in source_scores.items()}
        total = len(records)
        successes = sum(row["independent_success"] for row in records)
        costs = [row["cost_usd"] for row in records if row.get("cost_usd") is not None]
        known_cost = sum(costs) if costs else None
        full_cost = known_cost if len(costs) == total else None
        claims = [row for row in records if row.get("claimed_complete") is True]
        false_claims = sum(not row["independent_success"] for row in claims)
        known_claims = sum(row.get("claimed_complete") is not None for row in records)
        actual = [row for row in records if row.get("actual_input_tokens") is not None and row.get("actual_output_tokens") is not None]
        status_counts = {status: sum(row["status"] == status for row in records)
                         for status in sorted({row["status"] for row in records})}
        group = {
            "method": method, "backbone_version": backbone,
            "total_runs": total, "observed_runs": total - sum(row["run_id"] in missing_ids for row in records),
            "missing_runs": sum(row["run_id"] in missing_ids for row in records),
            "successes": successes, "micro_tsr": successes / total,
            "macro_tsr": _mean(list(domain_scores.values())),
            "task_scores": task_scores, "source_scores": source_scores, "domain_scores": domain_scores,
            "all_repeats_success_rate": sum(all(values) for values in task_runs.values()) / len(task_runs),
            "status_counts": status_counts,
            "known_cost_usd": known_cost, "cost_coverage": len(costs) / total,
            "total_cost_usd": full_cost,
            "success_unit_cost_usd": full_cost / successes if full_cost is not None and successes else None,
            "success_unit_cost_reason": "no_successes" if not successes else ("missing_cost" if full_cost is None else None),
            "actual_usage_coverage": len(actual) / total,
            "known_actual_input_tokens": sum(row["actual_input_tokens"] for row in actual) if actual else None,
            "known_actual_output_tokens": sum(row["actual_output_tokens"] for row in actual) if actual else None,
            "total_actual_input_tokens": sum(row["actual_input_tokens"] for row in actual) if len(actual) == total else None,
            "total_actual_output_tokens": sum(row["actual_output_tokens"] for row in actual) if len(actual) == total else None,
            "claimed_complete_runs": len(claims), "false_complete_runs": false_claims,
            "false_completion_rate": false_claims / len(claims) if claims else None,
            "false_complete_all_rate": false_claims / total if known_claims == total else None,
            "completion_claim_coverage": len(claims) / total,
            "claim_annotation_coverage": known_claims / total,
        }
        groups.append(group)
    return {"groups": groups, "duplicate_run_records": duplicates,
            "duplicate_plan_records": planned_duplicates,
            "denominator_scope": "frozen_plan" if expected_runs is not None else "observed_runs_only"}
