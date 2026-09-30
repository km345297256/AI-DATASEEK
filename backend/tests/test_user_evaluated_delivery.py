"""Publication is user-assessed; engineering receipts remain non-negotiable."""
import ast
import inspect
import textwrap
from unittest.mock import AsyncMock

import pytest

from app.domain.models.dataset import MountedDataset
from app.domain.models.event import StepEvent, StepStatus, PlanEvent, PlanStatus
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.delivery_links import reconcile_delivery_links
from app.domain.services.flows.plan_act import AgentStatus
from test_analysis_delivery_flow import artifact, runner_for
from test_analysis_repair_flow import collect, output, scenario
from test_analysis_answer_review_flow import with_observed_results


def test_default_finalization_has_no_review_or_semantic_replay_calls():
    source = ast.parse(textwrap.dedent(inspect.getsource(AgentTaskRunner._finalize_analysis_step)))
    calls = {node.func.attr for node in ast.walk(source)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls.intersection({"_review_analysis_answer", "_review_scope_completion"})
    assert "_review_artifact_repair" in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["analysis", "file_preview", "catalog_metadata"])
async def test_text_success_always_replaces_model_review_assertions_with_host_marker(intent):
    step = Step(success=True, result="Useful user-assessed explanation.", inputs={"dataset_intent": intent},
                outputs={"answer_review": {"version": 1, "status": "verified"},
                         "result_assessment": {"mode": "model_approved", "scientific_review": "passed"}})
    runner = runner_for(step, [])
    runner._review_analysis_answer = AsyncMock(side_effect=AssertionError("No review"))
    message = Message(message="Explain", datasets=[MountedDataset(dataset_id="chosen-dataset",
        data_center_id="fixture", data_center_name="Fixture", name="Synthetic",
        sandbox_path="/home/ubuntu/datasets/synthetic")], attachment_file_ids=["input-2", "input-1"])
    event = StepEvent(status=StepStatus.COMPLETED, step=step.model_copy(deep=True))
    assert await runner._finalize_analysis_step(event, message, []) == []
    for value in (step, event.step):
        assert value.success and value.outcome.status == "succeeded"
        assert value.result == "Useful user-assessed explanation."
        assert "answer_review" not in value.outputs
        assert value.outputs["result_assessment"] == {"version": 1, "mode": "user_evaluated",
            "scientific_review": "not_performed", "dataset_ids": ["chosen-dataset"],
            "input_file_ids": ["input-1", "input-2"]}
    runner._review_analysis_answer.assert_not_awaited()


@pytest.mark.parametrize("markup", [
    "[Missing](/home/ubuntu/output/absent.png)",
    "![Missing](sandbox:/home/ubuntu/output/absent.png)",
    '[Missing](</home/ubuntu/output/absent name.png> "title")',
    "[Missing](absent(1).png)",
    "[Missing](absent%20name.png)",
    "[Missing](/api/v1/files/absent/download)",
    "[Missing][x]\n\n[x]: /home/ubuntu/output/absent.png",
    "[Missing][]\n\n[Missing]: /home/ubuntu/output/absent.png",
    '<a href="/home/ubuntu/output/absent.png">Missing</a>',
    '<img src="/home/ubuntu/output/absent.png">',
])
def test_nonexistent_downloads_are_deactivated_without_a_model(markup):
    rendered = reconcile_delivery_links(markup, [])
    assert "文件未交付" in rendered
    assert "](" not in rendered and "href=" not in rendered and "src=" not in rendered
    assert "/home/ubuntu/output/" not in rendered


@pytest.mark.parametrize("markup", [
    "[Existing](/home/ubuntu/output/existing.png)",
    "![Existing](sandbox:/home/ubuntu/output/existing.png)",
    "[Existing](existing.png)",
    "[Existing](/api/v1/files/existing.png/download)",
    "[Reference](https://example.invalid/reference.pdf)",
    "[Malformed reference](https://[invalid)",
    "```markdown\n[Example](/home/ubuntu/output/not-executed.png)\n```\n",
    "~~~~markdown\n[Example](/home/ubuntu/output/not-executed.png)\n~~~~\n",
])
def test_existing_downloads_references_and_code_examples_are_preserved(markup):
    assert reconcile_delivery_links(markup, [artifact("existing.png", "image")[1]]) == markup


@pytest.mark.asyncio
@pytest.mark.parametrize("same_scope", [True, False])
async def test_followup_historical_link_is_scope_bound_and_never_a_new_delivery(same_scope):
    prior, prior_flow, _, prior_message, _ = scenario([[output("previous.png", "image")]], [{"kind": "image"}])
    await collect(prior, prior_message)
    history = [PlanEvent(status=PlanStatus.COMPLETED, plan=prior_flow.plan.model_copy(deep=True))]
    runner, flow, step, message, _ = scenario([[]], [])
    step.inputs["artifact_policy"] = "optional"
    message._session_events_snapshot = history
    # The real planner must start this follow-up, not resume its completed
    # historical plan. Only its model response is synthetic here.
    new_plan_event = flow._session_repository.get_events.return_value[0]
    async def create_followup_plan(_message):
        yield new_plan_event
    flow.status = AgentStatus.PLANNING
    flow.planner.create_plan = create_followup_plan
    flow._should_use_dataset_fast_path = lambda _message: False
    flow._normalize_plan_agents = lambda: None
    flow._ensure_vision_step_for_image_message = lambda _message: None
    if not same_scope:
        message.attachment_file_ids = ["different-input"]
    draft = "See [the previous chart](/home/ubuntu/output/private-repair-dir/previous.png)."
    with_observed_results(runner, flow, [draft], emit_tools=False)
    await collect(runner, message)
    assert step.success and not step.attachments and not runner._generated_files
    assert (step.result == draft) is same_scope
    assert ("文件未交付" in step.result) is not same_scope
