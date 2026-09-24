"""Offline prompt transport and physical-file contract boundaries, not model compliance."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services import dataset_request_resolver as resolver_module
from app.application.services.dataset_request_resolver import DatasetRequestResolver
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.dataset import DataCenterDataset
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.services.analysis_completion import assess_delivery, requirements_for_step
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.prompts.planner import PLANNER_SYSTEM_PROMPT


def dataset():
    return DataCenterDataset(dataset_id="test-source", data_center_id="test-center",
                             data_center_name="Test", name="Measurements")


def bind(requirements, question="Prepare the requested analysis report"):
    message = Message(message=question,
        deliverables=requirements, controller_requires_artifacts=True)
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = flow._create_dataset_fast_path_plan(message)
    flow._bind_delivery_contract(message)
    first = [item.model_dump() for item in flow.plan.steps[0].deliverables]
    flow._bind_delivery_contract(message)
    final = requirements_for_step(flow.plan.steps[0], message)
    assert [item.model_dump() for item in final] == first
    return final


def artifact(name, index=0):
    path = "/home/ubuntu/output/" + name
    digest = ("a" if index == 0 else "b") * 64
    record = dict(path=path, kind="report", valid=True, sha256=digest, size=512)
    upload = FileInfo(file_id=f"file-{index}", file_path=path, size=512,
                      metadata={"artifact_sha256": digest})
    return record, upload


@pytest.mark.asyncio
async def test_controller_transports_physical_file_guidance_and_keeps_combined_content(monkeypatch):
    objective = "Verify paired measurements; compare compatible groups; state limitations"
    contract = {"kind": "report", "min_count": 1, "formats": [],
                "objective": objective, "output_paths": []}
    response = {"safety": {"decision": "allow", "risk_level": "low", "categories": [],
                            "reason": "", "suggestion": ""},
        "execution": {"mode": "sandbox", "required_evidence": "file_content",
                      "required_capabilities": [], "requires_artifacts": True,
                      "deliverables": [contract]}, "answer": "", "catalog_queries": [], "reason": ""}
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(response))))
    model.bind = lambda **kwargs: model
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings",
                        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    resolver = DatasetRequestResolver()
    resolver._policy_store = SimpleNamespace(list_enabled=AsyncMock(return_value=[]))
    resolution = await resolver.resolve(question="Return one report covering verification, comparison and limitations.",
                                         datasets=[dataset()], events=[])
    messages = model.ainvoke.await_args.args[0]
    actual_prompt = "\n".join(str(message.content) for message in messages)
    assert "min_count counts physical output files" in actual_prompt
    assert "combine all requested content obligations" in actual_prompt
    assert "do not invent a\nMarkdown requirement" in actual_prompt
    assert "physical output files" in PLANNER_SYSTEM_PROMPT
    assert "combine these content obligations" in PLANNER_SYSTEM_PROMPT
    model.ainvoke.assert_awaited_once()
    requirements = bind(resolution.decision.execution.deliverables)
    assert len(requirements) == 1
    assert requirements[0].objective == objective
    assert requirements[0].formats == []
    record, upload = artifact("combined.txt")
    outcome = assess_delivery(requirements, [record], [upload], execution_success=True)
    assert outcome.status == "succeeded"  # delivery only; content review is a separate gate


@pytest.mark.parametrize("requirements", [
    [DeliverableRequirement(kind="report", min_count=2, objective="Two separate reports")],
    [DeliverableRequirement(kind="report", min_count=2,
        output_paths=["/home/ubuntu/output/first.md", "/home/ubuntu/output/second.md"],
        objective="Separate named reports")],
    [DeliverableRequirement(kind="report", formats=["md"], objective="Verify measurements"),
     DeliverableRequirement(kind="report", formats=["md"], objective="Compare groups")],
])
def test_explicit_counts_paths_and_distinct_obligations_are_not_posthoc_merged(requirements):
    bound = bind(requirements)
    assert [r.model_dump() for r in bound] == [r.model_dump() for r in requirements]
    first, first_upload = artifact("first.md")
    outcome = assess_delivery(bound, [first], [first_upload], execution_success=True)
    assert outcome.status == "partial"
    assert outcome.reason_code == "artifacts_missing"
    assert sum(item.min_count for item in outcome.missing) == 1
    second, second_upload = artifact("second.md", 1)
    assert assess_delivery(bound, [first, second], [first_upload, second_upload],
                           execution_success=True).status == "succeeded"


def test_explicit_markdown_is_not_satisfied_by_generic_json_report_count():
    bound = bind([DeliverableRequirement(kind="report", formats=["md"], objective="Requested Markdown report")])
    record, upload = artifact("summary.json")
    outcome = assess_delivery(bound, [record], [upload], execution_success=True)
    assert outcome.reason_code == "artifacts_missing"
    assert outcome.missing[0].formats == ["md"]


def test_named_files_remain_required_even_when_two_other_reports_exist():
    bound = bind([DeliverableRequirement(kind="report", min_count=2,
        output_paths=["/home/ubuntu/output/first.md", "/home/ubuntu/output/second.md"],
        objective="Separate named reports")])
    first, first_upload = artifact("first.md")
    other, other_upload = artifact("other.md", 1)
    outcome = assess_delivery(bound, [first, other], [first_upload, other_upload], execution_success=True)
    assert outcome.reason_code == "artifacts_missing"
    assert outcome.missing[0].output_paths == ["/home/ubuntu/output/second.md"]
