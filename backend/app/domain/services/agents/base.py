import logging
import asyncio
import uuid
from abc import ABC
from typing import List, Dict, Any, Optional, AsyncGenerator, Callable
import httpx
from openai import APIConnectionError, APIStatusError
from app.domain.models.message import Message
from app.domain.services.tools.base import BaseToolkit
from app.domain.models.event import (
    BaseEvent,
    ToolEvent,
    ToolStatus,
    ErrorEvent,
    MessageEvent,
)
from app.domain.repositories.agent_repository import AgentRepository
from langchain_classic.output_parsers.retry import RetryWithErrorOutputParser
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from app.core.config import get_settings
from app.infrastructure.external.llm import create_chat_model
from langchain.messages import AIMessage, HumanMessage, ToolCall, ToolMessage, SystemMessage
from app.domain.services.tools.base import Tool
from app.domain.utils.robust_json_parser import RobustJsonParser, ToolCallParseError, parse_json_lenient
from app.domain.services.token_usage_service import TokenUsageService


logger = logging.getLogger(__name__)


class LLMServiceUnavailableError(RuntimeError):
    """Stable user-facing error after transient provider retries are exhausted."""


def _is_retryable_llm_error(error: Exception) -> bool:
    """Return whether an OpenAI-compatible model call may safely be retried."""
    if isinstance(error, (APIConnectionError, httpx.NetworkError, httpx.TimeoutException)):
        return True
    if isinstance(error, APIStatusError):
        status_code = getattr(error, "status_code", None)
        return status_code in {408, 409, 429} or (
            isinstance(status_code, int) and 500 <= status_code <= 599
        )
    return False


class BaseAgent(ABC):
    """
    Base agent class, defining the basic behavior of the agent
    """

    name: str = ""
    system_prompt: str = ""
    format: Optional[str] = None
    max_iterations: int = 1500
    max_retries: int = 3
    retry_interval: float = 1.0
    tool_choice: Optional[str] = None
    bind_tools: bool = True
    MAX_TOOL_MESSAGE_CONTENT_BYTES = 256 * 1024
    MAX_MEMORY_BYTES = 1024 * 1024

    _JSON_PARSE_PROMPT = PromptTemplate.from_template(
        "Extract or repair the JSON from the following LLM output.\n\n{input}"
    )

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        tools: List[BaseToolkit] = [],
        dynamic_system_prompt_provider: Optional[Callable[[], str]] = None,
        llm_overrides: Optional[dict] = None,
        usage_context: Optional[dict] = None,
        token_usage_service: Optional[TokenUsageService] = None,
    ):
        settings = get_settings()
        self._agent_id = agent_id
        self._repository = agent_repository
        self._model_provider = settings.model_provider
        self._model_name = settings.model_name
        self._llm_retry_attempts = max(1, settings.llm_retry_attempts)
        self._llm_retry_base_seconds = max(0.0, settings.llm_retry_base_seconds)
        self._llm_retry_max_seconds = max(0.0, settings.llm_retry_max_seconds)
        llm_overrides = llm_overrides or {}
        system_prompt_override = llm_overrides.get('system_prompt')
        if system_prompt_override:
            self.system_prompt = self.system_prompt + "\n\n" + system_prompt_override
        llm_kwargs = {
            k: v
            for k, v in llm_overrides.items()
            if k not in {'system_prompt', 'agent_profile'}
        }
        # This outer loop owns Agent retries. Disable the OpenAI SDK's inner
        # loop here so four configured attempts really mean four HTTP calls.
        llm_kwargs["client_max_retries"] = 0
        self._model = create_chat_model(settings, overrides=llm_kwargs)
        self._model_provider = llm_kwargs.get("model_provider") or self._model_provider
        self._model_name = llm_kwargs.get("model_name") or self._model_name
        self._json_output_parser = RetryWithErrorOutputParser.from_llm(
            parser=JsonOutputParser(),
            llm=self._model,
            max_retries=self.max_retries,
        )
        self.toolkits = tools
        self.memory = None
        self.dynamic_system_prompt_provider = dynamic_system_prompt_provider
        self.usage_context = usage_context or {}
        self.token_usage_service = token_usage_service or TokenUsageService()

    async def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM output, with local repair before LLM retry."""
        try:
            return parse_json_lenient(text)
        except Exception:
            logger.warning("Local JSON parsing failed, falling back to LLM repair parser")
        prompt_value = self._JSON_PARSE_PROMPT.format_prompt(input=text)
        return await self._json_output_parser.aparse_with_prompt(text, prompt_value)

    def _message_content_to_text(self, content: Any) -> str:
        """Normalize LangChain message content into the string event contract."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return str(content)

    def _tool_result_for_memory(
        self,
        tool_result: ToolMessage,
        tool_call_id: str,
        tool_name: str,
    ) -> ToolMessage:
        """Keep model context bounded and avoid persisting raw tool artifacts in memory."""
        content = self._message_content_to_text(tool_result.content)
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_TOOL_MESSAGE_CONTENT_BYTES:
            prefix = (
                "[Tool result truncated for model context; full inline output is available "
                "from the task tool event.]\n"
            )
            available = self.MAX_TOOL_MESSAGE_CONTENT_BYTES - len(prefix.encode("utf-8"))
            content = prefix + encoded[:max(0, available)].decode("utf-8", errors="ignore")
            logger.warning(
                "Tool %s result truncated from %d bytes for agent memory",
                tool_name,
                len(encoded),
            )
        return ToolMessage(tool_call_id=tool_call_id, name=tool_name, content=content)
    
    def get_tool(self, name: str) -> Optional[Tool]:
        """Get specified tool"""
        for toolkit in self.toolkits:
            tool = toolkit.get_tool(name)
            if tool:
                return tool
        return None

    def get_tools(self) -> List[Tool]:
        """Get all available tools list"""
        return [tool for toolkit in self.toolkits for tool in toolkit.get_tools()]

    async def invoke_tool(self, tool: Tool, tool_call: ToolCall) -> ToolMessage:
        """Invoke specified tool, with retry mechanism."""
        retries = 0
        while retries <= self.max_retries:
            try:
                return await tool.ainvoke(tool_call)
            except Exception as e:
                last_error = str(e)
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(self.retry_interval)
                else:
                    logger.exception(f"Tool execution failed, {tool_call['name']}, {tool_call['args']}")
                    break

        return ToolMessage(tool_call_id=tool_call["id"], name=tool.name, content=last_error)
    
    async def execute(self, request: str, format: Optional[str] = None) -> AsyncGenerator[BaseEvent, None]:
        format = format or self.format
        message = await self.ask(request, format)
        for _ in range(self.max_iterations):
            if not message.tool_calls:
                break
            tool_responses = []
            for tool_call in message.tool_calls:
                function_name = tool_call["name"]
                tool_call_id = tool_call["id"] = tool_call["id"] or str(uuid.uuid4())
                function_args = tool_call["args"]
                
                tool = self.get_tool(function_name)
                if not tool:
                    yield ErrorEvent(error=f"Unknown tool: {function_name}")
                    tool_responses.append(
                        ToolMessage(
                            tool_call_id=tool_call_id,
                            name=function_name,
                            content=f"Tool is unavailable: {function_name}",
                        )
                    )
                    continue

                # Generate event before tool call
                yield ToolEvent(
                    status=ToolStatus.CALLING,
                    tool_call_id=tool_call_id,
                    tool_name=tool.toolkit.name,
                    function_name=function_name,
                    function_args=function_args
                )

                tool_result = await self.invoke_tool(tool, tool_call)
                if tool_result.tool_call_id != tool_call_id:
                    logger.warning(
                        "Tool %s returned mismatched tool_call_id %r; using active call %r",
                        function_name,
                        tool_result.tool_call_id,
                        tool_call_id,
                    )
                    tool_result.tool_call_id = tool_call_id

                # Generate event after tool call
                yield ToolEvent(
                    status=ToolStatus.CALLED,
                    tool_call_id=tool_call_id,
                    tool_name=tool.toolkit.name,
                    function_name=function_name,
                    function_args=function_args,
                    function_result=tool_result.artifact
                )

                tool_responses.append(
                    self._tool_result_for_memory(tool_result, tool_call_id, function_name)
                )

            message = await self.ask_with_messages(tool_responses)
        else:
            yield ErrorEvent(error="Maximum iteration count reached, failed to complete the task")
        
        yield MessageEvent(message=self._message_content_to_text(message.content))
    
    async def _ensure_memory(self):
        if not self.memory:
            self.memory = await self._repository.get_memory(self._agent_id, self.name)
    
    async def _add_to_memory(self, messages: List[Dict[str, Any]]) -> None:
        """Update memory and save to repository"""
        await self._ensure_memory()
        if self.memory.empty:
            self.memory.add_message(SystemMessage(content=self.system_prompt))
        self.memory.add_messages(messages)
        self.memory.bound(self.MAX_MEMORY_BYTES, self.MAX_TOOL_MESSAGE_CONTENT_BYTES)
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
    
    async def _roll_back_memory(self) -> None:
        await self._ensure_memory()
        self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def ask_with_messages(self, messages: List[Dict[str, Any]], format: Optional[str] = None) -> AIMessage:
        await self._add_to_memory(messages)

        response_format = None
        if format:
            response_format = {"type": format}

        # Stage 1-3: model chain | RobustJsonParser repairs invalid tool call JSON.
        # Stages 4-5: outer retry loop handles cases that survive stages 1-3.
        runnable = self._model.bind(response_format=response_format, tool_choice=self.tool_choice)
        if self.bind_tools:
            runnable = runnable.bind_tools(self.get_tools())
        chain = runnable | RobustJsonParser.from_llm(self._model)

        context, repaired_history = self._repair_tool_call_history(self.memory.get_messages())
        if repaired_history:
            self.memory.messages = context
            await self._repository.save_memory(self._agent_id, self.name, self.memory)
        if self.dynamic_system_prompt_provider:
            dynamic_system_prompt = self.dynamic_system_prompt_provider()
            if dynamic_system_prompt:
                context.insert(1, SystemMessage(content=dynamic_system_prompt))
        transient_attempt = 0
        parse_attempt = 0
        while True:
            try:
                message: AIMessage = await chain.ainvoke(context)
                await self._record_token_usage(message)
                break
            except ToolCallParseError as e:
                parse_attempt += 1
                parse_attempts = max(1, self.max_retries)
                if parse_attempt >= parse_attempts:
                    raise
                logger.warning(
                    "Attempt %d/%d: tool call JSON repair failed, retrying model",
                    parse_attempt,
                    parse_attempts,
                )
                if parse_attempt == 1:
                    # Stage 4 (RetryOutputParser style): silent retry, same context.
                    pass
                else:
                    # Stage 5 (RetryWithErrorOutputParser style): add error feedback.
                    context = e.make_retry_context(context)
            except Exception as e:
                if not _is_retryable_llm_error(e):
                    raise
                transient_attempt += 1
                retry_attempts = max(
                    1,
                    getattr(self, "_llm_retry_attempts", max(1, self.max_retries)),
                )
                if transient_attempt >= retry_attempts:
                    logger.error(
                        "LLM provider remained unavailable after %d attempts (%s)",
                        retry_attempts,
                        type(e).__name__,
                    )
                    raise LLMServiceUnavailableError(
                        "模型服务暂时繁忙，系统已自动重试但仍未恢复。"
                        "请稍后重新提交任务，或切换可用的模型服务。"
                    ) from e
                base_delay = max(
                    0.0,
                    getattr(self, "_llm_retry_base_seconds", self.retry_interval),
                )
                max_delay = max(
                    0.0,
                    getattr(self, "_llm_retry_max_seconds", 8.0),
                )
                delay = min(base_delay * (2 ** (transient_attempt - 1)), max_delay)
                logger.warning(
                    "Attempt %d/%d: transient LLM failure (%s), retrying in %.1fs",
                    transient_attempt,
                    retry_attempts,
                    type(e).__name__,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
        logger.debug(f"Response from model: {message}")

        await self._add_to_memory([message])
        return message

    def _repair_tool_call_history(self, messages: List[Any]) -> tuple[List[Any], bool]:
        """Ensure every assistant tool call is immediately followed by a result."""
        repaired = False
        normalized: List[Any] = []
        pending: dict[str, str] = {}

        def append_missing_results() -> None:
            nonlocal repaired
            for tool_call_id, tool_name in pending.items():
                normalized.append(
                    ToolMessage(
                        tool_call_id=tool_call_id,
                        name=tool_name,
                        content="Tool call was interrupted before a result was recorded.",
                    )
                )
                repaired = True
            pending.clear()

        for message in messages:
            if pending:
                if isinstance(message, ToolMessage) and message.tool_call_id in pending:
                    normalized.append(message)
                    pending.pop(message.tool_call_id, None)
                    continue
                append_missing_results()

            if isinstance(message, AIMessage) and message.tool_calls:
                normalized.append(message)
                for tool_call in message.tool_calls:
                    tool_call_id = tool_call.get("id") or str(uuid.uuid4())
                    if not tool_call.get("id"):
                        tool_call["id"] = tool_call_id
                        repaired = True
                    pending[tool_call_id] = tool_call.get("name") or "unknown_tool"
                continue

            if isinstance(message, ToolMessage):
                # A tool result without an immediately preceding tool call is invalid for OpenAI.
                repaired = True
                continue

            normalized.append(message)

        if pending:
            append_missing_results()

        return normalized, repaired

    async def _record_token_usage(self, message: AIMessage) -> None:
        await self.token_usage_service.record_from_message(
            message,
            user_id=self.usage_context.get("user_id"),
            workspace_id=self.usage_context.get("workspace_id"),
            session_id=self.usage_context.get("session_id"),
            task_id=self.usage_context.get("task_id"),
            model_provider=self._model_provider,
            model_name=self._model_name,
        )

    async def ask(self, request: str, format: Optional[str] = None) -> AIMessage:
        return await self.ask_with_messages([
            HumanMessage(content=request)
        ], format)
    
    async def roll_back(self, message: Message):
        await self._ensure_memory()
        last_message = self.memory.get_last_message()
        if not last_message:
            return
        if last_message.type != "ai":
            return
        if not last_message.tool_calls:
            return
        tool_call = last_message.tool_calls[0]
        function_name = tool_call["name"]
        tool_call_id = tool_call["id"]
        if function_name == "message_ask_user":
            self.memory.add_message(ToolMessage(tool_call_id=tool_call_id, name=function_name, content=message))
        else:
            self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
    
    async def compact_memory(self) -> None:
        await self._ensure_memory()
        self.memory.bound(self.MAX_MEMORY_BYTES, self.MAX_TOOL_MESSAGE_CONTENT_BYTES)
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
