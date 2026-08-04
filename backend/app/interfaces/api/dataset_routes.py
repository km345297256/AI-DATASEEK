from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.application.errors.exceptions import ForbiddenError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.dataset_suggested_question_service import DatasetSuggestedQuestionService
from app.domain.models.user import User, UserRole
from app.application.services.agent_service import AgentService
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.dataset import (
    DataCenterDatasetCatalogResponse,
    DataCenterDatasetResponse,
    DatasetSubmissionRequest,
    DatasetSuggestedQuestionsResponse,
    DatasetSessionHistoryItem,
    DatasetSessionHistoryResponse,
    dataset_response,
)


router = APIRouter(prefix="/datasets", tags=["datasets"])


def _require_dataset_demo_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise ForbiddenError("Only administrators can submit server directories for analysis")


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


@router.post("/submissions", response_model=APIResponse[DataCenterDatasetResponse])
async def create_dataset_submission(
    request: DatasetSubmissionRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[DataCenterDatasetResponse]:
    _require_dataset_demo_admin(current_user)
    dataset = await DataCenterDatasetService().create_submission(
        external_id=request.external_id,
        name=request.name,
        summary=request.summary,
        keywords=request.keywords,
        storage_directory=request.storage_directory,
        created_by=current_user.id,
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
