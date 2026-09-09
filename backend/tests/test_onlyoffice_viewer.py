"""Read-only office capabilities, independent-origin defenses and cleanup."""
import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.application.services import onlyoffice_viewer as office, unified_visualization as unified
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.scientific_visualization import ScientificPreviewRejected, VisualizationWorkerError
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot
from app.infrastructure.logging import OfficeLeaseLogFilter
from app.interfaces.api import onlyoffice_routes
from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog
from app.interfaces.middleware.office_origin_guard import OfficeOriginGuard, office_origin


class Preferences:
    def __init__(self): self.values = {}
    async def get_states(self, user): return self.values.get(user, {})
    async def set_state(self, user, plugin, enabled): self.values.setdefault(user, {})[plugin] = enabled


class Storage:
    def __init__(self):
        self.info = FileInfo(file_id="file", filename="report.docx", size=5, user_id="owner", file_path="/private/never-return/report.docx")
        self.reads = []
    async def get_file_info(self, file_id, user_id):
        return self.info if file_id == "file" and user_id == "owner" else None
    async def download_file_range(self, file_id, user_id, *, offset, length):
        self.reads.append((offset, length)); return b"bytes"[offset:offset + length], self.info
    async def download_file(self, *args, **kwargs): raise AssertionError("Unbounded file reads are forbidden")


class Leases:
    def __init__(self): self.records = {}; self.reads = {}; self.after_create = None
    async def create(self, token, lease):
        office._key(token)
        if len(self.records) >= office.MAX_ACTIVE_LEASES: raise ScientificPreviewRejected()
        self.records[token] = lease
        if self.after_create: await self.after_create()
    async def get(self, token):
        office._key(token)
        if token not in self.records: raise FileNotFoundError()
        return self.records[token]
    async def delete(self, token): self.records.pop(token, None)
    async def claim_source(self, token):
        await self.get(token); self.reads[token] = self.reads.get(token, 0) + 1
        if self.reads[token] > office.MAX_SOURCE_FETCHES: raise FileNotFoundError()


@pytest.fixture
def env(monkeypatch):
    settings = SimpleNamespace(onlyoffice_enabled=True, onlyoffice_public_origin="http://office.localhost:7001",
        onlyoffice_jwt_secret="a" * 64, onlyoffice_gateway_secret="b" * 64)
    root = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
    snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64,
        plugins=[json.loads(path.read_text()) for path in root.glob("*.json")])
    catalog = VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)), Preferences())
    catalog.repository.values["owner"] = {office.PLUGIN_ID: True}
    storage, leases = Storage(), Leases()
    monkeypatch.setattr(office, "get_settings", lambda: settings)
    monkeypatch.setattr(office, "get_office_lease_store", lambda: leases)
    monkeypatch.setattr(unified, "_BYTE_SLOTS", asyncio.Semaphore(2))
    return SimpleNamespace(settings=settings, catalog=catalog, storage=storage, files=FileService(storage), leases=leases, snapshot=snapshot)


async def prepare(env, **kwargs):
    plugin = await env.catalog.require_enabled("owner", office.PLUGIN_ID)
    return await office.prepare_office_viewer(env.files, env.catalog, "file", "owner", plugin,
        env.snapshot.revision, preview_version(env.storage.info), health=AsyncMock(), **kwargs)


@pytest.mark.asyncio
async def test_prepare_uses_unified_resource_contract_without_reading_source_or_disclosing_paths(env):
    result = await prepare(env)
    assert result.contract_version == 2 and result.kind == "resources"
    assert result.plugin_id == office.PLUGIN_ID and result.metadata["source_writeback"] is False
    assert result.payload["frame_url"].startswith("http://office.localhost:7001/office-viewer/frame/")
    assert len(result.payload["lease"]) == 43 and len(env.leases.records) == 1 and not env.storage.reads
    assert "/private/" not in result.model_dump_json() and "file_path" not in next(iter(env.leases.records.values())).model_dump_json()


@pytest.mark.asyncio
async def test_actual_unified_prepare_dispatches_onlyoffice_without_parallel_plugin_protocol(env, monkeypatch):
    original = office.prepare_office_viewer
    async def local_provider(*args, **kwargs): return await original(*args, **kwargs, health=AsyncMock())
    monkeypatch.setattr(office, "prepare_office_viewer", local_provider)
    result = await unified.unified_visualization(env.files, env.catalog, "sandbox:test", "file", "owner",
        unified.VisualizationRequest(plugin_id=office.PLUGIN_ID, operation="prepare"))
    assert result.kind == "resources" and result.payload["provider"] == "onlyoffice"
    assert not env.storage.reads


@pytest.mark.parametrize("operation,options", [("bytes", {}), ("page", {}), ("preview", {}), ("prepare", {"url": "http://arbitrary.invalid"})])
@pytest.mark.asyncio
async def test_unified_office_operation_allowlist_rejects_before_read(env, operation, options):
    with pytest.raises(ScientificPreviewRejected):
        await unified.unified_visualization(env.files, env.catalog, "sandbox:test", "file", "owner",
            unified.VisualizationRequest(plugin_id=office.PLUGIN_ID, operation=operation, options=options))
    assert not env.storage.reads and not env.leases.records


@pytest.mark.parametrize("field,value", [("onlyoffice_enabled", False), ("onlyoffice_jwt_secret", ""),
    ("onlyoffice_gateway_secret", "a" * 64), ("onlyoffice_public_origin", "http://localhost:7001"),
    ("onlyoffice_public_origin", "http://office.localhost.:7001"), ("onlyoffice_public_origin", "http://office.localhost:bad"),
    ("onlyoffice_public_origin", "http://office.localhost:7001/api"), ("onlyoffice_public_origin", "http://user@office.localhost:7001")])
@pytest.mark.asyncio
async def test_unconfigured_or_unsafe_origin_fails_before_read_or_lease(env, field, value):
    setattr(env.settings, field, value)
    with pytest.raises(VisualizationWorkerError): await prepare(env)
    assert not env.storage.reads and not env.leases.records


@pytest.mark.parametrize("size", [0, 64 * 1024 * 1024 + 1])
@pytest.mark.asyncio
async def test_office_source_budget_before_issuing_lease(env, size):
    env.storage.info.size = size
    with pytest.raises(ScientificPreviewRejected): await prepare(env)
    assert not env.leases.records and not env.storage.reads


@pytest.mark.asyncio
async def test_macro_format_cannot_be_prepared(env):
    env.storage.info.filename = "macros.docm"
    with pytest.raises(ScientificPreviewRejected): await prepare(env)


@pytest.mark.asyncio
async def test_output_budget_checked_before_lease_creation(env):
    plugin = await env.catalog.require_enabled("owner", office.PLUGIN_ID)
    plugin = plugin.model_copy(update={"limits": plugin.limits.model_copy(update={"max_output_bytes": 32})})
    env.catalog.runtime.visualization_snapshot.return_value = env.snapshot.model_copy(update={
        "plugins": [plugin if p.id == plugin.id else p for p in env.snapshot.plugins]})
    with pytest.raises(ScientificPreviewRejected): await prepare(env)
    assert not env.leases.records


@pytest.mark.asyncio
async def test_stop_race_after_lease_creation_revokes_unreturned_capability(env):
    env.leases.after_create = lambda: env.catalog.set_state("owner", office.PLUGIN_ID, False)
    with pytest.raises(VisualizationDisabledError): await prepare(env)
    assert not env.leases.records


@pytest.mark.parametrize("change", ["expired", "revoked", "version", "disabled", "revision", "owner"])
@pytest.mark.asyncio
async def test_every_capability_use_revalidates_owner_source_version_and_plugin(env, change):
    result = await prepare(env); token = result.payload["lease"]
    if change == "expired": env.leases.records[token].expires_at = int(time.time()) - 1
    if change == "revoked": await env.leases.delete(token)
    if change == "version": env.storage.info.size += 1
    if change == "disabled": await env.catalog.set_state("owner", office.PLUGIN_ID, False)
    if change == "revision": env.catalog.runtime.visualization_snapshot.return_value = env.snapshot.model_copy(update={"revision": "b" * 64})
    if change == "owner": env.leases.records[token].user_id = "other"
    with pytest.raises((FileNotFoundError, PreviewVersionChanged, VisualizationDisabledError)):
        await office.authorize_lease(token, env.files, env.catalog)
    assert not env.storage.reads


@pytest.mark.parametrize("extension,kind", [("docx", "word"), ("xlsx", "cell"), ("pptx", "slide")])
@pytest.mark.asyncio
async def test_signed_config_disables_edits_downloads_print_macros_plugins_and_original_callbacks(env, extension, kind):
    env.storage.info.filename = f"report.{extension}"
    result = await prepare(env); token = result.payload["lease"]
    lease = await env.leases.get(token)
    config = office.view_configuration(token, lease, env.storage.info)
    signed = jwt.decode(config["token"], env.settings.onlyoffice_jwt_secret, algorithms=["HS256"])
    assert signed["documentType"] == kind and signed["editorConfig"]["mode"] == "view"
    assert not any(signed["document"]["permissions"].values())
    assert signed["document"]["url"] == f"http://office-gateway:8081/files/{token}"
    assert signed["editorConfig"]["customization"]["macros"] is False
    assert signed["editorConfig"]["customization"]["plugins"] is False
    assert signed["exp"] == lease.expires_at
    assert "callbackUrl" not in json.dumps(config) and "/private/never-return" not in json.dumps(config)
    env.storage.info.filename = '</script><script>alert(1)</script>.docx'
    malicious = office.view_configuration(token, lease, env.storage.info)
    # The public filename sanitizer already removes path segments; independently
    # exercise the wrapper's JSON escaping for a future field containing HTML.
    malicious["document"]["title"] = '</script><script>alert(1)</script>'
    html = office.frame_html(malicious, lease.expires_at, token)
    assert '</script><script>alert' not in html and '\\u003c' in html
    assert 'setTimeout(stop' in html and '/office-viewer/status/' in html and 'destroyEditor' in html


def client_for(env):
    app = FastAPI(); app.add_middleware(OfficeOriginGuard)
    app.include_router(onlyoffice_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_file_service] = lambda: env.files
    app.dependency_overrides[get_visualization_catalog] = lambda: env.catalog
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    return TestClient(app)


def test_gateway_private_capabilities_are_not_a_second_public_openapi_protocol(env):
    with client_for(env) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/office-viewer/leases/{token}/revoke" in paths
    assert not any(path.startswith("/api/v1/office-viewer/private/") for path in paths)


@pytest.mark.asyncio
async def test_private_gateway_only_routes_and_bounded_source_reads(env):
    result = await prepare(env); token = result.payload["lease"]
    with client_for(env) as client:
        base = f"/api/v1/office-viewer/private/files/{token}"
        for headers in [{}, {"X-Office-Gateway": "wrong"}]: assert client.get(base, headers=headers).status_code == 404
        headers = {"X-Office-Gateway": env.settings.onlyoffice_gateway_secret}
        assert client.head(base, headers=headers).status_code == 200 and not env.storage.reads
        response = client.get(base, headers=headers)
        assert response.content == b"bytes" and response.status_code == 200 and env.storage.reads == [(0, 5)]
        assert client.get(f"/api/v1/office-viewer/private/frame/{token}", headers=headers).status_code == 200
        assert client.get(f"/api/v1/office-viewer/private/status/{token}", headers=headers).status_code == 204
        assert client.post(f"/api/v1/office-viewer/leases/{token}/revoke").status_code == 400
        await env.catalog.set_state("owner", office.PLUGIN_ID, False)
        assert client.post(f"/api/v1/office-viewer/leases/{token}/revoke", headers={"X-Visualization-Action": "revoke"}).status_code == 200
        assert client.get(base, headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_source_disconnect_race_closes_result_finished_during_disconnect_check(env, monkeypatch):
    result = await prepare(env); token = result.payload["lease"]
    started, finish = asyncio.Event(), asyncio.Event()
    original = unified._bytes
    spools = []
    async def read(*args):
        started.set()
        await finish.wait()
        spool = await original(*args)
        spools.append(spool)
        return spool
    async def disconnected():
        finish.set()
        await asyncio.shield(workers[0])
        return True
    workers = _control_source_worker(monkeypatch, read, started)
    request = SimpleNamespace(method="GET", is_disconnected=disconnected)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _invoke_source(env, token, request)
        assert spools[0].stream.closed and unified._BYTE_SLOTS._value == 2
    finally:
        for spool in spools: spool.close()


async def _invoke_source(env, token, request=None):
    request = request or SimpleNamespace(method="GET", is_disconnected=AsyncMock(return_value=False))
    return await onlyoffice_routes.office_source(token, request, env.files, env.catalog, env.settings.onlyoffice_gateway_secret)


def _control_source_worker(monkeypatch, worker, started):
    workers = []
    async def wait(tasks, *, timeout):
        work, = tasks
        workers.append(work)
        await started.wait()
        return ({work}, set()) if work.done() else (set(), {work})
    monkeypatch.setattr(unified, "_bytes", worker)
    # Replace only this route's API binding, not global asyncio primitives.
    route_asyncio = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
    route_asyncio.wait = wait
    monkeypatch.setattr(onlyoffice_routes, "asyncio", route_asyncio)
    return workers


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
async def test_source_asgi_failure_always_releases_spool_and_admission(env, monkeypatch, stage, failure):
    prepared = await prepare(env)
    original, spools, messages = unified._bytes, [], []
    async def read(*args):
        spool = await original(*args)
        spools.append(spool)
        return spool
    monkeypatch.setattr(unified, "_bytes", read)
    response = await _invoke_source(env, prepared.payload["lease"])
    async def receive(): raise AssertionError("ASGI 2.4 uses send failures")
    async def send(message):
        messages.append(message)
        assert not spools[0].stream.closed and unified._BYTE_SLOTS._value == 1
        if message["type"] == ("http.response.start" if stage == "headers" else "http.response.body"):
            if failure == "cancel":
                asyncio.current_task().cancel()
                await asyncio.Event().wait()
            raise OSError("closed transport")
    try:
        sending = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ClientDisconnect): await sending
        assert len(messages) == (1 if stage == "headers" else 2)
        assert spools[0].stream.closed and unified._BYTE_SLOTS._value == 2
    finally:
        for spool in spools: spool.close()


@pytest.mark.asyncio
async def test_source_asgi_success_transfers_ownership_until_body_sent(env, monkeypatch):
    prepared = await prepare(env)
    original, spools, messages = unified._bytes, [], []
    async def read(*args):
        spool = await original(*args)
        spools.append(spool)
        return spool
    monkeypatch.setattr(unified, "_bytes", read)
    response = await _invoke_source(env, prepared.payload["lease"])
    async def receive(): raise AssertionError("ASGI 2.4 uses send failures")
    async def send(message):
        if message["type"] == "http.response.start" or message.get("more_body"):
            assert not spools[0].stream.closed and unified._BYTE_SLOTS._value == 1
        messages.append(message)
    try:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert b"".join(message.get("body", b"") for message in messages) == b"bytes"
        assert spools[0].stream.closed and unified._BYTE_SLOTS._value == 2
        await response.background()
        assert unified._BYTE_SLOTS._value == 2
    finally:
        for spool in spools: spool.close()


@pytest.mark.asyncio
async def test_source_repeated_cancellation_cleans_worker_result_returned_later(env, monkeypatch):
    prepared = await prepare(env)
    original, spools = unified._bytes, []
    started, cancelling, finish, checking = [asyncio.Event() for _ in range(4)]
    async def read(*args):
        spool = await original(*args)
        spools.append(spool)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
        while not finish.is_set():
            try: await finish.wait()
            except asyncio.CancelledError: continue
        return spool
    async def disconnected():
        checking.set()
        await asyncio.Event().wait()
    workers = _control_source_worker(monkeypatch, read, started)
    parent = asyncio.create_task(_invoke_source(env, prepared.payload["lease"], SimpleNamespace(method="GET", is_disconnected=disconnected)))
    try:
        await checking.wait()
        parent.cancel()
        await cancelling.wait()
        parent.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError): await parent
        await asyncio.shield(workers[0])
        barrier = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(barrier.set_result, None)
        await barrier
        assert spools[0].stream.closed and unified._BYTE_SLOTS._value == 2
    finally:
        finish.set()
        for spool in spools: spool.close()


@pytest.mark.parametrize("headers", [{"Origin": "http://office.localhost:7001"}, {"Origin": "http://OFFICE.LOCALHOST.:7001"},
    {"Origin": "null"}, {"Referer": "http://office.localhost.:7001/office-viewer/frame/x"}, {"Host": "office.localhost.:7001"}])
def test_simple_cross_origin_requests_never_reach_anonymous_admin_api(headers):
    app = FastAPI(); app.add_middleware(OfficeOriginGuard)
    @app.post("/api/v1/change")
    async def change(): raise AssertionError("Must not execute")
    assert TestClient(app).post("/api/v1/change", headers=headers, content="x").status_code == 403


@pytest.mark.asyncio
async def test_guard_covers_websocket_and_preserves_regular_cli():
    endpoint = AsyncMock(); guard = OfficeOriginGuard(endpoint); send = AsyncMock()
    await guard({"type": "websocket", "path": "/api/v1/ws", "headers": [(b"origin", b"http://office.localhost.:7001")]}, AsyncMock(), send)
    assert send.call_args.args[0] == {"type": "websocket.close", "code": 1008}; endpoint.assert_not_awaited()
    await guard({"type": "http", "path": "/api/v1/files", "headers": []}, AsyncMock(), send)
    endpoint.assert_awaited_once()
    assert not office_origin("http://localhost:7001")


@pytest.mark.parametrize("path", ["private/frame", "private/files", "private/status", "leases"])
def test_access_logs_redact_bearers(path):
    token = "x" * 43
    record = logging.LogRecord("uvicorn.access", 20, "", 1, "%s %s", ("GET", f"/api/v1/office-viewer/{path}/{token}"), None)
    assert OfficeLeaseLogFilter().filter(record)
    assert token not in record.getMessage() and "[redacted]" in record.getMessage()
