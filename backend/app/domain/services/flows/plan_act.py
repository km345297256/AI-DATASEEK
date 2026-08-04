import logging
from datetime import datetime, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.domain.services.flows.base import BaseFlow
from app.domain.models.message import Message
from typing import AsyncGenerator, Optional
from enum import Enum
from app.domain.models.event import (
    BaseEvent,
    PlanEvent,
    PlanStatus,
    MessageEvent,
    DoneEvent,
    TitleEvent,
)
from app.domain.models.plan import ExecutionStatus, Step
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.agents.vision import VisionAgent
from app.domain.external.sandbox import Sandbox
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.models.session import SessionStatus
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.domain.services.tools.browser import BrowserToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.message import MessageToolkit
from app.domain.services.tools.search import SearchToolkit
from app.domain.services.tools.skill import SkillToolkit
from app.domain.services.skills import SkillRegistry, SkillRenderer
from app.core.config import get_settings
from app.domain.models.agent_profile import AgentSubAgentConfig, default_subagents
from app.application.services.data_center_dataset_service import render_dataset_context

logger = logging.getLogger(__name__)

class AgentStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    UPDATING = "updating"

class PlanActFlow(BaseFlow):
    def __init__(
        self,
        agent_id: str,
        user_id: str,
        agent_repository: AgentRepository,
        session_id: str,
        session_repository: SessionRepository,
        sandbox: Sandbox,
        browser: Browser,
        mcp_tool: MCPToolkit,
        search_engine: Optional[SearchEngine] = None,
        llm_overrides: Optional[dict] = None,
        file_storage: Optional[FileStorage] = None,
    ):
        self._agent_id = agent_id
        self._user_id = user_id
        self._repository = agent_repository
        self._session_id = session_id
        self._session_repository = session_repository
        self._sandbox = sandbox
        self._browser = browser
        self._file_storage = file_storage
        self.status = AgentStatus.IDLE
        self.plan = None
        settings = get_settings()
        self.skill_registry = SkillRegistry(
            skills_dir=settings.skills_dir,
            enabled=settings.skills_enabled,
            user_id=self._user_id,
            repository=None,
            user_skills_dir=settings.user_skills_dir,
        )
        self.active_skill_context = ""
        self.session_context = ""
        self.dataset_context = ""
        self.agent_profile_config = (llm_overrides or {}).get("agent_profile") or {}
        self.subagents = self._load_subagents(self.agent_profile_config)
        self.enabled_subagents = {
            subagent.key: subagent
            for subagent in self.subagents
            if subagent.enabled
        }
        base_llm_overrides = llm_overrides or {}
        tools = [
            ShellToolkit(sandbox),
            BrowserToolkit(browser),
            FileToolkit(sandbox),
            MessageToolkit(),
            SkillToolkit(
                self.skill_registry,
                session_id=self._session_id,
                user_id=self._user_id,
                session_repository=self._session_repository,
            ),
            mcp_tool,
        ]

        if search_engine:
            tools.append(SearchToolkit(search_engine))

        usage_context = {
            "user_id": self._user_id,
            "session_id": self._session_id,
        }

        self.planner = PlannerAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            tools=tools,
            dynamic_system_prompt_provider=self._dynamic_system_prompt,
            llm_overrides=self._agent_llm_overrides(base_llm_overrides, "planner"),
            usage_context=usage_context,
        )
        logger.debug(f"Created planner agent for Agent {self._agent_id}")

        self.executor = ExecutionAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            tools=tools,
            dynamic_system_prompt_provider=self._dynamic_system_prompt,
            llm_overrides=self._agent_llm_overrides(base_llm_overrides, "execution"),
            usage_context=usage_context,
        )
        logger.debug(f"Created execution agent for Agent {self._agent_id}")

        self.vision = VisionAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            tools=tools,
            dynamic_system_prompt_provider=self._dynamic_system_prompt,
            llm_overrides=self._agent_llm_overrides(base_llm_overrides, "vision"),
            usage_context=usage_context,
            file_storage=self._file_storage,
            user_id=self._user_id,
        )
        logger.debug(f"Created vision agent for Agent {self._agent_id}")

    async def run(self, message: Message) -> AsyncGenerator[BaseEvent, None]:

        # TODO: move to task runner
        session = await self._session_repository.find_by_id(self._session_id)
        if not session:
            raise ValueError(f"Session {self._session_id} not found")
        
        if session.status != SessionStatus.PENDING:
            logger.debug(f"Session {self._session_id} is not in PENDING status, rolling back")
            await self.executor.roll_back(message)
            await self.planner.roll_back(message)
        
        if session.status == SessionStatus.RUNNING:
            logger.debug(f"Session {self._session_id} is in RUNNING status")
            self.status = AgentStatus.PLANNING

        if session.status == SessionStatus.WAITING:
            logger.debug(f"Session {self._session_id} is in WAITING status")
            self.status = AgentStatus.EXECUTING

        await self._session_repository.update_status(self._session_id, SessionStatus.RUNNING)
        events = await self._session_repository.get_events(self._session_id)
        last_plan_event = next((e for e in reversed(events) if isinstance(e, PlanEvent)), None)
        self.plan = last_plan_event.plan if last_plan_event else None
        self.session_context = self._render_session_context(events)
        self.dataset_context = render_dataset_context(message.datasets)
        active_skills = self._activate_skills(message.skills or [])
        if active_skills:
            logger.info(
                "Agent %s activated skills: %s",
                self._agent_id,
                ", ".join(skill.name for skill in active_skills),
            )

        logger.info(f"Agent {self._agent_id} started processing message: {message.message[:50]}...")
        step = None
        while True:
            if self.status == AgentStatus.IDLE:
                logger.info(f"Agent {self._agent_id} state changed from {AgentStatus.IDLE} to {AgentStatus.PLANNING}")
                self.status = AgentStatus.PLANNING
            elif self.status == AgentStatus.PLANNING:
                # Create plan
                logger.info(f"Agent {self._agent_id} started creating plan")
                async for event in self.planner.create_plan(message):
                    if isinstance(event, PlanEvent) and event.status == PlanStatus.CREATED:
                        self.plan = event.plan
                        self._normalize_plan_agents()
                        self._ensure_vision_step_for_image_message(message)
                        logger.info(f"Agent {self._agent_id} created plan successfully with {len(event.plan.steps)} steps")
                        yield TitleEvent(title=event.plan.title)
                        yield MessageEvent(role="assistant", message=event.plan.message or "")
                    yield event
                logger.info(f"Agent {self._agent_id} state changed from {AgentStatus.PLANNING} to {AgentStatus.EXECUTING}")
                self.status = AgentStatus.EXECUTING
                if len(event.plan.steps) == 0:
                    logger.info(f"Agent {self._agent_id} created plan successfully with no steps")
                    self.status = AgentStatus.COMPLETED
                    
            elif self.status == AgentStatus.EXECUTING:
                # Execute plan
                self.plan.status = ExecutionStatus.RUNNING
                step = self.plan.get_next_step()
                if not step:
                    logger.info(f"Agent {self._agent_id} has no more steps, state changed from {AgentStatus.EXECUTING} to {AgentStatus.COMPLETED}")
                    self.status = AgentStatus.SUMMARIZING
                    continue
                # Execute step
                logger.info(f"Agent {self._agent_id} started executing step {step.id}: {step.description[:50]}...")
                if step.agent not in self.enabled_subagents:
                    step.status = ExecutionStatus.FAILED
                    step.success = False
                    step.error = f"Agent '{step.agent}' is not enabled in current Agent profile"
                    yield PlanEvent(status=PlanStatus.UPDATED, plan=self.plan)
                    self.status = AgentStatus.UPDATING
                    continue

                handler_type = self.enabled_subagents[step.agent].handler_type
                if handler_type == "vision":
                    async for event in self.vision.analyze_step(self.plan, step, message, sandbox=self._sandbox):
                        yield event
                    if self._should_complete_after_vision_step():
                        self.status = AgentStatus.COMPLETED
                        continue
                else:
                    async for event in self.executor.execute_step(self.plan, step, message):
                        yield event
                logger.info(f"Agent {self._agent_id} completed step {step.id}, state changed from {AgentStatus.EXECUTING} to {AgentStatus.UPDATING}")
                await self.executor.compact_memory()
                logger.debug(f"Agent {self._agent_id} compacted memory")
                self.status = AgentStatus.UPDATING
            elif self.status == AgentStatus.UPDATING:
                # Update plan
                logger.info(f"Agent {self._agent_id} started updating plan")
                async for event in self.planner.update_plan(self.plan, step):
                    if isinstance(event, PlanEvent):
                        self._normalize_plan_agents()
                    yield event
                logger.info(f"Agent {self._agent_id} plan update completed, state changed from {AgentStatus.UPDATING} to {AgentStatus.EXECUTING}")
                self.status = AgentStatus.EXECUTING
            elif self.status == AgentStatus.SUMMARIZING:
                # Conclusion
                logger.info(f"Agent {self._agent_id} started summarizing")
                async for event in self.executor.summarize():
                    yield event
                logger.info(f"Agent {self._agent_id} summarizing completed, state changed from {AgentStatus.SUMMARIZING} to {AgentStatus.COMPLETED}")
                self.status = AgentStatus.COMPLETED
            elif self.status == AgentStatus.COMPLETED:
                self.plan.status = ExecutionStatus.COMPLETED
                self._finalize_incomplete_steps()
                logger.info(f"Agent {self._agent_id} plan has been completed")
                yield PlanEvent(status=PlanStatus.COMPLETED, plan=self.plan)
                self.status = AgentStatus.IDLE
                break
        yield DoneEvent()
        
        logger.info(f"Agent {self._agent_id} message processing completed")
    
    def is_done(self) -> bool:
        return self.status == AgentStatus.IDLE

    def _dynamic_system_prompt(self) -> str:
        parts = [
            part
            for part in [
                self._runtime_context_prompt(),
                self._subagent_capabilities_prompt(),
                self.active_skill_context,
                getattr(self, "dataset_context", ""),
                self.session_context,
            ]
            if part
        ]
        return "\n\n".join(parts)

    def _activate_skills(self, requested_names: list[str]):
        # Reload before every user turn because a long-lived task runner may be
        # reused after the user changes their personal Skill library.
        self.skill_registry.clear_restriction()
        self.skill_registry.reload()
        active_skills = []
        seen: set[str] = set()
        for requested_name in requested_names:
            skill = self.skill_registry.get_skill(requested_name)
            if not skill:
                continue
            normalized = skill.name.strip().lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            active_skills.append(skill)

        self.skill_registry.restrict_to([skill.name for skill in active_skills])
        self.active_skill_context = SkillRenderer.render(active_skills)
        return active_skills

    def _runtime_context_prompt(self) -> str:
        settings = get_settings()
        timezone_name = settings.app_timezone or "Asia/Shanghai"
        try:
            local_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning("Invalid app timezone %s, falling back to UTC", timezone_name)
            timezone_name = "UTC"
            local_timezone = UTC

        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(local_timezone)
        return (
            "<runtime_context>\n"
            "This context is authoritative for the current task. Use it whenever the user asks for "
            "the current task/session identity, current date, current time, timestamps, or time-based filenames.\n"
            f"- Current session ID: {self._session_id}\n"
            f"- Current local time: {now_local.isoformat()}\n"
            f"- Local timezone: {timezone_name}\n"
            f"- Current UTC time: {now_utc.isoformat()}\n"
            f"- Current Unix timestamp: {int(now_utc.timestamp())}\n"
            "</runtime_context>"
        )

    def _load_subagents(self, agent_profile_config: dict) -> list[AgentSubAgentConfig]:
        raw_subagents = agent_profile_config.get("subagents") if isinstance(agent_profile_config, dict) else None
        if not raw_subagents:
            return default_subagents()
        subagents: list[AgentSubAgentConfig] = []
        for item in raw_subagents:
            try:
                subagents.append(AgentSubAgentConfig.model_validate(item))
            except Exception as exc:
                logger.warning("Ignoring invalid subagent profile config %s: %s", item, exc)
        return subagents or default_subagents()

    def _agent_llm_overrides(self, base_overrides: dict, agent_key: str) -> dict:
        overrides = dict(base_overrides or {})
        if agent_key == "planner":
            planner_config = self.agent_profile_config.get("planner_config") if isinstance(self.agent_profile_config, dict) else None
            if isinstance(planner_config, dict):
                overrides.update({key: value for key, value in planner_config.items() if value is not None})
            return overrides

        subagent = self.enabled_subagents.get(agent_key)
        if not subagent:
            return overrides
        overrides.update({key: value for key, value in subagent.model_settings.items() if value is not None})
        if subagent.system_prompt:
            overrides["system_prompt"] = subagent.system_prompt
        return overrides

    def _enabled_subagents(self) -> dict[str, AgentSubAgentConfig]:
        enabled_subagents = getattr(self, "enabled_subagents", None)
        if enabled_subagents is not None:
            return enabled_subagents
        return {
            subagent.key: subagent
            for subagent in default_subagents()
            if subagent.enabled
        }

    def _subagent_capabilities_prompt(self) -> str:
        enabled_subagents = self._enabled_subagents()
        if not enabled_subagents:
            return ""
        rendered_subagents = []
        for subagent in enabled_subagents.values():
            rendered_subagents.append(
                "\n".join(
                    line
                    for line in [
                        f"- SubAgent key: {subagent.key}",
                        f"  Name: {subagent.name}",
                        f"  Capability: {subagent.planner_capability}",
                        f"  Use when: {subagent.use_when}",
                        f"  Avoid when: {subagent.avoid_when}" if subagent.avoid_when else "",
                        f"  Input contract: {subagent.input_contract}" if subagent.input_contract else "",
                        f"  Output contract: {subagent.output_contract}" if subagent.output_contract else "",
                    ]
                    if line
                )
            )
        allowed_agents = ", ".join(enabled_subagents.keys())
        return (
            "<available_subagents>\n"
            "The current Agent profile can delegate plan steps only to these SubAgents. "
            "For every plan step, the `agent` field MUST be exactly one of the listed SubAgent keys. "
            "Do not invent unlisted agent names.\n\n"
            f"Allowed agent values: {allowed_agents}\n\n"
            f"{chr(10).join(rendered_subagents)}\n"
            "</available_subagents>"
        )

    def _normalize_plan_agents(self) -> None:
        if not self.plan:
            return
        enabled_subagents = self._enabled_subagents()
        for step in self.plan.steps:
            if step.agent in enabled_subagents:
                continue
            if step.agent == "vision" and "vision" not in enabled_subagents and "execution" in enabled_subagents:
                step.agent = "execution"
                continue
            if "execution" in enabled_subagents:
                logger.warning("Planner selected unavailable agent %s; falling back to execution", step.agent)
                step.agent = "execution"
            elif enabled_subagents:
                fallback = next(iter(enabled_subagents.keys()))
                logger.warning("Planner selected unavailable agent %s; falling back to %s", step.agent, fallback)
                step.agent = fallback

    def _render_session_context(self, events: list[BaseEvent]) -> str:
        vision_results: list[str] = []
        for event in events:
            if not isinstance(event, PlanEvent):
                continue
            for step in event.plan.steps:
                if step.agent == "vision" and step.result:
                    vision_results.append(step.result)
        if not vision_results:
            return ""
        latest_results = vision_results[-3:]
        rendered = "\n\n".join(
            f"[Vision result {index}]\n{result}"
            for index, result in enumerate(latest_results, start=1)
        )
        return (
            "<session_context>\n"
            "The following visual analysis results were produced earlier in this same conversation. "
            "Use them as authoritative context for follow-up requests. Do not search the sandbox filesystem "
            "for the original image unless the user explicitly asks to reprocess a new file.\n\n"
            f"{rendered}\n"
            "</session_context>"
        )

    def _ensure_vision_step_for_image_message(self, message: Message) -> None:
        enabled_subagents = self._enabled_subagents()
        if not self.plan or not self._message_has_image_attachment(message) or "vision" not in enabled_subagents:
            return
        if any(step.agent == "vision" for step in self.plan.steps):
            for step in self.plan.steps:
                if step.agent == "vision":
                    step.inputs["attachments"] = message.attachment_file_ids or message.attachments
            return
        self.plan.steps.insert(
            0,
            Step(
                id="vision-1",
                description="Analyze the uploaded image attachment(s) and answer the user's question",
                agent="vision",
                inputs={"attachments": message.attachment_file_ids or message.attachments},
            ),
        )

    def _message_has_image_attachment(self, message: Message) -> bool:
        if any(
            self._looks_like_image_attachment(file_info.filename or "", file_info.content_type or "")
            for file_info in message.attachment_file_infos
        ):
            return True
        return any(
            (attachment or "").lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff")
            )
            for attachment in message.attachments
        )

    def _looks_like_image_attachment(self, filename: str, content_type: str) -> bool:
        return (content_type or "").startswith("image/") or (filename or "").lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff")
        )

    def _should_complete_after_vision_step(self) -> bool:
        if not self.plan:
            return False
        return bool(self.plan.steps) and all(step.agent == "vision" and step.is_done() for step in self.plan.steps)

    def _finalize_incomplete_steps(self) -> None:
        if not self.plan:
            return
        for step in self.plan.steps:
            if step.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
                step.status = ExecutionStatus.COMPLETED
                step.success = True
