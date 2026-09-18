"""Committed outputs cannot be satisfied by unrelated same-kind attachments."""
import json

import pytest
from pydantic import ValidationError

from app.domain.models.analysis_outcome import AnalysisOutcome, DeliverableRequirement
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionResult, Plan, Step
from app.domain.services.analysis_completion import assess_delivery, blocking_receipt_paths, requirements_for_step


ROOT = "/home/ubuntu/output/"


def artifact(name, *, identity=None):
    path = ROOT + name
    record = {"path": path, "kind": "image", "valid": True, "sha256": "a" * 64, "size": 32}
    uploaded = FileInfo(file_id=identity or name, file_path=path, size=32,
                        metadata={"artifact_sha256": record["sha256"]})
    return record, uploaded


def assess(requirements, *artifacts, failed=()):
    return assess_delivery(requirements, [*(item[0] for item in artifacts), *failed],
                           [item[1] for item in artifacts], execution_success=True)


def missing(name):
    return {"path": ROOT + name, "kind": "image", "valid": False, "reason": "missing_artifact"}


def test_committed_output_is_not_replaced_by_another_valid_image():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "planned.png"])
    unrelated = artifact("unrelated.png")
    failed = missing("planned.png")
    outcome = assess([required], unrelated, failed=[failed])
    assert outcome.status == "partial"
    assert outcome.reason_code == "artifacts_missing"
    assert outcome.missing[0].output_paths == required.output_paths
    assert outcome.issues[0].blocking
    assert blocking_receipt_paths([required], [unrelated[0], failed], [unrelated[1]]) == {failed["path"]}


def test_same_basename_from_another_directory_never_substitutes_identity():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "planned/chart.png"])
    outcome = assess([required], artifact("incidental/chart.png"), failed=[missing("planned/chart.png")])
    assert outcome.status == "partial"
    assert outcome.missing[0].output_paths == [ROOT + "planned/chart.png"]


def test_all_named_paths_are_mandatory_and_remaining_count_slots_are_unchanged():
    required = DeliverableRequirement(kind="image", min_count=3,
                                      output_paths=[ROOT + "a.png", ROOT + "b.png"])
    outcome = assess([required], artifact("a.png"), artifact("other.png"), artifact("third.png"))
    assert outcome.status == "partial"
    assert outcome.missing[0].min_count == 1
    assert outcome.missing[0].output_paths == [ROOT + "b.png"]
    complete = assess([required], artifact("a.png"), artifact("b.png"), artifact("other.png"))
    assert complete.status == "succeeded"


def test_named_paths_raise_too_small_count_but_do_not_add_to_it():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "a.png", ROOT + "b.png"])
    assert required.min_count == 2
    assert assess([required], artifact("a.png"), artifact("b.png")).status == "succeeded"


def test_satisfied_named_slot_is_not_retained_as_missing_anonymous_slot():
    required = DeliverableRequirement(kind="image", min_count=2, output_paths=[ROOT + "a.png"])
    outcome = assess([required], artifact("a.png"))
    assert outcome.missing[0].min_count == 1
    assert outcome.missing[0].output_paths == []


def test_augmenting_matching_preserves_specific_and_generic_requirements():
    required = [DeliverableRequirement(kind="image"),
                DeliverableRequirement(kind="image", output_paths=[ROOT + "specific.png"])]
    assert assess(required, artifact("specific.png"), artifact("general.png")).status == "succeeded"
    assert assess(required, artifact("specific.png")).status == "partial"


def test_incidental_failed_output_is_still_nonblocking_after_promises_fulfilled():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "planned.png"])
    outcome = assess([required], artifact("planned.png"), failed=[missing("extra.png")])
    assert outcome.status == "succeeded"
    assert len(outcome.issues) == 1 and outcome.issues[0].blocking is False


def test_unrelated_failure_is_not_selected_for_named_only_repair():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "planned.png"])
    outcome = assess([required], failed=[missing("extra.png")])
    assert outcome.missing[0].output_paths == required.output_paths
    assert outcome.issues[0].blocking is False
    assert not blocking_receipt_paths([required], [missing("extra.png")])


def test_exact_path_still_requires_requested_format_and_verified_bytes():
    required = DeliverableRequirement(kind="image", formats=["svg"], output_paths=[ROOT + "plot.png"])
    assert assess([required], artifact("plot.png")).status == "partial"
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "plot.png"])
    record, uploaded = artifact("plot.png")
    uploaded.metadata["artifact_sha256"] = "b" * 64
    outcome = assess_delivery([required], [record], [uploaded], execution_success=True)
    assert outcome.status == "failed"
    assert outcome.reason_code == "delivery_failed"


@pytest.mark.parametrize("path", [
    "/Users/private/plot.png", "/home/ubuntu/datasets/input.png", "/tmp/plot.png", "plot.png",
    ROOT, ROOT + "../plot.png", ROOT + "./plot.png", ROOT + "a//plot.png", ROOT + "a/",
    ROOT + "a\\plot.png", ROOT + "secret\n.png", ROOT + "secret\x7f.png", ROOT + "secret\u202e.png",
])
def test_committed_paths_cannot_introduce_host_paths_or_noncanonical_identities(path):
    with pytest.raises(ValidationError):
        DeliverableRequirement(kind="image", output_paths=[path])


def test_contract_identities_are_bounded_deduplicated_and_revalidated():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "a.png", ROOT + "a.png"])
    assert required.output_paths == [ROOT + "a.png"]
    for value in [[ROOT + str(index) for index in range(17)], [ROOT + "x" * 4097]]:
        with pytest.raises(ValidationError):
            DeliverableRequirement(kind="image", output_paths=value)
    forged = required.model_copy(update={"output_paths": ["/Users/private/secret.png"]})
    with pytest.raises(ValidationError):
        assess([forged])


@pytest.mark.parametrize("objective", [
    "Analyze /Users/private/dataset.csv", "Analyze `/Users/private/dataset.csv`",
    "input:/Users/private/dataset.csv", "file=/tmp/output.png", "/private/dataset.csv",
    "Plot /home/ubuntu/output/chart.png", "Analyze C:\\Users\\private\\data.csv",
    "Analyze C:/Users/private/data.csv", r"Analyze \\server\share\data.csv", "Analyze ~/data.csv",
    "分析/Users/private/data.csv", "分析/私人/data.csv", "分析/secret", "分析C:\\Users\\data.csv",
])
def test_objectives_never_allow_host_or_sandbox_filesystem_paths_in_public_plans(objective):
    with pytest.raises(ValidationError):
        DeliverableRequirement(kind="image", objective=objective)


@pytest.mark.parametrize("objective", [
    "PCA / t-SNE comparison", "Ratio of A to B distributions", "相关矩阵与各类别分布",
    "Data described at https://example.org/data/page",
])
def test_semantic_objectives_allow_ratios_and_description_links(objective):
    assert DeliverableRequirement(kind="image", objective=objective).objective == objective


def test_private_contract_survives_plan_and_message_checkpoint_roundtrip():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "nested/chart.png"],
                                      objective="Requested component projection")
    step = Step(deliverables=[required])
    restored = Plan.model_validate_json(Plan(steps=[step]).model_dump_json())
    restored_message = Message.model_validate_json(Message(deliverables=[required]).model_dump_json())
    assert restored.steps[0].deliverables[0] == required
    assert restored_message.deliverables[0] == required
    assert requirements_for_step(restored.steps[0], restored_message) == [required]


def test_public_missing_projection_never_discloses_paths_or_private_objective():
    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "nested/chart.png"],
                                      objective="Private analytical contract")
    outcome = assess([required])
    public = outcome.model_dump()
    assert public["missing"] == [{"kind": "image", "min_count": 1, "formats": [], "label": "图表"}]
    assert "output_paths" not in outcome.model_dump_json()
    assert "Private analytical" not in outcome.model_dump_json()
    copied = AnalysisOutcome(status="failed", reason_code="artifacts_missing").model_copy(
        update={"missing": [required.model_dump(), {"kind": "image", "output_paths": ["/Users/private/secret"]}]})
    assert "private" not in json.dumps(copied.model_dump()).lower()
    assert len(copied.model_dump()["missing"]) == 1
    public_schema = json.dumps(AnalysisOutcome.model_json_schema(mode="serialization"))
    assert "output_paths" not in public_schema
    assert "objective" not in public_schema


def test_executor_final_claims_cannot_add_delivery_obligations():
    result = ExecutionResult.model_validate({"success": True, "result": "Finished",
        "attachments": [ROOT + "invented.png"],
        "deliverables": [{"kind": "image", "output_paths": [ROOT + "invented.png"]}]})
    assert "deliverables" not in result.model_dump()


def test_plan_merge_preserves_named_contracts_and_distinct_objectives():
    from app.domain.services.flows.plan_act import PlanActFlow

    required = DeliverableRequirement(kind="image", output_paths=[ROOT + "requested.png"], objective="Correlation")
    planned = DeliverableRequirement(kind="image", output_paths=[ROOT + "planned.png"], objective="Projection")
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(deliverables=[planned])])
    message = Message(deliverables=[required])
    flow._bind_delivery_contract(message)
    assert len(flow.plan.steps[-1].deliverables) == 2
    assert {item.objective for item in flow.plan.steps[-1].deliverables} == {"Correlation", "Projection"}
    assert message.deliverables == [required]


def test_plan_merge_refines_generic_floor_without_double_counting_named_outputs():
    from app.domain.services.flows.plan_act import PlanActFlow

    generic = DeliverableRequirement(kind="image", min_count=3)
    planned = DeliverableRequirement(kind="image", min_count=2,
        output_paths=[ROOT + "a.png", ROOT + "b.png"], objective="Requested projections")
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(deliverables=[planned])])
    flow._bind_delivery_contract(Message(deliverables=[generic]))
    assert len(flow.plan.steps[-1].deliverables) == 1
    merged = flow.plan.steps[-1].deliverables[0]
    assert merged.min_count == 3
    assert merged.output_paths == planned.output_paths
    assert merged.objective == planned.objective
    assert generic.output_paths == [] and generic.objective == ""


def test_multiple_objectives_share_generic_floor_without_invented_additional_images():
    from app.domain.services.flows.plan_act import PlanActFlow

    message = Message(deliverables=[DeliverableRequirement(kind="image", min_count=3)])
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(deliverables=[
        DeliverableRequirement(kind="image", objective="Correlation", output_paths=[ROOT + "a.png"]),
        DeliverableRequirement(kind="image", objective="Projection", output_paths=[ROOT + "b.png"]),
    ])])
    flow._bind_delivery_contract(message)
    before = flow.plan.model_dump()
    assert sum(item.min_count for item in flow.plan.steps[-1].deliverables) == 3
    assert assess(flow.plan.steps[-1].deliverables, artifact("a.png"), artifact("b.png"),
                  artifact("other.png")).status == "succeeded"
    flow._bind_delivery_contract(message)
    assert flow.plan.model_dump() == before


def test_two_specific_paths_for_the_same_objective_remain_required_across_plan_merge():
    from app.domain.services.flows.plan_act import PlanActFlow

    message = Message(deliverables=[DeliverableRequirement(kind="image", objective="Distributions",
                                                          output_paths=[ROOT + "a.png"])])
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(deliverables=[DeliverableRequirement(kind="image", objective="Distributions",
                                                                   output_paths=[ROOT + "b.png"])])])
    flow._bind_delivery_contract(message)
    merged = flow.plan.steps[-1].deliverables
    assert len(merged) == 1 and merged[0].min_count == 2
    assert merged[0].output_paths == [ROOT + "a.png", ROOT + "b.png"]
    assert assess(merged, artifact("a.png"), artifact("b.png")).status == "succeeded"
