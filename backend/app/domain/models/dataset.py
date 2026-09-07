from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatasetStorageType(str, Enum):
    MANAGED_UPLOAD = "managed_upload"
    HOST_PATH = "host_path"


class DatasetFile(BaseModel):
    path: str
    size: int = 0
    role: str = "data"
    content_type: str | None = None

    @property
    def name(self) -> str:
        return self.path


class DatasetLocation(BaseModel):
    location_id: str = Field(default_factory=lambda: f"dsl_{uuid.uuid4().hex[:16]}")
    node_id: str
    storage_type: DatasetStorageType
    source_path: str
    mount_name: str = ""
    read_only: bool = True
    verified: bool = False
    verification_message: str = ""
    version: str = "1"


class DataCenterDataset(BaseModel):
    dataset_id: str = Field(default_factory=lambda: f"ds_{uuid.uuid4().hex[:16]}")
    external_id: str = ""
    data_center_id: str
    data_center_name: str
    name: str
    name_key: str = ""
    description: str = ""
    # Stable catalog classification used by the management surface. Existing
    # Mongo documents predate this field and therefore retain the empty default.
    domain: str = ""
    temporal_coverage: str = ""
    spatial_coverage: str = ""
    data_type: str = ""
    tags: List[str] = Field(default_factory=list)
    preview_url: str = ""
    nc_view_url: str | None = None
    files: List[DatasetFile] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    locations: List[DatasetLocation] = Field(default_factory=list)
    enabled: bool = True
    is_submission: bool = False
    created_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DatasetMount(BaseModel):
    dataset_id: str
    source_id: str = ""
    display_name: str = ""
    node_id: str
    storage_type: DatasetStorageType
    source: str
    target: str
    read_only: bool = True
    version: str = "1"


class MountedDataset(DataCenterDataset):
    sandbox_path: str


class CuratedDatasetFile(BaseModel):
    """One repository-owned file declaration in a bundled dataset seed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    role: str = Field(default="data", min_length=1, max_length=100)
    content_type: str | None = Field(default=None, max_length=200)
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CuratedDatasetSeed(BaseModel):
    """Strict metadata contract for repository-bundled managed datasets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$",
    )
    external_id: str = Field(default="", max_length=200)
    data_center_id: str = Field(min_length=1, max_length=200)
    data_center_name: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4000)
    domain: str = Field(min_length=1, max_length=100)
    temporal_coverage: str = Field(default="", max_length=1000)
    spatial_coverage: str = Field(default="", max_length=1000)
    data_type: str = Field(default="", max_length=300)
    tags: List[str] = Field(default_factory=list, max_length=100)
    preview_url: str = Field(default="", max_length=2000)
    nc_view_url: str | None = Field(default=None, max_length=2000)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    files: List[CuratedDatasetFile] = Field(min_length=1, max_length=20_000)

    @field_validator(
        "external_id",
        "description",
        "temporal_coverage",
        "spatial_coverage",
        "data_type",
        "preview_url",
        mode="before",
    )
    @classmethod
    def normalize_nullable_catalog_text(cls, value: object) -> object:
        # Public manifests use JSON null for unavailable display metadata.
        # Persist one stable string shape so legacy response models remain
        # compatible without relaxing unknown-field validation.
        return "" if value is None else value
