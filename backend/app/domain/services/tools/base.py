from contextvars import ContextVar, Token
from typing import List, Callable
import inspect
import copy

from langchain_core.tools.structured import StructuredTool
from langchain.tools import BaseTool
from langchain.messages import ToolMessage
from langchain.messages import ToolCall
from langchain_core.tools.base import BaseToolkit as LangchainBaseToolkit, ArgsSchema
from typing import Any, Optional
from pydantic import BaseModel, create_model, ConfigDict, PrivateAttr

from app.domain.services.tools.pipeline import (
    ToolCancellationCallback,
    ToolExecutionContext,
    ToolExecutionDisposer,
    ToolExecutionPipeline,
)


_ACTIVE_TOOL_EXECUTION: ContextVar[
    tuple[object, ToolExecutionContext] | None
] = ContextVar("active_tool_execution", default=None)


def _noop_disposer() -> None:
    """No-op disposer for calls made outside a pipeline invocation."""
    return None


def create_model_without_fields(model_class: type[BaseModel], exclude_fields: set[str]) -> type[BaseModel]:
    fields = {}
    for field_name, field_info in model_class.model_fields.items():
        if field_name not in exclude_fields:
            fields[field_name] = (field_info.annotation, field_info)
    return create_model(model_class.__name__, **fields)

class Tool(BaseTool):
    
    name: str = ""
    description: str = ""
    args_schema: ArgsSchema | None = None
    toolkit: 'BaseToolkit' = None

    def __init__(self, tool: StructuredTool, **kwargs: Any):
        super().__init__(**kwargs)
        self.name = tool.name
        self.description = tool.description
        self.args_schema = create_model_without_fields(tool.args_schema, {'self'})
        self._tool = tool

    def _run(self, **kwargs: Any) -> Any:
        return self._tool.func(self.toolkit, **kwargs)

    async def _arun(self, **kwargs: Any) -> Any:
        if self.args_schema is not None:
            allowed_args = set(self.args_schema.model_fields.keys())
            kwargs = {key: value for key, value in kwargs.items() if key in allowed_args}
        return await self._tool.coroutine(self.toolkit, **kwargs)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> ToolMessage:
        """Invoke tool and return a ToolMessage with the raw result stored in artifact."""
        async def execute(context):
            token = self.toolkit._bind_tool_execution_context(context)
            try:
                raw_result = await self._arun(**dict(context.arguments))
                content = (
                    raw_result.model_dump_json()
                    if hasattr(raw_result, "model_dump_json")
                    else str(raw_result)
                )
                return ToolMessage(
                    tool_call_id=context.tool_call_id,
                    name=self.name,
                    content=content,
                    artifact=raw_result,
                )
            finally:
                self.toolkit._reset_tool_execution_context(token)

        return await self.toolkit.tool_execution_pipeline.invoke(
            tool=self,
            tool_call=input,
            execute=execute,
        )


class BaseToolkit(LangchainBaseToolkit):
    """Base toolset class, providing common tool calling methods"""

    name: str = ""
    tools: List[Tool] = []
    enabled: bool = True
    _tool_execution_pipeline: ToolExecutionPipeline = PrivateAttr(
        default_factory=ToolExecutionPipeline
    )
    model_config = ConfigDict(ignored_types=(BaseTool,), extra='allow')

    def __init__(self):
        super().__init__()
        self.tools = []

        for _, tool in inspect.getmembers(self, lambda x: isinstance(x, BaseTool)):
            self.tools.append(Tool(tool, toolkit=self))

    @property
    def tool_execution_pipeline(self) -> ToolExecutionPipeline:
        return self._tool_execution_pipeline

    @tool_execution_pipeline.setter
    def tool_execution_pipeline(self, pipeline: ToolExecutionPipeline) -> None:
        self._tool_execution_pipeline = pipeline

    def _bind_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> Token[tuple[object, ToolExecutionContext] | None]:
        """Bind one invocation without storing task-local state on the toolkit."""
        return _ACTIVE_TOOL_EXECUTION.set((self, context))

    @staticmethod
    def _reset_tool_execution_context(
        token: Token[tuple[object, ToolExecutionContext] | None],
    ) -> None:
        _ACTIVE_TOOL_EXECUTION.reset(token)

    def register_tool_cancellation_callback(
        self,
        callback: ToolCancellationCallback,
    ) -> ToolExecutionDisposer:
        """Attach cooperative cleanup to this toolkit's active invocation.

        Direct ``_arun`` calls used by compatibility code do not have a
        pipeline context. Returning a no-op disposer keeps those calls valid;
        bounded tools must still implement their own local timeout cleanup.
        A ContextVar makes the binding safe when the same toolkit serves
        concurrent invocations.
        """
        active = _ACTIVE_TOOL_EXECUTION.get()
        if active is None or active[0] is not self:
            return _noop_disposer
        return active[1].register_cancellation_callback(callback)


    def get_tools(self) -> List[Tool]:
        """Get all registered tools
        
        Returns:
            List of tools
        """
        return self.tools if self.enabled else []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
    
    def get_tool(self, tool_name: str) -> Optional[Tool]:
        """Get specified tool
        
        Args:
            tool_name: Tool name
            
        Returns:
            Tool
        """
        if not self.enabled:
            return None
        for tool in self.tools:
            if tool.name == tool_name:
                return tool
        return None
