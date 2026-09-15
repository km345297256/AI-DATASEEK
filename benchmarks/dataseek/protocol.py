"""Small, strict JSON contracts for the independent DataSeek pilot evaluator.

This module has no product imports. Paths in a task are relative artifact names;
dataset host paths and private gold files do not belong in run records.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any


class ProtocolError(ValueError):
    """A record cannot be interpreted without changing the frozen protocol."""


STATUSES = frozenset({"completed", "failed", "timeout", "cancelled", "budget_exceeded", "unavailable", "missing"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
DEFAULT_TOLERANCE = {"atol": 1e-6, "rtol": 1e-6}
_TASK_REQUIRED = {"id", "dataset_id", "domain", "source_family_id", "prompt", "expected", "required_artifacts"}
_TASK_OPTIONAL = {"tolerance", "field_tolerances", "comparison", "task_type", "difficulty", "split", "metadata", "input_files"}
_RUN_REQUIRED = {"run_id", "task_id", "method", "backbone_version", "run_index", "status"}
_RUN_OPTIONAL = {
    "cost_usd", "actual_input_tokens", "actual_output_tokens", "estimated_tokens",
    "model_call_count", "elapsed_seconds", "claimed_complete", "stop_reason", "metadata",
}


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{where} must be a JSON object with string keys")
    return value


def _keys(value: dict, required: set[str], optional: set[str], where: str) -> None:
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing or extra:
        raise ProtocolError(f"{where}: missing keys {sorted(missing)}; unsupported keys {sorted(extra)}")


def _string(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{where} must be a nonempty string")


def _identifier(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProtocolError(f"{where} must be a path-free identifier")


def finite_number(value: Any) -> bool:
    """JSON booleans are not measurements; reject all non-finite floats."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def validate_json_value(value: Any, where: str = "$", *, gold: bool = False) -> None:
    if value is None or type(value) is bool:
        if gold:
            raise ProtocolError(f"{where}: pilot gold supports numbers, strings, lists and objects only")
        return
    if isinstance(value, str) or finite_number(value):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, f"{where}[{index}]", gold=gold)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            validate_json_value(item, f"{where}.{key}", gold=gold)
        return
    raise ProtocolError(f"{where} contains a non-JSON or non-finite value")


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"Non-finite JSON constant {value} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def loads_json(text: str) -> Any:
    """Unlike json.loads, reject duplicate keys, NaN, Infinity and overflow."""
    try:
        value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("Invalid strict JSON document") from exc
    validate_json_value(value)
    return value


def load_json(path: str | Path) -> Any:
    return loads_json(Path(path).read_text(encoding="utf-8"))


def validate_tolerance(value: Any, where: str = "tolerance") -> dict:
    value = _object(value, where)
    _keys(value, {"atol", "rtol"}, set(), where)
    for key in ("atol", "rtol"):
        if not finite_number(value[key]) or value[key] < 0:
            raise ProtocolError(f"{where}.{key} must be finite and nonnegative")
    return value


def _paths(value: Any, path: str = "") -> dict[str, Any]:
    """Field overrides use escaped JSON Pointer paths, e.g. /answer/mean."""
    result = {path: value}
    if isinstance(value, dict):
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            result.update(_paths(child, f"{path}/{escaped}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(_paths(child, f"{path}/{index}"))
    return result


def validate_comparison_options(expected: Any, tolerance: Any = None,
                                field_tolerances: Any = None, comparison: Any = None) -> None:
    validate_json_value(expected, gold=True)
    validate_tolerance(DEFAULT_TOLERANCE if tolerance is None else tolerance)
    paths = _paths(expected)
    for path, rule in _object({} if field_tolerances is None else field_tolerances, "field_tolerances").items():
        if path not in paths or type(paths[path]) is not float:
            raise ProtocolError("Field tolerances must name an existing floating-point gold field using JSON Pointer")
        validate_tolerance(rule, f"field_tolerances[{path}]")
    for path, mode in _object({} if comparison is None else comparison, "comparison").items():
        if mode != "set" or path not in paths or not isinstance(paths[path], list):
            raise ProtocolError("comparison supports only 'set' on existing gold arrays")
        items = paths[path]
        if not all(isinstance(item, str) or finite_number(item) for item in items):
            raise ProtocolError("Set comparison supports finite scalar numbers and strings only")
        if len({(type(item).__name__, str(item)) for item in items}) != len(items):
            raise ProtocolError("A gold set must not contain duplicates")
        # Exact sets avoid ambiguous matching of floating-point tolerances.
        if any(type(item) is float for item in items):
            raise ProtocolError("Set comparison supports strings and integers; use ordered arrays for floats")


def validate_artifact_name(name: Any) -> str:
    _string(name, "artifact name")
    path = PurePosixPath(name)
    if (path.is_absolute() or "\\" in name or ":" in name or "\x00" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ProtocolError("Artifact names must be safe relative POSIX paths")
    return name


def validate_task(task: Any) -> dict:
    task = _object(task, "task")
    _keys(task, _TASK_REQUIRED, _TASK_OPTIONAL, "task")
    for key in ("id", "dataset_id", "domain", "source_family_id"):
        _identifier(task[key], f"task.{key}")
    _string(task["prompt"], "task.prompt")
    expected = _object(task["expected"], "task.expected")
    if set(expected) != {"answer"}:
        raise ProtocolError("task.expected must contain exactly the 'answer' wrapper")
    validate_comparison_options(expected, task.get("tolerance"), task.get("field_tolerances"), task.get("comparison"))
    artifacts = task["required_artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ProtocolError("task.required_artifacts must be a nonempty array")
    names = set()
    for artifact in artifacts:
        _object(artifact, "artifact")
        _keys(artifact, {"name", "kind"}, set(), "artifact")
        name = validate_artifact_name(artifact["name"])
        if name in names:
            raise ProtocolError("Duplicate required artifact name")
        names.add(name)
        if not isinstance(artifact["kind"], str) or artifact["kind"] not in {"json", "code"}:
            raise ProtocolError("Pilot artifact kinds are 'json' and 'code' only")
    if {"name": "answer.json", "kind": "json"} not in artifacts:
        raise ProtocolError("Every pilot task must require answer.json of kind json")
    if "input_files" in task:
        if not isinstance(task["input_files"], list) or not task["input_files"]:
            raise ProtocolError("task.input_files must be a nonempty array when provided")
        inputs = [validate_artifact_name(name) for name in task["input_files"]]
        if len(set(inputs)) != len(inputs):
            raise ProtocolError("Duplicate input file name")
    for key in ("task_type", "difficulty", "split"):
        if key in task:
            _string(task[key], f"task.{key}")
    if "metadata" in task:
        _object(task["metadata"], "task.metadata")
        validate_json_value(task["metadata"])
    return task


def validate_tasks(tasks: Any) -> list[dict]:
    if not isinstance(tasks, list) or not tasks:
        raise ProtocolError("tasks must be a nonempty list")
    ids = set()
    for task in tasks:
        validate_task(task)
        if task["id"] in ids:
            raise ProtocolError("Duplicate task id")
        ids.add(task["id"])
    return tasks


def validate_run(run: Any) -> dict:
    run = _object(run, "run")
    _keys(run, _RUN_REQUIRED, _RUN_OPTIONAL, "run")
    for key in ("run_id", "task_id", "method"):
        _identifier(run[key], f"run.{key}")
    _string(run["backbone_version"], "run.backbone_version")
    if type(run["run_index"]) is not int or run["run_index"] < 0:
        raise ProtocolError("run.run_index must be a nonnegative integer")
    if not isinstance(run["status"], str) or run["status"] not in STATUSES:
        raise ProtocolError(f"Unknown terminal run status; expected one of {sorted(STATUSES)}")
    for key in ("cost_usd", "elapsed_seconds"):
        if run.get(key) is not None and (not finite_number(run[key]) or run[key] < 0):
            raise ProtocolError(f"run.{key} must be null or finite and nonnegative")
    for key in ("actual_input_tokens", "actual_output_tokens", "estimated_tokens", "model_call_count"):
        if run.get(key) is not None and (type(run[key]) is not int or run[key] < 0):
            raise ProtocolError(f"run.{key} must be null or a nonnegative integer")
    if run.get("claimed_complete") is not None and type(run["claimed_complete"]) is not bool:
        raise ProtocolError("run.claimed_complete must be boolean or null")
    if "stop_reason" in run and run["stop_reason"] is not None:
        _string(run["stop_reason"], "run.stop_reason")
    if "metadata" in run:
        _object(run["metadata"], "run.metadata")
        validate_json_value(run["metadata"])
    return run
