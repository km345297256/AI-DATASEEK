from typing import Dict, Any, List, AsyncGenerator, Optional, Callable
import json
import logging
import re
from app.domain.models.plan import Plan, Step, ExecutionStatus, normalize_execution_status
from app.domain.models.message import Message
from app.domain.services.agents.base import BaseAgent
from app.domain.models.memory import Memory
from app.domain.services.prompts.system import SYSTEM_PROMPT
from app.domain.services.prompts.planner import (
    CREATE_PLAN_PROMPT, 
    UPDATE_PLAN_PROMPT,
    PLANNER_SYSTEM_PROMPT
)
from app.domain.models.event import (
    BaseEvent,
    PlanEvent,
    PlanStatus,
    ErrorEvent,
    MessageEvent,
    DoneEvent,
)
from app.domain.external.sandbox import Sandbox
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.domain.repositories.agent_repository import AgentRepository

logger = logging.getLogger(__name__)

_INTERNAL_SKILL_STEP_PATTERNS = (
    re.compile(r"\b(skill|skills|SKILL\.md)\b.*\b(read|load|inspect|instruction|instructions|workflow|catalog)\b", re.IGNORECASE),
    re.compile(r"\b(read|load|inspect)\b.*\b(skill|skills|SKILL\.md)\b", re.IGNORECASE),
    re.compile(r"技能.*(读取|加载|查看|了解|说明|工作流程)"),
    re.compile(r"(读取|加载|查看|了解).*技能"),
)

class PlannerAgent(BaseAgent):
    """
    Planner agent class, defining the basic behavior of planning
    """

    name: str = "planner"
    system_prompt: str = SYSTEM_PROMPT + PLANNER_SYSTEM_PROMPT
    format: Optional[str] = "json_object"
    tool_choice: Optional[str] = "none"
    bind_tools: bool = False

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        tools: List[BaseToolkit],
        dynamic_system_prompt_provider: Optional[Callable[[], str]] = None,
        llm_overrides: Optional[dict] = None,
        usage_context: Optional[dict] = None,
    ):
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            tools=tools,
            dynamic_system_prompt_provider=dynamic_system_prompt_provider,
            llm_overrides=llm_overrides,
            usage_context=usage_context,
        )


    async def create_plan(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        attachments = list(message.attachments)
        attachments.extend([f"uploaded_file_id:{file_id}" for file_id in message.attachment_file_ids])
        message = CREATE_PLAN_PROMPT.format(
            message=message.message,
            attachments="\n".join(attachments)
        )
        async for event in self.execute(message):
            if isinstance(event, MessageEvent):
                logger.info(event.message)
                parsed_response = await self._parse_json(event.message)
                parsed_response = self._sanitize_plan_payload(parsed_response)
                plan = Plan.model_validate(parsed_response)
                plan.steps = self._remove_internal_skill_steps(plan.steps)
                yield PlanEvent(status=PlanStatus.CREATED, plan=plan)
            else:
                yield event

    async def update_plan(self, plan: Plan, step: Step) -> AsyncGenerator[BaseEvent, None]:
        message = UPDATE_PLAN_PROMPT.format(plan=plan.dump_json(), step=step.model_dump_json())
        async for event in self.execute(message):
            if isinstance(event, MessageEvent):
                logger.debug(f"Planner agent update plan: {event.message}")
                parsed_response = await self._parse_json(event.message)
                parsed_response = self._sanitize_plan_payload(parsed_response)
                updated_plan = Plan.model_validate(parsed_response)
                new_steps = self._sanitize_updated_steps(
                    [Step.model_validate(step) for step in updated_plan.steps],
                    plan,
                )
                
                # Find the index of the first pending step
                first_pending_index = None
                for i, step in enumerate(plan.steps):
                    if not step.is_done():
                        first_pending_index = i
                        break
                
                # If there are pending steps, replace all pending steps
                if first_pending_index is not None:
                    # Keep completed steps
                    updated_steps = plan.steps[:first_pending_index]
                    # Add new steps
                    updated_steps.extend(new_steps)
                    # Update steps in plan
                    plan.steps = self._dedupe_plan_steps(updated_steps)
                
                yield PlanEvent(status=PlanStatus.UPDATED, plan=plan)
            else:
                yield event

    def _sanitize_updated_steps(self, new_steps: List[Step], current_plan: Plan) -> List[Step]:
        """Treat existing completed/failed steps as authoritative when LLM rewrites the plan."""
        done_steps_by_id = {step.id: step for step in current_plan.steps if step.is_done()}
        sanitized_steps: list[Step] = []
        for step in self._remove_internal_skill_steps(new_steps):
            existing_done_step = done_steps_by_id.get(step.id)
            if existing_done_step:
                continue
            if step.status == ExecutionStatus.RUNNING:
                step.status = ExecutionStatus.PENDING
            sanitized_steps.append(step)
        return sanitized_steps

    def _remove_internal_skill_steps(self, steps: List[Step]) -> List[Step]:
        """Remove planner-visible steps that only describe loading Skill instructions."""
        return [step for step in steps if not self._is_internal_skill_loading_step(step)]

    def _is_internal_skill_loading_step(self, step: Step) -> bool:
        description = step.description or ""
        return any(pattern.search(description) for pattern in _INTERNAL_SKILL_STEP_PATTERNS)

    def _dedupe_plan_steps(self, steps: List[Step]) -> List[Step]:
        """Keep the first occurrence of each step id so completed steps cannot be re-added as pending."""
        seen: set[str] = set()
        deduped: list[Step] = []
        for step in steps:
            if step.id in seen:
                continue
            seen.add(step.id)
            deduped.append(step)
        return deduped

    def _sanitize_plan_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize LLM-produced plan JSON before domain validation."""
        if not isinstance(payload, dict):
            return payload

        valid_statuses = {status.value for status in ExecutionStatus}
        sanitized = dict(payload)
        if "status" in sanitized:
            sanitized["status"] = normalize_execution_status(sanitized.get("status"))
        if sanitized.get("status") not in valid_statuses:
            sanitized.pop("status", None)

        steps = sanitized.get("steps")
        if isinstance(steps, list):
            sanitized_steps = []
            for step in steps:
                if not isinstance(step, dict):
                    sanitized_steps.append(step)
                    continue
                sanitized_step = dict(step)
                if "status" in sanitized_step:
                    sanitized_step["status"] = normalize_execution_status(sanitized_step.get("status"))
                if sanitized_step.get("status") not in valid_statuses:
                    sanitized_step.pop("status", None)
                sanitized_steps.append(sanitized_step)
            sanitized["steps"] = sanitized_steps
        return sanitized
