from datetime import datetime, UTC

import pytest

from app.application.services.file_preview import CSV_PAGE_BYTES, TEXT_PAGE_BYTES, PreviewVersionChanged
from app.application.services.file_service import FileService
from app.domain.models.file import FileInfo


class RangeStorage:
    def __init__(self, data: bytes, filename="data.csv", metadata=None):
        self.data = data
        self.info = FileInfo(file_id="opaque-file", filename=filename, size=len(data), user_id="owner", upload_date=datetime(2026, 9, 8, tzinfo=UTC), metadata=metadata or {})
        self.ranges = []
        self.full_downloads = 0

    async def get_file_info(self, file_id, user_id):
        return self.info if user_id == "owner" and file_id == "opaque-file" else None

    async def download_file_range(self, file_id, user_id, *, offset, length):
        assert user_id == "owner"
        self.ranges.append((offset, length))
        return self.data[offset:offset + length], self.info

    async def download_file(self, *args):
        self.full_downloads += 1
        raise AssertionError("Full downloads are forbidden in previews")


@pytest.mark.asyncio
async def test_large_text_reads_one_range_and_preserves_split_utf8():
    text = "a" * (TEXT_PAGE_BYTES - 1) + "世界" + "tail" * 50000
    storage = RangeStorage(text.encode(), "large.txt")
    service = FileService(storage)
    pieces = []
    offset = 0
    version = None
    while True:
        page = await service.preview_file("opaque-file", "owner", offset=offset, version=version)
        pieces.append(page.text)
        version = page.version
        assert page.bytes_read <= TEXT_PAGE_BYTES
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert "".join(pieces) == text
    assert storage.full_downloads == 0
    assert all(length <= TEXT_PAGE_BYTES for _, length in storage.ranges)


@pytest.mark.asyncio
async def test_csv_pages_preserve_quoted_multiline_escaped_quotes_and_utf8():
    records = ['name,note\r\n'] + [f'世界{i},"line one\r\nline ""two"""\r\n' for i in range(255)]
    storage = RangeStorage(('\ufeff' + ''.join(records)).encode())
    service = FileService(storage)
    rows = []
    offset = 0
    version = None
    while True:
        page = await service.preview_file("opaque-file", "owner", offset=offset, mode="csv", version=version)
        if offset == 0:
            assert page.headers == ["name", "note"]
        else:
            assert page.headers == []
        assert len(page.rows) <= 100
        rows.extend(page.rows)
        version = page.version
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert rows == [[f"世界{i}", 'line one\r\nline "two"'] for i in range(255)]
    assert storage.full_downloads == 0
    assert all(length <= CSV_PAGE_BYTES for _, length in storage.ranges)


@pytest.mark.asyncio
async def test_csv_does_not_consume_partial_record_at_byte_boundary():
    first_record = 'x,"' + '世' * 22000 + '\ninside"\r\n'
    data = ('name,note\r\n' + first_record * 3).encode()
    storage = RangeStorage(data)
    service = FileService(storage)
    page = await service.preview_file("opaque-file", "owner", mode="csv")
    assert len(page.rows) == 1
    assert page.next_offset == len(('name,note\r\n' + first_record).encode())
    next_page = await service.preview_file("opaque-file", "owner", mode="csv", offset=page.next_offset, version=page.version)
    assert len(next_page.rows) == 1
    assert page.rows == next_page.rows


@pytest.mark.asyncio
async def test_oversized_csv_record_fails_bounded_instead_of_reading_whole_file():
    storage = RangeStorage(b'"' + b'x' * (CSV_PAGE_BYTES * 2) + b'"\n')
    with pytest.raises(ValueError, match="exceeds"):
        await FileService(storage).preview_file("opaque-file", "owner", mode="csv")
    assert storage.ranges == [(0, CSV_PAGE_BYTES)]
    assert storage.full_downloads == 0


@pytest.mark.asyncio
async def test_preview_hides_missing_foreign_and_private_spill_files():
    storage = RangeStorage(b"secret", metadata={"source": "tool_output_spill"})
    service = FileService(storage)
    for file_id, user in [("opaque-file", "owner"), ("opaque-file", "other"), ("missing", "owner")]:
        with pytest.raises(FileNotFoundError):
            await service.preview_file(file_id, user)
    assert storage.ranges == []


@pytest.mark.asyncio
async def test_revision_mismatch_fails_before_range_download():
    storage = RangeStorage(b"first")
    service = FileService(storage)
    first = await service.preview_file("opaque-file", "owner")
    storage.info = storage.info.model_copy(update={"size": 99})
    with pytest.raises(PreviewVersionChanged):
        await service.preview_file("opaque-file", "owner", version=first.version)
    assert len(storage.ranges) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data,filename,expected", [(b"x\ty\n1\t2", "data.tsv", ["1", "2"]), (b"x\ty\n1\t2", "data.csv", ["1", "2"]), (b'"x,extra,comma"\ty\n1\t2', "data.csv", ["1", "2"]), (b"x,y\n1,2", "data.csv", ["1", "2"])])
async def test_tab_and_comma_dialects(data, filename, expected):
    page = await FileService(RangeStorage(data, filename)).preview_file("opaque-file", "owner", mode="csv")
    assert page.rows == [expected]


@pytest.mark.asyncio
async def test_csv_columns_are_capped_without_losing_record_boundary():
    data = (','.join(str(i) for i in range(70)) + '\n') * 3
    page = await FileService(RangeStorage(data.encode())).preview_file("opaque-file", "owner", mode="csv")
    assert len(page.headers) == 50
    assert all(len(row) == 50 for row in page.rows)
    assert page.columns_truncated
    assert page.next_offset is None


@pytest.mark.asyncio
@pytest.mark.parametrize("data,mode", [(b"", "text"), (b"", "csv"), (b"a,b\n", "csv")])
async def test_empty_and_header_only_files_finish_without_loop(data, mode):
    page = await FileService(RangeStorage(data)).preview_file("opaque-file", "owner", mode=mode)
    assert page.next_offset is None
    assert page.rows == []


@pytest.mark.asyncio
async def test_invalid_utf8_and_unclosed_csv_are_explicit_errors():
    for data, mode in [(b"\xff", "text"), (b'x,"unclosed', "csv")]:
        with pytest.raises(ValueError):
            await FileService(RangeStorage(data)).preview_file("opaque-file", "owner", mode=mode)


@pytest.mark.asyncio
async def test_range_metadata_drift_or_short_read_is_rejected():
    storage = RangeStorage(b"data")
    async def changed(*args, **kwargs):
        return b"data", storage.info.model_copy(update={"size": 5})
    storage.download_file_range = changed
    with pytest.raises(PreviewVersionChanged):
        await FileService(storage).preview_file("opaque-file", "owner")
    async def short(*args, **kwargs):
        return b"da", storage.info
    storage.download_file_range = short
    with pytest.raises(ValueError, match="Incomplete"):
        await FileService(storage).preview_file("opaque-file", "owner")


@pytest.mark.asyncio
async def test_csv_crlf_split_does_not_create_phantom_record_or_drop_bytes():
    header = b"name,note\r\n"
    # The first range ends on CR. The complete long record fits in the next
    # bounded request once the header has been consumed.
    record = b"x," + b"z" * (CSV_PAGE_BYTES - len(header) - 3) + b"\r\n"
    storage = RangeStorage(header + record + b"t,v\n")
    service = FileService(storage)
    first = await service.preview_file("opaque-file", "owner", mode="csv")
    assert first.rows == []
    assert first.next_offset == len(header)
    second = await service.preview_file("opaque-file", "owner", mode="csv", offset=first.next_offset, version=first.version)
    assert second.rows[0] == ["x", "z" * (CSV_PAGE_BYTES - len(header) - 3)]
    assert second.rows[1:] == [["t", "v"]]


@pytest.mark.asyncio
async def test_storage_without_ranges_never_uses_full_download():
    storage = RangeStorage(b"data")
    storage.download_file_range = None
    with pytest.raises(NotImplementedError):
        await FileService(storage).preview_file("opaque-file", "owner")
    assert storage.full_downloads == 0


@pytest.mark.asyncio
async def test_literal_quotes_in_unquoted_fields_follow_python_csv_semantics():
    storage = RangeStorage(b'a,b\nhello"world,x\nnext,value\n')
    page = await FileService(storage).preview_file("opaque-file", "owner", mode="csv")
    assert page.headers == ["a", "b"]
    assert page.rows == [['hello"world', 'x'], ['next', 'value']]
    assert page.next_offset is None


@pytest.mark.asyncio
async def test_leading_blank_window_preserves_header_and_reinfers_delimiter():
    storage = RangeStorage(b'\n' * CSV_PAGE_BYTES + '名称\tvalue\nworld\t1\n'.encode())
    service = FileService(storage)
    first = await service.preview_file("opaque-file", "owner", mode="csv")
    assert first.header_pending
    assert first.rows == first.headers == []
    second = await service.preview_file("opaque-file", "owner", mode="csv", offset=first.next_offset, version=first.version, header_pending=first.header_pending)
    assert not second.header_pending
    assert second.headers == ["名称", "value"]
    assert second.rows == [["world", "1"]]


@pytest.mark.asyncio
async def test_preview_http_contract_authorization_validation_and_private_paths():
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import json
    from pathlib import Path
    from app.application.services.visualization_catalog import VisualizationCatalogService
    from app.domain.models.visualization import VisualizationSnapshot
    from app.interfaces.api.file_routes import router
    from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog
    from app.interfaces.errors.exception_handlers import register_exception_handlers

    storage = RangeStorage(b"name,value\nworld,1\n", filename="/private/host/secret/data.csv")
    storage.info.file_path = "/private/host/secret/data.csv"
    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    app.dependency_overrides[get_file_service] = lambda: FileService(storage)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    manifest = Path(__file__).resolve().parents[2] / "plugin-host/visualizations/csv.json"
    snapshot = VisualizationSnapshot(engine="cordis", revision="a"*64, plugins=[json.loads(manifest.read_text())])
    catalog = VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)),
        SimpleNamespace(get_states=AsyncMock(return_value={})))
    app.dependency_overrides[get_visualization_catalog] = lambda: catalog
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"plugin_id": "csv", "operation": "page", "options": {}}
        response = await client.post("/files/opaque-file/visualization", json=body)
        assert response.status_code == 200
        assert response.json()["data"]["kind"] == "page"
        assert response.json()["data"]["payload"]["rows"] == [["world", "1"]]
        assert "/private/" not in response.text
        changed = await client.post("/files/opaque-file/visualization", json={**body, "version": "0"*64})
        assert changed.status_code == 409
        for query in [{"offset": -1}, {"mode": "binary"}, {"delimiter": "evil"}]:
            invalid = await client.post("/files/opaque-file/visualization", json={**body, "options": query})
            assert invalid.status_code == 422
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="other")
        foreign = await client.post("/files/opaque-file/visualization", json=body)
        missing = await client.post("/files/missing/visualization", json=body)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
        storage.info.metadata = {"source": "tool_output_spill"}
        hidden = await client.post("/files/opaque-file/visualization", json=body)
        assert hidden.status_code == 404
        assert hidden.json() == missing.json()
    assert storage.full_downloads == 0
