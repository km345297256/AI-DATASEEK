"""One request-local recovery context shared by all of its execution steps."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.domain.services.analysis_progress import AnalysisProgressGuard
from app.domain.services.execution_evidence import ToolExecutionLedger


@dataclass
class AnalysisRecoveryContext:
    review: Callable[..., Awaitable[object]]
    executions: ToolExecutionLedger = field(default_factory=ToolExecutionLedger)
    progress: AnalysisProgressGuard = field(default_factory=AnalysisProgressGuard)
    artifact_evidence: set[str] = field(default_factory=set)


_RECOVERY: ContextVar[AnalysisRecoveryContext | None] = ContextVar("analysis_recovery", default=None)


def current_analysis_recovery() -> AnalysisRecoveryContext | None:
    return _RECOVERY.get()


@contextmanager
def analysis_recovery_scope(context: AnalysisRecoveryContext):
    token = _RECOVERY.set(context)
    try:
        yield context
    finally:
        _RECOVERY.reset(token)
