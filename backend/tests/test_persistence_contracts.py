"""Snapshot gate, conservative classifier and pre-upgrade data round trips."""
import json
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models.event import AgentEvent, ToolEvent, ToolStatus
from app.domain.models.input_admission import InputAdmission
from app.domain.models.memory import Memory
from app.domain.models.plan import Plan, Step
from app.domain.models.session import Session
from app.domain.services.execution_history import ExecutionHistory
from app.domain.utils.persistence_contracts import (
    check_history, current_catalog, normalize, schema_changes, compare_catalogs, validate_catalog, canonical,
)
from app.interfaces.schemas.event import EventMapper

REPO = Path(__file__).resolve().parents[2]
ADAPTERS = {"AgentEvent": TypeAdapter(AgentEvent), "Plan": TypeAdapter(Plan), "Step": TypeAdapter(Step),
            "Memory": TypeAdapter(Memory), "InputAdmission": TypeAdapter(InputAdmission),
            "ExecutionHistory": TypeAdapter(ExecutionHistory)}


def test_frozen_catalog_matches_current_code():
    assert check_history(REPO, current_catalog()) == []


@pytest.mark.parametrize("name", ADAPTERS)
def test_frozen_legacy_records_read_and_roundtrip_without_silent_evidence_loss(name):
    fixtures = json.loads((Path(__file__).parent / "fixtures/persistence/legacy-records.json").read_text())
    adapter = ADAPTERS[name]
    value = adapter.validate_python(fixtures[name])
    encoded = adapter.dump_json(value)
    assert adapter.dump_json(adapter.validate_json(encoded)) == encoded
    if name == "Memory":
        assert value.messages[2].tool_call_id == value.messages[1].tool_calls[0]["id"]
        assert value.messages[2].content == "synthetic output"
    if name == "InputAdmission":
        assert value.execution_started is None  # old uncertainty never becomes NOT_STARTED
    if name == "AgentEvent":
        assert value.version == 1 and value.seq is None


def test_old_execution_cache_is_invalidated_not_misread_as_current():
    with pytest.raises(ValidationError):
        ExecutionHistory.model_validate({"version": 1, "seq": 0})


def test_additive_attempt_metadata_does_not_invent_recovery_for_pre_upgrade_events_or_cache():
    # Reuse the immutable pre-upgrade tool fixture, not a re-generated model
    # dump that would already contain the new optional field.
    replay = Path(__file__).parent / "fixtures/replays/versioned_session.jsonl"
    legacy_tool = json.loads(replay.read_text().splitlines()[2])["event"]
    assert "program_attempt" not in legacy_tool
    restored = ADAPTERS["AgentEvent"].validate_python(legacy_tool)
    assert isinstance(restored, ToolEvent) and restored.program_attempt is None
    cache = ExecutionHistory.model_validate({"version": 2, "seq": 4,
        "event_count": 1, "archive_events": [legacy_tool]})
    encoded = cache.model_dump_json()
    restored_cache = ExecutionHistory.model_validate_json(encoded)
    assert restored_cache.archive_events[0].program_attempt is None
    assert restored_cache.archive_events[0].function_result == legacy_tool["function_result"]
    assert restored_cache.model_dump_json() == encoded


def test_program_attempt_snapshot_is_an_additive_change_for_both_affected_roots():
    directory = REPO / "docs/persistence"
    before = json.loads((directory / "2026-09-24-baseline.json").read_text())
    after = json.loads((directory / "2026-09-24-program-attempt.json").read_text())
    changes = compare_catalogs(before, after)
    assert {change["root"] for change in changes} == {"AgentEvent", "ExecutionHistory"}
    for change in changes:
        assert change["classification"] == "compatible"
        assert change["before_version"] == change["after_version"]
        assert all("ProgramAttemptView" in path or path.endswith("/ToolEvent/properties/program_attempt")
                   for path in change["paths"])


def test_documentation_and_unordered_schema_elements_do_not_create_changes():
    a = {"description": "old", "properties": {"x": {"type": "string", "title": "X"}}, "required": ["x", "y"]}
    b = {"description": "new", "properties": {"x": {"type": "string", "title": "new"}}, "required": ["y", "x"]}
    assert normalize(a) == normalize(b)


def test_field_names_and_arbitrary_default_data_are_not_documentation():
    schema = {"type": "object", "title": "documentation", "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "payload": {"default": {"title": "value", "description": "data"}},
    }}
    normalized = normalize(schema)
    assert "title" not in normalized
    assert {"title", "description"} <= normalized["properties"].keys()
    assert normalized["properties"]["payload"]["default"] == {"title": "value", "description": "data"}
    changed = deepcopy(schema)
    changed["properties"]["title"]["type"] = "integer"
    assert schema_changes(schema, changed)[0]["classification"] == "breaking"


@pytest.mark.parametrize("mutate,classification", [
    (lambda v: v["properties"].update(y={"type": "string", "default": ""}), "compatible"),
    (lambda v: v["required"].append("y"), "breaking"),
    (lambda v: v["properties"]["x"].update(type="integer"), "breaking"),
    (lambda v: v["properties"]["x"].update(default="different"), "breaking"),
    (lambda v: v["properties"].pop("x"), "breaking"),
    (lambda v: v.update(additionalProperties=False), "breaking"),
])
def test_classifier_detects_required_enum_default_and_type_risks(mutate, classification):
    before = {"type": "object", "properties": {"x": {"type": "string", "default": ""}}, "required": ["x"]}
    after = deepcopy(before)
    mutate(after)
    changes = schema_changes(before, after)
    assert changes
    assert ("breaking" if any(item["classification"] == "breaking" for item in changes) else "compatible") == classification


def test_enum_widening_is_compatible_but_narrowing_is_breaking():
    assert schema_changes({"enum": ["a"]}, {"enum": ["a", "b"]})[0]["classification"] == "compatible"
    assert schema_changes({"enum": ["a", "b"]}, {"enum": ["a"]})[0]["classification"] == "breaking"


def test_adding_overlapping_oneof_branch_is_not_misclassified_as_safe():
    before = {"oneOf": [{"type": "integer"}]}
    after = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    assert schema_changes(before, after)[0]["classification"] == "breaking"


@pytest.mark.parametrize("key,before,after", [
    ("default", {"enum": ["a"]}, {"enum": ["a", "b"]}),
    ("default", {"required": ["a"]}, {"required": []}),
    ("const", {"properties": {}}, {"properties": {"foo": {"type": "string"}}}),
])
def test_arbitrary_json_default_and_const_changes_are_always_breaking(key, before, after):
    assert schema_changes({key: before}, {key: after}) == [{"path": f"/{key}", "classification": "breaking"}]


def test_snapshot_digest_detects_tampering():
    current = current_catalog()
    current["roots"]["Memory"]["writer_version"] = 999
    with pytest.raises(ValueError, match="digest"):
        validate_catalog(current)


def test_shared_step_change_is_reported_for_all_affected_owners():
    before = current_catalog()
    after = deepcopy(before)
    for name in ("AgentEvent", "Plan", "ExecutionHistory"):
        after["roots"][name]["read_schema"]["$defs"]["Step"]["required"] = ["new_required_field"]
    changes = compare_catalogs(before, after)
    assert {item["root"] for item in changes} == {"AgentEvent", "Plan", "ExecutionHistory"}
    assert all(item["classification"] == "breaking" for item in changes)


@pytest.mark.parametrize("acknowledged,bump,valid", [(False, False, False), (True, False, False), (True, True, True)])
def test_history_requires_per_root_explanation_test_and_real_writer_bump(tmp_path, acknowledged, bump, valid):
    directory = tmp_path / "docs/persistence"
    directory.mkdir(parents=True)
    test = tmp_path / "backend/tests/test_migration.py"
    test.parent.mkdir(parents=True)
    test.write_text("# synthetic migration test marker\n")
    before = current_catalog()
    after = deepcopy(before)
    root = after["roots"]["InputAdmission"]
    root["read_schema"]["required"].append("new_field")
    if bump:
        root["writer_version"] += 1
    root["sha256"] = hashlib.sha256(canonical({k: v for k, v in root.items() if k != "sha256"}).encode()).hexdigest()
    (directory / "old.json").write_text(json.dumps(before))
    (directory / "new.json").write_text(json.dumps(after))
    transition = {"file": "new.json"}
    if acknowledged:
        transition["changes"] = {"InputAdmission": {"reason": "synthetic change", "compatibility": "test migration",
                                                    "test": "backend/tests/test_migration.py"}}
    (directory / "history.json").write_text(json.dumps({"format_version": 1,
        "snapshots": [{"file": "old.json"}, transition]}))
    if valid:
        assert check_history(tmp_path, after) == []
    else:
        with pytest.raises(ValueError):
            check_history(tmp_path, after)


@pytest.mark.asyncio
async def test_private_producer_identity_and_cache_are_not_public_contract_fields():
    event = ToolEvent(tool_call_id="synthetic", tool_name="read", function_name="read", function_args={},
                      status=ToolStatus.CALLED)
    event._producer_event_id = "private-producer"
    public = await EventMapper.events_to_sse_events([event])
    assert "private-producer" not in public[0].model_dump_json()
    assert "_producer_event_id" not in str(TypeAdapter(AgentEvent).json_schema())
    assert "execution_history_projection" not in Session.model_json_schema()["properties"]
