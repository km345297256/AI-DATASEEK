"""Answer-only review contracts are guards, not an automatic science oracle."""
import asyncio
import copy
import hashlib
import json

import pytest

from app.domain.services.analysis_answer_review import _citations
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS
from app.domain.services.analysis_text_scientific_review import (
    FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES, answer_scientific_review_metadata,
    answer_scope_review_metadata, revalidate_answer_candidate,
)


QUESTION = "Describe every input table, its actual unit and limitations. No model fitting requested."
PARAGRAPHS = ["The observed row count is 42.", "The source unit is mg; only a description is claimed."]


def source(**changes):
    return dict(source_id="tool_0001_result", kind="tool_result", state="succeeded",
        function="file_read", text='{"rows":42,"unit":"mg"}', step_id="step_current",
        truncated=False, write_only=False) | changes


def checks():
    return [dict(dimension=dimension, status="verified", paragraph_indices=[0, 1],
                 evidence=[dict(source_id="tool_0001_result", quote='"rows":42')])
            for dimension in SCIENTIFIC_DIMENSIONS]


def run(items=None, **changes):
    kwargs = dict(request=QUESTION, paragraphs=PARAGRAPHS, citations=_citations,
                  lookup={"tool_0001_result": source()}, current_step_id="step_current")
    kwargs.update(changes)
    return answer_scientific_review_metadata(checks() if items is None else items, **kwargs)


def scope(check=None, **changes):
    kwargs = dict(request=QUESTION, paragraphs=PARAGRAPHS)
    kwargs.update(changes)
    return answer_scope_review_metadata(check if check is not None else
        dict(status="complete", paragraph_indices=[0, 1]), **kwargs)


def test_descriptive_answer_can_pass_all_dimensions_without_artifacts_or_a_fitted_model():
    items, lookup = checks(), {"tool_0001_result": source()}
    frozen = copy.deepcopy((items, lookup, PARAGRAPHS))
    result = run(items, lookup=lookup)
    assert result["status"] == "verified" and result["checked_dimensions"] == 4
    assert [item["dimension"] for item in result["dimensions"]] == list(SCIENTIFIC_DIMENSIONS)
    assert all(item["paragraph_count"] == 2 and item["evidence_source_count"] == 1 for item in result["dimensions"])
    assert (items, lookup, PARAGRAPHS) == frozen
    assert result["request_sha256"] == hashlib.sha256(QUESTION.encode()).hexdigest()
    assert result["candidate_sha256"] == hashlib.sha256(json.dumps(PARAGRAPHS,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_disabled_host_scope_does_not_call_citations_or_require_model_checks():
    def forbidden(*args): raise AssertionError("Must not run")
    result = run({"model_requested": True}, enabled=False, citations=forbidden)
    assert result["status"] == "not_applicable" and not result["enabled"]
    assert result["dimensions"] == []
    assert scope({"model_requested": True}, enabled=False)["status"] == "not_applicable"
    assert run(enabled="true")["enabled"] is False


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "unknown_dimension", "extra_key",
    "missing_key", "wrong_status", "not_applicable", "indices_missing", "indices_extra",
    "indices_duplicate", "indices_bool", "indices_float", "indices_string", "indices_negative"])
def test_schema_or_partial_paragraph_coverage_cannot_be_green(mutation):
    rows = checks()
    if mutation == "missing": rows.pop()
    elif mutation == "extra": rows.append(copy.deepcopy(rows[0]))
    elif mutation == "duplicate": rows[1]["dimension"] = rows[0]["dimension"]
    elif mutation == "unknown_dimension": rows[0]["dimension"] = "invented"
    elif mutation == "extra_key": rows[0]["reasoning"] = "private model content"
    elif mutation == "missing_key": rows[0].pop("evidence")
    elif mutation == "wrong_status": rows[0]["status"] = "pass"
    elif mutation == "not_applicable": rows[0]["status"] = "not_applicable"
    elif mutation == "indices_missing": rows[0]["paragraph_indices"] = [0]
    elif mutation == "indices_extra": rows[0]["paragraph_indices"] = [0, 1, 2]
    elif mutation == "indices_duplicate": rows[0]["paragraph_indices"] = [0, 1, 1]
    elif mutation == "indices_bool": rows[0]["paragraph_indices"] = [False, True]
    elif mutation == "indices_float": rows[0]["paragraph_indices"] = [0., 1.]
    elif mutation == "indices_string": rows[0]["paragraph_indices"] = ["0", "1"]
    else: rows[0]["paragraph_indices"] = [-1, 0, 1]
    result = run(rows)
    assert result["status"] == "unavailable"
    assert result["reason"] == "answer_scientific_check_invalid"


@pytest.mark.parametrize("raw", [None, {}, "verified", [], [None]])
def test_missing_checks_are_unavailable(raw):
    result = answer_scientific_review_metadata(raw, request=QUESTION, paragraphs=PARAGRAPHS,
        citations=_citations, lookup={"tool_0001_result": source()})
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("evidence", [[], [{"source_id": "unknown", "quote": "x"}],
    [{"source_id": "tool_0001_result", "quote": "invented value"}],
    [{"source_id": "tool_0001_result", "quote": "x" * 161}],
    [{"source_id": "tool_0001_result", "quote": " "}],
    [{"source_id": "tool_0001_result", "quote": '"rows":42', "extra": True}],
    [{"source_id": "tool_0001_result", "quote": '"rows":42'}] * 3,
    "an observation"])
def test_verified_needs_short_exact_independent_citation(evidence):
    rows = checks(); rows[0]["evidence"] = evidence
    assert run(rows)["status"] == "unavailable"


@pytest.mark.parametrize("change", [dict(kind="tool_request"), dict(kind="catalog"), dict(kind="prior_review"),
    dict(state="failed"), dict(truncated=True), dict(write_only=True), dict(method_only=True),
    dict(code=True), dict(code_only=True), dict(inventory=True), dict(current_request=True),
    dict(report_content_only=True), dict(self_content_only=True), dict(historical=True),
    dict(old_result=True), dict(step_id="previous_step"), dict(step_id=None),
    dict(function="file_write"), dict(function="file_append"), dict(function="file_str_replace"),
    dict(function="file_find_by_name"), dict(function="file_list"), dict(function="list_files")])
def test_request_inventory_code_self_content_and_cross_step_cannot_prove_science(change):
    result = run(lookup={"tool_0001_result": source(**change)})
    assert result["status"] == "unavailable"
    assert result["dimensions"][0]["reason"] == "answer_scientific_source_not_independent"


@pytest.mark.parametrize("suffix", ["py", "R", "sh", "ipynb"])
def test_source_code_read_is_not_evidence_without_a_preexisting_code_flag(suffix):
    lookup = {"tool_0001_result": source(), "tool_0001_request": dict(
        source_id="tool_0001_request", kind="tool_request", function="file_read",
        text=json.dumps({"file": "/home/ubuntu/method." + suffix}))}
    assert run(lookup=lookup)["status"] == "unavailable"


def test_authored_file_readback_is_not_independent_even_without_uploaded_artifacts():
    lookup = {"tool_0001_result": source(), "tool_0001_request": dict(
        source_id="tool_0001_request", kind="tool_request", function="file_read",
        text=json.dumps({"file": "/home/ubuntu/output/self.json"})),
        "tool_0000_request": dict(source_id="tool_0000_request", kind="tool_request", function="file_write",
        text=json.dumps({"file": "/home/ubuntu/output/self.json", "content": '{"rows":42}'}))}
    frozen = copy.deepcopy(lookup)
    assert run(lookup=lookup)["status"] == "unavailable"
    assert lookup == frozen


def test_original_data_read_and_unrelated_write_remain_eligible():
    lookup = {"tool_0001_result": source(), "tool_0001_request": dict(
        source_id="tool_0001_request", kind="tool_request", function="file_read",
        text=json.dumps({"file": "/home/ubuntu/datasets/original.csv"})),
        "tool_0000_request": dict(source_id="tool_0000_request", kind="tool_request", function="file_write",
        text=json.dumps({"file": "/home/ubuntu/output/other.json", "content": "draft"}))}
    assert run(lookup=lookup)["status"] == "verified"


def test_unresolved_truncated_read_request_does_not_authenticate_its_result():
    lookup = {"tool_0001_result": source(), "tool_0001_request": dict(
        source_id="tool_0001_request", kind="tool_request", function="file_read", text='{"file":', truncated=True)}
    assert run(lookup=lookup)["status"] == "unavailable"


def test_decoded_json_exact_citation_compatibility_is_preserved():
    rows = checks()
    for row in rows: row["evidence"][0]["quote"] = "原始观测"
    assert run(rows, lookup={"tool_0001_result": source(text=json.dumps({"label": "原始观测"}))})["status"] == "verified"


@pytest.mark.parametrize("flag", ["method_complete", "request_complete", "draft_complete"])
@pytest.mark.parametrize("value", [False, None, "true"])
def test_incomplete_host_input_blocks_green_but_preserves_valid_rejected_dimension(flag, value):
    assert run(**{flag: value})["status"] == "unavailable"
    rows = checks(); rows[1].update(status="rejected", evidence=[])
    result = run(rows, **{flag: value})
    assert result["status"] == "rejected"
    assert result["dimensions"][1]["status"] == "rejected"
    assert all(row["status"] == "unavailable" for index, row in enumerate(result["dimensions"]) if index != 1)


def test_valid_unclear_is_unavailable_not_substantive_rejection():
    rows = checks(); rows[2].update(status="unclear", evidence=[])
    assert run(rows)["status"] == "unavailable"


def test_malformed_rejection_cannot_become_substantive_failure():
    rows = checks(); rows[1].update(status="rejected", evidence=[dict(source_id="unknown", quote="false")])
    assert run(rows)["status"] == "unavailable"


def test_callback_errors_are_private_and_cancellation_propagates():
    secret = "SECRET-provider-error /Users/example/private-data"
    def fail(*args): raise RuntimeError(secret)
    result = run(citations=fail)
    assert result["status"] == "unavailable" and secret not in json.dumps(result)
    def cancel(*args): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): run(citations=cancel)


def test_callback_cannot_substitute_another_source_or_ignore_unknown_identity():
    def altered(*args): return [source(text="substituted")]
    assert run(citations=altered)["status"] == "unavailable"
    assert run(lookup={"tool_0001_result": source(source_id="different")})["status"] == "unavailable"


@pytest.mark.parametrize("paragraphs", [[], "a paragraph", [None], [""], [" "], ["x"] * 65, ["x" * 24001], ["\ud800"]])
def test_invalid_empty_or_oversized_candidate_never_passes(paragraphs):
    assert run(paragraphs=paragraphs)["status"] == "unavailable"
    assert scope(paragraphs=paragraphs)["status"] == "unavailable"


@pytest.mark.parametrize("request_text", [None, "", " ", "\ud800"])
def test_missing_original_request_never_passes(request_text):
    assert run(request=request_text)["status"] == "unavailable"
    assert scope(request=request_text)["status"] == "unavailable"


def test_scope_complete_uses_all_candidate_indices_and_binds_original_question():
    result = scope(dict(status="complete", paragraph_indices=[1, 0]))
    assert result["status"] == "verified" and result["checked_paragraph_count"] == 2
    assert result["request_sha256"] != scope(request=QUESTION + " And one more input.")["request_sha256"]


@pytest.mark.parametrize("check", [{}, {"status": "complete", "paragraph_indices": []},
    {"status": "complete", "paragraph_indices": [0]}, {"status": "complete", "paragraph_indices": [0, 1, 1]},
    {"status": "complete", "paragraph_indices": [False, True]}, {"status": "complete", "paragraph_indices": [0, 2]},
    {"status": "complete", "paragraph_indices": [0, 1], "reason": "hidden"},
    {"status": "not_applicable", "paragraph_indices": []}, {"status": "incomplete", "paragraph_indices": [-1]},
    {"status": "unclear", "paragraph_indices": [0, 0]}, {"status": "unclear", "paragraph_indices": ["0"]}])
def test_scope_malformed_or_selective_completion_is_unavailable(check):
    assert scope(check)["status"] == "unavailable"


@pytest.mark.parametrize("indices", [[], [0], [1, 0]])
def test_valid_scope_incomplete_is_separate_from_unknown_or_scientific_error(indices):
    assert scope(dict(status="incomplete", paragraph_indices=indices))["status"] == "incomplete"
    assert scope(dict(status="unclear", paragraph_indices=indices))["status"] == "unavailable"


def test_scope_cannot_complete_a_truncated_original_request():
    assert scope(request_complete=False)["status"] == "unavailable"
    assert scope(dict(status="incomplete", paragraph_indices=[]), request_complete=False)["status"] == "unavailable"


@pytest.mark.parametrize("changed", [[PARAGRAPHS[0] + " ", PARAGRAPHS[1]], list(reversed(PARAGRAPHS)),
    ["\n\n".join(PARAGRAPHS)], [PARAGRAPHS[0]], [PARAGRAPHS[0], PARAGRAPHS[1] + " [download](report.md)"], []])
def test_any_correction_publication_or_boundary_change_invalidates_old_green(changed):
    for original in (run(), scope()):
        frozen = copy.deepcopy(original)
        result = revalidate_answer_candidate(original, paragraphs=changed)
        assert result["status"] == "unavailable" and result["reason"] == "answer_candidate_changed"
        assert result["candidate_version_status"] == "changed" and original == frozen
        assert revalidate_answer_candidate(result, paragraphs=PARAGRAPHS)["status"] == "unavailable"


def test_unchanged_candidate_preserves_checks_without_mutating_metadata():
    original = run()
    result = revalidate_answer_candidate(original, paragraphs=tuple(PARAGRAPHS))
    assert result == original and result is not original


def test_valid_rejection_survives_text_change_bound_to_old_version_not_claiming_new_review():
    rows = checks(); rows[0].update(status="rejected", evidence=[])
    original = run(rows)
    result = revalidate_answer_candidate(original, paragraphs=["Corrected description"])
    assert result["status"] == "rejected" and result["candidate_version_status"] == "changed"
    assert result["candidate_sha256"] == original["candidate_sha256"]
    assert result["current_candidate_sha256"] != original["candidate_sha256"]
    assert result["dimensions"][0]["status"] == "rejected"
    assert all(row["status"] == "unavailable" for row in result["dimensions"][1:])


def test_old_incomplete_scope_is_not_reused_for_a_new_complete_text():
    original = scope(dict(status="incomplete", paragraph_indices=[]))
    assert revalidate_answer_candidate(original, paragraphs=["New full answer"])["status"] == "unavailable"


def test_public_metadata_never_contains_original_text_evidence_source_id_or_private_path():
    secret = "unique-sensitive-input /Users/private/file secret-token"
    rows = checks()
    for row in rows: row["evidence"][0]["quote"] = secret
    result = run(rows, request=secret, paragraphs=[secret, secret],
                 lookup={"tool_0001_result": source(text=secret)})
    public = json.dumps(result)
    assert result["status"] == "verified"
    for sensitive in (secret, "/Users/private", "tool_0001_result", "step_current", "quote", "paragraph_indices"):
        assert sensitive not in public
    assert secret not in json.dumps(revalidate_answer_candidate(result, paragraphs=[secret + " changed"]))


def test_rules_require_whole_original_scope_without_inventing_experiments_or_outputs():
    assert all(dimension in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES for dimension in SCIENTIFIC_DIMENSIONS)
    for phrase in ("FINAL FROZEN-CANDIDATE", "original request is immutable", "ALL candidate", "No tools",
                   "honest acknowledgment", "not a fitted model", "No", "not_applicable escape"):
        assert phrase in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES
    assert "do not invent a" in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES.lower()
