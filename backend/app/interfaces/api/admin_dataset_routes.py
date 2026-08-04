import json
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.application.errors.exceptions import BadRequestError, UnauthorizedError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.domain.models.dataset import DatasetLocation
from app.domain.models.user import User, UserRole
from app.interfaces.dependencies import get_current_user
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.dataset import (
    DataCenterDatasetCatalogResponse,
    DataCenterDatasetResponse,
    DatasetCreateRequest,
    DatasetLocationCreateRequest,
    DatasetUpdateRequest,
    dataset_response,
)


router = APIRouter(prefix="/admin/datasets", tags=["admin-datasets"])


def _require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise UnauthorizedError("Only administrators can manage data-center datasets")


@router.get("", response_model=APIResponse[DataCenterDatasetCatalogResponse])
async def list_datasets(
    query: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetCatalogResponse]:
    _require_admin(current_user)
    datasets, total = await DataCenterDatasetService().list_datasets(query, limit, offset, include_disabled=True)
    return APIResponse.success(DataCenterDatasetCatalogResponse(
        datasets=[dataset_response(item, include_locations=True, include_file_paths=True) for item in datasets],
        total=total,
    ))


@router.post("", response_model=APIResponse[DataCenterDatasetResponse])
async def create_dataset(
    request: DatasetCreateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    dataset = await DataCenterDatasetService().create_dataset(request.model_dump(), current_user.id)
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.get("/{dataset_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def get_dataset(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    dataset = await DataCenterDatasetService().get_dataset(dataset_id, include_disabled=True)
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.patch("/{dataset_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def update_dataset(
    dataset_id: str,
    request: DatasetUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    dataset = await DataCenterDatasetService().update_dataset(dataset_id, request.model_dump(exclude_unset=True))
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.delete("/{dataset_id}", response_model=APIResponse[dict])
async def delete_dataset(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[dict]:
    _require_admin(current_user)
    await DataCenterDatasetService().delete_dataset(dataset_id)
    return APIResponse.success({})


@router.post("/{dataset_id}/locations", response_model=APIResponse[DataCenterDatasetResponse])
async def add_dataset_location(
    dataset_id: str,
    request: DatasetLocationCreateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    location = DatasetLocation(
        **request.model_dump(),
        read_only=True,
        verified=True,
        verification_message=(
            "Managed volume location accepted"
            if request.storage_type.value == "managed_upload"
            else "Path accepted; Docker validates its existence when the dataset sandbox is created"
        ),
    )
    dataset = await DataCenterDatasetService().add_location(dataset_id, location)
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.delete("/{dataset_id}/locations/{location_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def remove_dataset_location(
    dataset_id: str,
    location_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    dataset = await DataCenterDatasetService().remove_location(dataset_id, location_id)
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.post("/{dataset_id}/files", response_model=APIResponse[DataCenterDatasetResponse])
async def upload_dataset_files(
    dataset_id: str,
    files: List[UploadFile] = File(...),
    relative_paths_json: str = Form(default="[]"),
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    try:
        relative_paths = json.loads(relative_paths_json)
    except json.JSONDecodeError as exc:
        raise BadRequestError("relative_paths_json must be a JSON array") from exc
    if relative_paths and (not isinstance(relative_paths, list) or len(relative_paths) != len(files)):
        raise BadRequestError("Relative path count must match uploaded file count")
    names = relative_paths or [item.filename or "" for item in files]
    dataset = await DataCenterDatasetService().upload_files(dataset_id, list(zip(names, files)))
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))


@router.post("/{dataset_id}/preview", response_model=APIResponse[DataCenterDatasetResponse])
async def upload_dataset_preview(
    dataset_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_admin(current_user)
    dataset = await DataCenterDatasetService().upload_preview(dataset_id, file)
    return APIResponse.success(dataset_response(dataset, include_locations=True, include_file_paths=True))
