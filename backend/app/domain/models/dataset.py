from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List
import uuid

from pydantic import BaseModel, Field


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
    temporal_coverage: str = ""
    spatial_coverage: str = ""
    data_type: str = ""
    tags: List[str] = Field(default_factory=list)
    preview_url: str = ""
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
