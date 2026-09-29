"""Independent dictionary protocols use production schemas and frozen samples."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models.answer_review import AnswerReviewMarker
from app.domain.models.memory import Memory
from app.domain.models.model_trace import ModelTraceRecord
from app.domain.models.spill import SpillArtifactNotice, SpillArtifactRecord, SpillArtifactRef
from app.domain.models.usage import TokenUsageRecord
from app.domain.services.tools.mcp_images import (
    MCPImageRef, MCP_IMAGE_REFS_KEY, MCP_IMAGE_REFS_VERSION, accepted_image_refs,
)
from app.domain.utils.persistence_contracts import compare_catalogs, current_catalog


MODELS = {
    "MCPImageRef": MCPImageRef, "SpillArtifactRef": SpillArtifactRef,
    "SpillArtifactNotice": SpillArtifactNotice, "SpillArtifactRecord": SpillArtifactRecord,
    "AnswerReviewMarker": AnswerReviewMarker, "ModelTraceRecord": ModelTraceRecord,
    "TokenUsageRecord": TokenUsageRecord,
}
FIXTURES = json.loads((Path(__file__).parent / "fixtures/persistence/legacy-subprotocols.json").read_text())


@pytest.mark.parametrize("name", MODELS)
def test_pre_upgrade_subprotocol_records_roundtrip_without_inventing_authority(name):
    adapter = TypeAdapter(MODELS[name])
    legacy = FIXTURES[name]
    restored = adapter.validate_python(legacy)
    # Preserve absence, not only values: missing cache metrics do not become 0,
    # and absent review scope/diagnostics must not be synthesized in history.
    dumped = adapter.dump_python(restored, mode="json", exclude_unset=True)
    assert dumped == legacy
    assert adapter.dump_python(adapter.validate_python(dumped), mode="json", exclude_unset=True) == legacy
    if name in {"ModelTraceRecord", "TokenUsageRecord"}:
        assert restored.cache_read_tokens is None and restored.cache_write_tokens is None
    if name == "SpillArtifactRecord":
        assert restored.storage_user_id is None


def test_inventory_versions_match_real_wire_envelopes_and_schema_fields():
    roots = current_catalog()["roots"]
    assert roots["MCPImageRef"]["writer_version"] == MCP_IMAGE_REFS_VERSION == 1
    assert MCP_IMAGE_REFS_KEY == "dataseek_mcp_image_refs_v1"
    for name in ("SpillArtifactRef", "SpillArtifactNotice", "AnswerReviewMarker", "ModelTraceRecord"):
        assert roots[name]["writer_version"] == 1
    for name in ("SpillArtifactRecord", "TokenUsageRecord"):
        assert roots[name]["writer_version"] == 0  # explicitly unversioned, not a fabricated migration
    assert roots["AnswerReviewMarker"]["owners"] == ["Step.outputs.answer_review"]


def test_new_inventory_adds_independent_roots_without_rewriting_existing_contracts():
    directory = Path(__file__).resolve().parents[2] / "docs/persistence"
    before = json.loads((directory / "2026-09-24-program-attempt.json").read_text())
    after = json.loads((directory / "2026-09-29-independent-subprotocols.json").read_text())
    changes = compare_catalogs(before, after)
    assert {item["root"] for item in changes} == set(MODELS)
    assert all(item["classification"] == "compatible" for item in changes)
    for name, contract in before["roots"].items():
        assert after["roots"][name] == contract


@pytest.mark.parametrize("name", MODELS)
def test_changes_hidden_by_generic_owner_dictionaries_are_detected_independently(name):
    before = current_catalog()
    after = deepcopy(before)
    after["roots"][name]["read_schema"].setdefault("required", []).append("future_required_field")
    changes = compare_catalogs(before, after)
    assert len(changes) == 1
    assert changes[0]["root"] == name and changes[0]["classification"] == "breaking"
    assert before["roots"]["Memory"] == after["roots"]["Memory"]
    assert before["roots"]["Step"] == after["roots"]["Step"]


def test_owner_changes_cannot_silently_relabel_a_subprotocol():
    before = current_catalog()
    after = deepcopy(before)
    after["roots"]["MCPImageRef"]["owners"] = ["different-envelope"]
    assert compare_catalogs(before, after)[0]["paths"] == ["/owners"]
    assert compare_catalogs(before, after)[0]["classification"] == "breaking"


def test_legacy_private_image_envelope_is_accepted_by_actual_memory_reader():
    # Freeze the actual key AND placeholder, not a new envelope constructed
    # from today's constants/rendering code (which would hide regressions).
    path = Path(__file__).parent / "fixtures/persistence/legacy-mcp-image-memory.json"
    memory = Memory.model_validate_json(path.read_text())
    restored = Memory.model_validate_json(memory.model_dump_json())
    reference = MCPImageRef.model_validate(FIXTURES["MCPImageRef"])
    for snapshot in (memory, restored):
        assert accepted_image_refs(snapshot.messages[-1]) == [reference]
        assert "data:image" not in snapshot.model_dump_json()
    assert restored.messages[-1].content == json.loads(path.read_text())["messages"][-1]["content"]


@pytest.mark.parametrize("version", [True, "1", 1.0, 2, None])
def test_review_marker_does_not_coerce_versions_into_trusted_history(version):
    with pytest.raises(ValidationError):
        AnswerReviewMarker.model_validate({**FIXTURES["AnswerReviewMarker"], "version": version})


def test_legacy_minimal_review_does_not_acquire_scope_or_scientific_approval():
    legacy = {"version": 1, "status": "corrected", "old_diagnostic": {"status": "unavailable"}}
    marker = AnswerReviewMarker.model_validate(legacy)
    assert marker.model_dump(exclude_unset=True) == legacy
