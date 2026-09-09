"""Public protocol parity and read/lifecycle/security regression coverage."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from app.application.services import unified_visualization as service
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot
from app.interfaces.api import file_routes
from app.interfaces.api.file_routes import router
from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog


class Preferences:
    def __init__(self): self.values = {}
    async def get_states(self, user): return self.values.get(user, {})
    async def set_state(self, user, plugin, enabled): self.values.setdefault(user, {})[plugin] = enabled


class Storage:
    def __init__(self, content=b"a,b\n1,2\n", filename="values.csv"):
        self.content = {"file": content}
        self.infos = {"file": FileInfo(file_id="file", filename=filename, size=len(content), user_id="owner")}
        self.reads = []
    def resource(self, filename, content=b"resource", **metadata):
        self.content["resource"] = content
        self.infos["resource"] = FileInfo(file_id="resource", filename=filename, size=len(content), user_id="owner", metadata=metadata)
    async def get_file_info(self, file_id, user_id):
        return self.infos.get(file_id) if user_id == "owner" else None
    async def download_file_range(self, file_id, user_id, *, offset, length):
        info = await self.get_file_info(file_id, user_id)
        if info is None: raise FileNotFoundError()
        self.reads.append((file_id, offset, length))
        return self.content[file_id][offset:offset + length], info
    async def download_file(self, *args, **kwargs):
        raise AssertionError("Unified preview must not use unbounded downloads")


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(service, "_BYTE_SLOTS", asyncio.Semaphore(2))
    root = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
    snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64,
        plugins=[json.loads(path.read_text()) for path in root.glob("*.json")])
    catalog = VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)), Preferences())
    return Storage(), catalog


async def invoke(environment, plugin="csv", operation="page", user="owner", **kwargs):
    storage, catalog = environment
    return await service.unified_visualization(FileService(storage), catalog, "sandbox:test", "file", user,
        service.VisualizationRequest(plugin_id=plugin, operation=operation, **kwargs))


@pytest.mark.asyncio
async def test_csv_page_unified_envelope_preserves_page_shape(environment):
    result = await invoke(environment)
    assert result.contract_version == 2 and result.kind == "page"
    assert result.payload["headers"] == ["a", "b"] and result.payload["rows"] == [["1", "2"]]
    assert "version" not in result.payload and result.version == preview_version(environment[0].infos["file"])
    assert set(result.model_dump()) == {"contract_version", "kind", "plugin_id", "version", "revision", "payload", "metadata", "warnings", "sampled"}


@pytest.mark.asyncio
async def test_page_budget_is_per_page_not_whole_file(environment):
    storage, _ = environment
    data = b"a,b\n" + b"1,2\n" * 100000
    storage.content["file"] = data; storage.infos["file"].size = len(data)
    result = await invoke(environment)
    assert result.sampled and result.payload["next_offset"] is not None
    assert storage.reads == [("file", 0, 128 * 1024)]


@pytest.mark.asyncio
async def test_text_page_uses_declared_reader_and_utf8(environment):
    storage, _ = environment
    storage.infos["file"].filename = "readme.txt"
    result = await invoke(environment, "text")
    assert result.kind == "page" and result.payload["text"] == "a,b\n1,2\n"


@pytest.mark.asyncio
async def test_page_lowered_input_budget_rejects_before_read(environment):
    storage, catalog = environment
    snapshot = await catalog.runtime.visualization_snapshot()
    catalog.runtime.visualization_snapshot.return_value = snapshot.model_copy(update={"plugins": [
        item.model_copy(update={"limits": item.limits.model_copy(update={"max_input_bytes": 4})})
        if item.id == "csv" else item for item in snapshot.plugins]})
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment)
    assert storage.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,operation,filename", [("csv", "page", "values.csv"), ("molecular", "prepare", "sample.pdb")])
async def test_common_envelope_obeys_declared_output_budget(environment, plugin, operation, filename):
    storage, catalog = environment
    storage.infos["file"].filename = filename
    snapshot = await catalog.runtime.visualization_snapshot()
    catalog.runtime.visualization_snapshot.return_value = snapshot.model_copy(update={"plugins": [
        item.model_copy(update={"limits": item.limits.model_copy(update={"max_output_bytes": 32})})
        if item.id == plugin else item for item in snapshot.plugins]})
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment, plugin, operation)


@pytest.mark.asyncio
async def test_disable_during_last_catalog_lookup_rejects_page(environment):
    _, catalog = environment
    original = catalog.list_for_user
    calls = 0
    async def snapshot(user):
        nonlocal calls
        calls += 1
        if calls == 2: await catalog.set_state(user, "csv", False)
        return await original(user)
    catalog.list_for_user = snapshot
    with pytest.raises(VisualizationDisabledError): await invoke(environment)


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,operation,options", [
    ("csv", "bytes", {}), ("image", "page", {}), ("viz-fastqc", "preview", {"confirm": True}),
    ("csv", "prepare", {}), ("csv", "page", {"mode": "text"}),
])
async def test_undeclared_operation_and_reader_options_fail_before_read(environment, plugin, operation, options):
    with pytest.raises((service.ScientificPreviewRejected, ValidationError)):
        await invoke(environment, plugin, operation, options=options)
    assert environment[0].reads == []


@pytest.mark.asyncio
async def test_whole_bytes_use_chunks_and_hold_admission_until_stream_closed(environment, monkeypatch):
    storage, _ = environment
    storage.infos["file"].filename = "image.png"
    monkeypatch.setattr(service, "_CHUNK_BYTES", 3)
    monkeypatch.setattr(service, "_READ_CHUNK_BYTES", 3)
    result = await invoke(environment, "image", "bytes")
    assert isinstance(result, service.VisualizationBytes) and service._BYTE_SLOTS._value == 1
    assert storage.reads == [("file", 0, 3), ("file", 3, 3), ("file", 6, 2)]
    assert b"".join([part async for part in result.chunks()]) == storage.content["file"]
    assert service._BYTE_SLOTS._value == 2 and result.stream.closed
    result.close(); assert service._BYTE_SLOTS._value == 2


@pytest.mark.asyncio
async def test_original_image_budget_not_reduced_to_extended_reader_limit(environment, monkeypatch):
    storage, catalog = environment
    storage.infos["file"].filename = "image.png"
    storage.infos["file"].size = 65 * 1024 * 1024
    reached = False
    async def bounded(*_, **kwargs):
        nonlocal reached
        reached = True
        assert kwargs["length"] == 8 * 1024 * 1024
        raise NotImplementedError("stop before materializing large data")
    storage.download_file_range = bounded
    with pytest.raises(NotImplementedError): await invoke(environment, "image", "bytes")
    assert reached and (await catalog.require_enabled("owner", "image")).limits.max_input_bytes == 256 * 1024 * 1024
    assert service._BYTE_SLOTS._value == 2


@pytest.mark.asyncio
async def test_byte_io_chunks_reduce_native_roundtrips_without_full_file_buffer(environment):
    storage, _ = environment
    storage.infos["file"].filename = "image.png"
    total = 8 * 1024 * 1024 + 3
    storage.infos["file"].size = total
    async def read(file_id, user, *, offset, length):
        storage.reads.append((file_id, offset, length))
        return b"x" * length, storage.infos[file_id]
    storage.download_file_range = read
    result = await invoke(environment, "image", "bytes")
    assert storage.reads == [("file", 0, 8 * 1024 * 1024), ("file", 8 * 1024 * 1024, 3)]
    assert result.stream._rolled, "large reads must spool to disk rather than accumulate full source in memory"
    delivered = 0
    async for chunk in result.chunks():
        assert len(chunk) <= 1024 * 1024
        delivered += len(chunk)
    assert delivered == total and service._BYTE_SLOTS._value == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["foreign", "private", "disabled", "version", "format", "oversize"])
async def test_whole_byte_denials_never_touch_storage(environment, reason):
    storage, catalog = environment
    storage.infos["file"].filename = "image.png"
    options = {}
    if reason == "private": storage.infos["file"].metadata = {"source": "tool_output_spill"}
    if reason == "disabled": await catalog.set_state("owner", "image", False)
    if reason == "version": options["version"] = "0" * 64
    if reason == "format": storage.infos["file"].filename = "secret.txt"
    if reason == "oversize": storage.infos["file"].size = 256 * 1024 * 1024 + 1
    with pytest.raises((FileNotFoundError, VisualizationDisabledError, PreviewVersionChanged, service.ScientificPreviewRejected)):
        await invoke(environment, "image", "bytes", user="other" if reason == "foreign" else "owner", **options)
    assert storage.reads == []


@pytest.mark.asyncio
async def test_binary_disable_during_read_closes_spool_and_releases_slot(environment):
    storage, catalog = environment
    storage.infos["file"].filename = "image.png"
    original = storage.download_file_range
    async def revoke(*args, **kwargs):
        data = await original(*args, **kwargs)
        await catalog.set_state("owner", "image", False)
        return data
    storage.download_file_range = revoke
    with pytest.raises(VisualizationDisabledError): await invoke(environment, "image", "bytes")
    assert service._BYTE_SLOTS._value == 2


@pytest.mark.asyncio
async def test_cancel_native_range_keeps_slot_until_thread_exits(environment):
    storage, _ = environment
    storage.infos["file"].filename = "image.png"
    entered, done = threading.Event(), threading.Event()
    def read():
        entered.set(); done.wait(2)
        return storage.content["file"], storage.infos["file"]
    async def native(*_, **__): return await asyncio.to_thread(read)
    storage.download_file_range = native
    task = asyncio.create_task(invoke(environment, "image", "bytes"))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert service._BYTE_SLOTS._value == 1
    done.set()
    for _ in range(100):
        if service._BYTE_SLOTS._value == 2: break
        await asyncio.sleep(.005)
    assert service._BYTE_SLOTS._value == 2


async def _loop_turn():
    """Flush queued task callbacks without a wall-clock timing assumption."""
    barrier = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(barrier.set_result, None)
    await barrier


async def _invoke_bytes_route(environment, request):
    storage, catalog = environment
    return await file_routes.visualize_file(
        "file", service.VisualizationRequest(plugin_id="image", operation="bytes"),
        request, FileService(storage), SimpleNamespace(id="owner"), catalog,
    )


def _control_route_worker(monkeypatch, environment, worker, started):
    """Enter the route's disconnect await while its actual worker is pending."""
    environment[0].infos["file"].filename = "image.png"
    captured = []

    async def pending_wait(tasks, *, timeout):
        assert timeout > 0
        work, = tasks
        captured.append(work)
        await started.wait()
        return ({work}, set()) if work.done() else (set(), {work})

    monkeypatch.setattr(file_routes, "unified_visualization", worker)
    monkeypatch.setattr(file_routes.asyncio, "wait", pending_wait)
    monkeypatch.setattr(file_routes, "get_settings", lambda: SimpleNamespace(sandbox_image="sandbox:test"))
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["disconnect", "parent_cancel"])
async def test_route_closes_bytes_completed_during_disconnect_check(environment, monkeypatch, interruption):
    started, finish = asyncio.Event(), asyncio.Event()
    results = []

    async def worker(*_):
        started.set()
        await finish.wait()
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        return result

    captured = _control_route_worker(monkeypatch, environment, worker, started)

    async def is_disconnected():
        finish.set()
        await asyncio.shield(captured[0])
        assert results and service._BYTE_SLOTS._value == 1
        if interruption == "parent_cancel":
            asyncio.current_task().cancel()
            await asyncio.Event().wait()
        return True

    try:
        with pytest.raises(asyncio.CancelledError):
            await _invoke_bytes_route(environment, SimpleNamespace(is_disconnected=is_disconnected))
        await _loop_turn()
        assert results[0].stream.closed
        assert service._BYTE_SLOTS._value == 2
    finally:
        for result in results:
            result.close()


@pytest.mark.asyncio
async def test_route_closes_bytes_returned_by_worker_suppressing_cancellation(environment, monkeypatch):
    started = asyncio.Event()
    results = []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return result

    captured = _control_route_worker(monkeypatch, environment, worker, started)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    try:
        with pytest.raises(asyncio.CancelledError):
            await _invoke_bytes_route(environment, request)
        await asyncio.shield(captured[0])
        await _loop_turn()
        assert results[0].stream.closed
        assert service._BYTE_SLOTS._value == 2
    finally:
        for result in results:
            result.close()


@pytest.mark.asyncio
async def test_route_worker_cleanup_failure_does_not_replace_request_cancellation(environment, monkeypatch):
    started = asyncio.Event()
    results = []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            result.close()
            raise RuntimeError("storage cleanup failed") from None

    captured = _control_route_worker(monkeypatch, environment, worker, started)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _invoke_bytes_route(environment, SimpleNamespace(is_disconnected=AsyncMock(return_value=True)))
        await _loop_turn()
        await _loop_turn()
        assert captured[0].done()
        assert isinstance(captured[0].exception(), RuntimeError)
        assert results[0].stream.closed and service._BYTE_SLOTS._value == 2
    finally:
        for result in results:
            result.close()


@pytest.mark.asyncio
async def test_route_repeated_parent_cancellation_cleans_late_bytes(environment, monkeypatch):
    started, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    results = []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
        # Native adapters may finish after cancellation. Even a second request
        # cancellation must not discard an eventual byte-stream owner.
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                continue
        return result

    captured = _control_route_worker(monkeypatch, environment, worker, started)
    checking = asyncio.Event()

    async def is_disconnected():
        checking.set()
        await asyncio.Event().wait()

    parent = asyncio.create_task(_invoke_bytes_route(environment, SimpleNamespace(is_disconnected=is_disconnected)))
    try:
        await checking.wait()
        parent.cancel()
        await cancelling.wait()
        parent.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await parent
        await asyncio.shield(captured[0])
        await _loop_turn()
        assert results[0].stream.closed and service._BYTE_SLOTS._value == 2
    finally:
        finish.set()
        for result in results:
            result.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["body", "background"])
async def test_route_stream_owns_bytes_until_body_or_background_cleanup(environment, monkeypatch, cleanup):
    started = asyncio.Event()
    results = []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        return result

    _control_route_worker(monkeypatch, environment, worker, started)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await _invoke_bytes_route(environment, request)
    try:
        await _loop_turn()
        assert not results[0].stream.closed and service._BYTE_SLOTS._value == 1
        if cleanup == "body":
            assert b"".join([part async for part in response.body_iterator]) == environment[0].content["file"]
        else:
            await response.background()
        assert results[0].stream.closed and service._BYTE_SLOTS._value == 2
        await response.background()
        assert service._BYTE_SLOTS._value == 2, "body and background cleanup must not release admission twice"
    finally:
        results[0].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
async def test_route_asgi_stream_failure_releases_bytes(environment, monkeypatch, stage, failure):
    started = asyncio.Event()
    results, messages = [], []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        return result

    _control_route_worker(monkeypatch, environment, worker, started)
    response = await _invoke_bytes_route(environment, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))

    async def receive():
        raise AssertionError("ASGI 2.4 streaming must observe transport failures through send")

    async def send(message):
        messages.append(message)
        assert not results[0].stream.closed and service._BYTE_SLOTS._value == 1
        if message["type"] == ("http.response.start" if stage == "headers" else "http.response.body"):
            if failure == "cancel":
                asyncio.current_task().cancel()
                await asyncio.Event().wait()
            raise OSError("client transport closed")

    try:
        sending = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ClientDisconnect):
            await sending
        assert len(messages) == (1 if stage == "headers" else 2)
        assert results[0].stream.closed, "ASGI failures must clean up even before the body iterator starts"
        assert service._BYTE_SLOTS._value == 2
    finally:
        results[0].close()


@pytest.mark.asyncio
async def test_route_asgi_stream_success_keeps_bytes_until_body_is_sent(environment, monkeypatch):
    started = asyncio.Event()
    results, messages = [], []

    async def worker(*_):
        result = await invoke(environment, "image", "bytes")
        results.append(result)
        started.set()
        return result

    _control_route_worker(monkeypatch, environment, worker, started)
    response = await _invoke_bytes_route(environment, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))

    async def receive():
        raise AssertionError("ASGI 2.4 does not require disconnect polling")

    async def send(message):
        if message["type"] == "http.response.start" or message.get("more_body"):
            assert not results[0].stream.closed and service._BYTE_SLOTS._value == 1
        messages.append(message)

    try:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert messages[0]["type"] == "http.response.start" and messages[0]["status"] == 200
        assert b"".join(message.get("body", b"") for message in messages) == environment[0].content["file"]
        assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert results[0].stream.closed and service._BYTE_SLOTS._value == 2
    finally:
        results[0].close()


@pytest.mark.asyncio
async def test_route_two_disconnects_leave_both_byte_slots_available(environment, monkeypatch):
    results = []
    try:
        for _ in range(2):
            started, finish = asyncio.Event(), asyncio.Event()

            async def worker(*_):
                started.set()
                await finish.wait()
                result = await invoke(environment, "image", "bytes")
                results.append(result)
                return result

            captured = _control_route_worker(monkeypatch, environment, worker, started)

            async def is_disconnected():
                finish.set()
                await asyncio.shield(captured[0])
                return True

            with pytest.raises(asyncio.CancelledError):
                await _invoke_bytes_route(environment, SimpleNamespace(is_disconnected=is_disconnected))
            await _loop_turn()
        assert all(result.stream.closed for result in results)
        assert service._BYTE_SLOTS._value == 2
        # Real subsequent previews must both be admitted, not only a counter
        # assertion. Hold the first response while acquiring the second slot.
        results.extend(await asyncio.gather(invoke(environment, "image", "bytes"), invoke(environment, "image", "bytes")))
        assert service._BYTE_SLOTS._value == 0
        for result in results[-2:]:
            assert b"".join([part async for part in result.chunks()]) == environment[0].content["file"]
        assert service._BYTE_SLOTS._value == 2
    finally:
        for result in results:
            result.close()


@pytest.mark.asyncio
async def test_molecular_prepare_is_read_only_metadata_not_a_file_download(environment):
    environment[0].infos["file"].filename = "POSCAR"
    result = await invoke(environment, "molecular", "prepare")
    assert result.kind == "molecule" and result.payload["source_format"] == "vasp"
    assert result.payload["periodic"] and result.payload["supports_unit_cell"]
    assert environment[0].reads == []


@pytest.mark.parametrize("reader,view,business,kind", [
    ("netcdf", "map", {"values": [1]}, "raster"),
    ("fastq", "quality", {"x": [0], "y": [1]}, "series"),
    ("fits", "image", {"values": [1]}, "raster"),
    ("hdf5", "tree", {"tree": []}, "tree"),
    ("tabular", "series", {"array": {"shape": [1], "values": [1]}}, "array"),
    ("excel", "table", {"table": {"columns": ["x"], "rows": [[1]]}}, "table"),
    ("office", "pdf", {"data_base64": "JVBERi0=", "media_type": "application/pdf"}, "media"),
    ("rdkit", "image", {"data_base64": "iVBORw0KGgo=", "media_type": "image/png"}, "media"),
    ("fastqc", "report", {"sections": []}, "report"),
])
def test_result_kind_describes_structure_not_reader(reader, view, business, kind):
    internal = {"contract_version": 1 if reader in service._SCIENCE_READERS else 2,
        "reader": reader, "type": reader, "kind": view, "plugin_id": "example", "version": "a" * 64,
        "revision": "b" * 64, "metadata": {}, "warnings": [], "sampled": False,
        "data_base64": None, "array": None, "table": None, **business}
    result = service.normalize_result(internal)
    assert result.contract_version == 2 and result.kind == kind
    assert result.payload["view_kind"] == view and "reader" not in result.payload


@pytest.mark.parametrize("reader,view", [("hdf5", "tree"), ("fastqc", "report"), ("tabular", "series")])
def test_missing_structured_result_is_not_mislabelled_as_a_tree(reader, view):
    internal = {"contract_version": 2, "reader": reader, "type": reader, "kind": view,
        "plugin_id": "example", "version": "a" * 64, "revision": "b" * 64,
        "metadata": {}, "warnings": [], "sampled": False,
        "data_base64": None, "array": None, "table": None, "tree": None, "sections": None}
    with pytest.raises(service.VisualizationWorkerError): service.normalize_result(internal)


@pytest.mark.asyncio
async def test_scientific_prefix_dispatch_preserves_bounded_reader_request(environment, monkeypatch):
    storage, _ = environment
    storage.infos["file"].filename = "large.fastq"
    storage.infos["file"].size = 100 * 1024 * 1024
    internal = {"contract_version": 1, "reader": "fastq", "kind": "quality", "x": [0], "y": [1],
        "plugin_id": "fastq-quality", "version": preview_version(storage.infos["file"]), "revision": "a" * 64,
        "metadata": {}, "warnings": [], "sampled": True}
    worker = AsyncMock(return_value=internal)
    monkeypatch.setattr(service, "scientific_visualization", worker)
    result = await invoke(environment, "fastq-quality", "preview")
    assert result.kind == "series" and result.sampled
    assert worker.await_args.args[5].plugin_id == "fastq-quality"


@pytest.mark.asyncio
async def test_expanded_reader_dispatch_has_same_public_envelope(environment, monkeypatch):
    storage, _ = environment
    result = {"contract_version": 2, "reader": "tabular", "type": "tabular", "kind": "table",
        "table": {"columns": ["a"], "rows": [[1]]}, "plugin_id": "viz-plotly",
        "version": preview_version(storage.infos["file"]), "revision": "a" * 64,
        "metadata": {}, "warnings": [], "sampled": False}
    monkeypatch.setattr(service, "extended_visualization", AsyncMock(return_value=result))
    public = await invoke(environment, "viz-plotly", "preview", options={"row_offset": 0})
    assert public.kind == "table" and public.payload["table"]["rows"] == [[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["shapefile", "html", "markdown"])
async def test_related_bytes_are_owner_and_source_scoped(environment, kind):
    storage, _ = environment
    if kind == "shapefile":
        storage.infos["file"].filename = "points.shp"
        storage.infos["file"].metadata = {"logical_path": "folder/points.shp", "source_archive": "source.zip"}
        storage.resource("points.dbf", logical_path="folder/points.dbf", source_archive="source.zip")
    else:
        storage.infos["file"].filename = "index.html" if kind == "html" else "readme.md"
        storage.infos["file"].metadata = {"session_id": "session"}
        storage.resource("image.png", session_id="session")
    result = await invoke(environment, kind, "bytes", options={"resource_id": "resource"})
    assert b"".join([chunk async for chunk in result.chunks()]) == b"resource"
    assert storage.reads[0][0] == "resource"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "metadata.file_path", "logical_path"])
async def test_legacy_html_bundle_uses_explicit_parent_paths_without_exposing_them(environment, field):
    storage, _ = environment
    storage.infos["file"].filename = "index.html"
    storage.resource("logo.png")
    source = "/private/old-session/result/index.html" if field != "logical_path" else "result/index.html"
    resource = "/private/old-session/result/assets/logo.png" if field != "logical_path" else "result/assets/logo.png"
    if field == "file_path":
        storage.infos["file"].file_path = source; storage.infos["resource"].file_path = resource
    else:
        key = "file_path" if field == "metadata.file_path" else "logical_path"
        storage.infos["file"].metadata = {key: source}; storage.infos["resource"].metadata = {key: resource}
    result = await invoke(environment, "html", "bytes", options={"resource_id": "resource"})
    assert b"".join([part async for part in result.chunks()]) == b"resource"
    assert result.plugin_id == "html" and "/private/" not in result.version


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target,sessions", [
    ("index.html", "logo.png", False),
    ("/index.html", "/any/logo.png", False),
    ("result/index.html", "other/logo.png", False),
    ("result/index.html", "result/../private/logo.png", False),
    ("result/index.html", "result/assets/logo.png", True),
])
async def test_legacy_document_resources_need_actual_bundle_evidence(environment, source, target, sessions):
    storage, _ = environment
    storage.infos["file"].filename = "readme.md"
    storage.infos["file"].metadata = {"logical_path": source}
    storage.resource("logo.png", logical_path=target)
    if sessions:
        storage.infos["file"].metadata["session_id"] = "one"
        storage.infos["resource"].metadata["session_id"] = "two"
    with pytest.raises(FileNotFoundError): await invoke(environment, "markdown", "bytes", options={"resource_id": "resource"})
    assert storage.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["different-folder", "different-session", "different-archive", "dataset-unverified"])
async def test_sidecar_relationship_does_not_fall_back_to_basename(environment, bad):
    storage, _ = environment
    storage.infos["file"].filename = "points.shp"
    storage.infos["file"].metadata = {"logical_path": "folder/points.shp", "session_id": "session", "source_archive": "archive"}
    storage.resource("points.dbf", logical_path="folder/points.dbf", session_id="session", source_archive="archive")
    metadata = storage.infos["resource"].metadata
    if bad == "different-folder": metadata["logical_path"] = "other/points.dbf"
    if bad == "different-session": metadata["session_id"] = "other"
    if bad == "different-archive": metadata["source_archive"] = "other"
    if bad == "dataset-unverified": metadata["source"] = "dataset_preview"
    with pytest.raises(FileNotFoundError): await invoke(environment, "shapefile", "bytes", options={"resource_id": "resource"})
    assert storage.reads == []


def test_public_routes_have_one_entry_and_old_preview_routes_are_absent(environment):
    storage, catalog = environment
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    app.dependency_overrides[get_file_service] = lambda: FileService(storage)
    app.dependency_overrides[get_visualization_catalog] = lambda: catalog
    paths = {route.path for route in app.routes}
    assert "/files/{file_id}/visualization" in paths
    assert "/files/{file_id}/visualization/jobs" in paths
    assert "/files/shapefile-preview/prepare" in paths, "Archive import remains a separate explicit mutation"
    for removed in ["/files/{file_id}/preview", "/files/{file_id}/visualization-v2", "/files/{file_id}/visualization-content", "/files/{file_id}/visualization-jobs", "/files/molecular-preview/prepare"]:
        assert removed not in paths
    with TestClient(app) as client:
        response = client.post("/files/file/visualization", json={"plugin_id": "csv", "operation": "page"})
        assert response.status_code == 200 and response.json()["data"]["kind"] == "page"
        assert client.post("/files/file/visualization", json={"plugin_id": "csv"}).status_code == 422
        assert client.post("/files/file/visualization", json={"plugin_id": "csv", "operation": "execute"}).status_code == 422
        assert client.post("/files/file/visualization", json={"plugin_id": "csv", "operation": "page", "options": {"offset": True}}).status_code == 422
        storage.infos["file"].filename = "example.png"
        binary = client.post("/files/file/visualization", json={"plugin_id": "image", "operation": "bytes"})
        assert binary.content == storage.content["file"]
        assert binary.headers["x-visualization-plugin"] == "image"
        assert binary.headers["x-visualization-revision"] == "a" * 64
        assert binary.headers["x-preview-version"] == preview_version(storage.infos["file"])
        assert binary.headers["cache-control"] == "no-store"
