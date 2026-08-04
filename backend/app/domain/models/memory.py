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
