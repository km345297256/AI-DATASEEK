"""Safe terminal messages for requests that never reached a normal result.

Use the existing analysis_outcome metadata contract. Never infer science,
artifacts, process cleanup or a lease cause from a session terminal alone.
"""
from typing import Literal

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import MessageEvent

PREPARATION_FAILURE_CODES = frozenset({
    "input_preparation_failed", "dataset_unreadable", "dataset_unsafe",
    "dataset_changed", "dataset_limit", "dataset_preparation_failed", "model_audit_unavailable",
})


def terminal_analysis_message(message: str, *, reason_code: str,
                              stage: Literal["routing", "input_preparation", "execution"],
                              analysis_started: bool | None, attempts: int | None = None) -> MessageEvent:
    metadata = {
        "analysis_outcome": AnalysisOutcome(status="failed", reason_code=reason_code).model_dump(exclude_none=True),
        "execution_stage": stage, "analysis_started": analysis_started,
    }
    if attempts is not None:
        metadata["preparation_attempts"] = attempts
    return MessageEvent(message=message, metadata=metadata)


def preparation_failure_message(code: str, attempts: int) -> MessageEvent:
    code = code if code in PREPARATION_FAILURE_CODES else "input_preparation_failed"
    explanations = {
        "dataset_changed": "只读挂载视图的身份校验不一致，请检查数据准备记录与挂载视图。",
        "dataset_unreadable": "分析用户无法读取数据，请检查来源的读取与目录遍历权限。",
        "dataset_unsafe": "数据未通过路径或文件类型安全检查，请检查来源中的链接和特殊文件。",
        "dataset_limit": "数据可读性检查超过当前安全范围，请检查目录规模与准备记录。",
        "model_audit_unavailable": "前置模型调用审计暂不可用，请先检查运行记录存储。",
    }
    return terminal_analysis_message(
        f"本轮在数据准备阶段失败（{code}，已尝试{attempts}次），分析尚未开始，未生成本轮分析成果。"
        + explanations.get(code, "请检查数据准备记录和运行环境。")
        + "系统不会自动重新执行此请求；排除原因后可发起新的请求。",
        reason_code=code, stage="input_preparation", analysis_started=False, attempts=attempts)
