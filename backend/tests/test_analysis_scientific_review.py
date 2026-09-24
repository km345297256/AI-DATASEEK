"""Scientific review contract tests; these do not prove scientific truth."""
import copy
import json
from types import SimpleNamespace

import pytest

from app.domain.services.analysis_answer_review import _citations
from app.domain.services.analysis_scientific_review import (
    SCIENTIFIC_DIMENSIONS, SCIENTIFIC_REVIEW_RULES, scientific_review_metadata,
)


def target(identity="report_0001", **changes):
    values = dict(report_id=identity, target_kind="structured", format="json", size=32,
                  sha256="a" * 64, text='{"observed":42}', read_complete=True,
                  file_path="/home/ubuntu/output/result.json", file_id="uploaded-result")
    return SimpleNamespace(**(values | changes))


def source(**changes):
    return dict(source_id="tool_0001_result", kind="tool_result", state="succeeded",
                function="program_run", text='{"observed":42,"scope":"descriptive"}',
                truncated=False, write_only=False) | changes


def checks(ids=("report_0001",)):
    return [dict(dimension=dimension, status="verified", target_ids=list(ids),
                 evidence=[dict(source_id="tool_0001_result", quote='"observed":42')])
            for dimension in SCIENTIFIC_DIMENSIONS]


def run(items=None, targets=None, lookup=None, **kwargs):
    return scientific_review_metadata(targets if targets is not None else [target()],
        checks() if items is None else items, citations=_citations,
        lookup=lookup if lookup is not None else {"tool_0001_result": source()}, **kwargs)


def test_complete_four_dimensions_bound_to_all_current_structured_targets():
    targets = [target(), target("report_0002", format="csv", file_path="/home/ubuntu/output/data.csv")]
    items = checks(("report_0002", "report_0001"))
    frozen = copy.deepcopy(items)
    result = run(items, targets)
    assert result["enabled"] and result["status"] == "verified"
    assert result["target_count"] == 2 and result["checked_dimensions"] == 4
    assert [item["dimension"] for item in result["dimensions"]] == list(SCIENTIFIC_DIMENSIONS)
    assert all(item["target_count"] == 2 for item in result["dimensions"])
    assert items == frozen


def test_non_structured_reports_do_not_activate_an_extra_contract():
    result = run({"unexpected": "ignored"}, [target(target_kind="report", format="md")])
    assert result["enabled"] is False and result["status"] == "not_applicable"
    assert result["dimensions"] == []


@pytest.mark.parametrize("change", [dict(text=None), dict(read_complete=False), dict(format="xml"),
    dict(size=None), dict(size=True), dict(sha256=None), dict(sha256="not-versioned")])
def test_unverified_or_unsupported_target_cannot_pass(change):
    assert run(targets=[target(**change)])["status"] == "unavailable"


def test_duplicate_target_identity_and_budget_incompleteness_fail_closed():
    assert run(targets=[target(), target()])["status"] == "unavailable"
    assert run(evidence_complete=False)["status"] == "unavailable"


@pytest.mark.parametrize("mutation", ["missing", "duplicate_dimension", "unknown_dimension", "unknown_key",
    "missing_target", "extra_target", "duplicate_target", "missing_evidence", "wrong_status", "extra_item"])
def test_missing_or_forged_checks_never_count_as_complete(mutation):
    items = checks()
    if mutation == "missing": items.pop()
    elif mutation == "duplicate_dimension": items[1]["dimension"] = items[0]["dimension"]
    elif mutation == "unknown_dimension": items[0]["dimension"] = "model_invented"
    elif mutation == "unknown_key": items[0]["private_reason"] = "private text"
    elif mutation == "missing_target": items[0]["target_ids"] = []
    elif mutation == "extra_target": items[0]["target_ids"].append("report_other")
    elif mutation == "duplicate_target": items[0]["target_ids"] *= 2
    elif mutation == "missing_evidence": items[0].pop("evidence")
    elif mutation == "wrong_status": items[0]["status"] = "pass"
    else: items.append(copy.deepcopy(items[0]))
    assert run(items)["status"] == "unavailable"


@pytest.mark.parametrize("items", [None, {}, "verified", [None], []])
def test_missing_checks_are_unavailable(items):
    assert scientific_review_metadata([target()], items, citations=_citations,
        lookup={"tool_0001_result": source()})["status"] == "unavailable"


@pytest.mark.parametrize("evidence", [[], [{"source_id": "unknown", "quote": "x"}],
    [{"source_id": "tool_0001_result", "quote": "not actually observed"}],
    [{"source_id": "tool_0001_result", "quote": "x" * 161}],
    [{"source_id": "tool_0001_result", "quote": ""}],
    [{"source_id": "tool_0001_result", "quote": '"observed":42', "extra": 1}],
    [{"source_id": "tool_0001_result", "quote": '"observed":42'}] * 3])
def test_verified_requires_bounded_exact_independent_citations(evidence):
    items = checks(); items[0]["evidence"] = evidence
    assert run(items)["status"] == "unavailable"


@pytest.mark.parametrize("change", [dict(method_only=True), dict(code=True), dict(write_only=True),
    dict(inventory=True), dict(report_content_only=True), dict(current_request=True),
    dict(kind="tool_request"), dict(kind="catalog"), dict(kind="prior_review"),
    dict(state="failed"), dict(truncated=True), dict(function="file_write"),
    dict(function="file_find_by_name"), dict(function="file_list")])
def test_code_write_inventory_request_or_self_content_is_not_measured_evidence(change):
    assert run(lookup={"tool_0001_result": source(**change)})["status"] == "unavailable"


def test_reading_delivered_target_is_not_self_authenticating():
    lookup = {"tool_0001_request": dict(source_id="tool_0001_request", kind="tool_request",
        state="succeeded", function="file_read", text=json.dumps({"file": "/home/ubuntu/output/result.json"}),
        truncated=False), "tool_0001_result": source(function="file_read")}
    frozen = copy.deepcopy(lookup)
    assert run(lookup=lookup)["status"] == "unavailable"
    assert lookup == frozen


@pytest.mark.parametrize("suffix", ["py", "R", "sh", "ipynb"])
def test_reading_program_source_is_not_a_measured_result_even_without_a_code_flag(suffix):
    lookup = {"tool_0001_request": dict(source_id="tool_0001_request", kind="tool_request",
        state="succeeded", function="file_read", text=json.dumps({"file": "/home/ubuntu/analysis." + suffix}),
        truncated=False), "tool_0001_result": source(function="file_read")}
    assert run(lookup=lookup)["status"] == "unavailable"


def test_reading_independent_original_data_still_supports_scope_and_numeric_fidelity():
    lookup = {"tool_0001_request": dict(source_id="tool_0001_request", kind="tool_request",
        state="succeeded", function="file_read", text=json.dumps({"file": "/home/ubuntu/datasets/original.csv"}),
        truncated=False), "tool_0001_result": source(function="file_read")}
    assert run(lookup=lookup)["status"] == "verified"


@pytest.mark.parametrize("dimension", SCIENTIFIC_DIMENSIONS)
def test_not_applicable_cannot_be_model_selected_by_default(dimension):
    items = checks(); next(row for row in items if row["dimension"] == dimension)["status"] = "not_applicable"
    assert run(items)["status"] == "unavailable"


def test_host_scope_may_allow_other_dimensions_but_never_numeric_consistency():
    items = checks()
    for row in items:
        if row["dimension"] != "numeric_consistency": row["status"] = "not_applicable"
    allowed = frozenset(SCIENTIFIC_DIMENSIONS) - {"numeric_consistency"}
    assert run(items, not_applicable_dimensions=allowed)["status"] == "verified"
    items[1]["status"] = "not_applicable"
    assert run(items, not_applicable_dimensions=frozenset(SCIENTIFIC_DIMENSIONS))["status"] == "unavailable"


def test_not_applicable_still_needs_independent_observation():
    items = checks(); items[0].update(status="not_applicable", evidence=[])
    assert run(items, not_applicable_dimensions=frozenset({"design_estimand"}))["status"] == "unavailable"


def test_valid_rejection_survives_unclear_dimension_without_claiming_success():
    items = checks(); items[0].update(status="rejected", evidence=[])
    items[2].update(status="unclear", evidence=[])
    result = run(items)
    assert result["status"] == "rejected" and result["reason"] == "scientific_claim_rejected"


def test_unclear_is_unavailable_even_with_valid_evidence():
    items = checks(); items[2]["status"] = "unclear"
    assert run(items)["status"] == "unavailable"


def test_no_raw_quotes_paths_or_exception_strings_in_public_metadata():
    secret = "/Users/private/secret-key"
    items = checks()
    for row in items: row["evidence"][0]["quote"] = secret
    result = run(items, lookup={"tool_0001_result": source(text=secret)})
    assert result["status"] == "verified"
    assert secret not in json.dumps(result)
    def broken(*_args): raise ValueError(secret)
    result = scientific_review_metadata([target()], items, citations=broken, lookup={})
    assert result["status"] == "unavailable" and secret not in json.dumps(result)


def test_prompt_rules_require_four_dimensions_scope_and_no_new_execution():
    assert all(dimension in SCIENTIFIC_REVIEW_RULES for dimension in SCIENTIFIC_DIMENSIONS)
    assert "not a proof" in SCIENTIFIC_REVIEW_RULES
    assert "Do not use tools" in SCIENTIFIC_REVIEW_RULES
    assert "numeric_consistency must never be not_applicable" in SCIENTIFIC_REVIEW_RULES
