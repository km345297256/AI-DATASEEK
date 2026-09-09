"""Independent v2/QC regression: no live DB, Docker worker, model or file writes."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.services import extended_visualization as preview
from app.application.services import visualization_jobs as service
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.models.file import FileInfo
from app.domain.models.spill import SpillArtifactRef, SpillArtifactChunk
from app.domain.models.visualization import VisualizationPlugin
from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError
from app.interfaces.api import visualization_job_routes as routes
from app.interfaces.dependencies import get_current_user
from test_analysis_job_service import InMemoryAnalysisJobRepository

OWNER = "visualization-owner"
FILE = "minio:visualization-unit-file"
REVISION = "a" * 64


def manifest(name="fastqc"):
    path = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations" / f"v2-{name}.json"
    return VisualizationPlugin.model_validate_json(path.read_text())


class Catalog:
    def __init__(self, plugin=None):
        self.plugin = plugin or manifest()
        self.enabled = True
        self.revision = REVISION
        self.calls = []

    async def require_enabled(self, owner, plugin_id):
        self.calls.append((owner, plugin_id))
        if not self.enabled:
            raise VisualizationDisabledError("disabled")
        assert plugin_id == self.plugin.id
        return self.plugin

    async def list_for_user(self, owner):
        return SimpleNamespace(revision=self.revision)


class Files:
    def __init__(self):
        self.content = b"@one\nACGT\n+\n!!!!\n"
        self.info = FileInfo(file_id=FILE, filename="sample.fastq", size=len(self.content),
                             user_id=OWNER, upload_date=datetime(2026, 1, 1, tzinfo=UTC), metadata={})
        self._file_storage = SimpleNamespace(download_file_range=AsyncMock(side_effect=self.read))

    async def get_file_info(self, file_id, owner):
        return self.info if self.info is not None and file_id == FILE and owner == OWNER else None

    async def read(self, file_id, owner, *, offset, length):
        assert (file_id, owner, offset, length) == (FILE, OWNER, 0, len(self.content))
        return self.content, self.info


class Jobs(AnalysisJobService):
    def __init__(self):
        self.repository = InMemoryAnalysisJobRepository()
        super().__init__(self.repository)
        self.tasks = []
        self.before_bind = None

    async def bind(self, job_id, task):
        self.tasks.append(task)
        if self.before_bind:
            self.before_bind(task)
        return await super().bind(job_id, task)

    async def settle(self):
        return await asyncio.gather(*self.tasks, return_exceptions=True)


class Artifacts:
    def __init__(self):
        self.saved = []
        self.read_owners = []
        self.text = ""
        self.chunk_override = None
        self.reference = None

    async def save_text(self, request):
        self.saved.append(request)
        self.text = request.content
        self.reference = SpillArtifactRef(locator="spill://artifact/" + "1" * 32,
            byte_count=len(self.text.encode()), sha256=hashlib.sha256(self.text.encode()).hexdigest(),
            media_type="application/json", retrieval_hint="private read")
        return self.reference

    async def read_text(self, locator, owner, *, offset=0, **_):
        self.read_owners.append(owner)
        assert (owner.user_id, owner.session_id) == (OWNER, service.scope_for_file(FILE))
        if self.chunk_override:
            return self.chunk_override(locator, offset)
        data = self.text.encode()
        return SpillArtifactChunk(locator=locator, content=data[offset:].decode(), start_byte=offset,
            next_byte=len(data), total_bytes=len(data), eof=True, sha256=self.reference.sha256, media_type="application/json")


def request(**changes):
    return preview.ExtendedPreviewRequest(plugin_id="viz-fastqc", options={"confirm": True}, **changes)


def qc_payload(files, catalog):
    return {"contract_version": 2, "type": "fastqc", "reader": "fastqc", "kind": "report",
        "media_type": "application/json", "sections": [{"name": "Basic Statistics", "status": "pass", "columns": ["Measure", "Value"], "rows": [["Total Sequences", "1"]]}],
        "metadata": {"engines": ["FastQC", "MultiQC"], "complete_input": True}, "warnings": [], "sampled": False,
        "version": preview_version(files.info), "revision": catalog.revision, "plugin_id": "viz-fastqc"}


@pytest.fixture(autouse=True)
def isolated_slots(monkeypatch):
    monkeypatch.setattr(service, "_ACTIVE", set())
    monkeypatch.setattr(service, "_START_LOCK", asyncio.Lock())
    monkeypatch.setattr(preview, "_SLOTS", asyncio.Semaphore(2))


@pytest.mark.asyncio
async def test_qc_success_is_durable_owner_scoped_without_agent_session(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    runner = AsyncMock(side_effect=lambda *_args, **_kwargs: qc_payload(files, catalog))
    monkeypatch.setattr(service, "extended_visualization", runner)
    view = await service.start_job(files, catalog, "sandbox:local", jobs, artifacts, FILE, OWNER, request())
    await jobs.settle()
    record = await jobs.get_for_owner(OWNER, service.scope_for_file(FILE), view.job_id)
    assert record.status == AnalysisJobStatus.SUCCEEDED
    assert record.catalog_revision == REVISION
    assert record.session_id.startswith("visualization-file:") and FILE not in record.session_id
    assert runner.call_args.kwargs["allow_job"] is True
    assert runner.call_args.args[5].version == preview_version(files.info)
    assert not service._ACTIVE
    result = await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)
    assert result["sections"][0]["rows"] == [["Total Sequences", "1"]]
    assert "user_id" not in view.model_dump() and "session_id" not in view.model_dump()


@pytest.mark.asyncio
async def test_qc_requires_explicit_confirmation_and_owned_public_file(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    runner = AsyncMock()
    monkeypatch.setattr(service, "extended_visualization", runner)
    with pytest.raises(ScientificPreviewRejected):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER,
            preview.ExtendedPreviewRequest(plugin_id="viz-fastqc", options={}))
    with pytest.raises(FileNotFoundError):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, "other", request())
    files.info = files.info.model_copy(update={"metadata": {"source": "tool_output_spill"}})
    with pytest.raises(FileNotFoundError):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    assert not jobs.repository.records and not runner.called


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [1, 1.0, "true", False, None])
async def test_qc_rejects_non_boolean_confirmation_before_creating_job(monkeypatch, confirm):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    runner = AsyncMock()
    monkeypatch.setattr(service, "extended_visualization", runner)
    with pytest.raises(ScientificPreviewRejected):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER,
            preview.ExtendedPreviewRequest(plugin_id="viz-fastqc", options={"confirm": confirm}))
    assert not jobs.repository.records and not runner.called and not artifacts.saved


@pytest.mark.asyncio
async def test_qc_rejects_stale_version_and_duplicate_running_file(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    with pytest.raises(PreviewVersionChanged):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request(version="f" * 64))
    blocker = asyncio.Event()
    async def run(*_, **__):
        await blocker.wait()
        return qc_payload(files, catalog)
    monkeypatch.setattr(service, "extended_visualization", run)
    view = await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    with pytest.raises(ScientificPreviewRejected):
        await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    blocker.set(); await jobs.settle()
    assert view.job_id not in service._ACTIVE


@pytest.mark.asyncio
async def test_qc_cancellation_before_coroutine_first_step_releases_active_capacity(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    jobs.before_bind = lambda task: task.cancel()
    runner = AsyncMock()
    monkeypatch.setattr(service, "extended_visualization", runner)
    view = await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    await jobs.settle(); await asyncio.sleep(0)
    assert view.job_id not in service._ACTIVE, "pre-start cancellation must not leak the process-wide two-job capacity"
    assert not runner.called and not artifacts.saved
    for _ in range(10):
        record = await jobs.get_for_owner(OWNER, service.scope_for_file(FILE), view.job_id)
        if record.status == AnalysisJobStatus.CANCELLED:
            break
        await asyncio.sleep(0)
    assert record.status == AnalysisJobStatus.CANCELLED, "a cancelled-before-start job must not block its file until lease expiry"


@pytest.mark.asyncio
async def test_qc_running_cancellation_records_cancelled_and_never_spills(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    running, blocker = asyncio.Event(), asyncio.Event()
    async def run(*_, **__):
        running.set(); await blocker.wait(); return qc_payload(files, catalog)
    monkeypatch.setattr(service, "extended_visualization", run)
    view = await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    await running.wait()
    await jobs.request_cancel(OWNER, service.scope_for_file(FILE), view.job_id)
    await jobs.settle()
    record = await jobs.get_for_owner(OWNER, service.scope_for_file(FILE), view.job_id)
    assert record.status == AnalysisJobStatus.CANCELLED
    assert not artifacts.saved and not service._ACTIVE


@pytest.mark.asyncio
async def test_qc_queue_revision_is_pinned_not_silently_replaced(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    jobs.before_bind = lambda _task: setattr(catalog, "revision", "b" * 64)
    runner = AsyncMock(side_effect=lambda *_args, **_kwargs: qc_payload(files, catalog))
    monkeypatch.setattr(service, "extended_visualization", runner)
    view = await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    await jobs.settle()
    record = await jobs.get_for_owner(OWNER, service.scope_for_file(FILE), view.job_id)
    assert record.status != AnalysisJobStatus.SUCCEEDED, "job record and execution must use the same catalog revision"
    assert not artifacts.saved


async def completed_job(monkeypatch):
    files, catalog, jobs, artifacts = Files(), Catalog(), Jobs(), Artifacts()
    monkeypatch.setattr(service, "extended_visualization", AsyncMock(side_effect=lambda *_a, **_k: qc_payload(files, catalog)))
    view = await service.start_job(files, catalog, "image", jobs, artifacts, FILE, OWNER, request())
    await jobs.settle()
    return files, catalog, jobs, artifacts, view


@pytest.mark.asyncio
async def test_qc_result_rechecks_current_file_owner_plugin_and_version(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    with pytest.raises(FileNotFoundError):
        await service.read_result(files, catalog, jobs, artifacts, FILE, "other", view.job_id)
    with pytest.raises(FileNotFoundError):
        await service.read_result(files, catalog, jobs, artifacts, "other-file", OWNER, view.job_id)
    catalog.enabled = False
    with pytest.raises(VisualizationDisabledError):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)
    catalog.enabled = True
    files.info = files.info.model_copy(update={"metadata": {"sha256": "new-content"}})
    with pytest.raises(PreviewVersionChanged):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


@pytest.mark.asyncio
async def test_qc_result_rechecks_plugin_after_artifact_read(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    original = artifacts.read_text
    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs); catalog.enabled = False; return result
    artifacts.read_text = revoke
    with pytest.raises(VisualizationDisabledError):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


@pytest.mark.asyncio
async def test_qc_result_disable_during_final_file_lookup_refuses_return(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    original = files.get_file_info
    calls = 0
    async def revoke(file_id, owner):
        nonlocal calls
        calls += 1
        result = await original(file_id, owner)
        if calls == 2:
            catalog.enabled = False
        return result
    files.get_file_info = revoke
    with pytest.raises(VisualizationDisabledError):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


@pytest.mark.asyncio
async def test_qc_result_rejects_actual_content_over_budget_even_if_chunk_metadata_lies(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    monkeypatch.setattr(service, "MAX_OUTPUT", artifacts.reference.byte_count + 32)
    value = qc_payload(files, catalog); value["metadata"]["padding"] = "x" * 2000
    artifacts.chunk_override = lambda locator, offset: SpillArtifactChunk(locator=locator,
        content=json.dumps(value), start_byte=0, next_byte=artifacts.reference.byte_count,
        total_bytes=artifacts.reference.byte_count, eof=True, sha256=artifacts.reference.sha256, media_type="application/json")
    with pytest.raises((ScientificPreviewRejected, VisualizationWorkerError, ValueError)):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


@pytest.mark.asyncio
async def test_qc_result_rejects_malformed_v2_data_even_when_version_matches(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    value = qc_payload(files, catalog); value["contract_version"] = 999
    artifacts.text = json.dumps(value)
    reference = artifacts.reference.model_copy(update={"byte_count": len(artifacts.text.encode()), "sha256": hashlib.sha256(artifacts.text.encode()).hexdigest()})
    artifacts.reference = reference
    jobs.repository.records[view.job_id] = jobs.repository.records[view.job_id].model_copy(update={"result_spill": reference})
    with pytest.raises((ScientificPreviewRejected, VisualizationWorkerError, ValueError)):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


@pytest.mark.asyncio
async def test_qc_result_rejects_wrong_chunk_digest(monkeypatch):
    files, catalog, jobs, artifacts, view = await completed_job(monkeypatch)
    original = artifacts.read_text
    async def bad_digest(*args, **kwargs):
        return (await original(*args, **kwargs)).model_copy(update={"sha256": "0" * 64})
    artifacts.read_text = bad_digest
    with pytest.raises((ScientificPreviewRejected, VisualizationWorkerError, ValueError)):
        await service.read_result(files, catalog, jobs, artifacts, FILE, OWNER, view.job_id)


def test_backend_accepts_worker_root_heatmap_and_long_valid_internal_hdf_path():
    assert "heatmap" in preview._KINDS["root"], "TH2 UI and sandbox worker already support heatmap"
    path = "/" + "/".join(["a" * 100] * 3)
    assert 256 < len(path) <= 512
    preview.validate_options("hdf5", {"path": path})


@pytest.mark.parametrize("reader,kind,payload", [
    ("tabular", "series", {"array": {"shape": [3], "values": [1.25, None, 3.5], "dimensions": ["x"]}}),
    ("hdf5", "tree", {"tree": [{"path": "/group/temperature", "node_type": "dataset", "shape": [3], "attributes": {"units": "K", "range": [1.5, 2.5]}}]}),
    ("excel", "table", {"table": {"columns": ["A", "B"], "rows": [[1.25, None]], "formulas": [[None, "=SUM(A1:A3)"]], "total_rows": 1, "total_columns": 2}}),
    ("fastqc", "report", {"sections": [{"name": "QC", "status": "pass", "columns": ["measure"], "rows": [["1"]]}]}),
    ("fastqc", "report", {"sections": [{"name": "Per base sequence quality", "status": "warn", "columns": ["Base", "Mean"], "rows": [[str(i), "32.5"] for i in range(500)]}]}),
    ("office", "pdf", {"media_type": "application/pdf", "data_base64": base64.b64encode(b"%PDF-1.7\nlocal").decode()}),
    ("rdkit", "image", {"media_type": "image/png", "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nlocal").decode()}),
])
def test_legitimate_worker_envelopes_pass_validation(reader, kind, payload):
    value = {"contract_version": 2, "type": reader, "reader": reader, "kind": kind, "media_type": "application/json", "metadata": {}, "warnings": [], "sampled": False, **payload}
    assert preview.validate_payload(value, reader, kind, 1024 * 1024) is value


@pytest.mark.parametrize("payload", [
    {"sections": {}},
    {"sections": [{"name": "QC", "status": "unknown", "columns": [], "rows": []}]},
    {"sections": [{"name": "QC", "status": "pass", "columns": ["measure"], "rows": [["ok"]]}] * 33},
    {"sections": [{"name": "QC", "status": "pass", "columns": ["measure"], "rows": [[{"nested": "bad"}]]}]},
])
def test_worker_quality_sections_must_conform_to_declared_structure(payload):
    value = {"contract_version": 2, "type": "fastqc", "reader": "fastqc", "kind": "report", "media_type": "application/json", "metadata": {}, "warnings": [], "sampled": False, **payload}
    with pytest.raises(ValueError):
        preview.validate_payload(value, "fastqc", "report", 1024 * 1024)


@pytest.mark.parametrize("values", [["123"], [True], [{"unsafe": "not a number"}], [[1]]])
def test_numeric_worker_array_does_not_accept_nested_or_nonnumeric_values(values):
    value = {"contract_version": 2, "type": "tabular", "reader": "tabular", "kind": "series", "media_type": "application/json", "metadata": {}, "warnings": [], "sampled": False, "array": {"shape": [1], "values": values}}
    with pytest.raises(ValueError):
        preview.validate_payload(value, "tabular", "series", 1024 * 1024)


def test_cancel_route_preserves_owner_scope_after_plugin_disabled_or_file_deleted():
    owner = SimpleNamespace(id=OWNER)
    record = SimpleNamespace(tool_name="visualization:viz-fastqc", public_view=lambda: {"status": "cancelled"})
    jobs = SimpleNamespace(get_for_owner=AsyncMock(return_value=record), request_cancel=AsyncMock(return_value=record))
    app = FastAPI(); app.include_router(routes.router, prefix="/files")
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_analysis_job_service] = lambda: jobs
    with TestClient(app) as client:
        assert client.post(f"/files/{FILE}/visualization-jobs/abc/cancel").status_code == 400
        response = client.post(f"/files/{FILE}/visualization-jobs/abc/cancel", headers={"X-Analysis-Job-Action": "cancel"})
        assert response.status_code == 200
    jobs.get_for_owner.assert_awaited_once_with(OWNER, service.scope_for_file(FILE), "abc")
    jobs.request_cancel.assert_awaited_once_with(OWNER, service.scope_for_file(FILE), "abc")


def test_cancel_route_cannot_cancel_foreign_file_or_job():
    jobs = SimpleNamespace(get_for_owner=AsyncMock(return_value=None), request_cancel=AsyncMock())
    app = FastAPI(); app.include_router(routes.router, prefix="/files")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="other")
    app.dependency_overrides[get_analysis_job_service] = lambda: jobs
    with TestClient(app) as client:
        response = client.post(f"/files/{FILE}/visualization-jobs/abc/cancel", headers={"X-Analysis-Job-Action": "cancel"})
    assert response.status_code == 404 and not jobs.request_cancel.called


@pytest.mark.asyncio
async def test_extended_binary_disable_during_range_read_refuses_return():
    files, catalog = Files(), Catalog(manifest("igv"))
    files.info = files.info.model_copy(update={"filename": "track.bed"})
    async def revoke(file_id, owner, **kwargs):
        result = await files.read(file_id, owner, **kwargs); catalog.enabled = False; return result
    files._file_storage.download_file_range = revoke
    with pytest.raises(VisualizationDisabledError):
        await preview.extended_visualization(files, catalog, None, FILE, OWNER,
            preview.ExtendedPreviewRequest(plugin_id=catalog.plugin.id), binary=True)
    assert preview._SLOTS._value == 2


@pytest.mark.asyncio
async def test_extended_binary_disable_during_final_file_lookup_refuses_return():
    files, catalog = Files(), Catalog(manifest("igv"))
    files.info = files.info.model_copy(update={"filename": "track.bed"})
    original = files.get_file_info
    calls = 0
    async def revoke(file_id, owner):
        nonlocal calls
        calls += 1
        result = await original(file_id, owner)
        if calls == 2:
            catalog.enabled = False
        return result
    files.get_file_info = revoke
    with pytest.raises(VisualizationDisabledError):
        await preview.extended_visualization(files, catalog, None, FILE, OWNER,
            preview.ExtendedPreviewRequest(plugin_id=catalog.plugin.id), binary=True)
    assert preview._SLOTS._value == 2


@pytest.mark.asyncio
async def test_extended_range_cancellation_retains_native_slot_until_read_finishes():
    files, catalog = Files(), Catalog(manifest("igv"))
    files.info = files.info.model_copy(update={"filename": "track.bed"})
    running, finish = asyncio.Event(), asyncio.Event()
    async def read(file_id, owner, **kwargs):
        running.set(); await finish.wait(); return await files.read(file_id, owner, **kwargs)
    files._file_storage.download_file_range = read
    task = asyncio.create_task(preview.extended_visualization(files, catalog, None, FILE, OWNER,
        preview.ExtendedPreviewRequest(plugin_id=catalog.plugin.id), binary=True))
    await running.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert preview._SLOTS._value == 1
    finish.set(); await asyncio.sleep(0); await asyncio.sleep(0)
    assert preview._SLOTS._value == 2
