"""Text-only designs require actual coverage, not files or request repetition."""
import asyncio
import json
from unittest.mock import AsyncMock

from langchain.messages import AIMessage
import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_answer_coverage import answer_objectives, review_coverage
from test_analysis_answer_review import paragraph, response, tool
from test_answer_review_paragraph_recovery import candidate, repair


REQUEST = ("将建议转化为Notebook结构：研究问题、输入说明、环境、清洗、分析、验证和结论。"
           "为每一节列出应展示的输入输出和检查项，并指出隐含状态、乱序执行及手工修改结果可能造成的具体问题。")
REFERENCE = "The reference recommends recording inputs, environment and ordered executable steps."
DESIGN = [
    ("研究问题", "原研究假设与候选变量", "可证伪的比较问题", "问题范围是否与观测样本一致"),
    ("数据来源", "只读数据及来源说明", "带校验和的数据目录", "原文件数量与登记是否一致"),
    ("软件环境", "运行时及依赖清单", "固定版本的环境记录", "从空环境能否重建依赖"),
    ("预处理", "原始字段和缺失定义", "清洗后数据与改动日志", "清洗前后行数及排除原因是否守恒"),
    ("统计分析", "清洗数据与预定假设", "带估计量定义的效应结果", "模型假设和样本独立性是否成立"),
    ("独立核验", "核心结果及独立计算方法", "误差对照和失败明细", "重启后数值是否在容差内一致"),
    ("研究结论", "已核验结果与边界", "限定适用范围的结论", "表图正文是否一致且没有因果夸大"),
]
RISKS = ["隐含状态可能使未记录变量影响运行结果。", "乱序执行可能读取过时变量并使结果依赖点击顺序。",
         "手工修改结果使图表失去可重建的计算来源。"]


def full_paragraphs():
    return [paragraph(f"{title}：建议输入为{a}；建议输出为{b}；检查项为{c}。", quote=REFERENCE)
            for title, a, b, c in DESIGN] + [paragraph(text, quote=REFERENCE) for text in RISKS]


def coverage_response(*, missing=(), title_only=False, echoed=False, reused=False):
    checks = []
    for objective in answer_objectives(REQUEST):
        i = objective.index
        if i in missing:
            checks.append({"index": i, "status": "missing", "spans": []})
            continue
        paragraph_index = i // 3 if i < 21 else i - 21 + 7
        quote = DESIGN[i // 3][i % 3 + 1] if i < 21 else RISKS[i - 21]
        if title_only and i < 21:
            quote = DESIGN[i // 3][0]
        if echoed:
            paragraph_index, quote = 1, REQUEST
        if reused:
            paragraph_index, quote = 0, "The source recommends dependency records."
        checks.append({"index": i, "status": "met", "spans": [{"paragraph_index": paragraph_index, "quote": quote}]})
    return AIMessage(content=json.dumps({"checks": checks}, ensure_ascii=False))


async def run(*replies, question=REQUEST):
    evidence = review.AnswerEvidence()
    evidence.begin_step("design")
    evidence.observe(tool("file_read", args={"file": "/home/ubuntu/reference.txt"}, data={"content": REFERENCE}))
    ask = AsyncMock(side_effect=replies)
    result = await review.review_answer(ask=ask, question=question, draft="untrusted proposal", files=[],
                                       evidence=evidence, requirements=())
    return result, ask


def test_objectives_come_from_request_not_dataset_or_fixed_seven_section_template():
    objectives = answer_objectives(REQUEST)
    assert len(objectives) == 24
    assert [(o.topic, o.aspect) for o in objectives[:3]] == [
        ("研究问题", "输入"), ("研究问题", "输出"), ("研究问题", "检查项")]
    other = answer_objectives("Design sections: Preparation, Sampling and Archive. For each section list inputs, outputs and checks.")
    assert len(other) == 9 and other[-1].topic == "Archive" and other[-1].aspect == "checks"
    assert not answer_objectives("Summarize the source recommendations.")
    compound = answer_objectives("请按以下章节：输入与来源、环境及版本、清洗和结论。")
    assert [o.topic for o in compound] == ["输入与来源", "环境及版本", "清洗", "结论"]


@pytest.mark.parametrize("question", [
    "请概括文献。文献结构：背景、方法、结果。",
    "The paper has sections: introduction, methods and discussion. Summarize its conclusion.",
    "例如设计结构：数据、计算、结果。请解释这里的结构一词。",
    "For example, design sections: Inputs, Analysis and Results. Explain the word sections.",
])
def test_descriptive_or_example_section_names_do_not_become_requested_slots(question):
    assert not answer_objectives(question)


@pytest.mark.asyncio
async def test_repeated_field_values_in_distinct_markdown_rows_are_allowed():
    request = "Design sections: Cleaning and Analysis. For each section list inputs, outputs and checks."
    rows = [
        ("Cleaning", "original observations", "cleaned table", "row conservation"),
        ("Analysis", "original observations", "estimated effect", "model assumptions"),
    ]
    table = "| section | input | output | check |\n|---|---|---|---|\n" + "\n".join(
        "| " + " | ".join(row) + " |" for row in rows)
    checks = [{"index": i, "status": "met", "spans": [{"paragraph_index": 0, "quote": rows[i // 3][i % 3 + 1]}]}
              for i in range(6)]
    result, ask = await run(response(paragraph(table, quote=REFERENCE)),
        AIMessage(content=json.dumps({"checks": checks})), question=request)
    assert result.status == "verified" and ask.await_count == 2


@pytest.mark.asyncio
async def test_truncated_structured_request_never_claims_complete_or_requests_more_evidence():
    request = REQUEST + " 附加说明。" * 2000
    result, ask = await run(response(*full_paragraphs()), question=request)
    assert result.status == "unavailable" and ask.await_count == 1
    assert result.metadata["answer_coverage"]["reason"] == "coverage_input_incomplete"
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_oversize_explicit_objective_matrix_is_unknown_not_silently_shortened():
    request = "Design sections: " + ", ".join(f"phase {i}" for i in range(9))
    request += ". For each section list " + ", ".join(f"field {i}" for i in range(8)) + "."
    objectives = answer_objectives(request)
    assert len(objectives) == 1 and objectives[0].aspect == "coverage_scope_exceeds_limit"
    ask = AsyncMock()
    result = await review_coverage(ask=ask, parse_response=review._parse_response, request=request,
        objectives=objectives, paragraphs=[{"text": "Supported answer", "kind": "analysis"}], timeout_seconds=1)
    assert result.status == "unavailable" and result.metadata["reason"] == "coverage_input_incomplete"
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_text_design_with_alias_titles_passes_with_zero_files():
    result, ask = await run(response(*full_paragraphs()), coverage_response())
    assert result.status == "verified" and ask.await_count == 2
    assert result.metadata["answer_coverage"]["status"] == "verified"
    assert result.metadata["answer_coverage"]["objective_count"] == 24
    assert result.missing_requirement_indices == ()
    assert "研究问题" not in json.dumps(result.metadata, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [(0, 1, 2), (8,), (21, 22, 23)])
async def test_named_section_field_or_risk_omission_prevents_completion(missing):
    result, _ = await run(response(*full_paragraphs()), coverage_response(missing=missing))
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_incomplete"
    assert result.metadata["validation_state"] == "rejected"
    assert result.metadata["answer_coverage"]["missing_indices"] == list(missing)
    assert DESIGN[0][1] in result.text and result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("reused,echoed", [(True, False), (False, True)])
async def test_valid_summary_plus_question_cannot_be_declared_complete(reused, echoed):
    summary = paragraph("The source recommends dependency records.", quote=REFERENCE)
    context = paragraph(REQUEST, kind="context", source="current_request", quote=REQUEST)
    result, _ = await run(response(summary, context), coverage_response(reused=reused, echoed=echoed))
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert "The source recommends dependency records." in result.text
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_citation_correction_cannot_drop_requested_design_and_still_pass():
    good = paragraph("The source recommends dependency records.", quote=REFERENCE)
    bad = paragraph("Rejected quote.", quote="not in the source")
    result, ask = await run(response(bad), repair(candidate(good["text"], "analysis", "tool_0001_result"), index=0),
                            coverage_response(reused=True))
    assert ask.await_count == 3 and result.status == "unavailable"
    assert result.metadata["citation_repair_status"] == "corrected"
    assert result.metadata["reason"] == "answer_coverage_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [TimeoutError(), RuntimeError("PRIVATE_PROVIDER_DETAILS")])
async def test_coverage_reviewer_unavailable_preserves_answer_without_claiming_execution_failure(fault):
    result, _ = await run(response(*full_paragraphs()), fault)
    assert result.status == "unavailable" and result.metadata["validation_state"] == "unavailable"
    assert result.metadata["reason"] == "answer_coverage_unverified" and DESIGN[0][1] in result.text
    assert "PRIVATE_PROVIDER" not in json.dumps(result.metadata)
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_simple_summary_keeps_one_review_and_no_structure_template():
    result, ask = await run(response(paragraph("The source recommends dependency records.", quote=REFERENCE)),
                            question="Summarize the source recommendations.")
    assert result.status == "verified" and ask.await_count == 1
    assert "answer_coverage" not in result.metadata


@pytest.mark.asyncio
async def test_title_only_or_placeholder_slots_are_not_substantive():
    result, _ = await run(response(*full_paragraphs()), coverage_response(title_only=True))
    assert result.status == "unavailable"
    assert result.metadata["answer_coverage"]["unverified_indices"]


@pytest.mark.asyncio
async def test_cancellation_during_coverage_propagates():
    with pytest.raises(asyncio.CancelledError):
        await run(response(*full_paragraphs()), asyncio.CancelledError())


@pytest.mark.asyncio
async def test_transport_failure_and_actual_citation_rejection_have_distinct_metadata():
    failed, _ = await run(RuntimeError("private transport"), question="Summarize the source.")
    interrupted, _ = await run(response(paragraph("Bad claim", quote="absent")), RuntimeError("unavailable correction"),
                               question="Summarize the source.")
    rejected, _ = await run(response(paragraph("Bad claim", quote="absent")),
        repair(candidate("Bad claim", "analysis", "verified_files"), index=0), question="Summarize the source.")
    assert failed.metadata["validation_state"] == "unavailable"
    assert interrupted.metadata["validation_state"] == "unavailable"
    assert rejected.metadata["validation_state"] == "rejected"
    assert "Bad claim" not in interrupted.text + rejected.text
