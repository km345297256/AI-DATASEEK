"""Read-only, secret-free runtime provenance and session usage for experiments.

The host needs only Python's standard library and Docker. Database credentials
stay inside the existing backend container; no service is started or changed.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

DEFAULT_BACKEND_CONTAINER = "ai-dataseek-backend-1"
SETTING_NAMES = (
    "model_provider", "model_name", "temperature", "max_tokens",
    "execution_max_tokens", "tool_preset_id", "tool_selection_mode",
    "code_mode_enabled", "domain_subagents_enabled",
    "model_context_capacity_tokens", "model_context_safety_tokens",
    "spill_enabled", "analysis_jobs_enabled", "sandbox_isolation",
)
KEY_FILES = (
    "core/config.py", "interfaces/api/session_routes.py",
    "interfaces/dependencies.py", "domain/services/agents/base.py",
    "domain/services/agents/execution.py", "domain/services/flows/plan_act.py",
    "infrastructure/external/llm/chat_model.py",
    "domain/services/tools/tool_selection.py",
    "domain/services/agent_task_runner.py", "domain/services/model_runtime.py",
    "domain/models/model_trace.py",
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_USAGE_FIELDS = {
    "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens",
    "total_tokens": "total_tokens",
}


class EnvironmentInspectionError(RuntimeError):
    """A sanitized error: never includes Docker stderr or database endpoints."""


def _run(args: list[str], *, source: str | None = None, timeout: float = 30) -> str:
    try:
        result = subprocess.run(args, input=source, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentInspectionError(
            f"Read-only environment inspection failed ({type(exc).__name__})"
        ) from None
    if result.returncode:
        raise EnvironmentInspectionError(
            f"Read-only environment inspection exited with status {result.returncode}"
        ) from None
    return result.stdout


def _docker_python(source: str, container: str, timeout: float) -> Any:
    if not isinstance(container, str) or not _SAFE_ID.fullmatch(container):
        raise ValueError("Invalid backend container name")
    output = _run(["docker", "exec", "-i", container, "python", "-"],
                  source=source, timeout=timeout)
    try:
        return json.loads(output)
    except (json.JSONDecodeError, TypeError):
        raise EnvironmentInspectionError("Backend returned invalid inspection data") from None


def public_runtime_snapshot(
    backend_container: str = DEFAULT_BACKEND_CONTAINER,
    *, repo_root: str | Path | None = None, timeout: float = 30,
) -> dict[str, Any]:
    """Capture whitelisted effective settings, image ID and live/repo SHA-256s.

    This reports deployment defaults. A selected Agent Profile or a task's
    execution snapshot must be checked separately for per-session overrides.
    """
    source = (
        "import hashlib, json, pathlib, app\n"
        "from app.core.config import get_settings\n"
        f"names = {SETTING_NAMES!r}\nfiles = {KEY_FILES!r}\n"
        "s = get_settings()\nroot = pathlib.Path(app.__file__).parent\n"
        "digests = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() "
        "if (root/name).is_file() else None for name in files}\n"
        "print(json.dumps({'settings': {name: getattr(s, name, None) for name in names}, "
        "'file_sha256': digests}))\n"
    )
    raw = _docker_python(source, backend_container, timeout)
    if not isinstance(raw, dict) or not isinstance(raw.get("settings"), dict) or not isinstance(raw.get("file_sha256"), dict):
        raise EnvironmentInspectionError("Backend snapshot shape is invalid")
    settings = {name: raw["settings"].get(name) for name in SETTING_NAMES}
    if any(value is not None and type(value) not in (str, int, float, bool) for value in settings.values()):
        raise EnvironmentInspectionError("Backend settings contain invalid public values")
    live = {name: raw["file_sha256"].get(name) for name in KEY_FILES}
    if any(value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)) for value in live.values()):
        raise EnvironmentInspectionError("Backend source fingerprints are invalid")
    image_id = _run(["docker", "inspect", "--type", "container", "--format", "{{.Image}}", backend_container],
                    timeout=timeout).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise EnvironmentInspectionError("Backend image identity is invalid")
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    repository = {}
    for name in KEY_FILES:
        path = root / "backend" / "app" / name
        repository[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    matches = {name: bool(live[name] and repository[name] and live[name] == repository[name]) for name in KEY_FILES}
    return {
        "schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
        "backend_container": backend_container, "backend_image_id": image_id,
        "settings": settings, "runtime_file_sha256": live,
        "repository_file_sha256": repository, "file_matches": matches,
        "all_key_files_match": all(matches.values()),
        "limitations": ["Deployment defaults only; selected profiles may override task settings.",
                        "No model request was made; model availability was not tested.",
                        "Image identity and source hashes do not establish dataset or sandbox-image identity."],
    }


def _session_ids(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("session_ids must be a list or tuple")
    if len(values) > 1000 or any(not isinstance(v, str) or not _SAFE_ID.fullmatch(v) for v in values):
        raise ValueError("Provide at most 1000 valid exact session IDs")
    return list(dict.fromkeys(values))


def _usage_pipeline(session_ids: list[str]) -> list[dict[str, Any]]:
    group: dict[str, Any] = {"_id": "$session_id", "record_count": {"$sum": 1}}
    for public, stored in _USAGE_FIELDS.items():
        value = "$" + stored
        valid = {"$and": [{"$in": [{"$type": value}, ["int", "long"]]}, {"$gte": [value, 0]}]}
        group[public + "_known_sum"] = {"$sum": {"$cond": [valid, value, 0]}}
        group[public + "_known_records"] = {"$sum": {"$cond": [valid, 1, 0]}}
    return [{"$match": {"session_id": {"$in": session_ids}}}, {"$group": group}]


def _normalise_usage(session_ids: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise EnvironmentInspectionError("Invalid session usage response")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("_id") not in session_ids or row["_id"] in by_id:
            raise EnvironmentInspectionError("Usage response is outside the requested session scope")
        expected = ["record_count"] + [field + suffix for field in _USAGE_FIELDS for suffix in ("_known_sum", "_known_records")]
        if any(type(row.get(key)) is not int or row[key] < 0 for key in expected):
            raise EnvironmentInspectionError("Usage response contains invalid counts")
        if any(row[field + "_known_records"] > row["record_count"] for field in _USAGE_FIELDS):
            raise EnvironmentInspectionError("Usage record coverage is invalid")
        by_id[row["_id"]] = row
    sessions = {}
    for sid in session_ids:
        row = by_id.get(sid, {})
        count = row.get("record_count", 0)
        item = {"record_count": count, "has_records": bool(count)}
        for field in _USAGE_FIELDS:
            known_count = row.get(field + "_known_records", 0)
            known_sum = row.get(field + "_known_sum", 0)
            item[field] = known_sum if count and known_count == count else None
            item[field + "_known_sum"] = known_sum if known_count else None
            item[field + "_known_records"] = known_count
        sessions[sid] = item
    totals: dict[str, Any] = {"record_count": sum(item["record_count"] for item in sessions.values()),
                              "requested_session_count": len(session_ids),
                              "sessions_with_records": sum(item["has_records"] for item in sessions.values())}
    for field in _USAGE_FIELDS:
        totals[field] = sum(item[field] for item in sessions.values()) if sessions and all(item[field] is not None for item in sessions.values()) else None
        values = [item[field + "_known_sum"] for item in sessions.values() if item[field + "_known_sum"] is not None]
        totals[field + "_known_sum"] = sum(values) if values else None
    return {
        "schema_version": 1, "source": "mongodb.token_usage", "session_ids": session_ids,
        "sessions": sessions, "totals": totals, "physical_request_coverage": "unknown",
        "complete_provider_billing": False,
        "limitations": [
            "Exact session IDs only; unrelated sessions and records without a matching session_id are excluded.",
            "Recorded provider usage is not a complete billing ledger: missing usage metadata, failed/cancelled requests or failed persistence may be absent.",
            "Zero records means unknown token use (null), not zero cost. Known sums are partial when a session or field is missing.",
            "Record count is not a verified physical provider-call count. All records for a selected session are included, so use one new session per experimental run.",
            "Never add these totals to model-trace totals; the two sources overlap.",
        ],
    }


def collect_session_usage(
    session_ids: Sequence[str], backend_container: str = DEFAULT_BACKEND_CONTAINER,
    *, timeout: float = 30,
) -> dict[str, Any]:
    """Read Mongo's token_usage for the exact provided sessions, without traces."""
    ids = _session_ids(session_ids)
    if not ids:
        return _normalise_usage([], [])
    pipeline = json.dumps(_usage_pipeline(ids), ensure_ascii=True)
    source = (
        "import json\nfrom pymongo import MongoClient\n"
        "from app.core.config import get_settings\n"
        f"pipeline = json.loads({pipeline!r})\n"
        "s = get_settings()\noptions = {'serverSelectionTimeoutMS': 10000, 'socketTimeoutMS': 15000}\n"
        "if s.mongodb_username and s.mongodb_password:\n"
        "    options.update(username=s.mongodb_username, password=s.mongodb_password)\n"
        "with MongoClient(s.mongodb_uri, **options) as client:\n"
        "    rows = list(client[s.mongodb_database]['token_usage'].aggregate(pipeline, maxTimeMS=10000))\n"
        "print(json.dumps(rows))\n"
    )
    rows = _docker_python(source, backend_container, timeout)
    return _normalise_usage(ids, rows)
