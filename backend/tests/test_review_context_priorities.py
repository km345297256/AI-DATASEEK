"""Regression for narrative starvation and private method-coverage diagnostics."""
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services.analysis_report_review import ReportTarget, fit_report_targets
from app.domain.services.analysis_scientific_review import executed_method_coverage
from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import tool
from test_structured_delivery_review import prepared, checked_answer


def target(identity, text, kind="report"):
    body = text.encode()
    return ReportTarget(identity, len(body), hashlib.sha256(body).hexdigest(),
                        text, "ready", True, target_kind=kind,
                        format="csv" if kind == "structured" else "md")


def test_large_table_cannot_starve_later_narrative_or_change_target_identity():
    # The old input order filled the allowance with a table, losing the report
    # that claimed the wrong curve direction and cross-condition comparisons.
    table = target("table", "x" * 18_000, "structured")
    report = target("report", "正文及末尾限制" * 500)
    fitted = fit_report_targets([table, report], text_budget=20_000)
    assert [item.report_id for item in fitted] == ["table", "report"]
    assert fitted[0].text is None and fitted[0].reason == "report_context_limit"
    assert fitted[0].size == table.size and fitted[0].sha256 == table.sha256
    assert fitted[1].text == report.text
    assert sum(len(item.text or "") for item in fitted) <= 20_000
    assert table.text is not None  # immutable input is retained, not rewritten


def test_unreadable_or_oversized_report_never_blocks_a_whole_small_target():
    table = target("table", "a,b\n1,2\n", "structured")
    oversized = target("large", "文" * 100)
    invalid = target("invalid", "stale")
    from dataclasses import replace
    invalid = replace(invalid, text="changed")
    fitted = fit_report_targets([table, oversized, invalid], text_budget=10)
    assert fitted[0].text == table.text
    assert [item.reason for item in fitted[1:]] == ["report_context_limit", "version_unverified"]
    assert fitted[1].text is None and fitted[2].text is None


def test_method_diagnostics_are_fixed_counts_without_code_or_private_values():
    secret = "/Users/private-fixture/credential.py"
    coverage = executed_method_coverage([
        {"executed_source_coverage": "full", "executed_program_source": secret},
        {"executed_source_coverage": "unverified", "executed_source_reason": "source_not_available"},
        {"executed_source_coverage": "unverified", "executed_source_reason": "evidence_budget_exceeded"},
        {"executed_source_coverage": "unverified", "executed_source_reason": secret},
        {"text": secret},
    ])
    assert coverage["status"] == "incomplete"
    assert coverage["program_result_count"] == 4
    assert coverage["full_source_count"] == 1 and coverage["unverified_source_count"] == 3
    assert coverage["unverified_reasons"] == {
        "source_not_available": 1, "evidence_budget_exceeded": 1, "other": 1}
    assert secret not in json.dumps(coverage)
    assert executed_method_coverage([])["status"] == "not_observed"


@pytest.mark.asyncio
async def test_real_review_persists_missing_method_reason_without_upgrading_science():
    info, _, artifact = await prepared()
    evidence = review.AnswerEvidence()
    evidence.begin_step("s")
    # A succeeded program result without a trusted launch receipt cannot prove
    # the exact method, even when the model supplies verified scientific checks.
    evidence.observe(tool("program_run", call="calc", args={"file": "/home/ubuntu/a.py"},
                          data={"stdout": "mean=3; unit=mg"}))
    ask = AsyncMock(side_effect=checked_answer)
    result = await review.review_answer(ask=ask, question="报告数值", draft="mean=3; unit=mg",
        files=[info], evidence=evidence, report_targets=[artifact])
    assert result.status == "unavailable"
    assert result.metadata["scientific_review"]["reason"] == "scientific_evidence_incomplete"
    assert result.metadata["executed_method_coverage"]["unverified_reasons"] == {"execution_unverified": 1}
    assert "/home/ubuntu/a.py" not in json.dumps(result.metadata["executed_method_coverage"])
