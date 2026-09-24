"""Content capability affects real completion; prompts are guidance, not proof."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.file import FileInfo
from app.domain.models.event import StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_completion import artifact_kind, assess_delivery, verified_deliveries
from app.domain.services.prompts.execution import EXECUTION_PROMPT, SCIENTIFIC_COUNTING_POLICY, SUMMARIZE_PROMPT
from app.domain.services.prompts.planner import PLANNER_SYSTEM_PROMPT


def markdown(*, kind="report", structured=True):
    path = "/home/ubuntu/output/evidence.md"
    metadata = {"format": "MARKDOWN", "validation_level": "nonempty_text",
                "table_validation": "rectangular", "table_count": 1,
                "tables": [{"row_count": 11, "column_count": 4}]} if structured else {}
    record = {"path": path, "kind": kind, "valid": True, "sha256": "a" * 64, "size": 512, "metadata": metadata}
    upload = FileInfo(file_id="checked-upload", file_path=path, size=512, metadata={"artifact_sha256": "a" * 64})
    return record, upload


def assess(requirement, record, upload):
    return assess_delivery([DeliverableRequirement(**requirement)], [record], [upload], execution_success=True)


@pytest.mark.parametrize("kind", ["report", "table"])
def test_actual_rectangular_markdown_satisfies_table_when_format_is_unspecified(kind):
    record, upload = markdown(kind=kind)
    assert assess({"kind": "table"}, record, upload).status == "succeeded"
    assert assess({"kind": "table", "formats": ["md"]}, record, upload).status == "succeeded"
    assert verified_deliveries([record], [upload]) == [upload]
    assert artifact_kind(record["path"]) == "report", "Default extension classification cannot assert table contents"


def test_markdown_never_substitutes_for_explicit_csv_or_committed_identity():
    record, upload = markdown()
    for requirement in [{"kind": "table", "formats": ["csv"]},
                        {"kind": "table", "output_paths": ["/home/ubuntu/output/required.md"]}]:
        result = assess(requirement, record, upload)
        assert result.status != "succeeded" and result.missing[0].min_count == 1


@pytest.mark.parametrize("metadata", [
    {}, {"table_validation": "rectangular"},
    {"table_validation": "inconsistent", "table_count": 1, "tables": [{"row_count": 3, "column_count": 4}]},
    {"table_validation": "rectangular", "table_count": True, "tables": [{"row_count": 3, "column_count": 4}]},
    {"table_validation": "rectangular", "table_count": 2, "tables": [{"row_count": 3, "column_count": 4}]},
    {"table_validation": "rectangular", "table_count": 1, "tables": [{"row_count": 1, "column_count": 4}]},
    {"table_validation": "rectangular", "table_count": 1, "tables": [{"row_count": 3, "column_count": True}]},
])
def test_prose_heading_ragged_content_or_invalid_metadata_cannot_claim_table_completion(metadata):
    record, upload = markdown()
    record["metadata"] = deepcopy(metadata)
    result = assess({"kind": "table"}, record, upload)
    assert result.status != "succeeded" and result.missing[0].kind == "table"
    # Readability of the same report is retained, not retroactively destroyed.
    assert assess({"kind": "report"}, record, upload).status == "succeeded"


def test_table_kind_on_markdown_without_byte_structure_receipt_is_rejected():
    record, upload = markdown(kind="table", structured=False)
    result = assess({"kind": "table"}, record, upload)
    assert result.status != "succeeded" and result.reason_code == "artifact_validation_failed"


def test_valid_table_metadata_does_not_waive_uploaded_version_or_count_checks():
    record, upload = markdown()
    upload.metadata["artifact_sha256"] = "b" * 64
    assert assess({"kind": "table"}, record, upload).reason_code == "delivery_failed"
    upload.metadata["artifact_sha256"] = "a" * 64
    outcome = assess({"kind": "table", "min_count": 2}, record, upload)
    assert outcome.status == "partial" and outcome.missing[0].min_count == 1


def test_text_design_remains_a_complete_product_without_new_file_or_calculation_requirements():
    result = assess_delivery([], [], [], execution_success=True)
    assert result.status == "succeeded" and result.missing == []


@pytest.mark.asyncio
@pytest.mark.parametrize("formats,complete", [([], True), (["csv"], False)])
async def test_runner_uses_byte_receipt_table_capability_and_keeps_explicit_format(formats, complete):
    record, upload = markdown()
    step = Step(success=True, result="The evidence table is available.",
                deliverables=[DeliverableRequirement(kind="table", formats=formats)])
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "agent", "session", "owner"
    runner._artifact_baseline_paths = set()
    runner._generated_files = [upload]
    runner._sandbox = SimpleNamespace(validate_artifacts=AsyncMock(return_value=SimpleNamespace(
        success=True, data={"version": 1, "files": [record]})))
    runner._list_sandbox_artifacts = AsyncMock(return_value=[record["path"]])
    runner._session_repository = SimpleNamespace()
    runner._flow = SimpleNamespace(plan=Plan(steps=[step]))
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message="Return the requested table."), [upload])
    assert (step.outcome.status == "succeeded") is complete
    if not complete:
        assert step.outcome.missing[0].formats == ["csv"]


def test_prompt_contract_renders_without_changing_format_arguments():
    # This verifies transport and scope wording only, not model compliance.
    rendered = EXECUTION_PROMPT.format(step="Design", message="Propose a notebook structure only",
        attachments="", language="en", dataset_contract=SCIENTIFIC_COUNTING_POLICY)
    assert rendered.count("<scientific_method_and_result_contract>") == 1
    assert rendered.count("<reproducible_delivery_contract>") == 1
    assert "without running the proposed study" in rendered
    assert "valid_mask from excluded_mask" in rendered
    assert "A check of internal consistency does not establish reference truth or method validity" in rendered
    assert "File hashes establish byte identity, not causal generation or scientific correctness" in rendered
    assert "execution_count null and outputs empty" in rendered
    assert "content sections from separate files" in PLANNER_SYSTEM_PROMPT
    assert "Do not invent an earlier error" in SUMMARIZE_PROMPT
