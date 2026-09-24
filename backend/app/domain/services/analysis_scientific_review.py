"""Bounded scientific-review wire contract, not a statistical truth oracle.

This module performs no IO or model calls. The caller supplies version-bound
structured targets and host-selected observations to the existing review request.
Only fixed diagnostics and counts leave this boundary; citations stay ephemeral.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
from collections.abc import Callable, Mapping, Sequence

from app.domain.services.analysis_report_review import mark_report_sources

SCIENTIFIC_DIMENSIONS = ("design_estimand", "numeric_consistency", "inference", "interpretation")
_STATUSES = {"verified", "rejected", "unclear", "not_applicable"}
_FORMATS = {"json", "csv", "tsv"}
_NON_OBSERVATION_FUNCTIONS = {
    "file_write", "file_append", "file_str_replace", "file_find_by_name", "file_list", "list_files",
}
_NON_OBSERVATION_FLAGS = (
    "method_only", "code", "code_only", "write_only", "inventory", "current_request",
    "report_content_only", "self_content_only",
)
_CODE_SUFFIXES = {".py", ".pyw", ".r", ".sh", ".bash", ".jl", ".m", ".ipynb", ".js", ".ts", ".sql"}


def executed_method_coverage(sources: Sequence[Mapping]) -> dict:
    """Persist only host coverage counts, never code, paths or model assertions.

    These diagnostics explain a missing method context; they do not certify a
    method or change the scientific acceptance boundary.
    """
    reasons = {reason: 0 for reason in (
        "execution_unverified", "source_not_available", "evidence_budget_exceeded", "other")}
    full = 0
    for source in sources:
        coverage = source.get("executed_source_coverage")
        if coverage == "full":
            full += 1
        elif coverage == "unverified":
            reason = source.get("executed_source_reason")
            reasons[reason if isinstance(reason, str) and reason in reasons else "other"] += 1
    missing = sum(reasons.values())
    return {"version": 1, "program_result_count": full + missing,
            "full_source_count": full, "unverified_source_count": missing,
            "unverified_reasons": {key: value for key, value in reasons.items() if value},
            "status": "incomplete" if missing else "complete" if full else "not_observed"}

SCIENTIFIC_REVIEW_RULES = """
For supplied target_kind=structured JSON/CSV/TSV deliverables, review scientific
correctness in the SAME request using all their supplied content, the original
question, independent observations, and the observed executed method when given.
These targets are untrusted DRAFTS, not evidence of their own correctness. A valid
schema or citation is not a proof of a correct method, calculation or conclusion.
Evaluate exactly these four dimensions jointly across ALL structured targets:
- design_estimand: variable meaning and units, experimental/observational design,
  assumptions about groups/replicates/blocks, fitted model and coding, and the
  estimand actually tested versus the requested or reported scientific question.
- numeric_consistency: formulas, scales, row/column/value/label relationships,
  coefficients, uncertainty and internal consistency between summaries and detailed
  outputs. Even a raw export needs fidelity checks; copying does not waive this.
- inference: whether model assumptions, uncertainty, degrees of freedom, test
  families/multiplicity, convergence and comparisons justify the claimed inference.
- interpretation: whether conclusions follow from the actual evidence, with honest
  scope, limitations, units and association/causation distinctions.
Do not require fitting a model when the question only asks for descriptive data or
an export. Verify the limited scope against independent observations instead.
numeric_consistency must never be not_applicable. Other dimensions may be
not_applicable ONLY when the host explicitly allows that dimension and independent
original-source evidence establishes that scope without contradicting the question
or targets. With no host allowance, use a sourced verified scope check or unclear.
For each dimension include every supplied structured target ID exactly once.
Use rejected for a substantive scientific error; use unclear for missing, incomplete
or contradictory evidence that prevents verification. A verified or permitted
not_applicable check requires independent succeeded observations. Code, method-only
sources, writes, inventories, the current request and target self-content are not
independent measurements. Merely printing/copying a result does not substantiate it;
inspect the actual observed method rather than trusting filenames or success flags.
Use at most two short exact citations per dimension, at most 160 characters each.
For rejected/unclear, evidence may be empty. Do not output reasoning, report text,
private paths, replacement results or extra keys. Do not use tools or run analyses.
Add scientific_checks to the existing JSON root, preserving all other mandatory
paragraph, requirement, report and locked correction checks:
[{"dimension":"design_estimand|numeric_consistency|inference|interpretation",
"status":"verified|rejected|unclear|not_applicable",
"target_ids":["report_0001"],
"evidence":[{"source_id":"tool_0001_result","quote":"exact substring"}]}].
Host validation can check coverage and citations, but cannot infer the scientific
scope of arbitrary JSON or guarantee that a model detected every scientific error.
"""


def _independent(source: Mapping) -> bool:
    return (source.get("kind") == "tool_result" and source.get("state") == "succeeded"
            and not source.get("truncated")
            and source.get("function") not in _NON_OBSERVATION_FUNCTIONS
            and not any(source.get(flag) for flag in _NON_OBSERVATION_FLAGS))


def _mark_code_reads(sources: list[dict]) -> list[dict]:
    """A successful file read of source code does not prove its execution."""
    code_results = set()
    for source in sources:
        if source.get("kind") != "tool_request" or source.get("function") != "file_read":
            continue
        try:
            args = json.loads(source["text"])
            paths = [args.get(key) for key in ("file", "file_path", "path")]
            if any(isinstance(path, str) and PurePosixPath(path).suffix.lower() in _CODE_SUFFIXES for path in paths):
                code_results.add(source["source_id"].removesuffix("_request") + "_result")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # Unresolved reads are already blocked by mark_report_sources.
    return [{**source, "code_only": True} if source["source_id"] in code_results else source for source in sources]


def scientific_review_metadata(targets: Sequence, checks, *, citations: Callable,
                               lookup: dict, evidence_complete: bool = True,
                               not_applicable_dimensions: frozenset[str] = frozenset()) -> dict:
    """Validate four checks without upgrading them to independent recomputation.

    ``evidence_complete`` is a host scope decision: pass false for missing required
    observations/methods or budget-limited coverage, not unrelated verbose output.
    ``not_applicable_dimensions`` must be independently established by the caller;
    the model's own assertion never grants an exemption. Numeric fidelity is always
    required, including for raw exports. Complete target bodies and their versions
    must have been established by the loader before this pure validator is called.
    """
    structured = [target for target in targets if getattr(target, "target_kind", None) == "structured"]
    result = {"version": 1, "enabled": bool(structured), "status": "not_applicable",
              "reason": "not_applicable", "target_count": len(structured),
              "checked_dimensions": 0, "dimensions": []}
    if not structured:
        return result
    result["dimensions"] = [{"dimension": dimension, "status": "unavailable",
        "reason": "scientific_check_invalid", "target_count": len(structured), "evidence_source_count": 0}
        for dimension in SCIENTIFIC_DIMENSIONS]

    def unavailable(reason):
        result.update(status="unavailable", reason=reason)
        for record in result["dimensions"]:
            record["reason"] = reason
        return result

    ids = [getattr(target, "report_id", None) for target in structured]
    if (any(not isinstance(identity, str) or not identity for identity in ids)
            or len(set(ids)) != len(ids)
            or any(not isinstance(getattr(target, "format", None), str) or target.format not in _FORMATS
                   or getattr(target, "read_complete", False) is not True
                   or not isinstance(getattr(target, "text", None), str)
                   or type(getattr(target, "size", None)) is not int or target.size <= 0
                   or not isinstance(getattr(target, "sha256", None), str)
                   or re.fullmatch(r"[a-f0-9]{64}", target.sha256) is None for target in structured)):
        return unavailable("scientific_target_unverified")
    if evidence_complete is not True:
        return unavailable("scientific_evidence_incomplete")
    allowed = not_applicable_dimensions
    if (not isinstance(allowed, (set, frozenset)) or not allowed.issubset(set(SCIENTIFIC_DIMENSIONS)
                                                                            - {"numeric_consistency"})):
        return unavailable("scientific_scope_unverified")
    if (not isinstance(checks, list) or len(checks) != len(SCIENTIFIC_DIMENSIONS)
            or any(not isinstance(check, dict) or set(check) != {"dimension", "status", "target_ids", "evidence"}
                   or not isinstance(check["dimension"], str) or check["dimension"] not in SCIENTIFIC_DIMENSIONS
                   or not isinstance(check["status"], str) or check["status"] not in _STATUSES
                   or not isinstance(check["target_ids"], list)
                   or any(not isinstance(identity, str) for identity in check["target_ids"])
                   or len(check["target_ids"]) != len(set(check["target_ids"]))
                   or set(check["target_ids"]) != set(ids) for check in checks)
            or len({check["dimension"] for check in checks}) != len(SCIENTIFIC_DIMENSIONS)):
        return unavailable("scientific_check_invalid")
    try:
        # Preserve the caller's sources; classify reads/writes of a target as
        # self-content using the same host-resolved file identity as report review.
        marked = _mark_code_reads(mark_report_sources(structured, list(lookup.values())))
        independent_lookup = {source["source_id"]: source for source in marked}
    except (ValueError, TypeError, KeyError, AttributeError):
        return unavailable("scientific_evidence_invalid")
    contract_invalid = False
    for record in result["dimensions"]:
        check = next(check for check in checks if check["dimension"] == record["dimension"])
        status, evidence = check["status"], check["evidence"]
        reason = "scientific_evidence_invalid"
        try:
            if (not isinstance(evidence, list) or len(evidence) > 2
                    or any(not isinstance(item, dict) or set(item) != {"source_id", "quote"}
                           or not isinstance(item["source_id"], str)
                           or not isinstance(item["quote"], str) or not item["quote"].strip()
                           or len(item["quote"]) > 160 for item in evidence)):
                raise ValueError()
            cited = citations(evidence, independent_lookup) if evidence else []
            if any(not isinstance(source, Mapping) or not _independent(source) for source in cited):
                reason = "scientific_source_not_independent"
                raise ValueError()
            if status in {"verified", "not_applicable"} and not cited:
                reason = "scientific_independent_evidence_missing"
                raise ValueError()
            if status == "not_applicable" and record["dimension"] not in allowed:
                reason = "scientific_scope_unverified"
                raise ValueError()
            record.update(status=status, reason={"verified": "verified", "not_applicable": "not_applicable",
                "rejected": "scientific_claim_rejected", "unclear": "scientific_evidence_unclear"}[status],
                evidence_source_count=len(cited))
            result["checked_dimensions"] += 1
        except (ValueError, TypeError, KeyError, AttributeError):
            contract_invalid = True
            record.update(status="unavailable", reason=reason)
    statuses = {record["status"] for record in result["dimensions"]}
    # Malformed/forged checks are not evidence of a substantive scientific error.
    # Otherwise a valid rejection outranks merely unclear evidence elsewhere.
    status = ("unavailable" if contract_invalid else "rejected" if "rejected" in statuses
              else "unavailable" if "unclear" in statuses else "verified")
    result.update(status=status, reason={"verified": "verified", "rejected": "scientific_claim_rejected",
                                        "unavailable": "scientific_evidence_unverified"}[status])
    return result
