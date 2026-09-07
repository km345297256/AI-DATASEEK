from functools import lru_cache

from app.core.config import get_settings
from app.domain.services.credential_service import CredentialService
from app.infrastructure.repositories.mongo_credential_repository import (
    MongoCredentialRepository,
)


@lru_cache()
def get_credential_service() -> CredentialService:
    """Build the vault only from its dedicated key; never fall back to model keys."""

    settings = get_settings()
    return CredentialService(
        MongoCredentialRepository(),
        encryption_key=getattr(settings, "credential_encryption_key", None),
    )


__all__ = ["get_credential_service"]
