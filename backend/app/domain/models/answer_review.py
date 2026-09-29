"""Durable host-review envelope embedded in otherwise generic Step.outputs.

This schema inventories the persisted marker, not evidence authority. The
history readers still enforce outcome, dataset scope, candidate hashes and
review-specific semantics. Diagnostics remain extensible and are not promoted
to trusted evidence merely because this envelope can be decoded.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator


class AnswerReviewMarker(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    version: Literal[1]
    status: Literal["verified", "corrected", "unavailable"]
    # Older host-reviewed records may predate explicit scope bindings. Readers
    # retain their existing compatibility rules; serialization must not invent
    # bindings or optional diagnostics that were absent from a stored record.
    dataset_ids: list[StrictStr] | None = None
    input_file_ids: list[StrictStr] | None = None
    discarded_attachment_claim_count: StrictInt | None = None
    partial_delivery_review: StrictBool | None = None
    preserved_evidence_review: StrictBool | None = None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("invalid_review_marker_version")
        return value
