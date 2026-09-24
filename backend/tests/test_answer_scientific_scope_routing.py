"""Host input/step scope reaches the factual reviewer without artifact heuristics."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.domain.models.analysis_input import AnalysisInputContext, AnalysisInputSource
from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.dataset import MountedDataset
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_answer_review import AnswerEvidence, AnswerReviewResult
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS


def with_answer_scope_checks(payload, result, *, status="unclear", evidence=(), scope="complete"):
    """Respond to the independent frozen-candidate phase without invented observations."""
    if "frozen_paragraphs" in payload:
        indices = payload["paragraph_indices"]
        result = {"answer_scientific_checks": [{"dimension": dimension, "status": status,
            "paragraph_indices": indices, "evidence": list(evidence)} for dimension in SCIENTIFIC_DIMENSIONS]
        , "answer_scope_check": {"status": scope, "paragraph_indices": indices}}
    return result


def dataset_message(**kwargs):
    return Message(message="Explain source groups and units, without fitting a model.",
        datasets=[MountedDataset(dataset_id="current-dataset", data_center_id="fixture",
            data_center_name="Fixture", name="Current measurements", sandbox_path="/home/ubuntu/datasets/current")],
        **kwargs)


def admission(mode="sandbox", evidence="file_content"):
    return SimpleNamespace(mode=mode, decision=SimpleNamespace(
        execution=SimpleNamespace(required_evidence=evidence)))


def runner_with(reviewer=None, resolution=None):
    runner = object.__new__(AgentTaskRunner)
    runner._flow = SimpleNamespace(executor=SimpleNamespace(review_delivery_answer=reviewer),
        plan=SimpleNamespace(language="en"), _domain_agents={})
    runner._front_controller_resolution = resolution
    return runner


@pytest.mark.parametrize("message,inputs,resolution,expected", [
    (dataset_message(), {"execution_mode":"dataset_fast_path", "dataset_intent":"analysis"}, None, True),
    (dataset_message(), {"execution_mode":"dataset_fast_path", "dataset_intent":"file_structure"}, None, True),
    (dataset_message(), {"execution_mode":"dataset_fast_path"}, None, True),
    (dataset_message(), {}, admission(), True),  # planned/skill path, host requires file evidence
    (dataset_message(), {"execution_mode":"direct"}, admission(), True),  # model input cannot undo host file scope
    (dataset_message(), {"dataset_intent":{"unexpected":"shape"}}, admission(), True),
    (dataset_message(), {"dataset_intent":" Analysis "}, None, True),
    (dataset_message(), {"dataset_intent":"analysis"}, None, True),
    (Message(message="Explain uploaded table units", analysis_inputs=AnalysisInputContext(sources=(
        AnalysisInputSource(kind="upload",source_id="upload:current",name="table"),))),
        {"execution_mode":"dataset_fast_path"},None,True),
    (Message(message="Analyze admitted upload",attachment_file_ids=["current-file"]),
        {"dataset_intent":"analysis"},None,True),
    (Message(message="Analyze admitted upload",attachment_file_ids=["current-file"]),
        {},admission(),True),
    (Message(message="Hello"),{},None,False),
    (dataset_message(),{"execution_mode":"dataset_fast_path"},admission("direct"),False),
    (dataset_message(),{"dataset_intent":"analysis"},admission("catalog"),False),
    (dataset_message(),{"dataset_intent":"analysis"},admission("reject"),False),
    (dataset_message(),{"execution_mode":"direct"},None,False),
    (dataset_message(),{"execution_mode":"dataset_fast_path","dataset_intent":"file_preview"},None,False),
    (Message(message="Download this file",attachment_file_ids=["current-file"]),{},None,False),
    (dataset_message(),{},admission(evidence="conversation"),False),
    (Message(message="Hello",analysis_inputs=AnalysisInputContext()),{"execution_mode":"dataset_fast_path"},None,False),
    (Message(message="Analyze",attachment_file_ids=["", " "]),{"execution_mode":"dataset_fast_path"},None,False),
    (Message(message="Hello",attachments=["/home/ubuntu/output/prior.csv"]),{"execution_mode":"dataset_fast_path"},None,False),
])
def test_scope_uses_current_host_inputs_and_analysis_route(message,inputs,resolution,expected):
    runner=runner_with(resolution=resolution)
    # The prior dataset and a model-provided result cannot influence this turn.
    runner._active_datasets=dataset_message().datasets
    step=Step(inputs=inputs,result="Strong scientific claims and artifacts exist.",
        outputs={"answer_scientific_scope":not expected})
    assert runner._answer_scientific_scope(step,message) is expected


@pytest.mark.parametrize("intent",[
    "file_preview", "preview_file", "preview", "file_inventory", "inventory", "files",
    "catalog_description", "catalog_semantics", "dataset_purpose", "purpose", "use_cases",
    "catalog_metadata", "metadata", "size", "file_count", "file_formats", "formats",
])
def test_pure_catalog_or_copy_does_not_require_measurement_science_even_on_fast_path(intent):
    runner=runner_with(resolution=admission())
    step=Step(inputs={"execution_mode":"dataset_fast_path","dataset_intent":intent},
        result="Dataset metadata includes counts and file names")
    assert runner._answer_scientific_scope(step,dataset_message()) is False


@pytest.mark.parametrize("intent",["analysis","file_structure","custom_question","visualization"])
def test_field_unit_explanations_remain_scientific_when_original_file_is_attached(intent):
    runner=runner_with(resolution=admission())
    step=Step(inputs={"execution_mode":"dataset_fast_path","dataset_intent":intent},
        attachments=["/home/ubuntu/output/original.csv"],result="Fields, sources and units")
    assert runner._answer_scientific_scope(step,dataset_message(attachment_file_ids=["original"])) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("has_output",[False,True])
async def test_runner_forwards_scope_for_dataset_facts_independent_of_output_files(has_output):
    reviewer=AsyncMock(return_value=AnswerReviewResult("Checked structure", "verified", {}))
    runner=runner_with(reviewer)
    step=Step(id="current",inputs={"execution_mode":"dataset_fast_path","dataset_intent":"analysis"},
        result="The table has five sources and three header rows.")
    files=[FileInfo(file_id="original-copy",filename="original.csv")] if has_output else []
    outcome=AnalysisOutcome(status="succeeded",reason_code="completed")
    await runner._review_analysis_answer(step,dataset_message(),files,[],outcome)
    args=reviewer.await_args.kwargs
    assert args["answer_scientific_scope"] is True
    assert args["files"] is files and args["requirements"]==[]
    assert outcome.status=="succeeded"


@pytest.mark.asyncio
async def test_new_generic_turn_does_not_inherit_previous_analysis_scope():
    reviewer=AsyncMock(return_value=AnswerReviewResult("Checked", "verified", {}))
    runner=runner_with(reviewer)
    first=Step(inputs={"execution_mode":"dataset_fast_path"},result="Dataset fact")
    second=Step(result="Hello")
    for step,message in [(first,dataset_message()),(second,Message(message="Hello"))]:
        await runner._review_analysis_answer(step,message,[],[],AnalysisOutcome(status="succeeded",reason_code="completed"))
    assert [call.kwargs["answer_scientific_scope"] for call in reviewer.await_args_list]==[True,False]


@pytest.mark.asyncio
async def test_unavailable_reviewer_fallback_keeps_true_scope(monkeypatch):
    captured={}
    async def review(*,ask,**kwargs):
        captured.update(kwargs)
        with pytest.raises(RuntimeError,match="answer_reviewer_unavailable"):
            await ask([])
        return AnswerReviewResult("Unable to verify", "unavailable", {"reason":"scientific_validation_unavailable"})
    monkeypatch.setattr("app.domain.services.analysis_answer_review.review_answer",review)
    runner=runner_with()
    step=Step(inputs={"execution_mode":"dataset_fast_path"},result="Dataset fact")
    outcome=AnalysisOutcome(status="succeeded",reason_code="completed")
    await runner._review_analysis_answer(step,dataset_message(),[],[],outcome)
    assert captured["answer_scientific_scope"] is True
    assert outcome.status=="failed" and outcome.reason_code=="scientific_validation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason",[
    ("failed","tool_execution_failed"),("partial","tool_execution_unknown"),("failed","analysis_cancelled"),
])
async def test_scoped_review_preserves_original_execution_failure(status,reason):
    reviewer=AsyncMock(return_value=AnswerReviewResult("Unable to verify", "unavailable",
        {"reason":"scientific_validation_unavailable"}))
    runner=runner_with(reviewer)
    step=Step(inputs={"execution_mode":"dataset_fast_path"},result="Some observed facts")
    outcome=AnalysisOutcome(status=status,reason_code=reason)
    before=outcome.model_dump()
    await runner._review_analysis_answer(step,dataset_message(),[],[],outcome)
    assert reviewer.await_args.kwargs["answer_scientific_scope"] is True
    assert outcome.model_dump()==before


@pytest.mark.asyncio
async def test_cancelled_scoped_review_propagates_without_retry_or_publication():
    reviewer=AsyncMock(side_effect=asyncio.CancelledError())
    runner=runner_with(reviewer)
    step=Step(inputs={"execution_mode":"dataset_fast_path"},result="Unreviewed draft")
    outcome=AnalysisOutcome(status="succeeded",reason_code="completed")
    with pytest.raises(asyncio.CancelledError):
        await runner._review_analysis_answer(step,dataset_message(),[],[],outcome)
    assert reviewer.await_count==1
    assert reviewer.await_args.kwargs["answer_scientific_scope"] is True
    assert "answer_review" not in step.outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied",[None,False,True])
async def test_execution_review_forwards_scope_without_new_model_request(monkeypatch,supplied):
    import app.domain.services.analysis_answer_review as review_module
    monkeypatch.setattr("app.domain.services.agents.execution.get_settings",lambda:SimpleNamespace(
        llm_retry_attempts=3,llm_retry_max_seconds=0.0,llm_retry_base_seconds=0.0))
    review=AsyncMock(return_value=AnswerReviewResult("Checked", "verified", {}))
    monkeypatch.setattr(review_module,"review_answer",review)
    agent=object.__new__(ExecutionAgent)
    agent._model=SimpleNamespace(bind=lambda **kwargs: pytest.fail("No extra model request"))
    kwargs={} if supplied is None else {"answer_scientific_scope":supplied}
    await agent.review_delivery_answer(question="Explain table",draft="Observed sources",files=[],
        evidence=AnswerEvidence(),requirements=[],language="en",**kwargs)
    assert review.await_args.kwargs["answer_scientific_scope"] is (False if supplied is None else supplied)
    assert review.await_count==1


@pytest.mark.asyncio
async def test_legacy_fake_reviewer_cannot_silently_drop_true_scope():
    async def outdated(*,question,draft,files,evidence,requirements,language):
        pytest.fail("An outdated reviewer cannot attest required scientific scope")
    runner=runner_with(outdated)
    step=Step(inputs={"execution_mode":"dataset_fast_path"},result="Dataset fact")
    with pytest.raises(TypeError,match="answer_scientific_scope"):
        await runner._review_analysis_answer(step,dataset_message(),[],[],AnalysisOutcome(status="succeeded",reason_code="completed"))
