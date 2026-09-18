from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from langchain.messages import ToolMessage

from app.domain.models.analysis_job import AnalysisJobStatus, AnalysisJobView
from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.model_runtime import ModelBudgetStopped
from app.domain.services.tools.pipeline import ToolExecutionContext, ToolExecutionInterceptor, ToolExecutionNext
from app.domain.services.tools.spill_projection import projected_tool_artifact, spill_notice_from_result


JOB_CONTEXT_KEY = "analysis_job_record"
JOB_ADMITTED_KEY = "analysis_job_admitted"
JOB_CORE_TOOLS = frozenset({"shell_run", "program_run", "dataset_unpack", "dataset_quicklook"})
logger = logging.getLogger(__name__)


class AnalysisJobCancelled(asyncio.CancelledError):
    """User cancellation stops this dependent turn without entering tool retries."""


class AnalysisJobInterrupted(asyncio.CancelledError):
    """The worker lost its execution lease; do not silently retry side effects."""


class AnalysisJobInterceptor(ToolExecutionInterceptor):
    """Track an independently cancellable execution, preserving the tool await contract."""

    def __init__(
        self,
        service: AnalysisJobService,
        *,
        user_id: str,
        session_id: str,
        identity_provider: Callable[[], dict[str, Any]],
        event_sink: Callable[[AnalysisJobView, ToolExecutionContext], Awaitable[None]] | None = None,
    ) -> None:
        self._service = service
        self._user_id = user_id
        self._session_id = session_id
        self._identity_provider = identity_provider
        self._event_sink = event_sink

    async def execute(self, context: ToolExecutionContext, call_next: ToolExecutionNext) -> Any:
        if not context.metadata.get("plugin") and context.tool_name not in JOB_CORE_TOOLS:
            return await call_next()
        identity = self._identity_provider()
        if not identity.get("task_id"):
            raise RuntimeError("Analysis job requires a pinned task identity")
        contract = context.metadata.get("execution_contract") or getattr(context.tool, "execution_contract", {})
        contract = contract if isinstance(contract, dict) else {}
        timeout = contract.get("timeout_seconds", 120)
        if context.tool_name in JOB_CORE_TOOLS:
            timeout = context.arguments.get("timeout_seconds", 30 if context.tool_name in {"shell_run", "program_run"} else 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)):
            timeout = 120
        timeout = max(1, min(float(timeout), 120))
        from app.domain.services.program_execution import is_trusted_program_tool
        if not context.metadata.get("plugin") and is_trusted_program_tool(context.tool):
            # program_run's argument controls one status observation window,
            # not the lifetime of the cancellable, continuously leased job.
            timeout = None

        async def publish(view: AnalysisJobView) -> None:
            if self._event_sink is not None:
                await self._event_sink(view, context)

        record = await self._service.create(
            user_id=self._user_id, session_id=self._session_id,
            task_id=identity["task_id"], tool_name=context.tool_name,
            tool_call_id=str(context.tool_call_id),
            execution_snapshot_id=identity.get("execution_snapshot_id"),
            catalog_revision=identity.get("catalog_revision"),
            cancellable=contract.get("cancellable") is not False,
            timeout_seconds=timeout, on_update=publish,
        )
        context.metadata[JOB_CONTEXT_KEY] = record

        async def admitted() -> None:
            latest = await self._service.mark_running(record.job_id)
            if latest.status != AnalysisJobStatus.RUNNING:
                raise asyncio.CancelledError()

        context.metadata[JOB_ADMITTED_KEY] = admitted
        # Bind before execution is admitted, so cancellation can win even in
        # the narrow interval between a queued event and worker registration.
        ready = asyncio.Event()

        async def run_worker() -> Any:
            await ready.wait()
            return await call_next()

        worker = asyncio.create_task(run_worker())
        try:
            await self._service.bind(record.job_id, worker)
            ready.set()
            return await worker
        except asyncio.CancelledError as error:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            from app.domain.services.tools.authorization import ToolAuthorizationStopped
            if isinstance(error, (ModelBudgetStopped, ToolAuthorizationStopped)):
                raise
            parent = asyncio.current_task()
            if parent is not None and parent.cancelling():
                raise
            # Follow the existing cancellation path. Returning a generic tool
            # failure here would let the compiler/Planner automatically retry
            # an operation that the user just stopped.
            try:
                latest = await self._service.get_for_owner(
                    self._user_id, self._session_id, record.job_id,
                )
            except Exception:
                latest = None
            if latest is not None and latest.cancel_requested_at is not None and latest.status != AnalysisJobStatus.INTERRUPTED:
                raise AnalysisJobCancelled() from None
            raise AnalysisJobInterrupted() from None
        except BaseException:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            raise

    async def complete(self, context: ToolExecutionContext, result: Any) -> None:
        record = context.metadata.get(JOB_CONTEXT_KEY)
        if record is None:
            return
        artifact = getattr(result, "artifact", None) or projected_tool_artifact(result)
        if isinstance(artifact, ToolResult):
            success, data = artifact.success, artifact.data
        elif isinstance(artifact, dict):
            success, data = artifact.get("success") is True, artifact.get("data")
        else:
            success, data = False, None
        data = data if isinstance(data, dict) else {}
        if data.get("status") == "cancelled":
            status, code = AnalysisJobStatus.CANCELLED, "cancelled"
        elif data.get("status") == "unknown":
            status, code = AnalysisJobStatus.INTERRUPTED, "worker_interrupted"
        elif data.get("status") in {"timed_out", "timeout"}:
            status, code = AnalysisJobStatus.TIMED_OUT, "tool_timeout"
        else:
            status = AnalysisJobStatus.SUCCEEDED if success else AnalysisJobStatus.FAILED
            code = None if success else "tool_failed"
        notice = spill_notice_from_result(result)
        try:
            await self._service.finish(
                record.job_id, status, error_code=code,
                result_spill=notice.reference if notice is not None else None,
            )
        except Exception as error:
            # Preserve the real tool result. An unconfirmed state is recovered
            # as interrupted after its lease, never replayed automatically.
            logger.warning("Analysis job completion persistence failed error_type=%s", type(error).__name__)

    async def on_error(self, context: ToolExecutionContext, error: BaseException) -> None:
        record = context.metadata.get(JOB_CONTEXT_KEY)
        if record is None:
            return
        if isinstance(error, AnalysisJobInterrupted):
            status, code = AnalysisJobStatus.INTERRUPTED, "worker_interrupted"
        elif isinstance(error, asyncio.CancelledError):
            status, code = AnalysisJobStatus.CANCELLED, "cancelled"
        elif getattr(error, "code", None) == "tool_timeout" or context.metadata.get("execution_outcome_hint") == "timeout":
            status, code = AnalysisJobStatus.TIMED_OUT, "tool_timeout"
        else:
            status, code = AnalysisJobStatus.FAILED, "tool_failed"
        await self._service.finish(record.job_id, status, error_code=code)
