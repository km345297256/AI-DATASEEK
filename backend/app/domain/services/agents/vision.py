from __future__ import annotations

import base64
import logging
import mimetypes
from typing import Any, AsyncGenerator, Callable, List, Optional
from urllib.parse import urlparse, urlunparse

from langchain.messages import HumanMessage

from app.core.config import get_settings
from app.domain.external.file import FileStorage
from app.domain.external.sandbox import Sandbox
from app.domain.models.event import BaseEvent, MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseToolkit

logger = logging.getLogger(__name__)


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
    ):
        self._file_storage = file_storage
        self._user_id = user_id
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            tools=tools,
            dynamic_system_prompt_provider=dynamic_system_prompt_provider,
            llm_overrides=self._build_vision_overrides(llm_overrides),
            usage_context=usage_context,
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
            logger.exception("Vision step failed: %s", exc)
            step.status = ExecutionStatus.FAILED
            step.success = False
            step.error = str(exc)
            yield StepEvent(status=StepStatus.FAILED, step=step)

    async def _build_image_blocks(self, message: Message, sandbox: Sandbox) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        blocks.extend(await self._build_storage_image_blocks(message))
        if blocks:
            return blocks
        for file_path in message.attachments:
            if not self._looks_like_image_path(file_path):
                continue
            file_data = await sandbox.file_download(file_path)
            raw = file_data.read()
            if not raw:
                continue
            mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
            blocks.append(self._image_block(raw, mime_type))
        return blocks

    async def _build_storage_image_blocks(self, message: Message) -> list[dict[str, Any]]:
        if not self._file_storage or not message.attachment_file_ids:
            return []

        blocks: list[dict[str, Any]] = []
        for file_id in message.attachment_file_ids:
            try:
                file_data, file_info = await self._file_storage.download_file(file_id, self._user_id)
                raw = file_data.read()
                if not raw:
                    continue
                filename = file_info.filename or ""
                content_type = file_info.content_type or mimetypes.guess_type(filename)[0] or "image/png"
                if not self._looks_like_image_type(filename, content_type):
                    continue
                blocks.append(self._image_block(raw, content_type))
            except Exception as exc:
                logger.warning("Failed to load vision attachment from storage %s: %s", file_id, exc)
        return blocks

    def _image_block(self, raw: bytes, mime_type: str) -> dict[str, Any]:
        encoded = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{encoded}",
            },
        }

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
