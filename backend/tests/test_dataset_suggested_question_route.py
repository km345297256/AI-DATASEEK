from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import app.interfaces.api.dataset_routes as dataset_routes
from app.application.errors.exceptions import ForbiddenError, NotFoundError
from app.domain.models.dataset import DataCenterDataset
from app.domain.models.user import User, UserRole
from app.interfaces.schemas.dataset import DatasetSubmissionRequest


QUESTIONS = [
    "该数据集覆盖哪些年份？",
    "不同区域的数据有何差异？",
    "哪些文件适合进行趋势分析？",
    "数据中的异常值如何分布？",
]


def _dataset() -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id="tds_owned",
        data_center_id="dataset-chat-demo",
        data_center_name="Test datasets",
        name="Owned dataset",
        created_by="owner-a",
        is_submission=True,
    )


def _user(role: UserRole = UserRole.ADMIN) -> User:
    return User(
        id="owner-a",
        fullname="Owner A",
        email="owner-a@example.com",
        role=role,
    )


def _install_route_services(monkeypatch, *, dataset_result=None, questions=None):
    dataset_service = AsyncMock()
    suggested_service = AsyncMock()
    if isinstance(dataset_result, Exception):
        dataset_service.get_dataset.side_effect = dataset_result
    else:
        dataset_service.get_dataset.return_value = dataset_result or _dataset()
    suggested_service.generate.return_value = QUESTIONS if questions is None else questions
    monkeypatch.setattr(dataset_routes, "DataCenterDatasetService", lambda: dataset_service)
    monkeypatch.setattr(dataset_routes, "DatasetSuggestedQuestionService", lambda: suggested_service)
    return dataset_service, suggested_service


def _submission_request() -> DatasetSubmissionRequest:
    return DatasetSubmissionRequest(
        external_id="external-1",
        name="Temporary dataset",
        summary="Temporary analysis request",
        keywords=["science"],
        storage_directory="/srv/datasets/example",
    )


@pytest.mark.asyncio
async def test_submission_route_allows_admin_and_passes_one_directory(monkeypatch):
    dataset = _dataset()
    dataset_service = AsyncMock()
    dataset_service.create_submission.return_value = dataset
    monkeypatch.setattr(dataset_routes, "DataCenterDatasetService", lambda: dataset_service)

    response = await dataset_routes.create_dataset_submission(
        _submission_request(),
        current_user=_user(),
    )

    dataset_service.create_submission.assert_awaited_once_with(
        external_id="external-1",
        name="Temporary dataset",
        summary="Temporary analysis request",
        keywords=["science"],
        storage_directory="/srv/datasets/example",
        created_by="owner-a",
    )
    assert response.data is not None
    assert response.data.dataset_id == dataset.dataset_id


@pytest.mark.asyncio
async def test_submission_route_rejects_non_admin_before_directory_inspection(monkeypatch):
    dataset_service = AsyncMock()
    monkeypatch.setattr(dataset_routes, "DataCenterDatasetService", lambda: dataset_service)

    with pytest.raises(ForbiddenError):
        await dataset_routes.create_dataset_submission(
            _submission_request(),
            current_user=_user(UserRole.USER),
        )

    dataset_service.create_submission.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggested_questions_route_scopes_lookup_to_current_owner(monkeypatch):
    dataset = _dataset()
    dataset_service, suggested_service = _install_route_services(
        monkeypatch,
        dataset_result=dataset,
    )

    response = await dataset_routes.generate_dataset_suggested_questions(
        dataset.dataset_id,
        current_user=_user(),
    )

    dataset_service.get_dataset.assert_awaited_once_with(
        dataset.dataset_id,
        user_id="owner-a",
    )
    suggested_service.generate.assert_awaited_once_with(dataset)
    assert response.data is not None
    assert response.data.questions == QUESTIONS
    assert len(response.data.questions) == 4


@pytest.mark.asyncio
async def test_suggested_questions_route_does_not_generate_for_unowned_dataset(monkeypatch):
    dataset_service, suggested_service = _install_route_services(
        monkeypatch,
        dataset_result=NotFoundError("not owned"),
    )

    with pytest.raises(NotFoundError):
        await dataset_routes.generate_dataset_suggested_questions(
            "tds_other_owner",
            current_user=_user(),
        )

    dataset_service.get_dataset.assert_awaited_once_with(
        "tds_other_owner",
        user_id="owner-a",
    )
    suggested_service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggested_questions_route_rejects_non_admin_before_lookup(monkeypatch):
    dataset_service, suggested_service = _install_route_services(monkeypatch)

    with pytest.raises(ForbiddenError):
        await dataset_routes.generate_dataset_suggested_questions(
            "tds_owned",
            current_user=_user(UserRole.USER),
        )

    dataset_service.get_dataset.assert_not_awaited()
    suggested_service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggested_questions_route_rejects_any_count_other_than_four(monkeypatch):
    dataset = _dataset()
    _install_route_services(
        monkeypatch,
        dataset_result=dataset,
        questions=QUESTIONS[:3],
    )

    with pytest.raises(ValidationError):
        await dataset_routes.generate_dataset_suggested_questions(
            dataset.dataset_id,
            current_user=_user(),
        )
