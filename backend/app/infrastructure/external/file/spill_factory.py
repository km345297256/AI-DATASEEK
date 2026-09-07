from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.domain.external.spill import SpillArtifactStore
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.external.file.spill import FileStorageSpillArtifactStore
from app.infrastructure.repositories.mongo_spill_artifact_repository import (
    MongoSpillArtifactRepository,
)


@lru_cache()
def get_spill_artifact_store() -> SpillArtifactStore:
    settings = get_settings()
    maximum_safe_read = max(1024, (settings.spill_max_inline_bytes - 4096) // 2)
    if settings.spill_read_chunk_bytes > maximum_safe_read:
        raise ValueError(
            "SPILL_READ_CHUNK_BYTES is too large for SPILL_MAX_INLINE_BYTES"
        )
    identity_key = (
        settings.spill_artifact_identity_key
        or settings.execution_snapshot_identity_key
        or settings.api_key
        or settings.jwt_secret_key
    )
    return FileStorageSpillArtifactStore(
        get_file_storage(),
        MongoSpillArtifactRepository(),
        identity_key=identity_key,
        retention_hours=settings.spill_artifact_retention_hours,
        read_chunk_bytes=settings.spill_read_chunk_bytes,
    )
