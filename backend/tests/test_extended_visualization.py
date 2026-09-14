import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.services import extended_visualization as module
from app.application.services.file_service import FileService
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot
from app.interfaces.api.visualization_routes import router
from app.interfaces.dependencies import get_current_user, get_visualization_catalog

class Storage:
    def __init__(self):
        self.info = FileInfo(file_id="opaque", filename="values.csv", size=8, user_id="owner")
        self.reads = []
    async def get_file_info(self, file_id, user_id):
        return self.info if file_id == "opaque" and user_id == "owner" else None
    async def download_file_range(self, file_id, user_id, *, offset, length):
        self.reads.append((offset, length))
        return b"a,b\n1,2\n", self.info
    async def download_file(self, *args, **kwargs):
        raise AssertionError("Unbounded read must never be used")

class Preferences:
    def __init__(self): self.states = {}
    async def get_states(self, user_id): return self.states.get(user_id, {})
    async def set_state(self, user_id, plugin_id, enabled): self.states.setdefault(user_id, {})[plugin_id] = enabled

@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(module, "_SLOTS", asyncio.Semaphore(2))
    root = Path(__file__).resolve().parents[2] / "plugin-host/visualizations"
    snapshot = VisualizationSnapshot(engine="cordis", revision="a"*64,
        plugins=[json.loads(p.read_text()) for p in root.glob("*.json")])
    catalog = VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)), Preferences())
    return catalog, Storage()

def payload():
    return {"contract_version": 2, "type": "tabular", "reader": "tabular", "kind": "table",
        "media_type": "application/json", "table": {"columns": ["a", "b"], "rows": [["1", "2"]]},
        "metadata": {}, "warnings": [], "sampled": False}

async def invoke(env, *, request=None, worker=None, user="owner", binary=False):
    return await module.extended_visualization(FileService(env[1]), env[0], "sandbox:test", "opaque", user,
        request or module.ExtendedPreviewRequest(plugin_id="viz-plotly"), binary=binary,
        worker=worker or (lambda *args, **kwargs: {"ok": True, "data": payload()}))

@pytest.mark.asyncio
async def test_real_contract_and_bounded_owner_read(environment):
    data = await invoke(environment)
    assert data["plugin_id"] == "viz-plotly" and len(data["version"]) == 64
    assert data["table"]["rows"] == [["1", "2"]]
    assert environment[1].reads == [(0, 8)]

@pytest.mark.asyncio
async def test_catalog_has_one_protocol_without_version_negotiation(environment):
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    app.dependency_overrides[get_visualization_catalog] = lambda: environment[0]
    with TestClient(app) as client:
        plugins = client.get('/visualizations').json()['data']['plugins']
        assert len(plugins) == 83 and all(p['contract_version'] == 2 for p in plugins)
        states = {plugin['id']: plugin['enabled'] for plugin in plugins}
        assert states['viz-docx'] is True
        assert states['viz-onlyoffice'] is False
        assert all('data_kind' not in p and 'capabilities' in p for p in plugins)
        # Obsolete query strings cannot select another public protocol.
        assert client.get('/visualizations?contract_version=1').json()['data']['plugins'] == plugins

@pytest.mark.asyncio
async def test_foreign_private_and_disabled_reject_before_read(environment):
    with pytest.raises(FileNotFoundError): await invoke(environment, user="foreign")
    environment[1].info.metadata = {"source": "tool_output_spill"}
    with pytest.raises(FileNotFoundError): await invoke(environment)
    environment[1].info.metadata = {}
    await environment[0].set_state("owner", "viz-plotly", False)
    with pytest.raises(VisualizationDisabledError): await invoke(environment)
    assert environment[1].reads == []

@pytest.mark.asyncio
async def test_size_stale_options_and_wrong_api_reject_before_read(environment):
    for request in [module.ExtendedPreviewRequest(plugin_id="viz-plotly", version="f"*64),
                    module.ExtendedPreviewRequest(plugin_id="viz-plotly", options={"path": "/private/secret"})]:
        with pytest.raises((PreviewVersionChanged, module.ScientificPreviewRejected)): await invoke(environment, request=request)
    with pytest.raises(module.ScientificPreviewRejected): await invoke(environment, binary=True)
    environment[1].info.size = module.MAX_INPUT + 1
    with pytest.raises(module.ScientificPreviewRejected): await invoke(environment)
    assert environment[1].reads == []

@pytest.mark.asyncio
async def test_native_content_requires_its_enabled_capability(environment):
    environment[1].info.filename = "small.pdf"
    data, version = await invoke(environment, request=module.ExtendedPreviewRequest(plugin_id="viz-pdfjs"), binary=True)
    assert data == b"a,b\n1,2\n" and len(version) == 64
    await environment[0].set_state("owner", "viz-pdfjs", False)
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment, request=module.ExtendedPreviewRequest(plugin_id="viz-pdfjs"), binary=True)

@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"metadata": {"path": "/Users/private/example"}}, {"script": "evil"},
    {"array": {"shape": [2], "values": [1]}}, {"table": {"columns": ["x"], "rows": [[1]]*201}},
    {"data_base64": "PHNjcmlwdD4=", "media_type": "text/html"}, {"metadata": {"x": float("nan")}},
    {"reader": "office"}])
async def test_untrusted_worker_result_is_validated(environment, changes):
    with pytest.raises(module.VisualizationWorkerError):
        await invoke(environment, worker=lambda *args, **kwargs: {"ok": True, "data": {**payload(), **changes}})

@pytest.mark.asyncio
async def test_disabled_while_running_rejects_result(environment):
    started, release = threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        started.set(); release.wait(2); return {"ok": True, "data": payload()}
    task = asyncio.create_task(invoke(environment, worker=worker))
    await asyncio.to_thread(started.wait, 2)
    await environment[0].set_state("owner", "viz-plotly", False); release.set()
    with pytest.raises(VisualizationDisabledError): await task

@pytest.mark.asyncio
async def test_cancel_retains_admission_until_native_cleanup(environment):
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        started.set(); kwargs["cancelled"].wait(2); stopped.set(); release.wait(2)
        return {"ok": True, "data": payload()}
    task = asyncio.create_task(invoke(environment, worker=worker))
    await asyncio.to_thread(started.wait, 2); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert await asyncio.to_thread(stopped.wait, 2)
    assert module._SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._SLOTS._value == 2: break
        await asyncio.sleep(.005)
    assert module._SLOTS._value == 2

def test_reader_specific_parameter_limits_and_no_implicit_metpy_units():
    for reader, options in [('tabular', {'row_offset': True}), ('tabular', {'y_columns': [100]}),
                            ('hdf5', {'path': '/../bad'}), ('metpy', {}), ('office', {'macro': True}), ('fastqc', {'confirm': 1})]:
        with pytest.raises(module.ScientificPreviewRejected): module.validate_options(reader, options)
    module.validate_options('hdf5', {'path': '/entry/data', 'indices': [0]})
