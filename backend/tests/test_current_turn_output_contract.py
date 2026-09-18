import pytest

from app.domain.models.analysis_input import assign_upload_namespace, build_analysis_inputs, upload_runtime_path
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services.analysis_completion import assess_delivery, requirements_for_step
from app.domain.services.analysis_request_contract import affirmative_request_text
from app.domain.services.flows.plan_act import PlanActFlow


def upload_message(question, *, required=None, deliverables=()):
    infos = assign_upload_namespace([FileInfo(file_id="input", filename="observations.csv", size=16)])
    infos[0].file_path = upload_runtime_path(infos[0])
    return Message(message=question, attachment_file_infos=infos, attachments=[infos[0].file_path],
        attachment_file_ids=["input"], analysis_inputs=build_analysis_inputs([], infos),
        controller_requires_artifacts=required, deliverables=list(deliverables))


@pytest.mark.parametrize("question", [
    "请实际读取当前选择的上传文件，只返回文件名和原始列名，不绘图、不生成文件。",
    "列出字段；不要画图或者生成报告。",
    "只解释字段，不需要重新生成图表，也不需要导出CSV。",
    "只读列名，不允许生成图表。",
    "列名即可，图表不需要。",
    "List the original columns. Do not plot or export a CSV file.",
    "Read the header without generating plots or files.",
    "List columns, no charts or files.",
    "Return the column names. Plots are not needed.",
])
def test_negated_actions_never_become_new_visualization_or_download_obligations(question):
    message = upload_message(question, required=False)
    plan = PlanActFlow._create_dataset_fast_path_plan(message)
    step = plan.steps[0]
    assert PlanActFlow._dataset_request_intent(question) == "analysis"
    assert "visualization" not in PlanActFlow._dataset_requested_dimensions(question)
    assert not PlanActFlow._requests_downloadable_result(question)
    assert step.inputs["dataset_intent"] == "analysis"
    assert step.inputs["artifact_policy"] == "optional"
    assert step.inputs["require_downloadable_result"] is False
    assert step.inputs["user_question"] == question
    assert plan.goal == question
    assert requirements_for_step(step, message) == []
    outcome = assess_delivery([], [], [], execution_success=True)
    assert outcome.status == "succeeded" and outcome.missing == []


@pytest.mark.parametrize("question,kind,intent", [
    ("不要画图，导出一份 CSV 文件供下载。", "table", "analysis"),
    ("不要生成报告，但是绘制柱状图。", "image", "visualization"),
    ("Do not plot; export a CSV file instead.", "table", "analysis"),
    ("Don't write a report, but draw a chart.", "image", "visualization"),
])
def test_separate_positive_output_requests_survive_negative_clauses(question, kind, intent):
    required = DeliverableRequirement(kind=kind)
    message = upload_message(question, required=True, deliverables=[required])
    step = PlanActFlow._create_dataset_fast_path_plan(message).steps[0]
    assert step.inputs["dataset_intent"] == intent
    assert step.inputs["artifact_policy"] == "required"
    assert requirements_for_step(step, message) == [required]
    assert assess_delivery([required], [], [], execution_success=True).reason_code == "artifacts_missing"


def test_current_controller_contract_overrides_local_keyword_and_planner_invention():
    # A chart name can be mentioned while asking about it, not requesting one.
    message = upload_message("Explain the chart filename and original fields", required=False)
    step = PlanActFlow._create_dataset_fast_path_plan(message).steps[0]
    assert step.inputs["dataset_intent"] == "analysis"
    assert "visualization" not in step.inputs["requested_dimensions"]
    invented = Step(inputs={"dataset_intent": "visualization", "artifact_policy": "required"},
        deliverables=[DeliverableRequirement(kind="image")])
    assert requirements_for_step(invented, message) == []
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[invented])
    flow._bind_delivery_contract(message)
    assert invented.deliverables == []
    assert invented.inputs["artifact_policy"] == "optional"


def test_next_turn_keeps_input_scope_without_inheriting_prior_chart_requirement():
    first = upload_message("画一个柱状图并提供下载", required=True,
        deliverables=[DeliverableRequirement(kind="image")])
    first_plan = PlanActFlow._create_dataset_fast_path_plan(first)
    second = first.model_copy(deep=True, update={"message": "读取并返回原始列名，不绘图、不生成文件。",
        "deliverables": [], "controller_requires_artifacts": False})
    second_plan = PlanActFlow._create_dataset_fast_path_plan(second)
    assert second.analysis_inputs == first.analysis_inputs
    assert requirements_for_step(first_plan.steps[0], first)[0].kind == "image"
    assert requirements_for_step(second_plan.steps[0], second) == []
    assert second_plan.steps[0].inputs["dataset_intent"] == "analysis"
    assert first_plan.steps[0].inputs["dataset_intent"] == "visualization"


def test_explicit_current_deliverables_are_not_dropped_on_inconsistent_controller_boolean():
    requirement = DeliverableRequirement(kind="table", formats=["csv"])
    message = upload_message("导出结果表", required=False, deliverables=[requirement])
    step = PlanActFlow._create_dataset_fast_path_plan(message).steps[0]
    assert step.inputs["artifact_policy"] == "required"
    assert requirements_for_step(step, message) == [requirement]


def test_uncertainty_is_not_treated_as_a_negated_action():
    assert "不确定性" in affirmative_request_text("分析不确定性并绘图")
