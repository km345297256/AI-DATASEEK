import logging
import json
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from app.domain.models.tool_result import ToolResult
from langchain.messages import AnyMessage

logger = logging.getLogger(__name__)

class Memory(BaseModel):
    """
    Memory class, defining the basic behavior of memory
    """
    messages: List[AnyMessage] = []

    def add_message(self, message: AnyMessage) -> None:
        """Add message to memory"""
        self.messages.append(message)
    
    def add_messages(self, messages: List[AnyMessage]) -> None:
        """Add messages to memory"""
        self.messages.extend(messages)

    def get_messages(self) -> List[AnyMessage]:
        """Get all message history"""
        return self.messages
    
    def get_last_message(self) -> Optional[AnyMessage]:
        """Get the last message"""
        if len(self.messages) > 0:  
            return self.messages[-1]
        return None
    
    def roll_back(self) -> None:
        """Roll back memory"""
        self.messages = self.messages[:-1]
    
    def compact(self) -> None:
        """Compact memory"""
        for message in self.messages:
            if message.type == "tool":
                if message.name in ["browser_view", "browser_navigate"]:
                    message.content = ToolResult(success=True, data='(removed)').model_dump_json()
                    logger.debug(f"Removed tool result from memory: {message.name}")

    @staticmethod
    def _serialized_size(messages: List[AnyMessage]) -> int:
        """Return the JSON size used when this memory is stored in MongoDB."""
        return len(json.dumps(
            {"messages": [message.model_dump(mode="json") for message in messages]},
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8"))

    @staticmethod
    def _truncate_message(message: AnyMessage, max_content_bytes: int) -> AnyMessage:
        """Return a persistence-safe copy of an exceptionally large message."""
        bounded = message.model_copy(deep=True)
        if getattr(bounded, "type", None) == "ai" and getattr(bounded, "tool_calls", None):
            compacted_calls = []
            argument_limit = max(1024, min(8 * 1024, max_content_bytes // 4))
            for tool_call in bounded.tool_calls:
                compacted_call = dict(tool_call)
                args = compacted_call.get("args")
                if isinstance(args, dict):
                    compacted_args = dict(args)
                    for key, value in args.items():
                        if not isinstance(value, str):
                            continue
                        encoded_value = value.encode("utf-8")
                        if len(encoded_value) > argument_limit:
                            compacted_args[key] = (
                                encoded_value[:argument_limit].decode("utf-8", errors="ignore")
                                + f"\n[tool argument truncated from {len(encoded_value)} bytes]"
                            )
                    compacted_call["args"] = compacted_args
                compacted_calls.append(compacted_call)
            bounded.tool_calls = compacted_calls
        content = bounded.content
        if isinstance(content, str):
            raw_content = content
        else:
            raw_content = json.dumps(content, ensure_ascii=False, default=str, separators=(",", ":"))
        encoded = raw_content.encode("utf-8")
        if len(encoded) <= max_content_bytes:
            return bounded

        preview = encoded[:max_content_bytes].decode("utf-8", errors="ignore")
        bounded.content = (
            f"[Message content truncated from {len(encoded)} bytes to keep task memory bounded]\n{preview}"
        )

        # Large response metadata and artifacts are not required for the next model turn.
        if hasattr(bounded, "artifact"):
            bounded.artifact = None
        if hasattr(bounded, "additional_kwargs"):
            bounded.additional_kwargs = {}
        if hasattr(bounded, "response_metadata"):
            bounded.response_metadata = {}
        if getattr(bounded, "type", None) == "ai" and getattr(bounded, "tool_calls", None):
            # Removing an oversized historical call also allows its paired tool results
            # to be discarded by BaseAgent's history repair before the next invocation.
            bounded.tool_calls = []
        return bounded

    @classmethod
    def _trim_newest_turn(
        cls,
        prefix: List[AnyMessage],
        turn: List[AnyMessage],
        max_bytes: int,
        message_content_bytes: int,
    ) -> List[AnyMessage]:
        """Keep the request plus the newest complete tool exchanges from one large turn."""
        if not turn:
            return []
        bounded_turn = [cls._truncate_message(message, message_content_bytes) for message in turn]
        if cls._serialized_size(prefix + bounded_turn) <= max_bytes:
            return bounded_turn

        header: List[AnyMessage] = []
        start_index = 0
        if bounded_turn[0].type == "human":
            header = [bounded_turn[0]]
            start_index = 1

        units: List[List[AnyMessage]] = []
        index = start_index
        while index < len(bounded_turn):
            message = bounded_turn[index]
            unit = [message]
            index += 1
            if message.type == "ai" and getattr(message, "tool_calls", None):
                expected_ids = {
                    tool_call.get("id")
                    for tool_call in message.tool_calls
                    if tool_call.get("id")
                }
                while index < len(bounded_turn):
                    candidate = bounded_turn[index]
                    if candidate.type != "tool" or (
                        expected_ids and getattr(candidate, "tool_call_id", None) not in expected_ids
                    ):
                        break
                    unit.append(candidate)
                    index += 1
            units.append(unit)

        retained_units: List[List[AnyMessage]] = []
        for unit in reversed(units):
            candidate_units = [unit] + retained_units
            candidate = prefix + header + [
                message for retained_unit in candidate_units for message in retained_unit
            ]
            if cls._serialized_size(candidate) <= max_bytes:
                retained_units = candidate_units

        trimmed = header + [
            message for retained_unit in retained_units for message in retained_unit
        ]
        return trimmed if cls._serialized_size(prefix + trimmed) <= max_bytes else header

    def bound(self, max_bytes: int, message_content_bytes: int = 256 * 1024) -> bool:
        """Keep recent complete conversation turns within a Mongo-safe memory budget.

        Agent memories are embedded in one Mongo document.  Preserve the system prompt
        and newest user turns, then discard whole oldest turns before a single document
        can approach MongoDB's 16 MiB BSON limit.
        """
        original_messages = list(self.messages)
        self.compact()

        system_messages = [message for message in self.messages if message.type == "system"]
        retained_system = system_messages[:1]
        if retained_system:
            retained_system = [self._truncate_message(retained_system[0], message_content_bytes)]

        non_system_messages = [message for message in self.messages if message.type != "system"]
        turns: List[List[AnyMessage]] = []
        current_turn: List[AnyMessage] = []
        for message in non_system_messages:
            if message.type == "human" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(message)
        if current_turn:
            turns.append(current_turn)

        retained_turns: List[List[AnyMessage]] = []
        for turn in reversed(turns):
            candidate = retained_system + [message for item in reversed(retained_turns) for message in item] + turn
            if self._serialized_size(candidate) <= max_bytes:
                retained_turns.append(turn)
                continue

            bounded_turn = [self._truncate_message(message, message_content_bytes) for message in turn]
            candidate = retained_system + [message for item in reversed(retained_turns) for message in item] + bounded_turn
            if not retained_turns and self._serialized_size(candidate) <= max_bytes:
                retained_turns.append(bounded_turn)
            elif not retained_turns:
                trimmed_turn = self._trim_newest_turn(
                    retained_system,
                    turn,
                    max_bytes,
                    message_content_bytes,
                )
                if trimmed_turn:
                    retained_turns.append(trimmed_turn)
            break

        self.messages = retained_system + [
            message for turn in reversed(retained_turns) for message in turn
        ]

        # A malformed or very unusual message must never defeat the storage guard.
        while self.messages and self._serialized_size(self.messages) > max_bytes:
            if len(self.messages) > len(retained_system):
                self.messages.pop(len(retained_system))
            else:
                self.messages = []

        changed = self.messages != original_messages
        if changed:
            logger.warning(
                "Compacted agent memory from %d to %d messages (%d bytes)",
                len(original_messages),
                len(self.messages),
                self._serialized_size(self.messages),
            )
        return changed

    @property
    def empty(self) -> bool:
        """Check if memory is empty"""
        return len(self.messages) == 0
