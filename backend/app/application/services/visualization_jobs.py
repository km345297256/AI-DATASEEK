"""Explicit file-scoped QC jobs using the existing durable job/artifact stores.

The namespaced owner scope is private, not an Agent session; no chat is created
and no model is called. Every read/cancel/result also checks current file access
and the enabled Cordis capability. Artifacts expire under the existing policy.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import uuid

from app.application.services.extended_visualization import MAX_INPUT, MAX_OUTPUT, extended_visualization, validate_payload
from app.application.services.file_preview import preview_version, PreviewVersionChanged
from app.application.services.file_service import _is_private_spill
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.domain.models.analysis_job import AnalysisJobStatus, ANALYSIS_JOB_ACTIVE_STATUSES
from app.domain.models.spill import SpillArtifactOwner, SpillArtifactSaveRequest, SpillArtifactSource

_START_LOCK = asyncio.Lock()
_ACTIVE: set[str] = set()


def scope_for_file(file_id: str) -> str:
    return "visualization-file:" + hashlib.sha256(file_id.encode()).hexdigest()


async def authorize(file_service, catalog, file_id, user_id):
    plugin = await catalog.require_enabled(user_id, "viz-fastqc")
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    if plugin.reader != "fastqc" or not plugin.matches_filename(info.filename or ""):
        raise ScientificPreviewRejected("此文件不支持 FastQC 质控插件。")
    if type(info.size) is not int or not 0 < info.size <= min(MAX_INPUT, plugin.limits.max_input_bytes):
        raise ScientificPreviewRejected("文件超过单次质控插件上限。")
    return info


async def start_job(file_service, catalog, image, jobs, artifacts, file_id, user_id, request):
    if (request.plugin_id != "viz-fastqc" or set(request.options) != {"confirm"}
        or request.options.get("confirm") is not True or request.kind not in (None, "report")):
        raise ScientificPreviewRejected("仅支持明确确认的 FastQC 质控任务。")
    info = await authorize(file_service, catalog, file_id, user_id)
    if request.version and preview_version(info) != request.version:
        raise PreviewVersionChanged()
    request = request.model_copy(update={"version": preview_version(info)})
    scope = scope_for_file(file_id)
    async with _START_LOCK:
        previous = await jobs.list_for_owner(user_id, scope, limit=100)
        if any(job.status in ANALYSIS_JOB_ACTIVE_STATUSES for job in previous) or len(_ACTIVE) >= 2:
            raise ScientificPreviewRejected("已有质控任务运行中，请等待完成或先取消。")
        revision = (await catalog.list_for_user(user_id)).revision
        record = await jobs.create(user_id=user_id, session_id=scope, task_id=uuid.uuid4().hex,
            tool_name="visualization:viz-fastqc", tool_call_id=uuid.uuid4().hex,
            catalog_revision=revision, timeout_seconds=60, cancellable=True)
        _ACTIVE.add(record.job_id)
        ready = asyncio.Event()
        entered = asyncio.Event()
        async def execute():
            try:
                entered.set()
                await ready.wait()
                async with asyncio.timeout(60):
                    if (await catalog.list_for_user(user_id)).revision != record.catalog_revision:
                        raise PreviewVersionChanged()
                    state = await jobs.mark_running(record.job_id)
                    if state.status != AnalysisJobStatus.RUNNING:
                        raise asyncio.CancelledError()
                    payload = await extended_visualization(file_service, catalog, image, file_id, user_id, request, allow_job=True)
                    if payload.get("revision") != record.catalog_revision:
                        raise PreviewVersionChanged()
                    reference = await artifacts.save_text(SpillArtifactSaveRequest(
                        owner=SpillArtifactOwner(user_id=user_id, session_id=scope),
                        source=SpillArtifactSource(tool_name="visualization:viz-fastqc", tool_call_id=record.job_id, label="visualization-qc"),
                        content=json.dumps(payload, ensure_ascii=False, allow_nan=False), media_type="application/json"))
                    await jobs.finish(record.job_id, AnalysisJobStatus.SUCCEEDED, result_spill=reference)
            except asyncio.CancelledError:
                await asyncio.shield(jobs.finish(record.job_id, AnalysisJobStatus.CANCELLED))
                raise
            except TimeoutError:
                await jobs.finish(record.job_id, AnalysisJobStatus.TIMED_OUT)
            except Exception:
                await jobs.finish(record.job_id, AnalysisJobStatus.FAILED)
            finally:
                _ACTIVE.discard(record.job_id)
        task = asyncio.create_task(execute())
        def completed(done):
            # A task cancelled before its first coroutine step never enters finally.
            _ACTIVE.discard(record.job_id)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        try:
            # Make cancellation enter execute's terminal-state handler even if
            # bind immediately observes an already requested cancellation.
            await entered.wait()
            await jobs.bind(record.job_id, task)
        except BaseException:
            task.cancel(); _ACTIVE.discard(record.job_id)
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            ready.set()
    return record.public_view()


async def require_job(file_service, catalog, jobs, file_id, user_id, job_id):
    await authorize(file_service, catalog, file_id, user_id)
    record = await jobs.get_for_owner(user_id, scope_for_file(file_id), job_id)
    if record is None or record.tool_name != "visualization:viz-fastqc":
        raise FileNotFoundError("Job not found")
    return record


async def read_result(file_service, catalog, jobs, artifacts, file_id, user_id, job_id):
    record = await require_job(file_service, catalog, jobs, file_id, user_id, job_id)
    if record.status != AnalysisJobStatus.SUCCEEDED or record.result_spill is None:
        raise ScientificPreviewRejected("质控任务尚未成功完成。")
    if record.result_spill.byte_count > MAX_OUTPUT:
        raise ScientificPreviewRejected("质控结果超过显示上限。")
    owner = SpillArtifactOwner(user_id=user_id, session_id=scope_for_file(file_id))
    offset, chunks, actual_bytes = 0, [], 0
    digest = hashlib.sha256()
    for _ in range(1024):
        part = await artifacts.read_text(record.result_spill.locator, owner, offset=offset)
        encoded = part.content.encode("utf-8")
        actual_bytes += len(encoded)
        digest.update(encoded)
        if (part.locator != record.result_spill.locator or part.sha256 != record.result_spill.sha256
            or part.start_byte != offset or part.total_bytes != record.result_spill.byte_count
            or actual_bytes > MAX_OUTPUT or part.next_byte != offset + len(encoded)
            or (not part.eof and part.next_byte <= offset)):
            raise ScientificPreviewRejected("质控结果范围无效。")
        chunks.append(part.content)
        if part.eof:
            break
        offset = part.next_byte
    else:
        raise ScientificPreviewRejected("质控结果超出读取窗口。")
    if actual_bytes != record.result_spill.byte_count or digest.hexdigest() != record.result_spill.sha256:
        raise ScientificPreviewRejected("质控结果完整性校验失败。")
    result = json.loads("".join(chunks))
    if not isinstance(result, dict) or result.get("plugin_id") != "viz-fastqc" or result.get("revision") != record.catalog_revision:
        raise ScientificPreviewRejected("质控结果协议不一致。")
    validate_payload({key: value for key, value in result.items() if key not in {"version", "plugin_id", "revision"}}, "fastqc", "report", MAX_OUTPUT)
    info = await authorize(file_service, catalog, file_id, user_id)
    if result.get("version") != preview_version(info) or result.get("revision") != (await catalog.list_for_user(user_id)).revision:
        raise PreviewVersionChanged()
    await catalog.require_enabled(user_id, "viz-fastqc")
    return result
