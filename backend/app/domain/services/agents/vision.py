from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncGenerator, Callable, List, Optional
from urllib.parse import urlparse, urlunparse

from langchain.messages import HumanMessage, SystemMessage

from app.core.config import get_settings
from app.domain.external.file import FileStorage
from app.domain.external.sandbox import Sandbox
from app.domain.models.event import BaseEvent, MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.pipeline import opaque_log_identifier
from app.domain.services.model_input_policy import ImageInputError, ImageInputSizeError, image_url, project_image, resolve_model_capability
from app.domain.services.model_runtime import flush_memory_changes, memory_checkpoint, note_memory_change
from app.domain.utils.public_error import public_error_message

logger = logging.getLogger(__name__)
_IMAGE_REFERENCE_PLACEHOLDER = {"type": "text", "text": "[Stored image attachment; restored with owner authorization before model use.]"}
_HISTORICAL_IMAGE_OMITTED = {
    "type": "text",
    "text": "[Earlier image not expanded in this request because of image limits. Its prior analysis and original attachment are retained.]",
}
_HISTORICAL_IMAGE_UNAVAILABLE = {
    "type": "text",
    "text": "[Earlier image is unavailable under the current owner's access. Historical textual analysis is retained; no image bytes were added.]",
}


class _StoredImageUnavailable(ImageInputError):
    pass


def _image_identity(block: dict) -> str:
    return hashlib.sha256((image_url(block) or "").encode("utf-8")).hexdigest()


class VisionAgent(BaseAgent):
    """Agent dedicated to image understanding steps planned by PlannerAgent."""

    name: str = "vision"
    system_prompt: str = (
        "You are a vision agent. Analyze image attachments and return a concise, "
        "structured answer in the user's working language. Focus on OCR, objects, "
        "charts, screenshots, scene details, and any findings relevant to the task."
    )
    format: Optional[str] = None
    tool_choice: Optional[str] = "none"

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        tools: List[BaseToolkit],
        dynamic_system_prompt_provider: Optional[Callable[[], str]] = None,
        llm_overrides: Optional[dict] = None,
        usage_context: Optional[dict] = None,
        file_storage: Optional[FileStorage] = None,
        user_id: Optional[str] = None,
        dynamic_user_context_provider: Optional[Callable[[], str]] = None,
    ):
        self._file_storage = file_storage
        self._user_id = user_id
        self._image_sources: dict[str, str] = {}
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            tools=tools,
            dynamic_system_prompt_provider=dynamic_system_prompt_provider,
            llm_overrides=self._build_vision_overrides(llm_overrides),
            usage_context=usage_context,
            dynamic_user_context_provider=dynamic_user_context_provider,
        )

    @staticmethod
    def _build_vision_overrides(base_overrides: Optional[dict] = None) -> dict:
        settings = get_settings()
        overrides = dict(base_overrides or {})
        if settings.vision_model_name:
            overrides["model_name"] = settings.vision_model_name
        if settings.vision_model_provider:
            overrides["model_provider"] = settings.vision_model_provider
        elif settings.vision_model_name:
            overrides["model_provider"] = "openai"
        if settings.vision_model_base:
            overrides["api_base"] = VisionAgent._normalize_openai_base_url(settings.vision_model_base)
        if settings.vision_model_api_key:
            overrides["api_key"] = settings.vision_model_api_key
        if settings.vision_temperature is not None:
            overrides["temperature"] = settings.vision_temperature
        if settings.vision_max_tokens is not None:
            overrides["max_tokens"] = settings.vision_max_tokens
        return overrides

    @staticmethod
    def _normalize_openai_base_url(base_url: str) -> str:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
            return urlunparse(parsed._replace(path="/v1"))
        return base_url.rstrip("/")

    async def analyze_step(
        self,
        plan: Plan,
        step: Step,
        message: Message,
        sandbox: Sandbox,
    ) -> AsyncGenerator[BaseEvent, None]:
        step.status = ExecutionStatus.RUNNING
        yield StepEvent(status=StepStatus.STARTED, step=step)

        try:
            image_blocks = await self._build_image_blocks(message, sandbox)
            logger.info(
                "VisionAgent attachments: file_ids=%d sandbox_paths=%d image_blocks=%d",
                len(message.attachment_file_ids),
                len(message.attachments),
                len(image_blocks),
            )
            if not image_blocks:
                step.status = ExecutionStatus.FAILED
                step.success = False
                step.error = "No supported image attachment was available for visual analysis."
                yield StepEvent(status=StepStatus.FAILED, step=step)
                return

            prompt = self._build_prompt(plan, step, message)
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}, *image_blocks]
            response = await self.ask_with_messages([HumanMessage(content=content)], format=None)
            result = self._message_content_to_text(response.content)
            step.status = ExecutionStatus.COMPLETED
            step.success = True
            step.result = result
            logger.info("VisionAgent result length: %d", len(result or ""))
            yield StepEvent(status=StepStatus.COMPLETED, step=step)
            if result:
                yield MessageEvent(message=result)
        except Exception as exc:
            logger.error(
                "Vision step failed error_type=%s",
                type(exc).__name__,
            )
            step.status = ExecutionStatus.FAILED
            step.success = False
            step.error = public_error_message(exc)
            yield StepEvent(status=StepStatus.FAILED, step=step)

    async def _build_image_blocks(self, message: Message, sandbox: Sandbox) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        blocks.extend(await self._build_storage_image_blocks(message))
        if blocks:
            return blocks
        for file_path in message.attachments:
            if not self._looks_like_image_path(file_path):
                continue
            self._check_image_count(len(blocks) + 1)
            settings = get_settings()
            file_data = await sandbox.file_download(file_path, max_bytes=settings.vision_max_source_bytes)
            try:
                raw = file_data.read(settings.vision_max_source_bytes + 1)
            finally:
                file_data.close()
            blocks.append(await self._project_image_block(raw))
            self._check_request_bytes(blocks)
        return blocks

    async def _build_storage_image_blocks(self, message: Message) -> list[dict[str, Any]]:
        if not self._file_storage or not message.attachment_file_ids:
            return []

        if not self._user_id:
            raise ImageInputError("Image attachment access requires an authenticated owner.")
        blocks: list[dict[str, Any]] = []
        for file_id in message.attachment_file_ids:
            try:
                block = await self._storage_image_block(file_id, image_count=len(blocks))
                if block is None:
                    continue
                self._check_image_count(len(blocks) + 1)
                blocks.append(block)
                self._check_request_bytes(blocks)
            except Exception as exc:
                logger.warning(
                    "Failed to load vision attachment from storage file=%s error_type=%s",
                    opaque_log_identifier(file_id, namespace="file"),
                    type(exc).__name__,
                )
                # Never analyze a partial set or bypass a failed owner check
                # by silently falling back to a sandbox path.
                if isinstance(exc, ImageInputError):
                    raise
                raise ImageInputError("An image attachment could not be read with the current owner's access.") from None
        return blocks

    def _check_image_count(self, count: int) -> None:
        if count > get_settings().vision_max_images:
            raise ImageInputError("Too many image attachments for one model request.")

    def _check_request_bytes(self, blocks: list[dict]) -> None:
        if sum(len((image_url(block) or "").encode("utf-8")) for block in blocks) > get_settings().vision_max_request_bytes:
            raise ImageInputError("Image attachments exceed the model request size limit.")

    async def _project_image_block(self, raw: bytes) -> dict[str, Any]:
        settings = get_settings()
        identity = getattr(getattr(self, "_model", None), "identity", None)
        capability = resolve_model_capability(settings,
            getattr(identity, "provider", self._model_provider),
            getattr(identity, "model_name", self._model_name))
        projected = await asyncio.to_thread(project_image, raw, settings=settings, capability=capability)
        return projected.block()

    async def _storage_image_block(self, file_id: str, *, image_count: int = 0, remember_source: bool = True) -> dict[str, Any] | None:
        if not self._file_storage or not self._user_id:
            raise ImageInputError("Image attachment access requires an authenticated owner.")
        settings = get_settings()
        # Both metadata and ranged bytes are authorized independently. Never
        # trust browser-supplied size/type or download the whole stored object.
        info = await self._file_storage.get_file_info(file_id, self._user_id)
        if info is None:
            raise _StoredImageUnavailable("Image attachment is no longer available.")
        if not self._looks_like_image_type(info.filename or "", info.content_type or ""):
            return None
        self._check_image_count(image_count + 1)
        if info.size is not None and info.size > settings.vision_max_source_bytes:
            raise ImageInputSizeError("Image attachment exceeds the source size limit.")
        raw, _ = await self._file_storage.download_file_range(
            file_id, self._user_id, offset=0, length=settings.vision_max_source_bytes + 1,
        )
        block = await self._project_image_block(raw)
        if remember_source:
            self._image_sources[_image_identity(block)] = file_id
        return block

    async def _persist_memory(self) -> None:
        """Store private owned-file references, never a chopped base64 image.

        This only projects known stored attachments. Existing sandbox-only
        images retain the legacy bounded persistence path; this does not add
        attachment support to resumable analysis checkpoints.
        """
        source = getattr(self, "_request_durable_memory", None) or self.memory
        durable = source.model_copy(deep=True)
        checkpoint = memory_checkpoint(self.memory.messages)
        live_sources = {}
        for message in durable.messages:
            if not isinstance(message.content, list):
                continue
            # A legacy mixed message may already contain durable references
            # and acquire another known source this turn. Merge, never erase
            # existing slots merely because only the new pixels were inline.
            refs = [{"index": index, "file_id": file_id}
                    for index, file_id in self._stored_image_refs(message).items()]
            for index, block in enumerate(message.content):
                if not isinstance(block, dict) or block.get("type") != "image_url":
                    continue
                identity = _image_identity(block)
                file_id = self._image_sources.get(identity)
                if file_id:
                    live_sources[identity] = file_id
                    refs.append({"index": index, "file_id": file_id})
                    message.content[index] = dict(_IMAGE_REFERENCE_PLACEHOLDER)
            if refs:
                message.additional_kwargs["dataseek_image_refs_v1"] = refs
                # Bound the durable text copy without converting the entire
                # multi-modal list into truncated JSON and dangling its refs.
                text_blocks = [block for index, block in enumerate(message.content)
                               if index not in {ref["index"] for ref in refs}
                               and isinstance(block, dict) and isinstance(block.get("text"), str)]
                text_limit = max(128, (self.MAX_TOOL_MESSAGE_CONTENT_BYTES // 2) // max(1, len(text_blocks)))
                while len(json.dumps(message.content, ensure_ascii=False).encode()) > self.MAX_TOOL_MESSAGE_CONTENT_BYTES - 1024 and text_blocks:
                    changed = False
                    for block in text_blocks:
                        raw = block["text"].encode("utf-8")
                        if len(raw) > text_limit:
                            block["text"] = raw[:text_limit].decode("utf-8", errors="ignore") + "\n[Stored text preview truncated]"
                            changed = True
                    if not changed or text_limit <= 128:
                        break
                    text_limit = max(128, text_limit // 2)
        durable.bound(self.MAX_MEMORY_BYTES, self.MAX_TOOL_MESSAGE_CONTENT_BYTES)
        for message in durable.messages:
            refs = message.additional_kwargs.get("dataseek_image_refs_v1")
            if refs and (not isinstance(message.content, list) or any(
                ref["index"] >= len(message.content) or message.content[ref["index"]] != _IMAGE_REFERENCE_PLACEHOLDER
                for ref in refs
            )):
                # Legacy/exceptional provider blocks may still trigger the
                # generic bound. Never persist references to missing slots.
                message.additional_kwargs.pop("dataseek_image_refs_v1", None)
        self._image_sources = live_sources
        note_memory_change(checkpoint, durable.messages, "durable_projection")
        await flush_memory_changes()
        await self._repository.save_memory(self._agent_id, self.name, durable)
        if getattr(self, "_request_durable_memory", None) is not None:
            self._request_durable_memory = durable

    async def _ensure_memory(self):
        if self.memory is not None:
            return
        memory = await self._repository.get_memory(self._agent_id, self.name)
        # Loading a long history must not consume the *request* image budget.
        # Owned bytes are restored lazily into a separate model-only copy.
        restored = memory.model_copy(deep=True)
        for message in restored.messages:
            self._stored_image_refs(message)
        self.memory = restored

    @staticmethod
    def _stored_image_refs(message) -> dict[int, str]:
        refs = message.additional_kwargs.get("dataseek_image_refs_v1")
        if refs is None:
            return {}
        if not isinstance(refs, list) or not isinstance(message.content, list):
            raise ImageInputError("Stored image references are invalid.")
        result = {}
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"index", "file_id"}
                    or type(ref["index"]) is not int or not 0 <= ref["index"] < len(message.content)
                    or ref["index"] in result or not isinstance(ref["file_id"], str) or not ref["file_id"]
                    or message.content[ref["index"]] != _IMAGE_REFERENCE_PLACEHOLDER):
                raise ImageInputError("Stored image references are invalid.")
            result[ref["index"]] = ref["file_id"]
        return result

    async def _project_history_for_images(self, current_images: list[dict]):
        """Retain all current images, then expand a newest-first history prefix.

        This copy alone may omit historical pixels. Durable references and
        textual visual evidence remain untouched and can be used next turn.
        Stop after the first byte-budget miss instead of reading every older
        object in search of a smaller one. No URIs are fetched here.
        """
        settings = get_settings()
        self._check_image_count(len(current_images))
        self._check_request_bytes(current_images)
        remaining_count = settings.vision_max_images - len(current_images)
        remaining_bytes = settings.vision_max_request_bytes - sum(
            len((image_url(block) or "").encode("utf-8")) for block in current_images
        )
        projected = self.memory.model_copy(deep=True)
        candidates = []
        for message in projected.messages:
            refs = self._stored_image_refs(message)
            message.additional_kwargs.pop("dataseek_image_refs_v1", None)
            if not isinstance(message.content, list):
                continue
            for index, block in enumerate(message.content):
                if index in refs:
                    candidates.append((message, index, refs[index], None))
                elif isinstance(block, dict) and block.get("type") == "image_url":
                    candidates.append((message, index, self._image_sources.get(_image_identity(block)), block))
                else:
                    continue
                message.content[index] = dict(_HISTORICAL_IMAGE_OMITTED)
        remaining_attempts = remaining_count
        for message, index, file_id, inline in reversed(candidates):
            if remaining_count <= 0 or remaining_bytes <= 0 or remaining_attempts <= 0:
                break
            remaining_attempts -= 1
            if file_id is not None:
                # Read only selected history and recheck the owner each time.
                try:
                    block = await self._storage_image_block(file_id, remember_source=False)
                except ImageInputSizeError:
                    # A stricter request/source budget may make an old image
                    # unsuitable now; never misreport it as a current input
                    # failure or discard its recoverable durable reference.
                    break
                except (PermissionError, FileNotFoundError, ImageInputError):
                    # Revocation/deletion of *old* images cannot make a new
                    # authorized input unusable. Keep the reference privately
                    # and never hide or downgrade a current attachment error.
                    message.content[index] = dict(_HISTORICAL_IMAGE_UNAVAILABLE)
                    continue
                if block is None:
                    message.content[index] = dict(_HISTORICAL_IMAGE_UNAVAILABLE)
                    continue
            else:
                # Sandbox-only legacy images already in memory need no fetch.
                block = inline
            size = len((image_url(block) or "").encode("utf-8"))
            if size > remaining_bytes:
                break
            message.content[index] = block
            remaining_count -= 1
            remaining_bytes -= size
        return projected

    async def _add_to_memory(self, messages) -> None:
        durable = getattr(self, "_request_durable_memory", None)
        if durable is None:
            await super()._add_to_memory(messages)
            return
        # BaseAgent operates on the model projection during this request, but
        # storage must never replace recoverable old refs with omission text.
        for memory in (self.memory, durable):
            if memory.empty:
                memory.add_message(SystemMessage(content=self.system_prompt))
            memory.add_messages([message.model_copy(deep=True) for message in messages])
        await self._persist_memory()

    async def ask_with_messages(self, messages, format=None, *, allow_tools=True, max_tokens=None):
        await self._ensure_memory()
        current_images = [block for message in messages if isinstance(message.content, list)
                          for block in message.content if isinstance(block, dict) and block.get("type") == "image_url"]
        checkpoint = memory_checkpoint(self.memory.messages)
        projected = await self._project_history_for_images(current_images)
        note_memory_change(checkpoint, projected.messages, "durable_projection")
        await flush_memory_changes()
        # History lookup never mutates _image_sources, including on failure
        # or cancellation; equal old pixels cannot replace a current source.
        durable = self.memory.model_copy(deep=True)
        self._request_durable_memory = durable
        self.memory = projected
        try:
            return await super().ask_with_messages(
                messages, format, allow_tools=allow_tools, max_tokens=max_tokens,
            )
        finally:
            # A failed/cancelled request retains the same accepted-input memory
            # semantics as BaseAgent, but never publishes a partial restore.
            self.memory = getattr(self, "_request_durable_memory", None) or durable
            self._request_durable_memory = None

    def _build_prompt(self, plan: Plan, step: Step, message: Message) -> str:
        return (
            f"User task: {message.message}\n"
            f"Plan goal: {plan.goal}\n"
            f"Current vision step: {step.description}\n"
            f"Working language: {plan.language or 'the user language'}\n\n"
            "Analyze the attached image(s). Return concrete observations and the answer needed for this task."
        )

    def _looks_like_image_path(self, file_path: str) -> bool:
        return (file_path or "").lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff")
        )

    def _looks_like_image_type(self, filename: str, content_type: str) -> bool:
        return (content_type or "").startswith("image/") or self._looks_like_image_path(filename)
