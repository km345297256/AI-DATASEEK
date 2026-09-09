"""Storage replacement must not destroy an already verified repair result."""
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.file import FileInfo
from app.domain.services.agent_task_runner import AgentTaskRunner


PATH = "/home/ubuntu/output/experiment/result.csv"


def storage_runner(*, pinned=False, upload_fails=False):
    original, repaired = b"value\n10\n", b"value\n42\n"
    original_digest = hashlib.sha256(original).hexdigest()
    old = FileInfo(file_id="verified-object", filename="result.csv", file_path=PATH,
                   size=len(original), metadata={"source": "sandbox_artifact", "session_id": "session",
                       "artifact_size": len(original), "artifact_sha256": original_digest})
    objects = {old.file_id: original}
    files = {old.file_id: old}

    async def upload(stream, filename, user_id, *, metadata):
        assert user_id == "owner"
        if upload_fails:
            raise OSError("storage unavailable")
        body = stream.read()
        objects["replacement-object"] = body
        return FileInfo(file_id="replacement-object", filename=filename, size=len(body), metadata=metadata)

    async def delete(file_id, user_id):
        assert user_id == "owner"
        del objects[file_id]
        return True

    async def add(session_id, info):
        assert session_id == "session"
        files[info.file_id] = info

    async def remove(session_id, file_id):
        assert session_id == "session"
        del files[file_id]

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "agent", "session", "owner"
    runner._analysis_verified_files = {"other-step": {PATH: old}} if pinned else {}
    runner._sandbox = SimpleNamespace(file_download=AsyncMock(side_effect=lambda _path: io.BytesIO(repaired)))
    runner._session_repository = SimpleNamespace(get_file_by_path=AsyncMock(return_value=old),
        add_file=AsyncMock(side_effect=add), remove_file=AsyncMock(side_effect=remove))
    runner._file_storage = SimpleNamespace(upload_file=AsyncMock(side_effect=upload), delete_file=AsyncMock(side_effect=delete))
    runner._artifact_fingerprints = {PATH: (len(original), original_digest)}
    runner._artifact_baseline_paths = {PATH}
    return runner, old, objects, files, repaired


@pytest.mark.asyncio
@pytest.mark.parametrize("preloaded", [False, True])
async def test_pinned_path_in_any_step_preserves_original_object_without_read_or_replace(preloaded):
    runner, old, objects, files, repaired = storage_runner(pinned=True)
    kwargs = ({"file_data": io.BytesIO(repaired),
               "fingerprint": (len(repaired), hashlib.sha256(repaired).hexdigest())} if preloaded else {})
    result = await runner._sync_file_to_storage(PATH, **kwargs)

    assert result is old
    assert objects[result.file_id] == b"value\n10\n"  # The returned link remains readable.
    assert files == {old.file_id: old}
    runner._sandbox.file_download.assert_not_awaited()
    runner._session_repository.get_file_by_path.assert_not_awaited()
    runner._session_repository.add_file.assert_not_awaited()
    runner._session_repository.remove_file.assert_not_awaited()
    runner._file_storage.upload_file.assert_not_awaited()
    runner._file_storage.delete_file.assert_not_awaited()
    assert runner._artifact_fingerprints[PATH] == (old.size, old.metadata["artifact_sha256"])


@pytest.mark.asyncio
async def test_unpinned_failed_artifact_can_be_replaced_by_its_repaired_version():
    runner, old, objects, files, repaired = storage_runner()
    result = await runner._sync_file_to_storage(PATH)

    assert result.file_id == "replacement-object"
    assert objects == {result.file_id: repaired}
    assert files == {result.file_id: result}
    assert result.metadata["artifact_sha256"] == hashlib.sha256(repaired).hexdigest()
    assert runner._artifact_fingerprints[PATH] == (len(repaired), hashlib.sha256(repaired).hexdigest())
    runner._sandbox.file_download.assert_awaited_once_with(PATH)
    runner._file_storage.upload_file.assert_awaited_once()
    runner._session_repository.remove_file.assert_awaited_once_with("session", old.file_id)
    runner._file_storage.delete_file.assert_awaited_once_with(old.file_id, "owner")


@pytest.mark.asyncio
async def test_unpinned_replacement_upload_failure_keeps_previous_object_and_reference():
    runner, old, objects, files, _ = storage_runner(upload_fails=True)
    result = await runner._sync_file_to_storage(PATH)

    assert result is None
    assert objects == {old.file_id: b"value\n10\n"}
    assert files == {old.file_id: old}
    runner._session_repository.remove_file.assert_not_awaited()
    runner._file_storage.delete_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_pin_for_another_path_does_not_freeze_repairable_file():
    runner, old, objects, _, repaired = storage_runner()
    runner._analysis_verified_files = {"step": {"/home/ubuntu/output/keep.png": old}}
    result = await runner._sync_file_to_storage(PATH)
    assert objects[result.file_id] == repaired
    runner._file_storage.upload_file.assert_awaited_once()
