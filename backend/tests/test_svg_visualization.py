"""Owner-scoped SVG previews never deliver active SVG bytes to the browser."""
import asyncio
import base64
import copy
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.services import extended_visualization as extended
from app.application.services import unified_visualization as unified
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.file_service import FileService
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.svg_visualization import validate_svg_preview_payload
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError
from app.interfaces.api import file_routes
from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog


def chunk(name, data):
    return len(data).to_bytes(4, "big") + name + data + (zlib.crc32(name + data) & 0xffffffff).to_bytes(4, "big")


def png(width=2, height=1, *, pixels=None):
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(
        b"IDAT", zlib.compress(pixels if pixels is not None else (b"\0" + b"\xff\0\0\xff" * width) * height)) + chunk(b"IEND", b"")


def payload():
    return {"contract_version": 2, "type": "svg", "reader": "svg", "kind": "image",
        "media_type": "image/png", "data_base64": base64.b64encode(png()).decode(),
        "metadata": {"source_format": "SVG", "width": 2, "height": 1, "rasterized": True},
        "warnings": [], "sampled": False}


class Storage:
    def __init__(self):
        self.content = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="1"><rect width="2" height="1" fill="red"/></svg>'
        # Browser preview must not trust an uploaded or generated generic MIME.
        self.info = FileInfo(file_id="opaque-svg", filename="chart.SVG", size=len(self.content),
                             content_type="application/octet-stream", user_id="owner")
        self.reads = []

    async def get_file_info(self, file_id, user_id):
        return self.info if file_id == self.info.file_id and user_id == "owner" else None

    async def download_file_range(self, file_id, user_id, *, offset, length):
        if await self.get_file_info(file_id, user_id) is None:
            raise FileNotFoundError()
        self.reads.append((file_id, user_id, offset, length))
        return self.content[offset:offset + length], self.info

    async def download_file(self, *args, **kwargs):
        raise AssertionError("SVG preview cannot use an unbounded or unsigned download")


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(extended, "_SLOTS", asyncio.Semaphore(2))
    definitions = Path(__file__).resolve().parents[2] / "plugin-host/visualizations"
    snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64,
        plugins=[json.loads(path.read_text()) for path in definitions.glob("*.json")])
    states = {}
    preferences = SimpleNamespace(get_states=AsyncMock(side_effect=lambda user: states.get(user, {})),
        set_state=AsyncMock(side_effect=lambda user, plugin, enabled: states.setdefault(user, {}).update({plugin: enabled})))
    catalog = VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)), preferences)
    storage = Storage()
    def worker(image, data, **kwargs):
        assert image == "sandbox:test" and data == storage.content
        assert kwargs["reader"] == "svg" and kwargs["kind"] == "image" and kwargs["format"] == "svg"
        assert kwargs["options"] == {} and kwargs["truncated"] is False
        return {"ok": True, "data": payload()}
    env = SimpleNamespace(storage=storage, catalog=catalog, worker=worker)
    async def render(*args, **kwargs):
        return await extended.extended_visualization(*args, **kwargs, worker=env.worker)
    monkeypatch.setattr(unified, "extended_visualization", render)
    return env


async def invoke(env, *, user="owner", plugin="viz-svg", operation="preview", kind="image", **kwargs):
    return await unified.unified_visualization(FileService(env.storage), env.catalog, "sandbox:test",
        "opaque-svg", user, unified.VisualizationRequest(plugin_id=plugin, operation=operation, kind=kind, **kwargs))


@pytest.mark.asyncio
async def test_svg_uses_public_visualization_protocol_with_safe_png_and_wrong_original_mime(environment):
    result = await invoke(environment)
    assert result.kind == "media" and result.plugin_id == "viz-svg" and result.contract_version == 2
    assert result.payload == {"view_kind": "image", "media_type": "image/png", "data_base64": payload()["data_base64"]}
    assert result.metadata == payload()["metadata"] and result.warnings == []
    assert base64.b64decode(result.payload["data_base64"]) == png()
    assert "<svg" not in result.model_dump_json() and "application/octet-stream" not in result.model_dump_json()
    assert environment.storage.reads == [("opaque-svg", "owner", 0, len(environment.storage.content))]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["foreign", "private", "disabled", "stale", "oversize", "wrong_format", "options"])
async def test_svg_rejects_invalid_scope_and_input_before_read(environment, reason):
    kwargs = {}
    expected = ScientificPreviewRejected
    if reason == "foreign": kwargs["user"] = "another-owner"; expected = FileNotFoundError
    if reason == "private": environment.storage.info.metadata = {"source": "tool_output_spill"}; expected = FileNotFoundError
    if reason == "disabled": await environment.catalog.set_state("owner", "viz-svg", False); expected = VisualizationDisabledError
    if reason == "stale": kwargs["version"] = "f" * 64; expected = PreviewVersionChanged
    if reason == "oversize": environment.storage.info.size = 8 * 1024 * 1024 + 1
    if reason == "wrong_format": environment.storage.info.filename = "chart.html"
    if reason == "options": kwargs["options"] = {"url": "https://outside.invalid/image.svg"}
    with pytest.raises(expected): await invoke(environment, **kwargs)
    assert not environment.storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,operation,kind", [("image", "bytes", None), ("viz-svg", "bytes", None),
    ("viz-svg", "prepare", None), ("viz-svg", "preview", "table")])
async def test_svg_cannot_fall_back_to_raw_image_bytes(environment, plugin, operation, kind):
    with pytest.raises(ScientificPreviewRejected): await invoke(environment, plugin=plugin, operation=operation, kind=kind)
    assert not environment.storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disable", "file_version", "plugin_version"])
async def test_svg_fences_results_changed_while_worker_was_running(environment, mutation):
    def worker(*args, **kwargs):
        if mutation == "disable":
            environment.catalog.repository.get_states.side_effect = lambda user: {"viz-svg": False}
        elif mutation == "file_version":
            environment.storage.info.metadata = {"sha256": "f" * 64}
        else:
            old = environment.catalog.runtime.visualization_snapshot.return_value
            environment.catalog.runtime.visualization_snapshot.return_value = old.model_copy(update={"revision": "b" * 64})
        return {"ok": True, "data": payload()}
    environment.worker = worker
    with pytest.raises((VisualizationDisabledError, PreviewVersionChanged)): await invoke(environment)
    assert extended._SLOTS._value == 2


@pytest.mark.asyncio
async def test_svg_does_not_forward_worker_error_source_or_paths(environment):
    environment.worker = lambda *args, **kwargs: {"ok": False, "error": "<svg><script>secret</script></svg> /Users/private/input.svg"}
    with pytest.raises(ScientificPreviewRejected) as error: await invoke(environment)
    assert "secret" not in str(error.value) and "/Users" not in str(error.value)


@pytest.mark.parametrize("mutation", ["html", "raw_svg", "png_mime_with_svg", "metadata_path", "wrong_dimensions",
    "bool_width", "too_wide", "truncated_png", "crc", "trailing_bytes", "wrong_rows", "unsafe_filter",
    "missing_data", "extra_fields", "sampled", "warning_markup", "wrong_kind"])
def test_svg_worker_payload_is_strict_and_raster_only(mutation):
    value = copy.deepcopy(payload())
    if mutation == "html": value["media_type"] = "text/html"
    if mutation == "raw_svg": value["media_type"] = "image/svg+xml"
    if mutation == "png_mime_with_svg": value["data_base64"] = base64.b64encode(b'<svg onload="alert(1)"/>').decode()
    if mutation == "metadata_path": value["metadata"]["source"] = "/private/original.svg"
    if mutation == "wrong_dimensions": value["metadata"]["width"] = 3
    if mutation == "bool_width": value["metadata"]["width"] = True
    if mutation == "too_wide": value["metadata"]["width"] = 2049
    if mutation == "truncated_png": value["data_base64"] = base64.b64encode(png()[:-12]).decode()
    if mutation == "crc": value["data_base64"] = base64.b64encode(png()[:-1] + b'0').decode()
    if mutation == "trailing_bytes": value["data_base64"] = base64.b64encode(png() + b'<script/>').decode()
    if mutation == "wrong_rows": value["data_base64"] = base64.b64encode(png(pixels=b'wrong')).decode()
    if mutation == "unsafe_filter": value["data_base64"] = base64.b64encode(png(pixels=b'\xff' * 9)).decode()
    if mutation == "missing_data": value.pop("data_base64")
    if mutation == "extra_fields": value["svg"] = "<svg/>"
    if mutation == "sampled": value["sampled"] = True
    if mutation == "warning_markup": value["warnings"] = ["<script>not accepted</script>"]
    if mutation == "wrong_kind": value["kind"] = "table"
    with pytest.raises(ValueError): validate_svg_preview_payload(value, kind="image", limit=8 * 1024 * 1024)


def test_valid_svg_payload_respects_aggregate_output_limit():
    assert validate_svg_preview_payload(payload(), kind="image", limit=8 * 1024 * 1024) == payload()
    with pytest.raises(ValueError): validate_svg_preview_payload(payload(), kind="image", limit=100)


@pytest.mark.asyncio
async def test_invalid_svg_renderer_payload_becomes_safe_worker_failure(environment):
    environment.worker = lambda *args, **kwargs: {"ok": True, "data": {**payload(), "media_type": "image/svg+xml"}}
    with pytest.raises(VisualizationWorkerError): await invoke(environment)


@pytest.mark.asyncio
async def test_svg_http_preview_returns_png_envelope_and_disabling_has_no_bytes_fallback(environment, monkeypatch):
    monkeypatch.setattr(file_routes, "get_settings", lambda: SimpleNamespace(sandbox_image="sandbox:test"))
    app = FastAPI()
    app.include_router(file_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    app.dependency_overrides[get_file_service] = lambda: FileService(environment.storage)
    app.dependency_overrides[get_visualization_catalog] = lambda: environment.catalog
    with TestClient(app) as client:
        request = {"plugin_id": "viz-svg", "operation": "preview", "kind": "image"}
        response = client.post("/api/v1/files/opaque-svg/visualization", json=request)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["kind"] == "media" and data["payload"]["media_type"] == "image/png"
        await environment.catalog.set_state("owner", "viz-svg", False)
        reads_before = list(environment.storage.reads)
        assert client.post("/api/v1/files/opaque-svg/visualization", json=request).status_code == 403
        assert client.post("/api/v1/files/opaque-svg/visualization",
                           json={"plugin_id": "image", "operation": "bytes"}).status_code == 422
        assert environment.storage.reads == reads_before
