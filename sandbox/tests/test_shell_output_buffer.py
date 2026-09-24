import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.exceptions import BadRequestException
from app.models.shell import ConsoleRecord
from app.schemas.shell import ShellViewRequest
from app.services.shell import ShellService
from app.services.shell_output import ShellOutputBuffer


def read_all(buffer, size=8192):
    cursor, parts = 0, []
    while cursor < buffer.total_bytes:
        text, page = buffer.read(cursor, size)
        assert not page["lossy"]
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
        parts.append(text)
    return "".join(parts)


@pytest.fixture
def buffer():
    value = ShellOutputBuffer()
    yield value
    value.close()


def test_large_output_memory_is_bounded_and_full_log_is_recoverable(buffer):
    text = "scientific 数据 😀\n" * 1000
    for _ in range(80):
        buffer.append(text)
    assert len(buffer.preview().encode()) <= buffer.preview_bytes
    assert len(buffer._head) + len(buffer._tail) < 2 * buffer.preview_bytes
    assert buffer.metadata()["preview_truncated"]
    assert buffer.metadata()["log_status"] == "available"
    assert read_all(buffer, 117) == text * 80


def test_preview_retains_first_and_final_error(buffer):
    buffer.append("START\n" + "x" * 100_000 + "\nTraceback: final failure")
    assert buffer.preview().startswith("START\n")
    assert buffer.preview().endswith("Traceback: final failure")
    assert "omitted" in buffer.preview()


def test_head_cut_never_skips_unicode_then_appends_later_text():
    buffer = ShellOutputBuffer(preview_bytes=256)
    try:
        text = "😀" * 400 + "later"
        for char in text:
            buffer.append(char)
        assert text.encode().startswith(buffer._head)
        assert read_all(buffer, 7) == text
    finally:
        buffer.close()


def test_model_and_observer_cursors_are_independent(buffer):
    buffer.append("abcdefghijklmnop")
    first = buffer.read(0, 4)
    assert buffer.read(0, 4) == first
    assert buffer.read(4, 4)[0] == "efgh"
    assert buffer.read(0, 4) == first
    buffer.append("tail")
    assert buffer.read(16, 4)[0] == "tail"


def test_utf8_pages_roundtrip_without_replacement_characters(buffer):
    buffer.append("中😀文\n" * 130)
    assert read_all(buffer, 4) == "中😀文\n" * 130
    text, page = buffer.read(1, 7)
    assert page["lossy"] and page["start"] == 3
    assert text == "😀"


def test_split_ansi_is_removed_only_from_display_log(buffer):
    for piece in ["A\x1b", "[31", "m中\x1b[0", "mB\x1b]0;secret title", "\x1b", "\\C"]:
        buffer.append(piece)
    assert read_all(buffer, 4) == "A中BC"
    assert buffer.preview() == "A中BC"
    buffer._raw_spool.seek(0)
    assert buffer._raw_spool.read().decode() == "A\x1b[31m中\x1b[0mB\x1b]0;secret title\x1b\\C"


def test_spool_files_are_anonymous_and_private(buffer):
    import os
    for spool in (buffer._spool, buffer._raw_spool):
        assert isinstance(spool.name, int)
        assert os.fstat(spool.fileno()).st_mode & 0o777 == 0o600
    assert not any("path" in key or "fd" in key for key in buffer.metadata())


def test_spool_creation_failure_is_explicit_and_tail_marks_lost_bytes(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private path must never be returned")
    monkeypatch.setattr("app.services.shell_output.tempfile.TemporaryFile", unavailable)
    buffer = ShellOutputBuffer(preview_bytes=256)
    buffer.append("x" * 2048)
    buffer.stream_complete = True
    text, page = buffer.read(0, 512)
    assert buffer.metadata()["log_status"] == "unavailable"
    assert page["lossy"] and page["source"] == "tail"
    assert page["start"] == 1792 and page["eof"]
    assert len(text) == 256


def test_spool_write_failure_keeps_bounded_output_and_never_claims_complete(buffer):
    buffer._spool.close()
    buffer.append("x" * 100_000)
    assert buffer.metadata()["log_status"] == "unavailable"
    assert len(buffer.preview().encode()) <= buffer.preview_bytes
    assert buffer.read(0, 8192)[1]["lossy"]


def test_per_log_storage_limit_does_not_stop_process_output():
    buffer = ShellOutputBuffer(max_log_bytes=1000)
    try:
        buffer.append("a" * 1000)
        assert buffer.metadata()["log_status"] == "available"
        buffer.append("more output")
        assert buffer.metadata()["log_status"] == "limit_exceeded"
        assert buffer.preview().endswith("more output")
        assert buffer.total_bytes == 1011
    finally:
        buffer.close()


def test_reader_eof_is_not_inferred_from_process_exit(buffer):
    buffer.append("tail")
    assert not buffer.read(0, 4)[1]["eof"]
    buffer.stream_complete = True
    assert buffer.read(0, 4)[1]["eof"]


@pytest.mark.parametrize("cursor,limit", [(True, 4), (-1, 4), (10, 4), (0, True), (0, 3), (0, 16385)])
def test_page_validation(buffer, cursor, limit):
    with pytest.raises(ValueError):
        buffer.read(cursor, limit)


@pytest.mark.parametrize("fields", [{"cursor": 0}, {"output_id": "a" * 32},
                                    {"cursor": True, "output_id": "a" * 32},
                                    {"cursor": 0, "output_id": "a" * 32, "max_bytes": 100_000}])
def test_view_schema_requires_generation_and_bounded_cursor(fields):
    with pytest.raises(ValidationError):
        ShellViewRequest(id="shell", **fields)


@pytest.fixture
def service():
    instance = ShellService()
    instance.active_shells = {}
    yield instance
    for shell in instance.active_shells.values():
        instance._retire_output(shell)


def register(service, output=""):
    process = SimpleNamespace(returncode=0)
    record = ConsoleRecord(ps1="$", command="synthetic", output="")
    shell = {"process": process, "output": "", "console": [record]}
    service.active_shells["test"] = shell
    service._append_process_output("test", process, record, output)
    return shell, process


@pytest.mark.asyncio
async def test_service_legacy_view_is_bounded_but_explicit_page_is_complete(service):
    shell, process = register(service, "x" * 100_000 + "last")
    view = await service.view_shell("test", console=True)
    assert view.output.endswith("last")
    assert view.console[0].output_truncated
    assert len(shell["output"].encode()) <= 32768
    assert len(shell["console"][0].output.encode()) <= 32768
    output_id = view.output_metadata["output_id"]
    page = await service.view_shell("test", output_id=output_id, cursor=0)
    assert page.output == "x" * 8192
    assert page.output_page["next_cursor"] == 8192
    with pytest.raises(BadRequestException):
        await service.view_shell("test", output_id="b" * 32, cursor=0)


def test_console_history_total_is_bounded(service):
    shell, process = register(service, "current")
    shell["console"] = [ConsoleRecord(ps1="$", command="x", output="y" * 30000) for _ in range(200)]
    service._bound_console(shell)
    assert len(shell["console"]) <= service.MAX_CONSOLE_RECORDS
    assert sum(len(item.model_dump_json().encode()) for item in shell["console"]) < 132000
    assert shell["console_truncated"]


def test_console_oversized_command_cannot_bypass_cap(service):
    shell, _ = register(service, "x")
    shell["console"][0].command = "a" * 200_000
    service._bound_console(shell)
    assert len(shell["console"][0].model_dump_json().encode()) < 132000
    assert shell["console_truncated"]


@pytest.mark.asyncio
async def test_service_final_drain_preserves_split_unicode_and_final_chunk(service):
    shell, process = register(service)
    data = "开始\x1b[31m错误\x1b[0m😀".encode()
    chunks = [data[:1], data[1:8], data[8:-1], data[-1:], b""]
    async def read(size):
        return chunks.pop(0)
    process.stdout = SimpleNamespace(read=read)
    await service._start_output_reader("test", process)
    view = await service.view_shell("test")
    assert view.output == "开始错误😀"
    assert view.output_metadata["stream_complete"]
    page = await service.view_shell("test", output_id=view.output_metadata["output_id"], cursor=0)
    assert page.output_page["eof"]


@pytest.mark.asyncio
async def test_retirement_closes_private_files_and_cancels_stuck_reader(service):
    shell, _ = register(service, "first")
    buffer = shell["output_buffer"]
    files = [buffer._spool, buffer._raw_spool]
    reader = shell["reader_task"] = asyncio.create_task(asyncio.sleep(10))
    service._retire_output(shell)
    await asyncio.gather(reader, return_exceptions=True)
    assert reader.cancelled()
    assert all(item.closed for item in files)
    assert buffer.metadata()["log_status"] == "closed"
    buffer.append("late")
    assert buffer.preview() == "first"


@pytest.mark.asyncio
async def test_replacement_retires_old_log_and_rejects_stale_cursor(service):
    await service.exec_command("replace-output-test", "/tmp", "printf first")
    old = service.active_shells["replace-output-test"]["output_buffer"]
    old_files = [old._spool, old._raw_spool]
    await service.exec_command("replace-output-test", "/tmp", "printf second")
    with pytest.raises(BadRequestException):
        await service.view_shell("replace-output-test", output_id=old.output_id, cursor=0)
    assert all(item.closed for item in old_files)
    latest = await service.view_shell("replace-output-test")
    assert latest.output == "second"
    assert latest.output_metadata["output_id"] != old.output_id
    await service.release_shell("replace-output-test")
    assert "replace-output-test" not in service.active_shells


def test_view_api_supports_cursor_without_consuming_other_readers(client):
    from conftest import BASE_URL
    from app.services.shell import shell_service
    from uuid import uuid4
    session_id = "output-api-" + uuid4().hex
    buffer = ShellOutputBuffer()
    buffer.append("abcdef中😀")
    buffer.stream_complete = True
    shell_service.active_shells[session_id] = {"process": SimpleNamespace(returncode=0),
        "output": buffer.preview(), "console": [], "output_buffer": buffer}
    try:
        response = client.post(BASE_URL + "/api/v1/shell/view", json={"id": session_id})
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["output"] == "abcdef中😀"
        request = {"id": session_id, "output_id": payload["output_metadata"]["output_id"], "cursor": 0, "max_bytes": 4}
        first = client.post(BASE_URL + "/api/v1/shell/view", json=request).json()["data"]
        repeated = client.post(BASE_URL + "/api/v1/shell/view", json=request).json()["data"]
        assert first == repeated
        assert first["output"] == "abcd" and first["output_page"]["next_cursor"] == 4
        assert client.post(BASE_URL + "/api/v1/shell/view", json={"id": session_id, "cursor": 0}).status_code == 422
    finally:
        shell_service.active_shells.pop(session_id)
        buffer.close()
