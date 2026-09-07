from __future__ import annotations

from collections.abc import Iterable

from langchain.tools import tool

from app.domain.models.tool_result import ToolResult
from app.domain.services.code_mode import (
    CodeModeDispatcher,
    CodeModeInterpreter,
    CodeModeLimits,
    CodeModeToolSpec,
)
from app.domain.services.tools.base import BaseToolkit


CODE_MODE_TOOL_NAME = "code_mode_run"


class CodeModeToolkit(BaseToolkit):
    """Experimental, opt-in wrapper for the restricted tool orchestrator."""

    name: str = "code_mode"

    def __init__(
        self,
        dispatcher: CodeModeDispatcher,
        tool_specs: Iterable[CodeModeToolSpec],
        *,
        enabled: bool = False,
        limits: CodeModeLimits | None = None,
    ) -> None:
        super().__init__()
        self.dispatcher = dispatcher
        self.tool_specs = tuple(tool_specs)
        self.interpreter = CodeModeInterpreter(limits)
        self.enabled = bool(enabled)
        self.dataset_fast_path_tool_names = (
            {CODE_MODE_TOOL_NAME} if self.enabled else set()
        )

    def set_enabled(self, enabled: bool) -> None:
        super().set_enabled(enabled)
        self.dataset_fast_path_tool_names = (
            {CODE_MODE_TOOL_NAME} if self.enabled else set()
        )

    @tool(parse_docstring=True)
    async def code_mode_run(self, code: str) -> ToolResult:
        """Run a bounded sequence of approved read-only scientific tools.

        The program may use literal assignments, exact ``await tools.call``
        expressions, dictionary indexing, and one final value expression. It
        cannot import modules, access Python objects, or execute host code.

        Args:
            code: Restricted Python-shaped Code Mode program.
        """
        result = await self.interpreter.run(
            code,
            dispatcher=self.dispatcher,
            tool_specs=self.tool_specs,
        )
        return ToolResult(
            success=True,
            message="Code Mode completed its bounded tool sequence.",
            data=result.public_data(),
        )
