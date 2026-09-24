"""Restore complete review targets only inside a measured borrowing envelope.

This is a request-local capacity calculation, not evidence or scientific review.
It performs no IO, model invocation, truncation, or changes to legacy acceptance.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
from typing import Any, Literal, TypedDict

from app.domain.services.analysis_report_review import ReportTarget, fit_report_targets


class ReviewCapacityDiagnostics(TypedDict):
    version: int
    baseline_payload_bytes: int
    final_payload_bytes: int
    borrow_envelope_bytes: int
    restored_target_count: int
    status: Literal["within", "legacy_over"]


def restore_whole_targets_with_spare_capacity(
    *,
    payload: Mapping[str, Any],
    targets: Sequence[ReportTarget],
    text_budget: int,
    borrow_envelope_bytes: int,
    serialize: Callable[[Any], str],
) -> tuple[tuple[ReportTarget, ...], ReviewCapacityDiagnostics]:
    """Keep legacy fitting, then test whole-body additions using full payload bytes.

    The caller supplies the exact final base payload and serializer; the base
    already includes all frozen sources and has no ``report_targets`` field.
    This envelope limits new borrowing, not legacy requests that exceeded it.
    Provider context/token admission remains the caller's separate boundary.
    """
    if type(text_budget) is not int or type(borrow_envelope_bytes) is not int or borrow_envelope_bytes < 0:
        raise ValueError("invalid_review_capacity_budget")
    if "report_targets" in payload:
        raise ValueError("review_capacity_targets_already_present")

    original = tuple(targets)
    fitted = list(fit_report_targets(original, text_budget=text_budget))
    frozen_payload = deepcopy(dict(payload))

    def payload_bytes(selected: Sequence[ReportTarget]) -> int:
        # Give each serialization a fresh value; even a custom serializer must
        # not mutate caller sources or alter the next candidate's base payload.
        value = deepcopy(frozen_payload)
        value["report_targets"] = [target.payload() for target in selected]
        encoded = serialize(value)
        if not isinstance(encoded, str):
            raise ValueError("invalid_review_capacity_serialization")
        return len(encoded.encode("utf-8"))

    baseline_bytes = final_bytes = payload_bytes(fitted)
    restored = 0
    if baseline_bytes < borrow_envelope_bytes:
        # Stable sorting gives reports priority and preserves order within each
        # kind. Returned target order and identities always stay unchanged.
        order = sorted(range(len(original)), key=lambda index: original[index].target_kind != "report")
        for index in order:
            target = original[index]
            if (fitted[index].reason != "report_context_limit" or fitted[index].text is not None
                    or target.reason != "ready" or target.read_complete is not True
                    or not isinstance(target.text, str) or type(target.size) is not int or target.size <= 0):
                continue
            raw = target.text.encode("utf-8")
            if len(raw) != target.size or hashlib.sha256(raw).hexdigest() != target.sha256:
                continue
            candidate = fitted.copy()
            candidate[index] = target
            candidate_bytes = payload_bytes(candidate)
            if candidate_bytes <= borrow_envelope_bytes:
                fitted = candidate
                final_bytes = candidate_bytes
                restored += 1

    return tuple(fitted), {
        "version": 1,
        "baseline_payload_bytes": baseline_bytes,
        "final_payload_bytes": final_bytes,
        "borrow_envelope_bytes": borrow_envelope_bytes,
        "restored_target_count": restored,
        "status": "legacy_over" if baseline_bytes > borrow_envelope_bytes else "within",
    }
