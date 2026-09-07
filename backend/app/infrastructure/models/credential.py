from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.domain.models.credential import CredentialRecord


class CredentialDocument(Document):
    """Fernet ciphertext and public binding metadata; never an API schema."""

    schema_version: int = 1
    reference: str
    user_id: str
    provider: str
    tool_name: str
    slot: str
    revision: int = 1
    revoked: bool = False
    encrypted_secret: str | None = None
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None

    @classmethod
    def from_domain(cls, record: CredentialRecord) -> "CredentialDocument":
        return cls.model_validate(record.model_dump(mode="python"))

    def to_domain(self) -> CredentialRecord:
        return CredentialRecord.model_validate(self.model_dump(exclude={"id"}))

    class Settings:
        name = "credentials"
        indexes = [
            IndexModel(
                [("reference", ASCENDING)],
                unique=True,
                name="credential_reference_unique",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("tool_name", ASCENDING),
                    ("slot", ASCENDING),
                ],
                unique=True,
                partialFilterExpression={"revoked": False},
                name="credential_active_binding_unique",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="credential_owner_created",
            ),
        ]


__all__ = ["CredentialDocument"]
