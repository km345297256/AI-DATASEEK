"""Bounded private delivery diagnostics, separate from chat and plan documents.

Each check is an attempt header plus independently bounded receipt documents.
A header is complete only after every receipt write has been acknowledged.
Partial/ambiguous writes remain incomplete; this store never retries a write or
turns missing audit evidence into permission for another autonomous operation.
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
import re
from uuid import uuid4

from app.domain.models.analysis_outcome import (
    AnalysisOutcome, ARTIFACT_ISSUE_REASONS, DELIVERABLE_LABELS, DeliverableRequirement,
)
from app.domain.services.analysis_completion import KINDS, REASONS, safe_diagnostics


RECEIPT_BATCH_SIZE = 32
REPAIR_REASONS = frozenset({
    "no_repair_needed", "local_artifact_repair_allowed", "validation_unavailable",
    "execution_not_confirmed", "invalid_validation_evidence", "protected_artifact_changed",
    "validation_not_locally_repairable", "artifact_repair_no_progress",
    "delivery_failed",
    "semantic_artifact_unresolved",
})


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}", value):
        raise ValueError("invalid_audit_identity")
    return value


def _step_identity(value: object) -> dict[str, str]:
    """Keep useful ordinary IDs without rejecting valid model step identities.

    Step.id allows Unicode and does not share the owner/session ID grammar.
    Unsafe or long IDs get a stable bounded digest, never a stored raw path.
    The encoding tag disambiguates a literal ID that happens to equal a digest.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_audit_step_identity")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}", value):
        return {"step_id": value, "step_id_encoding": "plain"}
    digest = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return {"step_id": "sha256:" + digest, "step_id_encoding": "sha256"}


def _output_path(value: object) -> str | None:
    if (not isinstance(value, str) or len(value) > 4096 or not value.startswith("/home/ubuntu/output/")
            or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts):
        return None
    return value


def _receipt(value: object) -> dict:
    """Explicit projection only: never copy metadata, parser text, or contents."""
    item = value if isinstance(value, dict) else {}
    path = _output_path(item.get("path"))
    kind = item.get("kind") if item.get("valid") is True else item.get("expected_kind") or item.get("kind")
    kind = kind if isinstance(kind, str) and kind in DELIVERABLE_LABELS else "any"
    digest = item.get("sha256")
    digest = digest if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) else None
    size = item.get("size")
    size = size if type(size) is int and 0 <= size <= 2**63 - 1 else None
    reason = item.get("reason")
    valid = item.get("valid") is True
    suffix = PurePosixPath(path).suffix.lower().lstrip(".") if path else ""
    format_matches = kind in KINDS and (suffix in KINDS[kind] or (kind == "table" and suffix == "json"))
    if valid and path is not None and digest is not None and size and format_matches:
        reason = "validated"
    elif item.get("valid") is False and isinstance(reason, str) and reason in ARTIFACT_ISSUE_REASONS:
        valid = False
    else:
        valid, reason = False, "validation_receipt_invalid"
    return {"path": path, "valid": valid, "kind": kind, "reason": reason,
            "sha256": digest, "size": size, "diagnostics": safe_diagnostics(item.get("diagnostics"))}


def _requirements(values) -> list[dict]:
    # Validate before the first write. These are bounded typed contracts, not
    # model-provided descriptions, dataset contents, or a copy of Step.inputs.
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        raise ValueError("invalid_audit_requirements")
    return [DeliverableRequirement.model_validate(value).model_dump() for value in values]


def _outcome(value) -> dict:
    if isinstance(value, AnalysisOutcome):
        value = value.model_dump()
    outcome = AnalysisOutcome.model_validate(value)
    return {
        "status": outcome.status,
        "reason_code": outcome.reason_code if outcome.reason_code in REASONS or outcome.reason_code == "completed"
                       else "execution_failed",
        "missing": _requirements(outcome.missing),
        "issues": outcome.model_dump()["issues"],
    }


class AnalysisDeliveryAuditStore:
    def __init__(self, collection=None):
        self._collection_override = collection

    @property
    def collection(self):
        if self._collection_override is not None:
            return self._collection_override
        from app.infrastructure.models.documents import SessionDocument
        return SessionDocument.get_pymongo_collection().database["analysis_delivery_checks"]

    async def record(self, *, user_id, session_id, input_seq, step_id,
                     records, requirements, outcome, repair_reason) -> None:
        """Persist one check. The caller owns its cancellation/3-second timeout.

        No receipt list is embedded in a Mongo document, and only one bounded
        batch is retained locally. Invalid raw fields are discarded, not logged.
        Every failure propagates so callers can withhold automatic repair while
        preserving their independently established delivery assessment.
        """
        if type(input_seq) is not int or not 0 < input_seq <= 2**63 - 1:
            raise ValueError("invalid_audit_input_seq")
        attempt_id = uuid4().hex
        identity = {"owner_id": _identifier(user_id), "session_id": _identifier(session_id),
                    "input_seq": input_seq, **_step_identity(step_id), "attempt_id": attempt_id}
        header = {
            "_id": attempt_id, **identity, "document_type": "attempt", "version": 1,
            "created_at": datetime.now(UTC), "state": "writing",
            "requirements": _requirements(requirements), "outcome": _outcome(outcome),
            "repair_reason": repair_reason if isinstance(repair_reason, str) and repair_reason in REPAIR_REASONS
                             else "invalid_validation_evidence",
        }
        # Resolve once so one record() never crosses injected collection scopes.
        collection = self.collection
        result = await collection.insert_one(header)
        if result.acknowledged is not True:
            raise RuntimeError("audit_write_unconfirmed")
        count, batch = 0, []
        for raw in records:
            batch.append({"_id": f"{attempt_id}:{count}", **identity,
                          "document_type": "receipt", "version": 1, "ordinal": count,
                          "receipt": _receipt(raw)})
            count += 1
            if len(batch) == RECEIPT_BATCH_SIZE:
                result = await collection.insert_many(batch, ordered=True)
                if result.acknowledged is not True:
                    raise RuntimeError("audit_write_unconfirmed")
                batch = []
        if batch:
            result = await collection.insert_many(batch, ordered=True)
            if result.acknowledged is not True:
                raise RuntimeError("audit_write_unconfirmed")
        result = await collection.update_one(
            {"_id": attempt_id, **identity, "document_type": "attempt", "state": "writing"},
            {"$set": {"state": "complete", "receipt_count": count, "completed_at": datetime.now(UTC)}},
        )
        if result.acknowledged is not True or result.matched_count != 1:
            raise RuntimeError("audit_completion_unconfirmed")
