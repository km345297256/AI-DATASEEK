"""Credential-free contracts for the model request boundary.

Capabilities describe the adapter, not guessed model context windows or a
promise that every model from a provider supports tools.
"""

from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field


MODEL_DRIVER_VERSION = "langchain-driver/v1"


class ModelIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(max_length=64)
    model_name: str = Field(max_length=200)
    driver_version: str = MODEL_DRIVER_VERSION


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tool_calling: Literal["adapter", "unknown"] = "unknown"
    json_object: Literal["adapter", "unsupported", "unknown"] = "unknown"
    vision: Literal["unknown"] = "unknown"
    usage: Literal["reported_or_estimated"] = "reported_or_estimated"
    # The host retains completed model messages and its existing SSE, rather
    # than introducing a second native token-stream protocol.
    native_streaming: bool = False


PreparedModelInvoke = Callable[[list[BaseMessage], int], Awaitable[AIMessage]]


class ModelRequestMiddleware(Protocol):
    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tool_schemas: Sequence[dict[str, Any]] = (),
        response_format: dict[str, Any] | None = None,
        max_output_tokens: int,
        provider: str,
        model_name: str,
        driver_version: str = MODEL_DRIVER_VERSION,
        invoke: PreparedModelInvoke,
    ) -> AIMessage:
        """Prepare/reserve a request, invoke it once, and settle its usage."""
        ...
