from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.models.file import FileInfo
from app.interfaces.api import dataset_routes


@pytest.fixture
def download_env(monkeypatch):
    payload = b"year,value\n2024,1\n" * 100_000
    info = FileInfo(file_id="file-a", filename="data.csv", size=len(payload), user_id="owner")
    product = SimpleNamespace(
        product_id="dp_test", dataset_id="ds_test", version=1,
        files=[SimpleNamespace(file_id="file-a", relative_path="tables/data.csv")],
    )
    archives = []
    original_spool = dataset_routes.SpooledTemporaryFile

    def spool(*args, **kwargs):
        value = original_spool(*args, **kwargs)
        archives.append(value)
        return value

    async def read_range(file_id, user_id, *, offset, length):
        assert file_id == "file-a" and user_id == "owner"
        assert 0 < length <= 1024 * 1024
        return payload[offset:offset + length], info

    storage = SimpleNamespace(
        download_file=AsyncMock(side_effect=AssertionError("Unbounded object download")),
        download_file_range=AsyncMock(side_effect=read_range),
    )
    monkeypatch.setattr(dataset_routes, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(dataset_routes, "DataCenterDatasetService", lambda: SimpleNamespace(get_dataset=AsyncMock()))
    monkeypatch.setattr(dataset_routes, "DataProductService", lambda: SimpleNamespace(get=AsyncMock(return_value=product)))
    monkeypatch.setattr(dataset_routes, "get_file_storage", lambda: storage)
    return SimpleNamespace(product=product, storage=storage, archives=archives, payload=payload, info=info)


async def download():
    return await dataset_routes.download_dataset_data_product("ds_test", "dp_test", SimpleNamespace(id="owner"))


@pytest.mark.asyncio
async def test_product_download_uses_bounded_ranges_and_valid_archive(download_env, monkeypatch):
    event_thread = threading.get_ident()
    original_write = zipfile._ZipWriteFile.write
    write_threads = []

    def write(self, data):
        write_threads.append(threading.get_ident())
        return original_write(self, data)

    monkeypatch.setattr(zipfile._ZipWriteFile, "write", write)
    response = await download()
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks and all(len(chunk) <= 1024 * 1024 for chunk in chunks)
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        assert archive.namelist() == ["tables/data.csv"]
        assert archive.read("tables/data.csv") == download_env.payload
    assert download_env.storage.download_file_range.await_count == 2
    assert write_threads and event_thread not in write_threads
    await response.background()
    assert all(archive.closed for archive in download_env.archives)


@pytest.mark.asyncio
async def test_product_download_closes_archive_when_storage_fails(download_env):
    download_env.storage.download_file_range.side_effect = FileNotFoundError("missing")
    with pytest.raises(NotFoundError):
        await download()
    assert download_env.archives and all(archive.closed for archive in download_env.archives)


@pytest.mark.asyncio
async def test_product_download_rejects_truncated_object_and_closes_archive(download_env):
    download_env.storage.download_file_range.side_effect = None
    download_env.storage.download_file_range.return_value = (b"short", download_env.info)
    with pytest.raises(HTTPException) as exc:
        await download()
    assert exc.value.status_code == 409
    assert all(archive.closed for archive in download_env.archives)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../escape.csv", "/absolute.csv", r"..\escape.csv", "C:/escape.csv", "C:escape.csv", "a//b.csv", ".", "a\x00.csv"])
async def test_product_download_rejects_unsafe_archive_members_before_storage(download_env, path):
    download_env.product.files[0].relative_path = path
    with pytest.raises(BadRequestError):
        await download()
    download_env.storage.download_file.assert_not_called()
    download_env.storage.download_file_range.assert_not_called()


@pytest.mark.asyncio
async def test_product_download_rejects_duplicate_archive_names_before_storage(download_env):
    download_env.product.files.append(SimpleNamespace(file_id="file-b", relative_path="tables/data.csv"))
    with pytest.raises(BadRequestError):
        await download()
    download_env.storage.download_file_range.assert_not_called()


@pytest.mark.asyncio
async def test_product_download_closes_archive_on_cancellation(download_env):
    entered = asyncio.Event()

    async def read_range(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    download_env.storage.download_file_range.side_effect = read_range
    task = asyncio.create_task(download())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert download_env.archives and all(archive.closed for archive in download_env.archives)


@pytest.mark.asyncio
async def test_product_download_rejects_object_revision_change(download_env):
    first = download_env.payload[:1024 * 1024]
    remaining = download_env.payload[1024 * 1024:]
    revised = download_env.info.model_copy(update={"metadata": {"content_sha256": "changed"}})
    download_env.storage.download_file_range.side_effect = [(first, download_env.info), (remaining, revised)]
    with pytest.raises(HTTPException) as exc:
        await download()
    assert exc.value.status_code == 409
    assert all(archive.closed for archive in download_env.archives)


@pytest.mark.asyncio
async def test_product_download_rejects_file_directory_conflict(download_env):
    download_env.product.files.append(SimpleNamespace(file_id="file-b", relative_path="tables/data.csv/child.csv"))
    with pytest.raises(BadRequestError):
        await download()
    download_env.storage.download_file_range.assert_not_called()


@pytest.mark.asyncio
async def test_product_download_preserves_empty_file(download_env):
    empty_info = download_env.info.model_copy(update={"size": 0})
    download_env.storage.download_file_range.side_effect = None
    download_env.storage.download_file_range.return_value = (b"", empty_info)
    response = await download()
    chunks = [chunk async for chunk in response.body_iterator]
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        assert archive.read("tables/data.csv") == b""
    await response.background()


@pytest.mark.asyncio
async def test_cancellation_waits_for_active_compression_before_closing(download_env, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original_write = zipfile._ZipWriteFile.write
    closed_during_write = []

    def write(self, data):
        started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("Test writer was not released")
        closed_during_write.append(download_env.archives[0].closed)
        return original_write(self, data)

    monkeypatch.setattr(zipfile._ZipWriteFile, "write", write)
    task = asyncio.create_task(download())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not download_env.archives[0].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed_during_write == [False]
    assert download_env.archives[0].closed
