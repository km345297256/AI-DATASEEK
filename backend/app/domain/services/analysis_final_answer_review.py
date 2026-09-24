"""Independently review every immutable analysis publication candidate.

This boundary can only ask for judgments about frozen text and frozen evidence.
It cannot rewrite the candidate, request tools, or replay analytical execution.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.messages import HumanMessage, SystemMessage

from app.domain.services.analysis_text_scientific_review import (
    answer_scientific_review_metadata,
    answer_scientific_shape_error,
    answer_scope_review_metadata,
    answer_scope_shape_error,
)


async def review_final_candidate(*, ask: Callable[[list], Awaitable[Any]], request: str,
                                 paragraphs: Sequence[str], sources: list[dict],
                                 current_step_id: str | None, citations: Callable,
                                 historical_sources: dict[str, str],
                                 parse_response: Callable, failure_code: Callable,
                                 method_complete: bool, request_complete: bool,
                                 draft_complete: bool, timeout_seconds: float,
                                 schema_actions: dict[str, str] | None = None) -> dict:
    """One final-version check with at most one same-evidence schema recovery.

    A substantive rejection or insufficient evidence is not a retry trigger.
    Protocol or transport failures are unavailable, never scientific rejection.
    Only fixed diagnostics and version digests are returned to public metadata.
    """
    from app.domain.services.analysis_text_scientific_review import FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES

    candidate = tuple(paragraphs)
    historical_sources = dict(historical_sources)
    actions = {**(schema_actions or {}),
        "final_review_schema_root": "Return exactly answer_scientific_checks and answer_scope_check; no other root fields.",
        "review_answer_science_indices": "Copy the supplied paragraph_indices exactly for every science dimension. These host indices refer to candidate_paragraphs, not the draft or a new response. No booleans, strings, duplicates or omissions.",
        "review_answer_scope_indices": "Use only unique integer indices from the supplied paragraph_indices. complete requires the full list; incomplete/unclear may retain an honest subset or []. Do not change a judgment to satisfy coverage.",
    }
    # One serialization detaches the payload from mutable caller state; the
    # identical HumanMessage is reused during schema-only recovery.
    frozen = HumanMessage(content=json.dumps({
        "request": request, "frozen_paragraphs": list(candidate), "sources": sources,
        "candidate_paragraphs": [{"index": index, "text": text} for index, text in enumerate(candidate)],
        "paragraph_indices": list(range(len(candidate))),
        "historical_source_ids": sorted(historical_sources),
        "current_step_id": current_step_id,
        "method_complete": method_complete, "request_complete": request_complete,
        "draft_complete": draft_complete,
    }, ensure_ascii=False, separators=(",", ":")))
    lookup = {source["source_id"]: source for source in json.loads(frozen.content)["sources"]}

    def metadata(science=None, scope=None):
        return {
            "answer_scientific_review": answer_scientific_review_metadata(
                science, request=request, paragraphs=candidate, citations=citations, lookup=lookup,
                historical_sources=historical_sources,
                current_step_id=current_step_id, method_complete=method_complete,
                request_complete=request_complete, draft_complete=draft_complete),
            "answer_scope_review": answer_scope_review_metadata(
                scope, request=request, paragraphs=candidate, request_complete=request_complete),
        }

    result = metadata()
    audit = {"attempted": True, "status": "unavailable", "protocol_attempts": 0}
    schema_error = None
    for attempt in range(2):
        audit["protocol_attempts"] = attempt + 1
        system = FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES + (
            "\nThe host has already frozen and numbered candidate_paragraphs. "
            "Copy the supplied paragraph_indices exactly for each check; do not generate, merge, "
            "split or renumber paragraphs. Review every supplied paragraph against the original request.")
        if schema_error:
            system += ("\nYour prior response was not accepted. Fixed structural error: " + schema_error
                       + ". Required structural correction: " + actions.get(schema_error, "Use the specified JSON schema.")
                       + ". Return ONLY answer_scientific_checks and answer_scope_check. "
                       "The immutable paragraphs are indexed 0 through " + str(len(candidate) - 1)
                       + "; every science dimension must list all indices exactly once. "
                       "Do not change any judgment to satisfy the schema; unclear is allowed. "
                       "Do not return replacement paragraphs, extra fields, reasoning or tools.")
        try:
            response = await asyncio.wait_for(ask([SystemMessage(content=system), frozen]),
                                              timeout=timeout_seconds)
            try:
                value = parse_response(response)
                error = ("final_review_schema_root" if not isinstance(value, dict) or set(value) != {
                    "answer_scientific_checks", "answer_scope_check"} else
                    answer_scientific_shape_error(value["answer_scientific_checks"], paragraph_count=len(candidate))
                    or answer_scope_shape_error(value["answer_scope_check"], paragraph_count=len(candidate)))
            except (ValueError, TypeError, KeyError) as exc:
                if type(exc) is ValueError and str(exc) == "review_requested_tools":
                    raise
                error = "final_review_schema_root"
            if error:
                schema_error = error
                audit["error"] = error
                if not attempt:
                    continue
                break
            result = metadata(value["answer_scientific_checks"], value["answer_scope_check"])
            statuses = [result[key]["status"] for key in ("answer_scientific_review", "answer_scope_review")]
            audit["status"] = ("verified" if statuses == ["verified", "verified"] else
                               "rejected" if "rejected" in statuses else "unavailable")
            if schema_error:
                audit["schema_recovered"] = True
            break
        except Exception as exc:
            # Cancellation is a BaseException and intentionally propagates.
            audit["error"] = failure_code(exc)
            break
    if audit["status"] == "unavailable" and all(
            result[key]["reason"] in {"answer_scientific_check_invalid", "answer_scope_check_invalid"}
            for key in ("answer_scientific_review", "answer_scope_review")):
        for key in ("answer_scientific_review", "answer_scope_review"):
            result[key]["reason"] = "final_answer_review_unavailable"
    result["final_candidate_review"] = audit
    return result
