from __future__ import annotations

from typing import Any, Iterable

from app.domain.services.tools.pipeline import (
    ToolExecutionDisposer,
    ToolExecutionInterceptor,
    ToolExecutionPipeline,
)


def _tool_name(value: Any) -> str | None:
    if isinstance(value, dict):
        function = value.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            return name if isinstance(name, str) and name else None
        return None
    name = getattr(value, "name", None)
    return name if isinstance(name, str) and name else None


class ToolRegistry:
    """Compatibility registry over the existing ordered toolkit collection.

    Lookups intentionally remain live and first-match-wins, matching the
    previous ``BaseAgent`` behavior.  This also preserves toolkit enable/disable
    switches and filtered tool lists without copying their state.
    """

    def __init__(self, toolkits: Iterable[Any] = ()) -> None:
        self._toolkits = tuple(toolkits)

    def matches(self, toolkits: Iterable[Any]) -> bool:
        current = tuple(toolkits)
        return len(current) == len(self._toolkits) and all(
            candidate is registered
            for candidate, registered in zip(current, self._toolkits)
        )

    def get_tool(self, name: str) -> Any | None:
        for toolkit in self._toolkits:
            tool = toolkit.get_tool(name)
            if tool:
                return tool
        return None

    def get_tools(self) -> list[Any]:
        return [
            tool
            for toolkit in self._toolkits
            for tool in toolkit.get_tools()
        ]

    def tool_names(self) -> tuple[str, ...]:
        """Return the current model-facing names in deterministic order."""
        return tuple(
            name
            for item in self.get_tools()
            if (name := _tool_name(item)) is not None
        )

    def assert_unique_tool_names(self) -> None:
        """Reject ambiguous model-facing schemas before an Agent turn starts."""
        seen: set[str] = set()
        duplicates: set[str] = set()
        for toolkit in self._toolkits:
            for item in toolkit.get_tools():
                name = _tool_name(item)
                if name is None:
                    continue
                if name in seen:
                    duplicates.add(name)
                seen.add(name)
        if duplicates:
            rendered = ", ".join(sorted(duplicates)[:8])
            if len(duplicates) > 8:
                rendered += ", ..."
            raise ValueError(f"Agent tool names are ambiguous: {rendered}")

    def assert_execution_pipelines(self) -> None:
        """Require policy/interceptor coverage for every registered toolkit.

        A toolkit may discover or enable tools after the Agent is configured.
        Checking only toolkits that are non-empty at registration time would
        let those late tools bypass the production interceptor chain.
        """
        unsupported: list[str] = []
        for toolkit in self._toolkits:
            pipeline = getattr(toolkit, "tool_execution_pipeline", None)
            if isinstance(pipeline, ToolExecutionPipeline):
                continue
            name = getattr(toolkit, "name", type(toolkit).__name__)
            unsupported.append(str(name)[:80])
        if unsupported:
            rendered = ", ".join(sorted(unsupported)[:8])
            if len(unsupported) > 8:
                rendered += ", ..."
            raise ValueError(
                "Agent toolkits lack a supported execution pipeline: " + rendered
            )

    def register_interceptor(
        self,
        interceptor: ToolExecutionInterceptor,
    ) -> ToolExecutionDisposer:
        """Attach an interceptor to every distinct registered toolkit pipeline."""
        self.assert_execution_pipelines()
        disposers: list[ToolExecutionDisposer] = []
        seen_pipelines: set[int] = set()
        for toolkit in self._toolkits:
            pipeline = getattr(toolkit, "tool_execution_pipeline", None)
            if not isinstance(pipeline, ToolExecutionPipeline):
                continue
            pipeline_id = id(pipeline)
            if pipeline_id in seen_pipelines:
                continue
            seen_pipelines.add(pipeline_id)
            disposers.append(pipeline.register(interceptor))

        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            for unregister in reversed(disposers):
                unregister()

        return dispose

    def register_interceptors(
        self,
        interceptors: Iterable[ToolExecutionInterceptor],
    ) -> ToolExecutionDisposer:
        """Register an ordered interceptor bundle as one reversible unit."""
        self.assert_execution_pipelines()
        disposers: list[ToolExecutionDisposer] = []
        try:
            for interceptor in interceptors:
                disposers.append(self.register_interceptor(interceptor))
        except BaseException:
            for unregister in reversed(disposers):
                unregister()
            raise

        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            for unregister in reversed(disposers):
                unregister()

        return dispose
