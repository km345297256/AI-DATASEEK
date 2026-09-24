"""Decode complete native tool arguments without inventing executable input.

Only a complete JSON object, optionally enclosed in a complete JSON fence, can
be promoted from invalid_tool_calls. Partial parsing and model rewriting may
drop a key or finish a truncated string, so neither may authorize execution.
ToolCallParseError preserves the caller's existing bounded resend protocol.
The separate parse_json_lenient helper is for non-executable response parsing.
"""
import asyncio
import logging
import json
import math
import re
import uuid
from typing import Any, Optional

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.tool import tool_call as create_tool_call
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.utils.json import parse_json_markdown, parse_partial_json
from langchain_classic.output_parsers.fix import OutputFixingParser
from app.domain.services.model_runtime import model_call_role

logger = logging.getLogger(__name__)

_EXPLICIT_JSON_FENCE = re.compile(
    r"```[ \t]*json[ \t]*(?:\r?\n|\s)(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_COMPLETE_JSON_FENCE = re.compile(
    r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```\s*", re.IGNORECASE | re.DOTALL,
)


def _complete_tool_argument_object(raw: str) -> Optional[dict]:
    """Decode the entire object, rejecting ambiguous keys and non-JSON numbers."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_argument_key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("nonfinite_argument_number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_argument_number")
        return number

    try:
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=invalid_constant, parse_float=finite_float)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None

_RETRY_WITH_ERROR_TEMPLATE = (
    "Your previous response contained invalid tool call arguments or identities.\n"
    "Error details:\n{error}\n\n"
    "Return one shorter, complete native tool call in the next response. "
    "Never guess or fill in missing or truncated content. "
    "If the intended content is too long for one response, use smaller write/append calls, each with "
    "a complete JSON object and complete intended content for that part, one call per response. "
    "Do not replay operations that already succeeded."
    " Every tool call in a response must have a distinct non-empty call ID."
)


def validate_tool_call_identity(message: AIMessage) -> AIMessage:
    """Validate the whole batch before dispatch; fill absent IDs on a copy only.

    Reject collisions rather than executing a valid prefix. Arguments and raw
    provider diagnostics never appear in correction feedback. This also runs
    after lossless promotion of invalid_tool_calls, so mixed batches cannot
    bypass the identity check.
    """
    seen: set[str] = set()
    missing = False
    for call in message.tool_calls:
        identity = call.get("id")
        if identity is None or identity == "":
            missing = True
            continue
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            detail = "Tool call identity is invalid or duplicated; this batch was not executed."
            raise ToolCallParseError(detail, message, [detail])
        seen.add(identity)
    if not missing:
        return message
    normalized = message.model_copy(deep=True)
    for call in normalized.tool_calls:
        if not call.get("id"):
            identity = str(uuid.uuid4())
            while identity in seen:
                identity = str(uuid.uuid4())
            call["id"] = identity
            seen.add(identity)
    return normalized


def _escape_unescaped_quotes_in_strings(raw: str) -> str:
    """Escape likely accidental quotes inside JSON string values.

    This handles a common LLM failure mode such as:
    {"title": "3亿北斗工程现"脆皮底座""}
    """
    result: list[str] = []
    in_string = False
    escaped = False
    i = 0

    while i < len(raw):
        char = raw[i]

        if escaped:
            result.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\":
            result.append(char)
            escaped = in_string
            i += 1
            continue

        if char == '"':
            if not in_string:
                in_string = True
                result.append(char)
                i += 1
                continue

            j = i + 1
            while j < len(raw) and raw[j].isspace():
                j += 1
            next_char = raw[j] if j < len(raw) else ""

            if next_char in {":", ",", "}", "]", ""}:
                in_string = False
                result.append(char)
            else:
                result.append('\\"')
            i += 1
            continue

        result.append(char)
        i += 1

    return "".join(result)


def parse_json_lenient(raw: str) -> Any:
    """Parse JSON from LLM output with local repairs for common invalid output."""
    # LangChain's generic markdown parser selects the first fenced block even
    # when that block is a file tree, SQL, or Python and a later ``json`` block
    # contains the actual response object. Prefer every explicitly-labelled JSON
    # candidate before trying the whole response.
    for match in _EXPLICIT_JSON_FENCE.finditer(raw):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if parsed is not None:
                return parsed
        except Exception:
            pass
        try:
            parsed = parse_partial_json(candidate)
            if parsed is not None:
                return parsed
        except Exception:
            pass

    try:
        parsed = parse_json_markdown(raw)
        if parsed is not None:
            return parsed
    except Exception:
        pass

    try:
        parsed = parse_partial_json(raw)
        if parsed is not None:
            return parsed
    except Exception:
        pass

    repaired = _escape_unescaped_quotes_in_strings(raw)
    if repaired != raw:
        try:
            parsed = parse_json_markdown(repaired)
            if parsed is not None:
                return parsed
        except Exception:
            pass
        try:
            parsed = json.loads(repaired)
            if parsed is not None:
                return parsed
        except Exception:
            pass

    raise OutputParserException(f"Invalid json output: {raw}")


class ToolCallParseError(OutputParserException):
    """Raised when native arguments cannot be decoded without changing values.

    Carries the rejected AIMessage and safe per-call error details for the
    caller's bounded resends. The rejected batch must not enter tool history.
    """

    def __init__(
        self,
        message: str,
        invalid_message: AIMessage,
        error_details: list[str],
    ) -> None:
        super().__init__(message)
        self.invalid_message = invalid_message
        self.error_details = error_details

    def make_retry_context(self, context: list[Any]) -> list[Any]:
        """Append safe feedback without opening an unexecuted tool-call turn.

        Args:
            context: Current conversation messages.

        Returns:
            Original context plus a corrective HumanMessage. A failed mixed
            batch can contain valid tool_calls that were never dispatched;
            appending it would require fake tool responses or break protocol.
        """
        error_str = "\n\n".join(self.error_details)
        return context + [HumanMessage(content=_RETRY_WITH_ERROR_TEMPLATE.format(error=error_str))]


class RobustJsonParser(Runnable[AIMessage, AIMessage]):
    """Lossless decoding for native tool arguments.

    Implements Runnable[AIMessage, AIMessage] so it composes cleanly with a
    bound model via the | operator::

        chain = (
            model
            .bind(response_format=..., tool_choice=...)
            .bind_tools(tools)
            | RobustJsonParser.from_llm(llm)
        )
        message = await chain.ainvoke(messages)

    Incomplete or ambiguous input raises ToolCallParseError before any member
    of the response batch can execute. The caller owns bounded model resends.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm
        # Retain the explicit legacy helper for compatibility. Automatic tool
        # promotion never uses a model-generated rewrite of these arguments.
        self._fixing_parser: OutputFixingParser = OutputFixingParser.from_llm(
            llm=llm,
            parser=JsonOutputParser(),
            max_retries=1,
        )

    @classmethod
    def from_llm(cls, llm: BaseChatModel) -> "RobustJsonParser":
        """Create a RobustJsonParser from a chat model.

        Args:
            llm: Chat model retained for the explicit legacy repair helper.

        Returns:
            A RobustJsonParser instance ready for use in a chain.
        """
        return cls(llm=llm)

    # ------------------------------------------------------------------
    # The historical method name is retained for caller compatibility.
    # ------------------------------------------------------------------

    def _stage1_partial_json(self, raw: str) -> Optional[dict]:
        """Accept only a complete JSON object; never close or discard tokens."""
        return _complete_tool_argument_object(raw)

    # ------------------------------------------------------------------
    # Complete JSON fence decoding
    # ------------------------------------------------------------------

    def _stage2_json_markdown(self, raw: str) -> Optional[dict]:
        """Unwrap one complete fence without dropping any surrounding prose."""
        match = _COMPLETE_JSON_FENCE.fullmatch(raw)
        return _complete_tool_argument_object(match[1]) if match else None

    # ------------------------------------------------------------------
    # Stage 3: OutputFixingParser(JsonOutputParser)
    # ------------------------------------------------------------------

    async def _stage3_output_fixing(self, raw: str) -> Optional[dict]:
        """Legacy explicit helper; never used to promote native tool arguments."""
        try:
            with model_call_role("tool_json_repair"):
                result = await self._fixing_parser.aparse(raw)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Per-message lossless decoding
    # ------------------------------------------------------------------

    async def _repair_invalid_tool_calls(self, message: AIMessage) -> AIMessage:
        """Decode complete invalid_tool_call envelopes without semantic repair.

        Repaired calls are promoted from invalid_tool_calls to tool_calls.
        Calls that cannot be repaired remain in invalid_tool_calls.
        """
        if not message.invalid_tool_calls:
            return message

        repaired_calls = list(message.tool_calls)
        still_invalid = []

        for itc in message.invalid_tool_calls:
            name: str = itc.get("name") or ""
            raw_args: str = itc.get("args") or ""

            fixed = self._stage1_partial_json(raw_args)
            if fixed is None:
                fixed = self._stage2_json_markdown(raw_args)

            if fixed is not None:
                logger.info(
                    "Decoded complete tool argument envelope (raw args length: %d)",
                    len(raw_args),
                )
                repaired_calls.append(
                    create_tool_call(name=name, args=fixed, id=itc.get("id"))
                )
            else:
                still_invalid.append(itc)

        return message.model_copy(
            update={
                "tool_calls": repaired_calls,
                "invalid_tool_calls": still_invalid,
            }
        )

    def _collect_errors(self, message: AIMessage) -> list[str]:
        return [
            f"Tool call {index}: arguments must be one complete, unambiguous JSON object; "
            "no arguments were completed or discarded and this batch was not executed."
            for index, _ in enumerate(message.invalid_tool_calls or [])
        ]

    # ------------------------------------------------------------------
    # Runnable interface
    # ------------------------------------------------------------------

    def invoke(
        self,
        input: AIMessage,
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> AIMessage:
        return asyncio.get_event_loop().run_until_complete(
            self.ainvoke(input, config, **kwargs)
        )

    async def ainvoke(
        self,
        input: AIMessage,
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> AIMessage:
        """Decode complete invalid_tool_calls or request a bounded resend.

        Args:
            input: The AIMessage produced by the model.
            config: Optional LangChain runnable config (unused but required by
                the Runnable interface).

        Returns:
            AIMessage with all tool call arguments successfully parsed.

        Raises:
            ToolCallParseError: If one or more tool calls cannot be decoded
                losslessly. The exception carries the partially decoded
                AIMessage and safe per-call error details for bounded resends.
        """
        message = await self._repair_invalid_tool_calls(input)

        if message.invalid_tool_calls:
            errors = self._collect_errors(message)
            raise ToolCallParseError(
                message=(
                    f"Tool call JSON repair failed ({len(message.invalid_tool_calls)} "
                    f"call(s) unrepairable).\n" + "\n".join(errors)
                ),
                invalid_message=message,
                error_details=errors,
            )

        return validate_tool_call_identity(message)
