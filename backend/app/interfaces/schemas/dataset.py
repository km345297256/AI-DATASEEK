import re
from datetime import datetime
from os import PathLike, fspath
from pathlib import PurePosixPath
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models.dataset import DatasetStorageType
from app.domain.models.session import SessionStatus


_OMIT_METADATA_VALUE = object()
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`<>]+"
)
_UNC_ABSOLUTE_PATH = re.compile(
    r"(?<![:A-Za-z0-9_])(?:\\\\|//)[^\s'\"`<>]+"
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?!/)[^\s'\"`<>]+"
)
_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9_])file:(?://)?[\\/]")

# These keys describe server-side dataset registration or mount configuration.
# Omitting them even when their current value looks harmless protects future
# records whose value may be a path component, allowlist, socket, or bind root.
_SENSITIVE_METADATA_KEYS = {
    "absolute_path",
    "allowed_root",
    "allowed_roots",
    "base_directory",
    "bind_source",
    "canonical_path",
    "canonical_source_directory",
    "canonical_source_path",
    "cwd",
    "data_directory",
    "dataset_allowed_roots",
    "dataset_directory",
    "dataset_docker_host_root",
    "dataset_host_path_allowlist",
    "dataset_local_path_allowlist",
    "docker_host",
    "docker_socket",
    "home_directory",
    "host_path",
    "host_paths",
    "host_root",
    "host_roots",
    "mount_name",
    "mount_source",
    "path_allowlist",
    "path_allowlists",
    "pwd",
    "source_directories",
    "source_directory",
    "source_path",
    "source_paths",
    "storage_directories",
    "storage_directory",
    "working_directory",
}
_INFRASTRUCTURE_KEY_PARTS = {
    "absolute",
    "bind",
    "docker",
    "host",
    "mount",
    "server",
    "source",
    "storage",
}
_PATH_KEY_PARTS = {
    "directory",
    "directories",
    "path",
    "paths",
    "root",
    "roots",
    "socket",
}


def _normalized_metadata_key(value: str) -> str:
    with_word_boundaries = _CAMEL_CASE_BOUNDARY.sub("_", value)
    return _NON_ALPHANUMERIC.sub("_", with_word_boundaries.casefold()).strip("_")


def _metadata_key_is_sensitive(value: str) -> bool:
    if _contains_absolute_host_path(value):
        return True
    normalized = _normalized_metadata_key(value)
    if normalized in _SENSITIVE_METADATA_KEYS:
        return True
    parts = set(normalized.split("_"))
    return bool(parts & _INFRASTRUCTURE_KEY_PARTS) and bool(parts & _PATH_KEY_PARTS)


def _contains_absolute_host_path(value: str) -> bool:
    candidate = value.strip()
    if candidate == "/":
        return True
    return any(
        pattern.search(candidate)
        for pattern in (
            _FILE_URI,
            _WINDOWS_ABSOLUTE_PATH,
            _UNC_ABSOLUTE_PATH,
            _POSIX_ABSOLUTE_PATH,
        )
    )


def _sanitize_public_metadata(value: Any) -> Any:
    """Recursively return only metadata safe for browser-facing responses.

    Dataset metadata is intentionally flexible and older records may contain
    server registration details.  Treat mapping keys and scalar values as
    separate leak channels: infrastructure keys are omitted regardless of
    value, while otherwise useful analysis metadata is retained unless its
    value contains an obvious absolute host path.
    """

    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            if _metadata_key_is_sensitive(key):
                continue
            public_item = _sanitize_public_metadata(item)
            if public_item is not _OMIT_METADATA_VALUE:
                sanitized[key] = public_item
        if value and not sanitized:
            return _OMIT_METADATA_VALUE
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        sanitized_items = []
        for item in value:
            public_item = _sanitize_public_metadata(item)
            if public_item is not _OMIT_METADATA_VALUE:
                sanitized_items.append(public_item)
        if value and not sanitized_items:
            return _OMIT_METADATA_VALUE
        return sanitized_items
    if isinstance(value, PathLike):
        rendered_path = fspath(value)
        if isinstance(rendered_path, bytes):
            try:
                rendered_path = rendered_path.decode()
            except UnicodeDecodeError:
                return _OMIT_METADATA_VALUE
        if _contains_absolute_host_path(rendered_path):
            return _OMIT_METADATA_VALUE
        return rendered_path
    if isinstance(value, str) and _contains_absolute_host_path(value):
        return _OMIT_METADATA_VALUE
    return value


class DatasetFileResponse(BaseModel):
    name: str
    path: str
    size: int
    role: str
    content_type: str | None = None


class DatasetLocationResponse(BaseModel):
    """Browser-safe storage-location metadata.

    The server-side ``DatasetLocation`` also contains ``source_path`` and
    ``mount_name``.  Both values can reveal parts of the Docker host
    filesystem, so they intentionally are not part of any HTTP response
    schema.  Location mutation requests use a separate input model and can
    still accept a path without echoing it back to the browser.
    """

    location_id: str
    node_id: str
    storage_type: DatasetStorageType
    read_only: bool
    verified: bool
    verification_message: str
    version: str


class DataCenterDatasetResponse(BaseModel):
    dataset_id: str
    external_id: str = ""
    data_center_id: str
    data_center_name: str
    name: str
    description: str
    temporal_coverage: str
    spatial_coverage: str
    data_type: str
    tags: List[str] = Field(default_factory=list)
    preview_url: str = ""
    files: List[DatasetFileResponse] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    locations: List[DatasetLocationResponse] = Field(default_factory=list)
    enabled: bool = True
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class DataCenterDatasetCatalogResponse(BaseModel):
    datasets: List[DataCenterDatasetResponse] = Field(default_factory=list)
    total: int = 0


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data_center_id: str = Field(min_length=1, max_length=100)
    data_center_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    temporal_coverage: str = ""
    spatial_coverage: str = ""
    data_type: str = ""
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class DatasetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    data_center_id: str | None = None
    data_center_name: str | None = None
    description: str | None = None
    temporal_coverage: str | None = None
    spatial_coverage: str | None = None
    data_type: str | None = None
    tags: List[str] | None = None
    metadata: Dict[str, Any] | None = None
    enabled: bool | None = None


class DatasetLocationCreateRequest(BaseModel):
    node_id: str
    storage_type: DatasetStorageType = DatasetStorageType.HOST_PATH
    source_path: str
    version: str = "1"


class DatasetSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    keywords: List[str] = Field(min_length=1, max_length=100)
    storage_directory: str = Field(min_length=1, max_length=4096)

    @field_validator("external_id", "name", "summary")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("field must not be blank")
        return normalized

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            item = value.strip()
            if not item or item in normalized:
                continue
            if len(item) > 200:
                raise ValueError("keyword must contain at most 200 characters")
            normalized.append(item)
        if not normalized:
            raise ValueError("at least one keyword is required")
        return normalized

    @field_validator("storage_directory")
    @classmethod
    def normalize_storage_directory(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("server storage directory must not be blank")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("server storage directory contains control characters")
        return normalized


class DatasetSuggestedQuestionsResponse(BaseModel):
    questions: List[str] = Field(min_length=4, max_length=4)


class DatasetSessionHistoryItem(BaseModel):
    session_id: str
    title: str | None = None
    latest_message: str | None = None
    latest_message_at: int | None = None
    status: SessionStatus


class DatasetSessionHistoryResponse(BaseModel):
    sessions: List[DatasetSessionHistoryItem] = Field(default_factory=list)


def dataset_response(
    value,
    *,
    include_locations: bool = False,
    include_file_paths: bool = False,
) -> DataCenterDatasetResponse:
    payload = value.model_dump()
    public_metadata = _sanitize_public_metadata(value.metadata)
    payload["metadata"] = (
        {} if public_metadata is _OMIT_METADATA_VALUE else public_metadata
    )
    host_location_roots = set()
    for location in value.locations:
        if location.storage_type != DatasetStorageType.HOST_PATH:
            continue
        # Older records may not have a persisted mount_name.  Mirror the
        # server-side fallback solely to recognize and remove its synthetic
        # prefix; the derived value is never put into the response.
        mount_name = location.mount_name or (
            PurePosixPath(location.source_path.rstrip("/").replace("\\", "/")).name
            or "source"
        )
        host_location_roots.add(
            PurePosixPath("sources") / location.location_id / mount_name
        )
    files = []
    for item in value.files:
        public_path = PurePosixPath(item.path.replace("\\", "/"))
        if (
            public_path.is_absolute()
            or ".." in public_path.parts
            or str(public_path) in {"", "."}
            or (
                public_path.parts
                and len(public_path.parts[0]) == 2
                and public_path.parts[0][1] == ":"
            )
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in str(public_path)
            )
        ):
            # Fail closed for legacy or malformed records.  File paths in an
            # HTTP response must always be dataset-relative.
            continue
        for host_root in host_location_roots:
            if public_path == host_root:
                # A manually registered host directory is represented
                # internally by a synthetic root entry, not by a real file.
                # Omitting it also avoids exposing the host directory basename.
                public_path = None
                break
            if host_root in public_path.parents:
                public_path = public_path.relative_to(host_root)
                break
        if public_path is None:
            continue
        file_payload = item.model_dump()
        file_payload.update(
            name=public_path.name,
            path=str(public_path) if include_file_paths else public_path.name,
        )
        files.append(file_payload)
    payload["files"] = files
    if include_locations:
        # Build an explicit allowlist instead of relying only on Pydantic's
        # extra-field handling.  This keeps future schema configuration changes
        # from accidentally serializing a host path.
        payload["locations"] = [
            {
                "location_id": location.location_id,
                "node_id": location.node_id,
                "storage_type": location.storage_type,
                "read_only": location.read_only,
                "verified": location.verified,
                "verification_message": location.verification_message,
                "version": location.version,
            }
            for location in value.locations
        ]
    else:
        payload["locations"] = []
    return DataCenterDatasetResponse.model_validate(payload)
