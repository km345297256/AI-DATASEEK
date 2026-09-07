from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain.messages import ToolMessage

from app.application.services.file_service import FileService
from app.domain.models.event import FileToolContent, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.spill import (
    SpillArtifactOwner,
    SpillArtifactRecord,
    SpillArtifactRef,
    SpillArtifactSaveRequest,
    SpillArtifactSource,
)
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.pipeline import ToolExecutionContext
from app.domain.services.tools.spill import (
    SPILL_READ_TOOL_NAME,
    SpillArtifactInterceptor,
    SpillArtifactToolkit,
    retain_utf8_head_tail,
)
from app.domain.services.tools.spill_projection import (
    projected_tool_artifact,
    spill_notice_from_result,
)
from app.infrastructure.external.file.spill import FileStorageSpillArtifactStore
from app.interfaces.schemas.event import ToolSSEEvent


OWNER = SpillArtifactOwner(user_id="user-1", session_id="session-1")


class InMemorySpillRepository:
    def __init__(self) -> None:
        self.records: dict[str, SpillArtifactRecord] = {}

    async def save(self, record: SpillArtifactRecord) -> None:
        if record.artifact_id in self.records:
            raise RuntimeError("duplicate")
        self.records[record.artifact_id] = record

    async def find_by_artifact_id(self, artifact_id: str):
        return self.records.get(artifact_id)

    async def list_by_owner(self, owner_user_id: str, owner_session_id: str):
        return [
            record
            for record in self.records.values()
            if record.owner_user_id == owner_user_id
            and record.owner_session_id == owner_session_id
        ]

    async def list_expired(self, before: datetime, *, limit: int):
        def as_utc(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

        def cleanup_order(record: SpillArtifactRecord):
            attempted = record.cleanup_attempted_at
            if attempted is not None:
                attempted = as_utc(attempted)
            expires = as_utc(record.expires_at)
            return (
                attempted is not None,
                attempted or datetime.min.replace(tzinfo=UTC),
                expires,
                record.artifact_id,
            )

        return [
            record
            for record in sorted(
                self.records.values(),
                key=cleanup_order,
            )
            if record.status == "deleting"
            or as_utc(record.expires_at) <= as_utc(before)
        ][:limit]

    async def mark_deleting(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ):
        record = self.records.get(artifact_id)
        if (
            record is None
            or record.storage_file_id != expected_storage_file_id
        ):
            return None
        record = record.model_copy(update={
            "status": "deleting",
            "cleanup_attempted_at": datetime.now(UTC),
        })
        self.records[artifact_id] = record
        return record

    async def delete(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ) -> None:
        record = self.records.get(artifact_id)
        if (
            record is not None
            and record.storage_file_id == expected_storage_file_id
        ):
            self.records.pop(artifact_id, None)


class InMemoryFileStorage:
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, FileInfo]] = {}
        self.uploads = 0
        self.full_downloads = 0
        self.range_downloads: list[tuple[int, int]] = []
        self.fail_delete = False
        self.delete_returns_false = False
        self.fail_info = False

    async def upload_file(
        self,
        file_data,
        filename,
        user_id,
        content_type=None,
        metadata=None,
    ):
        content = file_data.read()
        file_id = f"internal-storage-{self.uploads}"
        self.uploads += 1
        info = FileInfo(
            file_id=file_id,
            filename=filename,
            user_id=user_id,
            size=len(content),
            content_type=content_type,
            metadata=dict(metadata or {}),
        )
        self.files[file_id] = content, info
        return info

    async def download_file(self, file_id, user_id=None):
        self.full_downloads += 1
        content, info = self.files[file_id]
        if user_id is not None and info.user_id != user_id:
            raise PermissionError("wrong user")
        return io.BytesIO(content), info.model_copy(deep=True)

    async def download_file_range(
        self,
        file_id,
        user_id=None,
        *,
        offset,
        length,
    ):
        content, info = self.files[file_id]
        if user_id is not None and info.user_id != user_id:
            raise PermissionError("wrong user")
        self.range_downloads.append((offset, length))
        return content[offset:offset + length], info.model_copy(deep=True)

    async def delete_file(self, file_id, user_id):
        if self.fail_delete:
            raise RuntimeError("private object-store detail")
        if self.delete_returns_false:
            return False
        value = self.files.get(file_id)
        if value is None or value[1].user_id != user_id:
            return False
        del self.files[file_id]
        return True

    async def get_file_info(self, file_id, user_id=None):
        if self.fail_info:
            raise RuntimeError("private metadata-store detail")
        value = self.files.get(file_id)
        if value is None or (user_id is not None and value[1].user_id != user_id):
            return None
        return value[1].model_copy(deep=True)


class RecordingLargeUploadStorage:
    def __init__(self, *, fail_abort: bool = False) -> None:
        self.fail_abort = fail_abort
        self.init_calls = 0
        self.part_calls = 0
        self.complete_calls = 0
        self.aborted_sessions = []

    async def init_large_upload(self, *args, **kwargs):
        self.init_calls += 1
        return SimpleNamespace(upload_id="created-upload")

    async def upload_large_upload_part(self, *args, **kwargs):
        self.part_calls += 1
        return "etag"

    async def complete_large_upload(self, *args, **kwargs):
        self.complete_calls += 1
        return FileInfo(file_id="completed-file", filename="result.bin", size=1)

    async def abort_large_upload(self, session):
        self.aborted_sessions.append(session)
        if self.fail_abort:
            raise RuntimeError("private multipart provider detail")
        session.status = "aborted"


class CapturingStore:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.requests = []
        self.error = error

    async def save_text(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return SpillArtifactRef(
            locator="spill://artifact/0123456789abcdef0123456789abcdef",
            byte_count=len(request.content.encode("utf-8")),
            sha256="a" * 64,
            media_type="application/json",
            retrieval_hint="Call spill_artifact_read with the locator.",
        )

    async def read_text(self, *args, **kwargs):
        raise NotImplementedError

    async def delete_owner(self, owner):
        return 0

    async def reap_expired(self, *, limit=100):
        return 0


def _context(name: str = "large_tool") -> ToolExecutionContext:
    tool = SimpleNamespace(name=name)
    return ToolExecutionContext(
        tool=tool,
        tool_call={"name": name, "id": "call-1", "args": {}},
    )


def _message(content: str, *, success: bool = True) -> ToolMessage:
    artifact = ToolResult(success=success, data={"raw": content})
    return ToolMessage(
        tool_call_id="call-1",
        name="large_tool",
        content=content,
        artifact=artifact,
    )


def test_utf8_head_tail_never_exceeds_budget_or_splits_characters():
    original = "开头🙂" * 100 + "结尾🚀" * 100
    preview, retained = retain_utf8_head_tail(original, 127)

    assert len(preview.encode("utf-8")) <= 127
    assert 0 < retained < len(original.encode("utf-8"))
    assert "middle omitted" in preview
    preview.encode("utf-8").decode("utf-8")


@pytest.mark.asyncio
async def test_spill_threshold_is_strict_and_projection_keeps_raw_artifact_private():
    store = CapturingStore()
    interceptor = SpillArtifactInterceptor(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=240,
    )
    exact = _message("x" * 1024)
    assert await interceptor.result(_context(), exact) is exact

    original = _message("敏" * 600)
    transformed = await interceptor.result(_context(), original)
    notice = spill_notice_from_result(transformed)

    assert notice is not None
    assert notice.status == "stored"
    assert notice.reference is not None
    assert notice.original_bytes == 1800
    assert notice.omitted_bytes == notice.original_bytes - notice.retained_bytes
    assert len(transformed.content.encode("utf-8")) <= 1024
    assert transformed.artifact is original.artifact
    assert projected_tool_artifact(transformed) is not transformed.artifact
    assert "敏" * 600 not in transformed.content
    assert store.requests[0].content == "敏" * 600


@pytest.mark.asyncio
async def test_spill_failure_stays_bounded_and_preserves_tool_status(caplog):
    store = CapturingStore(error=RuntimeError("private-storage-secret"))
    interceptor = SpillArtifactInterceptor(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    )
    transformed = await interceptor.result(
        _context(),
        _message("secret-value-" * 200, success=False),
    )
    notice = spill_notice_from_result(transformed)

    assert notice is not None
    assert notice.status == "unavailable"
    assert notice.reference is None
    assert projected_tool_artifact(transformed).success is False
    assert len(transformed.content.encode("utf-8")) <= 1024
    assert "private-storage-secret" not in caplog.text


@pytest.mark.asyncio
async def test_spill_artifact_ceiling_keeps_projection_bounded_without_storage():
    store = CapturingStore()
    interceptor = SpillArtifactInterceptor(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
        max_artifact_bytes=1500,
        preview_bytes=200,
    )

    transformed = await interceptor.result(_context(), _message("x" * 2000))
    notice = spill_notice_from_result(transformed)

    assert notice is not None and notice.status == "unavailable"
    assert store.requests == []
    assert len(transformed.content.encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_spill_timeout_does_not_cancel_inflight_storage_write():
    class SlowStore(CapturingStore):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.completed = asyncio.Event()
            self.cancelled = False

        async def save_text(self, request):
            self.requests.append(request)
            self.started.set()
            try:
                await self.release.wait()
                return await super().save_text(request)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            finally:
                self.completed.set()

    store = SlowStore()
    interceptor = SpillArtifactInterceptor(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
        store_timeout_seconds=0.01,
    )

    transformed = await interceptor.result(
        _context(),
        _message("large result" * 200),
    )
    notice = spill_notice_from_result(transformed)

    assert store.started.is_set()
    assert notice is not None and notice.status == "unavailable"
    assert store.cancelled is False
    store.release.set()
    await asyncio.wait_for(store.completed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert store.cancelled is False


@pytest.mark.asyncio
async def test_spill_reader_is_excluded_from_recursive_spilling():
    store = CapturingStore()
    interceptor = SpillArtifactInterceptor(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
    )
    result = _message("x" * 2048)

    assert await interceptor.result(_context(SPILL_READ_TOOL_NAME), result) is result
    assert store.requests == []


@pytest.mark.asyncio
async def test_file_store_is_idempotent_private_and_pages_utf8():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
        read_chunk_bytes=1024,
    )
    request = SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="call-1"),
        content="🙂中文🚀tail",
    )

    first = await store.save_text(request)
    second = await store.save_text(request)

    assert first == second
    assert files.uploads == 1
    assert "internal-storage" not in first.locator
    pages = []
    offset = 0
    while True:
        page = await store.read_text(
            first.locator,
            OWNER,
            offset=offset,
            max_bytes=8,
        )
        pages.append(page.content)
        assert page.next_byte > offset or page.eof
        offset = page.next_byte
        if page.eof:
            break
    assert "".join(pages) == request.content
    assert files.full_downloads == 0
    assert files.range_downloads
    assert all(length <= 8 for _, length in files.range_downloads)

    with pytest.raises(PermissionError):
        await store.read_text(
            first.locator,
            SpillArtifactOwner(user_id="user-1", session_id="session-2"),
        )


@pytest.mark.asyncio
async def test_idempotent_save_repairs_a_missing_backing_object():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    request = SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="repair"),
        content="durable payload",
    )
    first = await store.save_text(request)
    original_storage_id = next(iter(repository.records.values())).storage_file_id
    files.files.pop(original_storage_id)

    repaired = await store.save_text(request)

    assert repaired.locator == first.locator
    assert files.uploads == 2
    replacement = next(iter(repository.records.values()))
    assert replacement.storage_file_id != original_storage_id
    assert await store.read_text(repaired.locator, OWNER)


@pytest.mark.asyncio
async def test_expired_artifact_is_revoked_and_session_delete_is_mapping_first():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    reference = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="expired"),
        content="payload",
    ))
    record = next(iter(repository.records.values()))
    repository.records[record.artifact_id] = record.model_copy(update={
        # PyMongo returns UTC datetimes without tzinfo by default.
        "expires_at": datetime.utcnow() - timedelta(seconds=1),
    })

    with pytest.raises(FileNotFoundError):
        await store.read_text(reference.locator, OWNER)
    assert repository.records == {}

    active = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="delete"),
        content="another payload",
    ))
    files.fail_delete = True
    assert await store.delete_owner(OWNER) == 0
    assert len(repository.records) == 1
    assert next(iter(repository.records.values())).status == "deleting"
    with pytest.raises(FileNotFoundError):
        await store.read_text(active.locator, OWNER)
    files.fail_delete = False
    assert await store.reap_expired() == 1
    assert repository.records == {}
    assert files.files == {}


@pytest.mark.asyncio
async def test_cleanup_keeps_tombstone_when_absence_cannot_be_verified():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="tri-state"),
        content="payload",
    ))
    files.delete_returns_false = True
    files.fail_info = True

    assert await store.delete_owner(OWNER) == 0
    assert len(repository.records) == 1
    assert next(iter(repository.records.values())).status == "deleting"
    assert len(files.files) == 1

    files.delete_returns_false = False
    files.fail_info = False
    assert await store.reap_expired() == 1
    assert repository.records == {}
    assert files.files == {}


@pytest.mark.asyncio
async def test_expiry_cleanup_cas_does_not_delete_a_replacement_mapping():
    class PausingDeleteStorage(InMemoryFileStorage):
        def __init__(self) -> None:
            super().__init__()
            self.pause_file_id: str | None = None
            self.object_deleted = asyncio.Event()
            self.allow_cleanup_to_finish = asyncio.Event()

        async def delete_file(self, file_id, user_id):
            deleted = await super().delete_file(file_id, user_id)
            if deleted and file_id == self.pause_file_id:
                self.object_deleted.set()
                await self.allow_cleanup_to_finish.wait()
            return deleted

    files = PausingDeleteStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    reference = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="cas-race"),
        content="replacement-safe payload",
    ))
    original = next(iter(repository.records.values()))
    original_content, original_info = files.files[original.storage_file_id]
    expired = original.model_copy(update={
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    })
    repository.records[original.artifact_id] = expired
    files.pause_file_id = original.storage_file_id

    cleanup = asyncio.create_task(store.reap_expired())
    await asyncio.wait_for(files.object_deleted.wait(), timeout=1)
    try:
        replacement_info = await files.upload_file(
            io.BytesIO(original_content),
            original_info.filename,
            original_info.user_id,
            content_type=original_info.content_type,
            metadata=original_info.metadata,
        )
        replacement = original.model_copy(update={
            "storage_file_id": replacement_info.file_id,
            "status": "active",
            "cleanup_attempted_at": None,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        })
        # Model the exact interleaving: a second cleanup removes the old
        # mapping and an idempotent save installs a new backing object before
        # the first cleanup reaches its final metadata delete.
        repository.records[original.artifact_id] = replacement
    finally:
        files.allow_cleanup_to_finish.set()

    assert await asyncio.wait_for(cleanup, timeout=1) == 1
    assert repository.records[original.artifact_id] == replacement
    assert replacement.storage_file_id in files.files
    page = await store.read_text(reference.locator, OWNER)
    assert page.content == "replacement-safe payload"


@pytest.mark.asyncio
async def test_failed_cleanup_rotates_behind_unattempted_expired_records():
    class SelectiveFailureStorage(InMemoryFileStorage):
        fail_file_id: str | None = None

        async def delete_file(self, file_id, user_id):
            if file_id == self.fail_file_id:
                raise RuntimeError("temporary cleanup failure")
            return await super().delete_file(file_id, user_id)

    files = SelectiveFailureStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    for call_id in ("first", "second"):
        await store.save_text(SpillArtifactSaveRequest(
            owner=OWNER,
            source=SpillArtifactSource(tool_name="demo", tool_call_id=call_id),
            content=f"payload-{call_id}",
        ))
    records = list(repository.records.values())
    for index, record in enumerate(records):
        repository.records[record.artifact_id] = record.model_copy(update={
            "expires_at": datetime.now(UTC) - timedelta(seconds=2 - index),
        })
    files.fail_file_id = records[0].storage_file_id

    assert await store.reap_expired(limit=1) == 0
    assert repository.records[records[0].artifact_id].cleanup_attempted_at is not None
    assert await store.reap_expired(limit=1) == 1
    assert records[0].artifact_id in repository.records
    assert records[1].artifact_id not in repository.records

    files.fail_file_id = None
    assert await store.reap_expired(limit=1) == 1
    assert repository.records == {}
    assert files.files == {}


@pytest.mark.asyncio
async def test_unconfirmed_metadata_failure_retains_upload_when_index_is_unavailable(
    caplog,
):
    class FailingRepository(InMemorySpillRepository):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def find_by_artifact_id(self, artifact_id: str):
            self.lookups += 1
            if self.lookups > 1:
                raise RuntimeError("private-lookup-secret")
            return None

        async def save(self, record: SpillArtifactRecord) -> None:
            raise RuntimeError("private-save-secret")

    files = InMemoryFileStorage()
    store = FileStorageSpillArtifactStore(
        files,
        FailingRepository(),
        identity_key="test-identity-key",
    )

    with pytest.raises(RuntimeError, match="private-save-secret"):
        await store.save_text(SpillArtifactSaveRequest(
            owner=OWNER,
            source=SpillArtifactSource(tool_name="demo", tool_call_id="failed"),
            content="payload",
        ))

    # The save may have committed before reporting its error. With every
    # reconciliation lookup unavailable, deleting the upload could corrupt a
    # live locator, so the conservative outcome is an orphan rather than loss.
    assert len(files.files) == 1
    assert "private-lookup-secret" not in caplog.text
    assert "private-save-secret" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_provider_size_is_compensated_after_upload():
    class InvalidSizeStorage(InMemoryFileStorage):
        async def upload_file(self, *args, **kwargs):
            info = await super().upload_file(*args, **kwargs)
            return info.model_copy(update={"size": "not-an-integer"})

    files = InvalidSizeStorage()
    store = FileStorageSpillArtifactStore(
        files,
        InMemorySpillRepository(),
        identity_key="test-identity-key",
    )

    with pytest.raises(ValueError):
        await store.save_text(SpillArtifactSaveRequest(
            owner=OWNER,
            source=SpillArtifactSource(tool_name="demo", tool_call_id="bad-size"),
            content="payload",
        ))

    assert files.files == {}


@pytest.mark.asyncio
async def test_ambiguous_metadata_commit_keeps_the_committed_object():
    class CommitThenFailRepository(InMemorySpillRepository):
        async def save(self, record: SpillArtifactRecord) -> None:
            await super().save(record)
            raise RuntimeError("ambiguous network response")

    files = InMemoryFileStorage()
    repository = CommitThenFailRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    reference = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="ambiguous"),
        content="durable payload",
    ))

    assert len(repository.records) == 1
    assert len(files.files) == 1
    page = await store.read_text(reference.locator, OWNER)
    assert page.content == "durable payload"


@pytest.mark.asyncio
async def test_ambiguous_commit_tombstone_reconciles_without_deleting_live_object():
    class CommitThenHideRepository(InMemorySpillRepository):
        def __init__(self) -> None:
            super().__init__()
            self.committed_artifact_id: str | None = None
            self.lookup_failures = 0

        async def save(self, record: SpillArtifactRecord) -> None:
            if record.status == "active":
                await super().save(record)
                self.committed_artifact_id = record.artifact_id
                self.lookup_failures = 3
                raise RuntimeError("ambiguous network response")
            await super().save(record)

        async def find_by_artifact_id(self, artifact_id: str):
            if (
                artifact_id == self.committed_artifact_id
                and self.lookup_failures > 0
            ):
                self.lookup_failures -= 1
                raise RuntimeError("temporary metadata outage")
            return await super().find_by_artifact_id(artifact_id)

    files = InMemoryFileStorage()
    repository = CommitThenHideRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )

    with pytest.raises(RuntimeError, match="ambiguous network response"):
        await store.save_text(SpillArtifactSaveRequest(
            owner=OWNER,
            source=SpillArtifactSource(tool_name="demo", tool_call_id="hidden"),
            content="do not delete me",
        ))

    active = next(
        record for record in repository.records.values()
        if record.status == "active"
    )
    tombstone = next(
        record for record in repository.records.values()
        if record.reconcile_artifact_id is not None
    )
    assert tombstone.reconcile_artifact_id == active.artifact_id
    assert tombstone.storage_file_id == active.storage_file_id
    assert len(files.files) == 1

    assert await store.reap_expired() == 1
    assert list(repository.records) == [active.artifact_id]
    assert active.storage_file_id in files.files
    page = await store.read_text(active.locator, OWNER)
    assert page.content == "do not delete me"


@pytest.mark.asyncio
async def test_cancelled_uncertain_commit_reconciles_without_deleting_live_object():
    class SlowRepository(InMemorySpillRepository):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.committed_artifact_id: str | None = None
            self.lookup_failures = 0

        async def save(self, record: SpillArtifactRecord) -> None:
            if record.status != "active":
                await super().save(record)
                return
            self.started.set()
            await self.release.wait()
            await super().save(record)
            self.committed_artifact_id = record.artifact_id
            self.lookup_failures = 3

        async def find_by_artifact_id(self, artifact_id: str):
            if (
                artifact_id == self.committed_artifact_id
                and self.lookup_failures > 0
            ):
                self.lookup_failures -= 1
                raise RuntimeError("temporary metadata outage")
            return await super().find_by_artifact_id(artifact_id)

    files = InMemoryFileStorage()
    repository = SlowRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    operation = asyncio.create_task(store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="cancelled"),
        content="payload",
    )))

    await repository.started.wait()
    operation.cancel()
    await asyncio.sleep(0)
    assert operation.done() is False
    repository.release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert len(repository.records) == 2
    record = next(
        item for item in repository.records.values()
        if item.status == "active"
    )
    tombstone = next(
        item for item in repository.records.values()
        if item.reconcile_artifact_id is not None
    )
    assert record.status == "active"
    assert tombstone.reconcile_artifact_id == record.artifact_id
    assert len(files.files) == 1
    assert await store.reap_expired() == 1
    assert list(repository.records) == [record.artifact_id]
    assert await store.read_text(record.locator, OWNER)
    assert await store.delete_owner(OWNER) == 1
    assert repository.records == {}
    assert files.files == {}


@pytest.mark.asyncio
async def test_generic_file_service_cannot_download_inspect_or_delete_spill_blob():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
    )
    reference = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="private"),
        content="private payload",
    ))
    record = next(iter(repository.records.values()))
    storage_file_id = record.storage_file_id
    service = FileService(file_storage=files)

    assert await service.get_file_info(storage_file_id, OWNER.user_id) is None
    assert await service.delete_file(storage_file_id, OWNER.user_id) is False
    with pytest.raises(FileNotFoundError):
        await service.download_file(storage_file_id, OWNER.user_id)
    assert await service.get_file_info(storage_file_id, None) is None
    with pytest.raises(FileNotFoundError):
        await service.download_file(storage_file_id, None)
    assert files.full_downloads == 0
    assert record.storage_user_id is not None
    assert await service.delete_file(storage_file_id, record.storage_user_id) is False

    assert await store.read_text(reference.locator, OWNER)
    assert await store.delete_owner(OWNER) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"source": "tool_output_spill"},
    {"spill_artifact_id": "0" * 32},
    {"SPILL_SCHEMA_VERSION": 1},
])
async def test_generic_file_upload_rejects_reserved_spill_metadata(metadata):
    files = InMemoryFileStorage()
    service = FileService(file_storage=files)

    with pytest.raises(ValueError, match="Reserved file metadata"):
        await service.upload_file(
            io.BytesIO(b"payload"),
            "ordinary.txt",
            OWNER.user_id,
            metadata=metadata,
        )

    assert files.uploads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"source": "tool_output_spill"},
    {"spill_artifact_id": "0" * 32},
    {"SPILL_SCHEMA_VERSION": 1},
])
async def test_large_upload_init_rejects_reserved_spill_metadata(metadata):
    storage = RecordingLargeUploadStorage()
    service = FileService(file_storage=storage)

    with pytest.raises(ValueError, match="Reserved file metadata"):
        await service.init_large_upload(
            "ordinary.bin",
            1024,
            OWNER.user_id,
            metadata=metadata,
        )

    assert storage.init_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["part", "complete"])
async def test_legacy_reserved_large_upload_is_aborted_before_part_or_complete(
    monkeypatch,
    operation,
):
    storage = RecordingLargeUploadStorage()
    service = FileService(file_storage=storage)
    session = SimpleNamespace(
        upload_id="legacy-reserved",
        status="initiated",
        metadata={"source": "tool_output_spill"},
    )

    async def get_large_upload(upload_id, user_id):
        assert upload_id == session.upload_id
        assert user_id == OWNER.user_id
        return session

    monkeypatch.setattr(service, "get_large_upload", get_large_upload)

    with pytest.raises(ValueError, match="Reserved file metadata"):
        if operation == "part":
            await service.upload_large_upload_part(
                session.upload_id,
                1,
                OWNER.user_id,
                b"payload",
            )
        else:
            await service.complete_large_upload(
                session.upload_id,
                [{"part_number": 1, "etag": "etag"}],
                OWNER.user_id,
            )

    assert storage.aborted_sessions == [session]
    assert storage.part_calls == 0
    assert storage.complete_calls == 0


@pytest.mark.asyncio
async def test_legacy_reserved_large_upload_rejection_survives_abort_failure(monkeypatch):
    storage = RecordingLargeUploadStorage(fail_abort=True)
    service = FileService(file_storage=storage)
    session = SimpleNamespace(
        upload_id="legacy-reserved",
        status="uploading",
        metadata={"spill_artifact_id": "0" * 32},
    )

    async def get_large_upload(*args, **kwargs):
        return session

    monkeypatch.setattr(service, "get_large_upload", get_large_upload)

    with pytest.raises(ValueError, match="Reserved file metadata") as exc_info:
        await service.upload_large_upload_part(
            session.upload_id,
            1,
            OWNER.user_id,
            b"payload",
        )

    assert "private multipart provider detail" not in str(exc_info.value)
    assert storage.aborted_sessions == [session]
    assert storage.part_calls == 0


@pytest.mark.asyncio
async def test_reserved_large_upload_can_still_use_explicit_abort(monkeypatch):
    storage = RecordingLargeUploadStorage()
    service = FileService(file_storage=storage)
    session = SimpleNamespace(
        upload_id="legacy-reserved",
        status="uploading",
        metadata={"source": "tool_output_spill"},
    )

    async def get_large_upload(*args, **kwargs):
        return session

    monkeypatch.setattr(service, "get_large_upload", get_large_upload)

    await service.abort_large_upload(session.upload_id, OWNER.user_id)

    assert storage.aborted_sessions == [session]
    assert session.status == "aborted"


@pytest.mark.asyncio
async def test_spill_toolkit_returns_stable_access_and_validation_errors():
    class DenyingStore(CapturingStore):
        async def read_text(self, locator, owner, **kwargs):
            raise PermissionError("private owner value")

    toolkit = SpillArtifactToolkit(DenyingStore(), owner=OWNER)
    tool = toolkit.get_tool(SPILL_READ_TOOL_NAME)
    result = await tool._arun(
        locator="spill://artifact/0123456789abcdef0123456789abcdef",
        offset=0,
        max_bytes=32,
    )

    assert result.success is False
    assert result.data == {"error": "spill_artifact_access_denied"}
    assert "private owner value" not in (result.message or "")


@pytest.mark.asyncio
async def test_spill_toolkit_bounds_json_escaping_and_keeps_page_progress():
    files = InMemoryFileStorage()
    repository = InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(
        files,
        repository,
        identity_key="test-identity-key",
        read_chunk_bytes=4096,
    )
    original = "\x00\x01\n🙂" * 500
    reference = await store.save_text(SpillArtifactSaveRequest(
        owner=OWNER,
        source=SpillArtifactSource(tool_name="demo", tool_call_id="escaped"),
        content=original,
    ))
    toolkit = SpillArtifactToolkit(
        store,
        owner=OWNER,
        max_inline_bytes=1024,
    )
    tool = toolkit.get_tool(SPILL_READ_TOOL_NAME)
    reconstructed: list[str] = []
    offset = 0

    while True:
        result = await tool._arun(
            locator=reference.locator,
            offset=offset,
            max_bytes=4096,
        )
        assert result.success is True
        assert len(result.model_dump_json().encode("utf-8")) <= 1024
        page = result.data
        reconstructed.append(page["content"])
        assert page["next_byte"] > offset or page["eof"]
        offset = page["next_byte"]
        if page["eof"]:
            break

    assert "".join(reconstructed) == original


@pytest.mark.asyncio
async def test_sse_exposes_typed_spill_reference_without_path_sanitizer_damage():
    interceptor = SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    )
    private_preview = (
        "Authorization: Bearer private-token /Users/alice/private.csv\n" * 40
    )
    transformed = await interceptor.result(_context(), _message(private_preview))
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="plugin",
        function_name="large_tool",
        function_args={},
        function_result=projected_tool_artifact(transformed),
        presentation={"kind": "log", "title": "Extension card"},
    )

    mapped = await ToolSSEEvent.from_event_async(event)

    assert mapped.data.spill is not None
    assert mapped.data.spill.status == "stored"
    assert mapped.data.spill.reference is not None
    assert mapped.data.spill.reference.locator == (
        "spill://artifact/0123456789abcdef0123456789abcdef"
    )
    assert "/Users/alice" not in mapped.data.spill.preview
    assert "private-token" not in mapped.data.spill.preview
    assert "[protected path]" in mapped.data.spill.preview
    assert "[redacted credential]" in mapped.data.spill.preview
    assert mapped.data.presentation is None


@pytest.mark.asyncio
async def test_runner_sanitizes_spill_preview_before_redis_and_mongo_persistence():
    interceptor = SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=300,
    )
    private_output = (
        "Bearer bare-token Authorization: Basic cHJpdmF0ZQ== "
        "api_key=private-key /Users/alice/private.csv "
        "https://example.test/docs /home/ubuntu/output/report.csv\n"
    ) * 30
    transformed = await interceptor.result(_context(), _message(private_output))
    raw_projection = projected_tool_artifact(transformed)
    original_notice = spill_notice_from_result(raw_projection)
    assert original_notice is not None
    assert "private-key" in original_notice.preview

    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="file",
        function_name="large_tool",
        function_args={
            "path": "/Users/alice/input.csv",
            "authorization": "Bearer argument-token",
        },
        function_result=raw_projection,
        tool_content=FileToolContent(content=(
            f"{original_notice.preview}\n"
            "spill://artifact/0123456789abcdef0123456789abcdef"
        )),
        presentation={
            "kind": "log",
            "description": "token=card-token /Users/alice/card.txt",
        },
    )

    class Repository:
        def __init__(self):
            self.persisted = None

        async def reserve_event_sequence(self, session_id, durable_event):
            assert session_id == OWNER.session_id
            durable_event.seq = 1
            return 1

        async def add_event(self, session_id, durable_event):
            assert session_id == OWNER.session_id
            self.persisted = durable_event.model_dump_json()

    class Queue:
        def __init__(self):
            self.payload = None

        async def put(self, payload):
            self.payload = payload
            return "redis-event-1"

    repository = Repository()
    queue = Queue()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id = OWNER.session_id
    runner._agent_id = "agent-1"
    runner._session_repository = repository

    await runner._put_and_add_event(
        SimpleNamespace(output_stream=queue),
        event,
    )

    for serialized in (queue.payload, repository.persisted):
        assert serialized is not None
        assert "bare-token" not in serialized
        assert "cHJpdmF0ZQ" not in serialized
        assert "private-key" not in serialized
        assert "argument-token" not in serialized
        assert "card-token" not in serialized
        assert "/Users/alice" not in serialized
        assert "[redacted credential]" in serialized
        assert "[protected path]" in serialized
        assert "https://example.test/docs" in serialized
        assert "/home/ubuntu/output/report.csv" in serialized
        assert "spill://artifact/0123456789abcdef0123456789abcdef" in serialized

    # Persistence uses a cloned projection and does not change the current
    # invocation's raw tool result.
    assert "private-key" in original_notice.preview


@pytest.mark.asyncio
async def test_event_size_guard_preserves_spill_locator_when_arguments_are_huge():
    transformed = await SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    ).result(_context(), _message("large output " * 200))
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="plugin",
        function_name="large_tool",
        function_args={"payload": "x" * (AgentTaskRunner.MAX_EVENT_PAYLOAD_BYTES + 100_000)},
        function_result=projected_tool_artifact(transformed),
        presentation={
            "kind": "log",
            "data": "y" * (AgentTaskRunner.MAX_EVENT_PAYLOAD_BYTES + 100_000),
        },
    )
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"

    durable = runner._durable_event_projection(event)
    bounded = runner._bound_event_payload(durable)
    notice = spill_notice_from_result(bounded.function_result)

    assert isinstance(bounded, ToolEvent)
    assert notice is not None
    assert notice.reference is not None
    assert notice.reference.locator == (
        "spill://artifact/0123456789abcdef0123456789abcdef"
    )
    assert bounded.presentation is None
    assert runner._event_payload_size(bounded) <= runner.MAX_EVENT_PAYLOAD_BYTES


@pytest.mark.asyncio
async def test_sse_sanitizes_untrusted_spill_reference_display_strings():
    reference = SpillArtifactRef(
        locator="spill://artifact/0123456789abcdef0123456789abcdef",
        byte_count=2048,
        sha256="a" * 64,
        media_type="text/plain; path=/Users/alice/private.txt",
        retrieval_hint="Bearer private-token; read /Users/alice/private.txt",
    )
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="plugin",
        function_name="large_tool",
        function_args={},
        function_result=ToolResult(
            success=True,
            data={
                "spill": {
                    "schema_version": 1,
                    "status": "stored",
                    "reference": reference.model_dump(mode="python"),
                    "preview": "Authorization: Bearer preview-token /Users/alice/data.csv",
                    "original_bytes": 2048,
                    "retained_bytes": 64,
                    "omitted_bytes": 1984,
                },
            },
        ),
    )

    mapped = await ToolSSEEvent.from_event_async(event)
    serialized = mapped.model_dump_json()

    assert mapped.data.spill is not None
    assert mapped.data.spill.reference is not None
    assert mapped.data.spill.reference.locator == reference.locator
    assert "private-token" not in serialized
    assert "preview-token" not in serialized
    assert "/Users/alice" not in serialized
    assert "[redacted credential]" in serialized
    assert "[protected path]" in serialized


@pytest.mark.asyncio
async def test_followup_context_replays_only_opaque_spill_reference():
    interceptor = SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    )
    secret = "Authorization: Bearer private-token /Users/alice/private.csv\n" * 40
    transformed = await interceptor.result(_context(), _message(secret))
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="plugin",
        function_name="large_tool",
        function_args={},
        function_result=projected_tool_artifact(transformed),
    )

    flow = PlanActFlow.__new__(PlanActFlow)
    payload = json.loads(flow._render_session_context([event]))

    assert payload["spill_artifacts"] == [{
        "locator": "spill://artifact/0123456789abcdef0123456789abcdef",
        "byte_count": len(secret.encode("utf-8")),
        "sha256": "a" * 64,
        "media_type": "application/json",
    }]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private-token" not in serialized
    assert "/Users/alice" not in serialized


@pytest.mark.asyncio
async def test_spilled_failed_shell_result_stays_failed_in_console_projection():
    interceptor = SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    )
    transformed = await interceptor.result(
        _context("shell_run"),
        _message("failure output" * 200, success=False),
    )
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="shell",
        function_name="shell_run",
        function_args={"command": "bounded-command"},
        function_result=projected_tool_artifact(transformed),
    )

    console = AgentTaskRunner._completed_shell_console_from_result(event)

    assert console is not None
    assert console[0]["status"] == "failed"
    assert console[0]["returncode"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("function_name,renderer", [
    ("dataset_analysis_run", AgentTaskRunner._dataset_analysis_console),
    ("dataset_quicklook", AgentTaskRunner._dataset_quicklook_console),
])
@pytest.mark.parametrize("success", [True, False])
async def test_dataset_spill_console_preserves_status_without_raw_preview(
    function_name,
    renderer,
    success,
):
    interceptor = SpillArtifactInterceptor(
        CapturingStore(),
        owner=OWNER,
        max_inline_bytes=1024,
        preview_bytes=200,
    )
    private_preview = "Authorization: Bearer private-token /Users/alice/data.csv\n" * 40
    transformed = await interceptor.result(
        _context("shell_run"),
        _message(private_preview, success=success),
    )
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="shell",
        function_name=function_name,
        function_args={},
        function_result=projected_tool_artifact(transformed),
    )

    console = renderer(event)

    assert console[0]["status"] == ("completed" if success else "failed")
    assert console[0]["returncode"] == (0 if success else 1)
    assert "private-token" not in console[0]["output"]
    assert "/Users/alice" not in console[0]["output"]
    assert "spill://artifact/" not in console[0]["output"]
