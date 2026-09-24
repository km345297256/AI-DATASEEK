"""Offline persistence compatibility gate; never opens stores or model clients.

Schemas prove structural compatibility only. Every accepted transition also
requires a human explanation and a committed behavior/migration test. Snapshot
history is append-only; do not regenerate an accepted snapshot after a change.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import get_args

from pydantic import TypeAdapter

from app.domain.models.event import AgentEvent, PlanEvent, StepEvent
from app.domain.models.input_admission import InputAdmission
from app.domain.models.memory import Memory
from app.domain.models.plan import Plan, Step
from app.domain.services.execution_history import ExecutionHistory


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize(value, key=""):
    """Ignore documentation/ordering, not defaults, constraints or field names."""
    if key in {"default", "const"}:
        return value  # arbitrary JSON data, not schema keywords
    if key == "enum" and isinstance(value, list):
        return sorted(value, key=canonical)
    if isinstance(value, dict):
        return {name: normalize(item, name) for name, item in sorted(value.items())
                if key in {"properties", "$defs"} or name not in {"title", "description", "examples", "$comment"}}
    if isinstance(value, list):
        items = [normalize(item) for item in value]
        return sorted(items, key=canonical) if key in {"required", "enum", "anyOf", "oneOf", "allOf"} else items
    return value


def writer_version(model) -> int:
    schema = model.model_json_schema()
    value = schema.get("properties", {}).get("version", {}).get("const", 0)
    return value if type(value) is int else 0


def current_catalog() -> dict:
    event_versions = {writer_version(model) for model in get_args(AgentEvent)}
    if len(event_versions) != 1:
        raise ValueError("Agent event writer versions must agree")
    definitions = {
        "AgentEvent": (TypeAdapter(AgentEvent), event_versions.pop(), "durable"),
        "Plan": (TypeAdapter(Plan), writer_version(PlanEvent), "event-owned"),
        "Step": (TypeAdapter(Step), writer_version(StepEvent), "event-owned"),
        "Memory": (TypeAdapter(Memory), writer_version(Memory), "durable"),
        "InputAdmission": (TypeAdapter(InputAdmission), writer_version(InputAdmission), "durable"),
        "ExecutionHistory": (TypeAdapter(ExecutionHistory), writer_version(ExecutionHistory), "rebuildable"),
    }
    roots = {}
    for name, (adapter, version, storage) in definitions.items():
        contract = {"writer_version": version, "storage": storage,
                    "read_schema": normalize(adapter.json_schema(mode="validation")),
                    "write_schema": normalize(adapter.json_schema(mode="serialization"))}
        roots[name] = {**contract, "sha256": hashlib.sha256(canonical(contract).encode()).hexdigest()}
    return {"format_version": 1, "roots": roots}


def schema_changes(before, after, path="") -> list[dict]:
    """Conservative backward-read classification. Unknown edits are breaking."""
    return _schema_changes(normalize(before), normalize(after), path)


def _schema_changes(before, after, path="") -> list[dict]:
    if before == after:
        return []
    change = lambda classification, at=path: {"path": at or "/", "classification": classification}
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            location = f"{path}/{key}"
            if key == "properties" and isinstance(before.get(key, {}), dict) and isinstance(after.get(key, {}), dict):
                old, new = before.get(key, {}), after.get(key, {})
                for name in sorted(old.keys() | new.keys()):
                    if name not in old:
                        result.append(change("breaking" if name in after.get("required", []) else "compatible", f"{location}/{name}"))
                    elif name not in new:
                        result.append(change("breaking", f"{location}/{name}"))
                    else:
                        result.extend(_schema_changes(old[name], new[name], f"{location}/{name}"))
            elif key == "$defs" and isinstance(before.get(key, {}), dict) and isinstance(after.get(key, {}), dict):
                old, new = before.get(key, {}), after.get(key, {})
                for name in sorted(old.keys() | new.keys()):
                    if name not in old:
                        result.append(change("compatible", f"{location}/{name}"))
                    elif name not in new:
                        result.append(change("breaking", f"{location}/{name}"))
                    else:
                        result.extend(_schema_changes(old[name], new[name], f"{location}/{name}"))
            elif key == "required":
                old, new = set(before.get(key, [])), set(after.get(key, []))
                if old != new:
                    result.append(change("compatible" if new <= old else "breaking", location))
            elif key not in before or key not in after:
                result.append(change("breaking", location))
            elif before[key] != after[key]:
                if key in {"default", "const"}:
                    # These values are arbitrary JSON payloads, not schemas.
                    # A payload key named enum/properties must not turn a
                    # changed constant/default into a compatible schema edit.
                    result.append(change("breaking", location))
                elif key == "enum" and set(map(canonical, before[key])) <= set(map(canonical, after[key])):
                    result.append(change("compatible", location))
                # Widening oneOf can make an old value match TWO branches and
                # therefore become invalid. Only anyOf is monotonic here.
                elif key == "anyOf" and set(map(canonical, before[key])) <= set(map(canonical, after[key])):
                    result.append(change("compatible", location))
                else:
                    result.extend(_schema_changes(before[key], after[key], location))
        return result
    return [change("breaking")]


def compare_catalogs(before: dict, after: dict) -> list[dict]:
    changes = []
    old, new = before["roots"], after["roots"]
    for name in sorted(old.keys() | new.keys()):
        if name not in old or name not in new:
            changes.append({"root": name, "classification": "compatible" if name not in old else "breaking",
                            "before_version": old.get(name, {}).get("writer_version", 0),
                            "after_version": new.get(name, {}).get("writer_version", 0), "paths": ["/"]})
            continue
        details = []
        for field in ("read_schema", "write_schema"):
            details.extend(schema_changes(old[name][field], new[name][field], f"/{field}"))
        if old[name]["storage"] != new[name]["storage"]:
            details.append({"path": "/storage", "classification": "breaking"})
        if details or old[name]["writer_version"] != new[name]["writer_version"]:
            changes.append({"root": name,
                "classification": "breaking" if any(item["classification"] == "breaking" for item in details) else "compatible",
                "before_version": old[name]["writer_version"], "after_version": new[name]["writer_version"],
                "paths": sorted({item["path"] for item in details})})
    return changes


def validate_catalog(catalog: dict) -> None:
    if catalog.get("format_version") != 1 or not catalog.get("roots"):
        raise ValueError("Invalid persistence catalog")
    for root in catalog["roots"].values():
        body = {key: value for key, value in root.items() if key != "sha256"}
        if hashlib.sha256(canonical(body).encode()).hexdigest() != root.get("sha256"):
            raise ValueError("Persistence snapshot digest mismatch")


def check_history(repo: Path, current: dict) -> list[dict]:
    """Verify frozen snapshots, acknowledged transitions, and exact freshness."""
    directory = repo / "docs/persistence"
    manifest = json.loads((directory / "history.json").read_text())
    records = manifest.get("snapshots", [])
    if manifest.get("format_version") != 1 or not records:
        raise ValueError("Missing persistence history")
    previous = None
    seen = set()
    for entry in records:
        filename = entry.get("file", "")
        if Path(filename).name != filename or not filename.endswith(".json") or filename in seen:
            raise ValueError("Invalid or duplicate snapshot file")
        seen.add(filename)
        snapshot = json.loads((directory / filename).read_text())
        validate_catalog(snapshot)
        if previous is not None:
            changes = compare_catalogs(previous, snapshot)
            explanations = entry.get("changes", {})
            if set(explanations) != {item["root"] for item in changes}:
                raise ValueError("Every changed root requires an explicit compatibility record")
            for change in changes:
                acknowledgement = explanations[change["root"]]
                if not all(isinstance(acknowledgement.get(key), str) and acknowledgement[key].strip()
                           for key in ("reason", "compatibility", "test")):
                    raise ValueError("Incomplete persistence compatibility explanation")
                test = Path(acknowledgement["test"])
                if test.is_absolute() or ".." in test.parts or not str(test).startswith("backend/tests/test_") or not (repo / test).is_file():
                    raise ValueError("Compatibility record must name a committed behavior/migration test")
                if change["after_version"] < change["before_version"]:
                    raise ValueError("Persistence writer versions cannot go backwards")
                if change["classification"] == "breaking" and change["after_version"] <= change["before_version"]:
                    raise ValueError(f"{change['root']}: breaking persistence change requires a higher writer version")
        previous = snapshot
    validate_catalog(current)
    return compare_catalogs(previous, current)
