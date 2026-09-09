from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from tempfile import SpooledTemporaryFile
from pathlib import PurePosixPath
import zipfile
import asyncio

from app.application.errors.exceptions import ForbiddenError, UpstreamServiceError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.dataset_suggested_question_service import DatasetSuggestedQuestionService
from app.application.services.data_product_service import DataProductService
from app.application.services.dataset_file_preview import DatasetFilePreviewRequest
from app.application.services.file_preview import PreviewVersionChanged
from app.core.config import get_settings
from app.domain.models.user import User, UserRole
from app.infrastructure.external.sso_client import resolve_sso_uid
from app.application.services.agent_service import AgentService
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.infrastructure.external.file.factory import get_file_storage
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.dataset import (
    DataCenterDatasetCatalogResponse,
    DataCenterDatasetResponse,
    DatasetMetadataUpdateRequest,
    DatasetRegistrationRequest,
    DatasetSubmissionRequest,
    DatasetSuggestedQuestionsResponse,
    DatasetSessionHistoryItem,
    DatasetSessionHistoryResponse,
    DataProductResponse,
    DataProductUpdateRequest,
    DatasetFilePreviewResponse,
    dataset_response,
)
from app.interfaces.schemas.file import FileInfoResponse


router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/{dataset_id}/files/preview", response_model=APIResponse[DatasetFilePreviewResponse])
async def prepare_dataset_file_preview(
    dataset_id: str,
    request: DatasetFilePreviewRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DatasetFilePreviewResponse]:
    from app.application.services.visualization_catalog import VisualizationNotFoundError, VisualizationDisabledError
    from app.domain.external.plugin_runtime import PluginRuntimeError
    work = asyncio.create_task(get_file_storage().previews.prepare(dataset_id, current_user.id, request))
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=0.25)
            if not work.done() and await http_request.is_disconnected():
                raise asyncio.CancelledError()
        prepared = await work
        return APIResponse.success(DatasetFilePreviewResponse(
            file=FileInfoResponse.public_from_file_info(prepared.file),
            related_files=[FileInfoResponse.public_from_file_info(info) for info in prepared.related_files],
        ))
    except (FileNotFoundError, VisualizationNotFoundError):
        raise HTTPException(status_code=404, detail="Dataset file or visualization plugin not found") from None
    except VisualizationDisabledError:
        raise HTTPException(status_code=403, detail="当前文件的可视化插件已停用，请先在插件管理中启用。") from None
    except PluginRuntimeError:
        raise HTTPException(status_code=503, detail="Cordis visualization runtime is unavailable") from None
    except PreviewVersionChanged:
        raise HTTPException(status_code=409, detail="文件或插件版本已变化，请重新打开预览。") from None
    except (ValueError, OSError):
        raise HTTPException(status_code=422, detail="此数据文件暂不能预览，请检查文件是否仍存在及其只读访问权限。") from None
    finally:
        if not work.done():
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                pass


def _require_dataset_demo_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise ForbiddenError("Only administrators can submit server directories for analysis")


def _require_dataset_manager(user: User) -> None:
    if user.role not in {UserRole.ADMIN, UserRole.USER}:
        raise ForbiddenError("Dataset management is available to administrators and users")


@router.get("", response_model=APIResponse[DataCenterDatasetCatalogResponse])
async def list_data_center_datasets(
    _current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetCatalogResponse]:
    datasets, total = await DataCenterDatasetService().list_datasets()
    return APIResponse.success(
        DataCenterDatasetCatalogResponse(
            datasets=[dataset_response(item) for item in datasets],
            total=total,
        )
    )


@router.get("/manage", response_model=APIResponse[DataCenterDatasetCatalogResponse])
async def list_managed_datasets(
    query: str | None = Query(default=None, max_length=200),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetCatalogResponse]:
    _require_dataset_manager(current_user)
    datasets, total = await DataCenterDatasetService().list_managed_datasets(
        actor_id=current_user.id,
        is_admin=current_user.role == UserRole.ADMIN,
        query=query,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return APIResponse.success(DataCenterDatasetCatalogResponse(
        datasets=[dataset_response(item) for item in datasets],
        total=total,
    ))


@router.post("/submissions", response_model=APIResponse[DataCenterDatasetResponse])
async def create_dataset_submission(
    submission: DatasetSubmissionRequest,
    _request: Request,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_dataset_demo_admin(current_user)
    sso_uid = None
    if get_settings().auth_provider != "none":
        if not submission.token:
            return Response(status_code=401)
        try:
            sso_uid = await resolve_sso_uid(submission.token)
        except UpstreamServiceError:
            # This is an API-to-API contract. Never redirect the caller's POST to
            # the SSO website because 307 would preserve the POST and cause a 405.
            return Response(status_code=401)
    dataset = await DataCenterDatasetService().create_submission(
        external_id=submission.external_id or "",
        name=submission.name,
        summary=submission.summary,
        keywords=submission.keywords,
        storage_directory=submission.storage_directory,
        nc_view_url=str(submission.ncViewUrl) if submission.ncViewUrl else None,
        created_by=current_user.id,
        sso_uid=sso_uid,
    )
    return APIResponse.success(dataset_response(dataset))


@router.post("/registrations", response_model=APIResponse[DataCenterDatasetResponse])
async def create_dataset_registration(
    registration: DatasetRegistrationRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    # Registering a host directory expands the set of paths the product can
    # mount. Keep the existing administrator-only submission boundary.
    _require_dataset_demo_admin(current_user)
    dataset = await DataCenterDatasetService().create_registration(
        name=registration.name,
        description=registration.description,
        domain=registration.domain,
        storage_directory=registration.storage_directory,
        created_by=current_user.id,
    )
    return APIResponse.success(dataset_response(dataset))


@router.patch("/{dataset_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def update_dataset_registration(
    dataset_id: str,
    changes: DatasetMetadataUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_dataset_manager(current_user)
    dataset = await DataCenterDatasetService().update_registration(
        dataset_id,
        actor_id=current_user.id,
        is_admin=current_user.role == UserRole.ADMIN,
        **changes.model_dump(exclude_none=True),
    )
    return APIResponse.success(dataset_response(dataset))


@router.delete("/{dataset_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def archive_dataset_registration(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_dataset_manager(current_user)
    dataset = await DataCenterDatasetService().archive_dataset(
        dataset_id,
        actor_id=current_user.id,
        is_admin=current_user.role == UserRole.ADMIN,
    )
    return APIResponse.success(dataset_response(dataset))


@router.post(
    "/{dataset_id}/suggested-questions",
    response_model=APIResponse[DatasetSuggestedQuestionsResponse],
)
async def generate_dataset_suggested_questions(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DatasetSuggestedQuestionsResponse]:
    _require_dataset_demo_admin(current_user)
    dataset = await DataCenterDatasetService().get_dataset(
        dataset_id,
        user_id=current_user.id,
    )
    questions = await DatasetSuggestedQuestionService().generate(dataset)
    return APIResponse.success(DatasetSuggestedQuestionsResponse(questions=questions))


@router.get("/{dataset_id}", response_model=APIResponse[DataCenterDatasetResponse])
async def get_data_center_dataset(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    dataset = await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    return APIResponse.success(dataset_response(dataset))


@router.get(
    "/{dataset_id}/data-products",
    response_model=APIResponse[list[DataProductResponse]],
)
async def list_dataset_data_products(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[list[DataProductResponse]]:
    await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    products = await DataProductService().list_for_dataset(dataset_id, current_user.id)
    return APIResponse.success([DataProductResponse.model_validate(item) for item in products])


@router.put("/{dataset_id}/data-products/{product_id}", response_model=APIResponse[DataProductResponse])
async def update_dataset_data_product(
    dataset_id: str,
    product_id: str,
    request: DataProductUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataProductResponse]:
    await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    product = await DataProductService().get(product_id, current_user.id)
    if product.dataset_id != dataset_id:
        raise ForbiddenError("Data product does not belong to this dataset")
    updated = await DataProductService().update_metadata(product_id, current_user.id, request.name, request.description, request.generation_method, request.created_by, request.directories, [file.model_dump() for file in request.files])
    return APIResponse.success(DataProductResponse.model_validate(updated))


@router.delete("/{dataset_id}/data-products/{product_id}", response_model=APIResponse[dict])
async def delete_dataset_data_product(
    dataset_id: str,
    product_id: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[dict]:
    await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    product = await DataProductService().get(product_id, current_user.id)
    if product.dataset_id != dataset_id:
        raise ForbiddenError("Data product does not belong to this dataset")
    await DataProductService().delete(product_id, current_user.id)
    return APIResponse.success({"deleted": True})


@router.get("/{dataset_id}/data-products/{product_id}/download")
async def download_dataset_data_product(
    dataset_id: str,
    product_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    product = await DataProductService().get(product_id, current_user.id)
    if product.dataset_id != dataset_id:
        raise ForbiddenError("Data product does not belong to this dataset")
    archive = SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    storage = get_file_storage()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in product.files:
            path = PurePosixPath(item.relative_path)
            if path.is_absolute() or ".." in path.parts:
                continue
            stream, _ = await storage.download_file(item.file_id, current_user.id)
            try:
                bundle.writestr(path.as_posix(), stream.read())
            finally:
                stream.close()
    archive.seek(0)
    filename = f"{product.product_id}-v{product.version}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(archive.close),
    )


@router.get("/{dataset_id}/preview", response_class=FileResponse)
async def get_dataset_preview(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    path = await DataCenterDatasetService().preview_path(dataset_id, user_id=current_user.id)
    return FileResponse(path)


@router.get(
    "/{dataset_id}/sessions",
    response_model=APIResponse[DatasetSessionHistoryResponse],
)
async def list_dataset_chat_sessions(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[DatasetSessionHistoryResponse]:
    await DataCenterDatasetService().get_dataset(dataset_id, user_id=current_user.id)
    summaries = await agent_service.get_dataset_sessions(current_user.id, dataset_id)
    return APIResponse.success(DatasetSessionHistoryResponse(
        sessions=[
            DatasetSessionHistoryItem(
                session_id=item.id,
                title=item.title,
                latest_message=item.latest_message,
                latest_message_at=(
                    int(item.latest_message_at.timestamp()) if item.latest_message_at else None
                ),
                status=item.status,
            )
            for item in summaries
        ]
    ))
