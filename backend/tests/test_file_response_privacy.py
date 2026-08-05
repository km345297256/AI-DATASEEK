from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.interfaces.api import session_routes
from app.interfaces.schemas.event import EventMapper, MessageSSEEvent
from app.interfaces.schemas.file import FileInfoResponse, FileViewResponse, public_filename


PRIVATE_ARTIFACT_PATH = "/home/ubuntu/output/reports/assets/chart.png"


def _private_file() -> FileInfo:
    return FileInfo(
        file_id="file-123",
        filename=PRIVATE_ARTIFACT_PATH,
        file_path=PRIVATE_ARTIFACT_PATH,
        content_type="image/png",
        metadata={
            "file_path": PRIVATE_ARTIFACT_PATH,
            "user_id": "user-private",
            "filePath": "/root/private/duplicate.png",
            "user-id": "user-private-kebab",
            "label": "monthly chart",
            "nested": {
                "source": "/srv/private/tenant-a/source.csv",
                "docs": "https://example.test/files/chart.png",
            },
            "windows": [r"C:\private\tenant-a\chart.png"],
            "path_object": Path("/srv/private/tenant-a/object.csv"),
        },
        user_id="user-private",
        file_url="file:///home/ubuntu/output/reports/assets/chart.png",
    )


def _assert_public_file_payload(payload: dict) -> None:
    serialized = str(payload)
    assert payload["file_id"] == "file-123"
    assert payload["filename"] == "chart.png"
    assert payload["relative_path"] == "reports/assets/chart.png"
    assert payload["metadata"]["label"] == "monthly chart"
    assert payload["metadata"]["nested"]["docs"] == "https://example.test/files/chart.png"
    assert payload["metadata"]["nested"]["source"] == "[redacted path]"
    assert payload["metadata"]["windows"] == ["[redacted path]"]
    assert payload["metadata"]["path_object"] == "[redacted path]"
    assert "file_path" not in payload["metadata"]
    assert "filePath" not in payload["metadata"]
    assert "user-id" not in payload["metadata"]
    assert "/home/ubuntu" not in serialized
    assert "/srv/private" not in serialized
    assert r"C:\private" not in serialized
    assert "user-private" not in serialized


def test_file_info_response_hides_internal_paths_without_mutating_domain_model():
    file_info = _private_file()

    response = FileInfoResponse.public_from_file_info(file_info)

    _assert_public_file_payload(response.model_dump())
    assert response.file_url is None
    assert file_info.file_path == PRIVATE_ARTIFACT_PATH
    assert file_info.metadata["file_path"] == PRIVATE_ARTIFACT_PATH


def test_other_file_responses_do_not_echo_absolute_paths():
    view = FileViewResponse(content="chart", file=PRIVATE_ARTIFACT_PATH)

    assert view.file == "reports/assets/chart.png"
    assert public_filename("/srv/private/tenant-a/report.csv") == "report.csv"


@pytest.mark.parametrize(
    ("file_url", "expected"),
    [
        ("https://files.example.test/chart.png", "https://files.example.test/chart.png"),
        ("/api/v1/files/file-123?signature=safe", "/api/v1/files/file-123?signature=safe"),
        ("file:///srv/private/chart.png", None),
        ("/home/ubuntu/output/chart.png", None),
        (r"C:\private\chart.png", None),
        ("/api/v1/files/../private", None),
    ],
)
def test_file_info_response_only_allows_browser_file_urls(file_url, expected):
    response = FileInfoResponse.public_from_file_info(
        _private_file(),
        file_url=file_url,
    )

    assert response.file_url == expected


@pytest.mark.asyncio
async def test_message_sse_attachment_uses_path_safe_file_response(monkeypatch):
    class StubFileService:
        async def create_signed_url(self, file_id, user_id=None):
            assert (file_id, user_id) == ("file-123", "user-private")
            return "/api/v1/files/file-123?signature=safe"

    from app.interfaces import dependencies

    monkeypatch.setattr(dependencies, "get_file_service", lambda: StubFileService())
    event = MessageEvent(
        message="artifact ready",
        attachments=[_private_file()],
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, MessageSSEEvent)
    _assert_public_file_payload(mapped.data.attachments[0].model_dump())
    assert mapped.data.attachments[0].file_url.startswith("/api/v1/files/file-123")


class StubAgentService:
    def __init__(self, file_info: FileInfo):
        self.file_info = file_info

    async def is_session_shared(self, session_id):
        return True

    async def get_session_files(self, session_id, user_id=None):
        return [self.file_info]

    async def get_shared_session_files(self, session_id):
        return [self.file_info]


@pytest.mark.asyncio
async def test_owner_session_file_list_uses_path_safe_response():
    file_info = _private_file()

    response = await session_routes.get_session_files(
        "session-1",
        sort_by="filename",
        sort_order="asc",
        current_user=SimpleNamespace(id="user-private"),
        agent_service=StubAgentService(file_info),
    )

    _assert_public_file_payload(response.data[0].model_dump())
    assert file_info.file_path == PRIVATE_ARTIFACT_PATH


@pytest.mark.asyncio
async def test_public_shared_file_list_uses_path_safe_response(monkeypatch):
    file_info = _private_file()

    class StubFileService:
        async def enrich_with_file_url(self, item):
            item.file_url = "/api/v1/files/file-123?signature=shared"
            return item

    monkeypatch.setattr(
        session_routes,
        "get_file_service",
        lambda: StubFileService(),
    )

    response = await session_routes.get_shared_session_files(
        "session-1",
        sort_by="filename",
        sort_order="asc",
        agent_service=StubAgentService(file_info),
    )

    payload = response.data[0].model_dump()
    _assert_public_file_payload(payload)
    assert payload["file_url"].startswith("/api/v1/files/file-123")
    assert file_info.file_path == PRIVATE_ARTIFACT_PATH
