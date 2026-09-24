"""The trusted zero-match response must never follow a destination write."""
from unittest.mock import AsyncMock

import pytest

from app.services.file import FileService


@pytest.mark.asyncio
async def test_zero_match_returns_before_any_write_and_preserves_original_file(tmp_path):
    path = tmp_path / "analysis.py"
    original = "print('原始内容')\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat()
    service = FileService()
    service.write_file = AsyncMock(side_effect=AssertionError("zero matches must not write"))

    result = await service.str_replace(str(path), "missing text", "replacement")

    assert result.file == str(path) and result.replaced_count == 0
    service.write_file.assert_not_awaited()
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.asyncio
async def test_zero_match_http_contract_keeps_failure_and_exact_file_identity(tmp_path, monkeypatch):
    from app.api.v1 import file as file_api
    from app.schemas.file import FileReplaceRequest

    path = tmp_path / "script.py"
    path.write_text("original", encoding="utf-8")
    service = FileService()
    service.write_file = AsyncMock(side_effect=AssertionError("no mutation may be attempted"))
    monkeypatch.setattr(file_api, "file_service", service)

    result = await file_api.replace_in_file(FileReplaceRequest(
        file=str(path), old_str="not-present", new_str="replacement"))

    assert result.success is False
    assert result.data == {"file": str(path), "replaced_count": 0}
    service.write_file.assert_not_awaited()
