from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models.dataset import DatasetStorageType
from app.domain.models.session import SessionStatus


class DatasetFileResponse(BaseModel):
    name: str
    path: str
    size: int
    role: str
    content_type: str | None = None


class DatasetLocationResponse(BaseModel):
    location_id: str
    node_id: str
    storage_type: DatasetStorageType
    source_path: str
    mount_name: str = ""
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
    payload["files"] = [
        {
            **item.model_dump(),
            "name": PurePosixPath(item.path.replace("\\", "/")).name,
            "path": item.path if include_file_paths else PurePosixPath(item.path.replace("\\", "/")).name,
        }
        for item in value.files
    ]
    if not include_locations:
        payload["locations"] = []
    return DataCenterDatasetResponse.model_validate(payload)
