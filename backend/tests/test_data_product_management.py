from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.errors.exceptions import BadRequestError
from app.application.services import data_product_service as module
from app.application.services.data_product_service import DataProductService
from app.domain.models.file import FileInfo


@pytest.fixture
def product_env(monkeypatch):
    item = SimpleNamespace(
        name="Original", description="Original description", created_by="owner",
        owner_id="owner", generation_method="agent_tool", directories=[],
        files=[
            {"file_id": "a", "relative_path": "a.csv", "filename": "a.csv", "is_primary": True},
            {"file_id": "b", "relative_path": "b.csv", "filename": "b.csv", "is_primary": False},
        ],
        save=AsyncMock(),
    )
    item.to_domain = lambda: item
    documents = SimpleNamespace(find_one=AsyncMock(return_value=item))
    storage = SimpleNamespace(download_file=AsyncMock(), upload_file=AsyncMock())
    monkeypatch.setattr(module, "DataProductDocument", documents)
    return SimpleNamespace(item=item, service=DataProductService(storage), storage=storage)


async def update(env, *, files=None, directories=None):
    return await env.service.update_metadata(
        "dp_test", "owner", "Renamed", "Description", "manual", "owner",
        directories or [], files if files is not None else deepcopy(env.item.files),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/private/report.csv", r"C:\private\report.csv", "C:report.csv", "../report.csv", "a//b.csv", "a/./b.csv", ".", "report\x00.csv"])
@pytest.mark.parametrize("target", ["file", "directory"])
async def test_metadata_rejects_unsafe_paths_without_mutating_document(product_env, path, target):
    files = deepcopy(product_env.item.files)
    directories = []
    if target == "file":
        files[0]["relative_path"] = path
    else:
        directories = [path]
    with pytest.raises(BadRequestError):
        await update(product_env, files=files, directories=directories)
    assert product_env.item.name == "Original"
    assert product_env.item.files[0]["relative_path"] == "a.csv"
    product_env.item.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["duplicate_id", "duplicate_path", "file_is_parent", "file_is_directory", "file_is_directory_parent", "two_primary"])
async def test_metadata_rejects_ambiguous_file_layout(product_env, case):
    files = deepcopy(product_env.item.files)
    directories = []
    if case == "duplicate_id":
        files[1]["file_id"] = "a"
    elif case == "duplicate_path":
        files[1]["relative_path"] = "a.csv"
    elif case == "file_is_parent":
        files[1]["relative_path"] = "a.csv/b.csv"
    elif case == "file_is_directory":
        directories = ["a.csv"]
    elif case == "file_is_directory_parent":
        directories = ["a.csv/nested"]
    else:
        files[1]["is_primary"] = True
    with pytest.raises(BadRequestError):
        await update(product_env, files=files, directories=directories)
    product_env.item.save.assert_not_awaited()
    assert product_env.item.name == "Original"


@pytest.mark.asyncio
async def test_metadata_keeps_valid_unicode_nested_paths_and_file_identity(product_env):
    files = deepcopy(product_env.item.files)
    files[0].update(relative_path="结果/测量.csv", filename="untrusted-name.csv", size=123)
    result = await update(product_env, files=files, directories=["empty", "empty"])
    assert result.files[0]["relative_path"] == "结果/测量.csv"
    assert result.files[0]["filename"] == "a.csv" and "size" not in result.files[0]
    assert result.directories == ["empty", "结果"]
    assert result.name == "Renamed"
    result.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_creation_rejects_duplicate_selection_before_copying_files(product_env):
    with pytest.raises(BadRequestError, match="only once"):
        await product_env.service.create(
            dataset_id="ds_test", session_id="session", user_id="owner",
            name="Product", description="", generation_method="agent_tool",
            selected_file_ids=["a", "a"], primary_file_id="a", files=[],
        )
    product_env.storage.download_file.assert_not_awaited()
    product_env.storage.upload_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("filenames", [["data.csv", "data.csv"], ["a", "a/data.csv"], ["C:/private.csv"]])
async def test_creation_rejects_unsafe_or_colliding_paths_before_storage(product_env, filenames):
    files = [FileInfo(file_id=str(index), filename=name) for index, name in enumerate(filenames)]
    with pytest.raises(BadRequestError):
        await product_env.service.create(
            dataset_id="ds_test", session_id="session", user_id="owner",
            name="Product", description="", generation_method="agent_tool",
            selected_file_ids=[file.file_id for file in files], primary_file_id=None, files=files,
        )
    product_env.storage.download_file.assert_not_awaited()
    product_env.storage.upload_file.assert_not_awaited()
