import pytest
from pydantic import ValidationError

from app.domain.models.analysis_outcome import AnalysisOutcome, ArtifactIssue, DeliverableRequirement, safe_artifact_name
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.services.analysis_completion import (
    assess_delivery, blocking_receipt_paths, issues_from_records, outcome_message, repair_feedback,
    requirements_for_step, verified_deliveries, verified_delivery_counts,
)


def artifact(name="plot.png", kind="image", *, identity="file-1", digest="a" * 64, size=32):
    path = f"/home/ubuntu/output/{name}"
    record = {"path": path, "kind": kind, "valid": True, "sha256": digest, "size": size}
    uploaded = FileInfo(file_id=identity, file_path=path, size=size, metadata={"artifact_sha256": digest})
    return record, uploaded


def assess(requirements, artifacts, **kwargs):
    return assess_delivery([DeliverableRequirement(**item) for item in requirements],
                           [item[0] for item in artifacts], [item[1] for item in artifacts],
                           execution_success=True, **kwargs)


@pytest.mark.parametrize("kind", ["image", "any"])
def test_code_attachment_never_satisfies_a_result_requirement(kind):
    outcome = assess([{"kind": kind}], [artifact("script.py", "code")])
    assert outcome.status == "partial"
    assert outcome.reason_code == "artifacts_missing"
    assert outcome.missing[0].kind == kind


def test_type_counts_and_format_requirements_all_apply():
    requirements = [{"kind": "image", "min_count": 2, "formats": ["png"]}, {"kind": "table", "formats": ["csv"]}]
    outcome = assess(requirements, [artifact(), artifact("other.jpg", identity="file-2"), artifact("table.csv", "table", identity="file-3")])
    assert outcome.status == "partial"
    assert [(item.kind, item.min_count, item.formats) for item in outcome.missing] == [("image", 1, ["png"])]


def test_one_artifact_cannot_pay_for_two_requirement_count_slots():
    outcome = assess([{"kind": "image"}, {"kind": "any"}], [artifact()])
    assert outcome.status == "partial"
    assert sum(item.min_count for item in outcome.missing) == 1


@pytest.mark.parametrize("requirements", [
    [{"kind": "any"}, {"kind": "image"}],
    [{"kind": "image"}, {"kind": "any"}],
    [{"kind": "image"}, {"kind": "image", "formats": ["png"]}],
])
def test_overlapping_requirements_use_matching_not_greedy_assignment(requirements):
    outcome = assess(requirements, [artifact(), artifact("plot2.jpg", identity="file-2")])
    assert outcome.status == "succeeded"


def test_duplicate_receipt_or_upload_cannot_fake_quantity():
    pair = artifact()
    outcome = assess([{"kind": "image", "min_count": 2}], [pair, pair])
    assert outcome.status == "partial"
    assert outcome.missing[0].min_count == 1
    alias = artifact("alias.png")  # Same uploaded file identity is not a second file.
    outcome = assess([{"kind": "image", "min_count": 2}], [pair, alias])
    assert outcome.status != "succeeded"
    assert outcome.missing[0].min_count == 1


@pytest.mark.parametrize("field,value", [("digest", "b" * 64), ("size", 33)])
def test_receipt_is_bound_to_exact_uploaded_bytes(field, value):
    record, uploaded = artifact()
    if field == "digest":
        uploaded.metadata["artifact_sha256"] = value
    else:
        uploaded.size = value
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [record], [uploaded], execution_success=True)
    assert outcome.status != "succeeded"
    assert outcome.reason_code == "delivery_failed"


def test_verified_deliveries_selects_exact_upload_object_not_every_matching_path():
    record, uploaded = artifact()
    wrong_digest = uploaded.model_copy(update={"metadata": {"artifact_sha256": "b" * 64}})
    wrong_size = uploaded.model_copy(update={"size": uploaded.size + 1})
    missing_id = uploaded.model_copy(update={"file_id": ""})
    duplicate = uploaded.model_copy(update={"file_id": "duplicate-upload"})
    selected = verified_deliveries([record], [wrong_digest, wrong_size, missing_id, uploaded, duplicate])
    assert len(selected) == 1 and selected[0] is uploaded


def test_verified_deliveries_conflicting_receipts_poison_the_entire_path():
    record, uploaded = artifact()
    for conflict in [{**record, "sha256": "b" * 64}, {**record, "valid": False, "reason": "invalid_content"}]:
        assert verified_deliveries([record, conflict], [uploaded]) == []


def test_verified_deliveries_deduplicates_uploaded_identity_across_paths():
    first, first_upload = artifact("one.png")
    second, second_upload = artifact("two.png")
    selected = verified_deliveries([first, second, first], [first_upload, second_upload, first_upload])
    assert len(selected) == 1 and selected[0] is first_upload


def test_verified_deliveries_requires_receipts_without_discarding_other_valid_batches():
    record, uploaded = artifact()
    _, unchecked = artifact("unverified.png", identity="unverified")
    assert verified_deliveries([], [uploaded, unchecked], validation_available=False) == []
    selected = verified_deliveries([record], [uploaded, unchecked], validation_available=False)
    assert len(selected) == 1 and selected[0] is uploaded


def test_verified_deliveries_cannot_relabel_code_as_a_checked_image():
    record, uploaded = artifact("analysis.py", "image")
    assert verified_deliveries([record], [uploaded]) == []


@pytest.mark.parametrize("change", [
    {"valid": False}, {"valid": "true"}, {"sha256": "a"}, {"sha256": None}, {"size": 0},
    {"size": True}, {"kind": "unknown"}, {"path": "/Users/private/plot.png"},
    {"path": "/home/ubuntu/output/../secret.png"}, {"path": "/home/ubuntu/output/./plot.png"},
])
def test_invalid_receipt_never_becomes_success_even_when_model_claims_success(change):
    record, uploaded = artifact()
    record.update(change)
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [record], [uploaded], execution_success=True)
    assert outcome.status != "succeeded"
    assert outcome.reason_code == "artifact_validation_failed"


def test_code_cannot_be_relabelled_image_by_a_malformed_receipt():
    outcome = assess([{"kind": "image"}], [artifact("script.py", "image")])
    assert outcome.status != "succeeded"
    assert outcome.reason_code == "artifact_validation_failed"


def test_validation_unavailable_is_not_an_existence_or_model_success_fallback():
    _, uploaded = artifact()
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [], [uploaded],
                              execution_success=True, validation_available=False)
    assert outcome.status != "succeeded"
    assert outcome.reason_code == "validation_unavailable"


def test_upload_failure_cannot_be_declared_complete():
    record, _ = artifact()
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [record], [], execution_success=True)
    assert outcome.status == "failed"
    assert outcome.reason_code == "delivery_failed"


def test_conflicting_duplicate_receipts_are_rejected():
    record, uploaded = artifact()
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [record, {**record, "sha256": "b" * 64}], [uploaded], execution_success=True)
    assert outcome.status != "succeeded"


def test_report_only_analysis_can_finish_without_artifact_contract():
    outcome = assess_delivery([], [], [], execution_success=True)
    assert outcome.status == "succeeded"


def test_private_blocking_paths_never_confuse_identical_basenames():
    good, uploaded = artifact("accepted/result.csv", "table")
    failed = {"path": "/home/ubuntu/output/failed/result.csv", "kind": "table", "valid": False,
              "reason": "inconsistent_table_width"}
    optional = {"path": "/home/ubuntu/output/extra/result.pdf", "kind": "report", "valid": False,
                "reason": "unsupported_format"}
    requirements = [DeliverableRequirement(kind="table", min_count=2, formats=["csv"])]
    assert blocking_receipt_paths(requirements, [good, failed, optional], [uploaded]) == {failed["path"]}
    requirements[0].min_count = 1
    assert not blocking_receipt_paths(requirements, [good, failed, optional], [uploaded])


def test_private_blocking_paths_are_not_limited_by_public_issue_display_size():
    records = [{"path": f"/home/ubuntu/output/folder-{index}/result.csv", "kind": "table", "valid": False,
                "reason": "empty_file"} for index in range(80)]
    required = [DeliverableRequirement(kind="table", formats=["csv"])]
    assert len(issues_from_records(required, records)) == 64
    assert blocking_receipt_paths(required, records) == {item["path"] for item in records}


def test_private_blocking_paths_use_format_exact_bytes_and_safe_internal_roots():
    valid, uploaded = artifact("table.csv", "table")
    uploaded.metadata["artifact_sha256"] = "b" * 64
    unsafe = {"path": "/Users/private/table.csv", "kind": "table", "valid": False,
              "reason": "unavailable_or_unsafe_path"}
    other_format = {"path": "/home/ubuntu/output/table.xlsx", "kind": "table", "valid": False,
                    "reason": "empty_table"}
    unknown = {"path": "/home/ubuntu/output/unknown.csv", "expected_kind": "table", "valid": False,
               "reason": "validation_unavailable"}
    required = [DeliverableRequirement(kind="table", formats=["csv"])]
    assert blocking_receipt_paths(required, [valid, unsafe, other_format, unknown], [uploaded],
                                  validation_available=False) == {valid["path"], unknown["path"]}


def test_step_scope_does_not_repeat_task_wide_requirements_on_every_step():
    message = Message(deliverables=[DeliverableRequirement(kind="image", min_count=2)])
    assert requirements_for_step(Step(description="inspect inputs"), message) == []
    declared = requirements_for_step(Step(deliverables=[DeliverableRequirement(kind="table")]), message)
    assert [item.kind for item in declared] == ["table"]
    assert [item.kind for item in requirements_for_step(Step(inputs={"dataset_intent": "visualization"}), message)] == ["image"]


def test_public_labels_and_extensions_are_bounded_and_do_not_disclose_model_paths():
    requirement = DeliverableRequirement(kind="image", formats=[".PNG", "png"], label="/Users/private/data/plot")
    assert requirement.label == "图表"
    assert requirement.formats == ["png"]
    for formats in [["/Users/private/file"], ["../png"], ["png|jpg"], ["a" * 17]]:
        with pytest.raises(ValidationError):
            DeliverableRequirement(kind="image", formats=formats)
    with pytest.raises(ValidationError):
        DeliverableRequirement(kind="image", min_count=True)
    result = AnalysisOutcome(status="partial", reason_code="artifacts_missing", missing=[requirement])
    assert "/Users" not in result.model_dump_json()
    assert "/Users" not in outcome_message(result)
    copied = requirement.model_copy(update={"label": "/Users/private/label"})
    assert "/Users" not in copied.model_dump_json()


@pytest.mark.parametrize("field,value", [("reason_code", "/Users/private/error"), ("resume_from", "../token"), ("can_resume", "yes")])
def test_public_outcome_rejects_uncontrolled_contract_fields(field, value):
    payload = {"status": "failed", "reason_code": "execution_failed", field: value}
    with pytest.raises(ValidationError):
        AnalysisOutcome.model_validate(payload)


def test_copied_internal_requirement_cannot_bypass_strict_count_validation():
    weakened = DeliverableRequirement(kind="image", min_count=2).model_copy(update={"min_count": 0})
    with pytest.raises(ValidationError):
        assess_delivery([weakened], [], [], execution_success=True)


def test_partial_summary_reports_the_actual_chart_and_code_without_a_generic_script_warning():
    _, chart = artifact()
    _, script = artifact("analysis.py", "code", identity="code-file")
    outcome = AnalysisOutcome(status="partial", reason_code="artifacts_missing",
                              missing=[DeliverableRequirement(kind="table", min_count=2)])
    text = outcome_message(outcome, delivered_files=[chart, script])
    assert "已交付并保留：图表 × 1、代码 × 1。" in text
    assert "待完成：数据表 × 2。" in text
    assert "代码附件不代表" not in text
    assert "阶段性文件" not in text
    assert "/home/ubuntu" not in text


def test_summary_distinguishes_missing_markdown_from_delivered_json_reports():
    files = [artifact(f"result-{index}.json", "report", identity=f"json-{index}") for index in range(3)]
    outcome = assess([{"kind": "report", "formats": ["md"]}], files)
    before = outcome.model_dump()
    text = outcome_message(outcome, delivered_files=[uploaded for _, uploaded in files])
    assert "已交付并保留：报告 × 3。" in text
    assert "待完成：报告（MD） × 1。" in text
    assert outcome.model_dump() == before
    assert outcome.status == "partial"
    assert outcome.reason_code == "artifacts_missing"
    assert [(item.kind, item.min_count, item.formats) for item in outcome.missing] == [("report", 1, ["md"])]


@pytest.mark.parametrize("formats,expected", [
    ([".MD", "md", "TXT"], "报告（MD / TXT） × 2"),
    ([], "报告 × 2"),
    (["unknownformat"], "报告 × 2"),
])
def test_missing_summary_formats_are_bounded_labels(formats, expected):
    outcome = AnalysisOutcome(status="partial", reason_code="artifacts_missing", missing=[
        DeliverableRequirement(kind="report", min_count=2, formats=formats),
    ])
    assert f"待完成：{expected}。" in outcome_message(outcome)


@pytest.mark.parametrize("formats", [
    None, "md", {}, [None], [1], ["md", "/private/secret"], ["<script>"],
    ["md\nsecret"], [" md "], ["..md"], ["md"] * 9,
])
def test_missing_summary_does_not_expose_malformed_formats_from_internal_copies(formats):
    outcome = AnalysisOutcome(status="partial", reason_code="artifacts_missing", missing=[
        DeliverableRequirement(kind="report"),
    ])
    outcome.missing[0] = outcome.missing[0].model_copy(update={"formats": formats})
    assert "待完成：报告 × 1。" in outcome_message(outcome)


def test_success_summary_does_not_say_failed_or_hide_verified_delivery_types():
    _, chart = artifact()
    text = outcome_message(AnalysisOutcome(status="succeeded", reason_code="completed"), delivered_files=[chart])
    assert text.startswith("本次分析已完成。")
    assert "图表 × 1" in text
    assert "未完成" not in text and "待完成" not in text


def test_code_only_delivery_does_not_claim_charts_or_remove_missing_chart_count():
    _, script = artifact("analysis.py", "code")
    outcome = AnalysisOutcome(status="partial", reason_code="artifacts_missing",
                              missing=[DeliverableRequirement(kind="image", min_count=2)])
    text = outcome_message(outcome, delivered_files=[script])
    assert "已交付并保留：代码 × 1。" in text
    assert "待完成：图表 × 2。" in text
    assert "代码附件不代表" not in text


def test_actual_file_list_overrides_legacy_count_and_deduplicates_ids_and_paths():
    _, chart = artifact()
    _, duplicate_id = artifact("alias.png")
    duplicate_path = chart.model_copy(update={"file_id": "second-id"})
    outcome = AnalysisOutcome(status="partial", reason_code="execution_failed")
    text = outcome_message(outcome, delivered_count=99, delivered_files=[chart, chart, duplicate_id, duplicate_path])
    assert "图表 × 1" in text and "99" not in text
    assert "已交付" not in outcome_message(outcome, delivered_count=99, delivered_files=[])


def test_summary_legacy_count_is_truthful_without_inventing_types():
    outcome = AnalysisOutcome(status="partial", reason_code="finalization_timeout")
    text = outcome_message(outcome, delivered_count=2)
    assert "已交付并保留 2 个文件。" in text
    assert all(label not in text for label in ["图表", "代码", "数据表", "报告"])
    for count in [0, -1, True]:
        assert "已交付" not in outcome_message(outcome, delivered_count=count)


@pytest.mark.parametrize("change", [
    {"file_id": None}, {"size": 0}, {"size": True}, {"metadata": {}},
    {"file_path": "/Users/private/file.png"}, {"file_path": "/home/ubuntu/output/../private.png"},
])
def test_incomplete_or_unsafe_upload_objects_do_not_become_completed_counts(change):
    _, chart = artifact()
    values = chart.model_dump()
    values.update(change)
    assert verified_delivery_counts([values]) == {}


def test_dictionary_delivery_can_preserve_a_receipt_verified_json_table_kind():
    _, table = artifact("summary.json", "table")
    values = table.model_dump()
    values["kind"] = "table"
    assert verified_delivery_counts([values]) == {"table": 1}


def test_unknown_reason_is_honest_without_reexecution_promises_or_raw_diagnostics():
    outcome = AnalysisOutcome(status="failed", reason_code="future_unknown_code")
    text = outcome_message(outcome)
    assert "具体原因暂未确认" in text
    assert "future_unknown_code" not in text
    assert all(claim not in text for claim in ["重新执行", "重新运行", "自动重试", "已交付", "已保留"])


def test_resume_hint_requires_actual_safe_checkpoint_and_never_appears_on_completed_task():
    token = "a" * 32
    partial = AnalysisOutcome(status="partial", reason_code="execution_failed", can_resume=True)
    assert "继续未完成部分" not in outcome_message(partial)
    partial.resume_from = token
    assert "继续未完成部分" in outcome_message(partial)
    completed = AnalysisOutcome(status="succeeded", reason_code="completed", can_resume=True, resume_from=token)
    assert "继续未完成部分" not in outcome_message(completed)


def test_inconsistent_completed_status_does_not_claim_success_over_missing_requirements():
    outcome = AnalysisOutcome(status="succeeded", reason_code="completed", missing=[DeliverableRequirement(kind="image")])
    text = outcome_message(outcome)
    assert "本次分析已完成" not in text
    assert "待完成：图表 × 1" in text


@pytest.mark.parametrize("reason,expected", [
    ("analysis_budget_deadline_exceeded", "最长执行时间"),
    ("analysis_budget_store_unavailable", "无法可靠记录执行额度"),
    ("budget_no_progress_loop", "未产生新的有效进展"),
])
def test_budget_stop_preserves_the_specific_reason_and_does_not_promise_rerun(reason, expected):
    outcome = assess_delivery([], [], [], execution_success=False, stop_code=reason)
    assert outcome.reason_code == reason
    text = outcome_message(outcome)
    assert expected in text
    assert "请重发" not in text and "自动重跑" not in text


def failed_artifact(name="extra.csv", kind="table", reason="inconsistent_table_width", **extra):
    return {"path": f"/home/ubuntu/output/{name}", "kind": kind, "expected_kind": kind,
            "valid": False, "reason": reason, "sha256": "f" * 64, "size": 123,
            "diagnostics": {"row_number": 2, "expected_columns": 3, "actual_columns": 4}, **extra}


@pytest.mark.parametrize("kind,name", [
    ("image", "result.png"), ("table", "result.csv"), ("report", "result.md"), ("code", "result.py"),
])
def test_optional_bad_artifact_does_not_reject_a_completed_required_kind(kind, name):
    good, uploaded = artifact(name, kind)
    outcome = assess_delivery([DeliverableRequirement(kind=kind)], [good, failed_artifact()], [uploaded], execution_success=True)
    assert outcome.status == "succeeded" and outcome.reason_code == "completed"
    assert not outcome.missing
    assert len(outcome.issues) == 1 and outcome.issues[0].blocking is False
    assert outcome.issues[0].reason_code == "inconsistent_table_width"
    assert "diagnostics" not in outcome.model_dump_json()
    assert "/home/ubuntu" not in outcome.model_dump_json()


@pytest.mark.parametrize("kind,name", [
    ("image", "result.webp"), ("image", "result.avif"), ("table", "result.tsv"), ("table", "result.xlsx"),
    ("report", "result.html"), ("report", "result.markdown"), ("report", "result.json"),
    ("code", "result.sql"), ("code", "result.ipynb"),
])
def test_required_file_format_counts_are_based_on_exact_validated_delivery(kind, name):
    outcome = assess([{"kind": kind, "formats": [name.rsplit(".", 1)[1]]}], [artifact(name, kind)])
    assert outcome.status == "succeeded"


def test_failed_required_table_keeps_detail_and_does_not_hide_delivered_chart():
    image, uploaded = artifact()
    requirements = [DeliverableRequirement(kind="image"), DeliverableRequirement(kind="table", formats=["csv"])]
    records = [image, failed_artifact()]
    outcome = assess_delivery(requirements, records, [uploaded], execution_success=True)
    assert outcome.status == "partial" and outcome.reason_code == "artifact_validation_failed"
    assert [(item.kind, item.min_count) for item in outcome.missing] == [("table", 1)]
    assert outcome.issues[0].blocking is True
    feedback = repair_feedback(requirements, records, [uploaded])
    assert feedback == [{"artifact_name": "extra.csv", "kind": "table", "reason_code": "inconsistent_table_width",
                         "blocking": True, "diagnostics": {"actual_columns": 4, "expected_columns": 3, "row_number": 2}}]
    assert "图表 × 1" in outcome_message(outcome, delivered_files=[uploaded])


def test_bad_same_kind_is_only_blocking_when_its_format_and_count_slot_is_missing():
    first, first_upload = artifact("one.csv", "table")
    second, second_upload = artifact("two.tsv", "table", identity="two")
    bad = failed_artifact("extra.tsv")
    requirements = [DeliverableRequirement(kind="table", min_count=2, formats=["csv"])]
    outcome = assess_delivery(requirements, [first, second, bad], [first_upload, second_upload], execution_success=True)
    assert outcome.missing[0].min_count == 1 and not outcome.issues[0].blocking
    assert outcome.reason_code == "artifacts_missing"
    bad["path"] = "/home/ubuntu/output/extra.csv"
    outcome = assess_delivery(requirements, [first, second, bad], [first_upload, second_upload], execution_success=True)
    assert outcome.issues[0].blocking and outcome.reason_code == "artifact_validation_failed"


def test_valid_required_delivery_survives_partial_validation_service_failure():
    good, uploaded = artifact()
    unavailable = failed_artifact(reason="validation_unavailable", diagnostics={})
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [good, unavailable], [uploaded],
                              execution_success=True, validation_available=False)
    assert outcome.status == "succeeded" and not outcome.issues[0].blocking
    outcome = assess_delivery([DeliverableRequirement(kind="table")], [good, unavailable], [uploaded],
                              execution_success=True, validation_available=False)
    assert outcome.status == "partial" and outcome.reason_code == "validation_unavailable"
    assert outcome.issues[0].blocking


def test_auxiliary_upload_failure_is_a_warning_not_required_delivery_failure():
    good, uploaded = artifact()
    optional, _ = artifact("extra.xlsx", "table", identity="extra")
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [good, optional], [uploaded], execution_success=True)
    assert outcome.status == "succeeded"
    assert outcome.issues[0].reason_code == "delivery_failed" and not outcome.issues[0].blocking


def test_no_file_requirement_does_not_promote_optional_files_into_required_work():
    outcome = assess_delivery([], [failed_artifact()], [], execution_success=True)
    assert outcome.status == "succeeded" and not outcome.issues[0].blocking
    assert not outcome.missing
    assert "数据表 ×" not in outcome_message(outcome, delivered_files=[])


@pytest.mark.parametrize("stop_code", ["tool_execution_unknown", "tool_execution_failed", "execution_failed"])
def test_required_files_cannot_override_real_execution_failure(stop_code):
    good, uploaded = artifact()
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [good], [uploaded],
                              execution_success=False, stop_code=stop_code)
    assert outcome.status == "partial" and outcome.reason_code == stop_code
    assert not outcome.missing


def test_conflicting_receipt_cannot_count_even_when_one_version_matches_upload():
    good, uploaded = artifact()
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [good, {**good, "size": 1}],
                              [uploaded], execution_success=True)
    assert outcome.status == "failed" and outcome.missing[0].min_count == 1
    assert outcome.issues[0].reason_code == "validation_receipt_invalid"


def test_per_file_issues_and_repair_feedback_never_emit_uncontrolled_text_or_path():
    record = failed_artifact("nested/unsafe\u202ename.csv", reason="/Users/private/raw parser detail",
                             diagnostics={"row_number": 2, "expected_columns": True, "actual_columns": 4,
                                          "column_name": "secret", "path": "/Users/private", "size_bytes": 2**64})
    issue = issues_from_records([DeliverableRequirement(kind="table")], [record])[0]
    assert issue.artifact_name == "unsafename.csv" and issue.reason_code == "invalid_content"
    feedback = repair_feedback([DeliverableRequirement(kind="table")], [record])
    assert feedback[0]["diagnostics"] == {"actual_columns": 4, "row_number": 2}
    assert all(text not in str(feedback) for text in ("/Users", "/home/ubuntu", "secret", "parser detail", "\u202e"))


@pytest.mark.parametrize("change", [{"kind": []}, {"reason": {}}, {"valid": []}, {"path": []}])
def test_malformed_receipts_fail_closed_without_raising_or_crediting_files(change):
    record = failed_artifact(**change)
    outcome = assess_delivery([DeliverableRequirement(kind="table")], [record], [], execution_success=True)
    assert outcome.status == "failed" and outcome.missing


def test_public_issue_collection_is_bounded_and_keeps_required_faults_first():
    records = [failed_artifact(f"extra-{index}.csv") for index in range(70)]
    records.append(failed_artifact("required.png", kind="image", reason="invalid_content"))
    issues = issues_from_records([DeliverableRequirement(kind="image")], records)
    assert len(issues) == 64 and issues[0].blocking and issues[0].artifact_name == "required.png"
