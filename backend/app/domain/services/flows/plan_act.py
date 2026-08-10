import json
import logging
import re
from datetime import datetime, UTC
from pathlib import PurePosixPath
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
from app.domain.models.plan import ExecutionStatus, Plan, Step
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
from app.domain.models.dataset import DatasetFile

logger = logging.getLogger(__name__)

class AgentStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    UPDATING = "updating"

class PlanActFlow(BaseFlow):
    # Follow-up dataset questions need conversational continuity, but replaying a
    # complete event/tool transcript would make the hot path slow and noisy. Keep
    # only a small, deterministic window of user/assistant text.
    MAX_SESSION_CONTEXT_MESSAGES = 8
    MAX_SESSION_CONTEXT_MESSAGE_BYTES = 2 * 1024
    MAX_SESSION_CONTEXT_BYTES = 12 * 1024
    _EXPLICIT_FILE_REFERENCE = re.compile(
        r"[^\W\d_][^\s,，。;；!?！？:：\"'“”‘’<>《》]{0,511}"
        r"\.[a-z0-9][a-z0-9+_-]{0,15}"
        r"(?=$|[\s,，。;；!?！？:：\"'“”‘’()（）\[\]【】<>《》])",
        re.IGNORECASE,
    )
    _FILE_PREVIEW_ACTION_MARKERS = (
        "展示",
        "预览",
        "打开",
        "查看",
        "显示",
        "看一下",
        "看下",
        "看看",
        "可视化下",
        "可视化一下",
        "显示原图",
        "查看原图",
        "preview",
        "open",
        "view",
        "show",
        "display",
        "look at",
        "show the image",
        "display the image",
        "view the image",
    )
    _FILE_REFERENCE_PREFIX_MARKERS = _FILE_PREVIEW_ACTION_MARKERS + (
        "分析",
        "基于",
        "读取",
        "使用",
        "处理",
        "检查",
        "针对",
        "比较",
        "可视化",
        "文件",
    )
    _FILE_REFERENCE_SUFFIX_MARKERS = (
        "生成",
        "进行",
        "分析",
        "针对",
        "基于",
        "读取",
        "使用",
        "绘制",
        "制作",
        "计算",
        "比较",
        "文件",
        "数据",
    )
    _IMAGE_FILE_SUFFIXES = {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
    _PREVIEWABLE_FILE_SUFFIXES = _IMAGE_FILE_SUFFIXES | {
        ".css",
        ".geojson",
        ".htm",
        ".html",
        ".ini",
        ".json",
        ".log",
        ".md",
        ".pdf",
        ".py",
        ".rst",
        ".sql",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }

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
        self._dataset_fast_path_active = False
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
            BrowserToolkit(
                browser,
                readiness_check=(
                    getattr(sandbox, "ensure_browser_ready", None)
                    or getattr(sandbox, "ensure_sandbox", None)
                ),
            ),
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
            dynamic_user_context_provider=self._dynamic_user_context,
            llm_overrides=self._agent_llm_overrides(base_llm_overrides, "planner"),
            usage_context=usage_context,
        )
        logger.debug(f"Created planner agent for Agent {self._agent_id}")

        self.executor = ExecutionAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            tools=tools,
            dynamic_system_prompt_provider=self._dynamic_system_prompt,
            dynamic_user_context_provider=self._dynamic_user_context,
            llm_overrides=self._agent_llm_overrides(base_llm_overrides, "execution"),
            usage_context=usage_context,
        )
        logger.debug(f"Created execution agent for Agent {self._agent_id}")

        self.vision = VisionAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            tools=tools,
            dynamic_system_prompt_provider=self._dynamic_system_prompt,
            dynamic_user_context_provider=self._dynamic_user_context,
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
        self.session_context = self._render_session_context(
            events,
            current_user_message=message.message,
        )
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
                if self._should_use_dataset_fast_path(message):
                    self._dataset_fast_path_active = True
                    self.plan = self._create_dataset_fast_path_plan(message)
                    logger.info(
                        "Agent %s selected the bounded dataset fast path",
                        self._agent_id,
                    )
                    yield TitleEvent(title=self.plan.title)
                    yield MessageEvent(role="assistant", message=self.plan.message or "")
                    yield PlanEvent(status=PlanStatus.CREATED, plan=self.plan)
                else:
                    self._dataset_fast_path_active = False
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
                if len(self.plan.steps) == 0:
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
                complete_after_vision_step = False
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
                    complete_after_vision_step = self._should_complete_after_vision_step()
                else:
                    async for event in self.executor.execute_step(self.plan, step, message):
                        yield event
                logger.info(f"Agent {self._agent_id} completed step {step.id}")
                # Persist the complete updated plan before moving on so task or
                # process recovery cannot repeat a successful side-effecting step.
                yield PlanEvent(status=PlanStatus.UPDATED, plan=self.plan, step=step)
                if complete_after_vision_step:
                    self.status = AgentStatus.COMPLETED
                    continue
                await self.executor.compact_memory()
                logger.debug(f"Agent {self._agent_id} compacted memory")
                # A successful step does not invalidate the original plan. Replanning
                # after every success adds an LLM round trip and can also make a later
                # step repeat work that has already finished. Replan only on failure.
                if self._should_complete_after_execution_step(step):
                    # A dataset fast-path request is intentionally one bounded step.
                    # Likewise, any successful single-step plan has already delivered
                    # its complete result. Do not add a duplicate summarizer round trip.
                    self.status = AgentStatus.COMPLETED
                else:
                    self.status = self._status_after_execution_step(step)
                logger.info(
                    "Agent %s state changed from %s to %s",
                    self._agent_id,
                    AgentStatus.EXECUTING,
                    self.status,
                )
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

    @staticmethod
    def _status_after_execution_step(step: Step) -> AgentStatus:
        if step.status == ExecutionStatus.COMPLETED and step.success:
            return AgentStatus.EXECUTING
        return AgentStatus.UPDATING

    def _should_complete_after_execution_step(self, step: Step) -> bool:
        step_succeeded = bool(
            step.status == ExecutionStatus.COMPLETED and step.success
        )
        if getattr(self, "_dataset_fast_path_active", False):
            # Dataset fast-path execution is terminal by contract. A structured
            # failure after its bounded execution must be shown to the user, not
            # handed back to the general planner for an unbounded retry loop.
            return step.is_done()
        return bool(self.plan and len(self.plan.steps) == 1 and step_succeeded)

    def _dynamic_system_prompt(self) -> str:
        if getattr(self, "_dataset_fast_path_active", False):
            # Keep the hot path focused. Skill/MCP/vision requests are excluded
            # from this path and therefore do not need their orchestration
            # capabilities repeated in every execution-model request.
            return "\n\n".join(
                part
                for part in [
                    self._runtime_context_prompt(),
                    self.dataset_context,
                ]
                if part
            )
        parts = [
            part
            for part in [
                self._runtime_context_prompt(),
                self._subagent_capabilities_prompt(),
                self.active_skill_context,
                getattr(self, "dataset_context", ""),
            ]
            if part
        ]
        return "\n\n".join(parts)

    def _dynamic_user_context(self) -> str:
        return getattr(self, "session_context", "")

    def _should_use_dataset_fast_path(self, message: Message) -> bool:
        """Use one bounded execution step for ordinary mounted-dataset requests.

        Skills, MCP servers, and image understanding have their own orchestration
        contracts and continue through the planner. All other dataset exploration,
        profiling, analysis, and visualization requests can be handled directly by
        the execution agent with the authoritative mounted manifest.
        """
        return bool(
            message.datasets
            and "execution" in self._enabled_subagents()
            and not message.skills
            and not message.mcp_servers
            and not self._message_has_image_attachment(message)
        )

    @staticmethod
    def _create_dataset_fast_path_plan(message: Message) -> Plan:
        dataset_name = next(
            (dataset.name.strip() for dataset in message.datasets if dataset.name.strip()),
            "dataset",
        )
        target_file = PlanActFlow._resolve_dataset_file_reference(message)
        dataset_intent = PlanActFlow._dataset_request_intent(message.message)
        if target_file and PlanActFlow._is_file_preview_request(
            message.message,
            target_file,
            classified_intent=dataset_intent,
        ):
            dataset_intent = "file_preview"
        intent_config = {
            "file_preview": {
                "description": "预览数据集中的指定文件",
                "instruction": (
                    "仅处理挂载清单中已精确解析的目标逻辑文件，直接返回该原始文件供用户预览或下载；"
                    "不要对整个数据集执行快速探查，不要生成替代图表，也不要启动临时 HTTP 服务。"
                    "不得暴露宿主机真实路径。"
                ),
                "include_archive_tree": False,
                "allow_terminal_quicklook": False,
            },
            "visualization": {
                "description": "分析当前数据集并生成可视化结果",
                "instruction": (
                    "围绕用户明确提出的可视化需求分析数据。对于概览、分布、空间格局、"
                    "质量和描述统计等快速探查能够覆盖的请求，先且只调用一次通用快速探查；"
                    "只有用户明确指定其不支持的计算、分组或专业方法时，才执行有针对性的分析和绘图。"
                    "最终用用户使用的语言解释图表所依据的数据、主要发现、方法与限制，"
                    "并返回图表、摘要和可复用数据结果。"
                ),
                "include_archive_tree": False,
                "allow_terminal_quicklook": True,
            },
            "inventory": {
                "description": "探查数据集文件组织并回答用户问题",
                "instruction": (
                    "检查挂载清单以及压缩包内部目录；最终答案必须直接展示清晰、可读的文件组织树，"
                    "包括压缩包节点、解压后的目录层级和文件名，并说明清单是否因规模限制而截断。"
                    "除非用户同时明确要求绘图，否则不要生成图表。"
                ),
                "include_archive_tree": True,
                "allow_terminal_quicklook": False,
            },
            "catalog_metadata": {
                "description": "读取数据集目录元数据并回答用户问题",
                "instruction": (
                    "仅使用数据中心已验证的登记清单回答总大小、文件数量和格式分组；"
                    "不读取或推断文件内容，不暴露宿主机真实路径。登记清单不完整时必须回退到挂载数据检查。"
                ),
                "include_archive_tree": False,
                "allow_terminal_quicklook": False,
            },
            "catalog_description": {
                "description": "根据数据集登记说明回答用途问题",
                "instruction": (
                    "仅使用数据中心登记的名称、说明、标签和覆盖范围回答数据集用途或研究价值；"
                    "明确区分登记说明与本次实测结论，不运行快速探查，不读取文件内容，也不暴露宿主机真实路径。"
                    "登记说明不足时必须明确说明证据不足，不得根据文件名或领域常识补造用途。"
                ),
                "include_archive_tree": False,
                "allow_terminal_quicklook": False,
            },
            "analysis": {
                "description": "分析当前数据集并回答用户问题",
                "instruction": (
                    "完整保留并回答用户的具体问题，只读取回答该问题所需的数据。先给直接结论，"
                    "再列出可核验的数据证据、分析方法和必要限制；定量结论必须对应实际读取的文件、"
                    "字段或波段以及筛选范围，推断必须与观测事实分开。不要把普通问答改写成通用数据"
                    "探查或可视化任务；仅在用户明确要求图表时生成图表。"
                ),
                "include_archive_tree": False,
                "allow_terminal_quicklook": False,
            },
        }[dataset_intent]
        allow_terminal_quicklook = (
            dataset_intent == "visualization"
            and len(message.datasets or []) == 1
            and not target_file
            and not PlanActFlow._message_has_explicit_file_reference(message.message)
            and PlanActFlow._is_broad_quicklook_request(message.message)
        )
        requested_dimensions = PlanActFlow._dataset_requested_dimensions(
            message.message
        )
        if dataset_intent == "file_preview":
            requested_dimensions = ["file_preview"]
        has_explicit_file_reference = bool(target_file) or (
            PlanActFlow._message_has_explicit_file_reference(message.message)
        )
        prefer_quicklook_evidence = (
            dataset_intent in {"analysis", "visualization"}
            and len(message.datasets or []) == 1
            and not has_explicit_file_reference
            and PlanActFlow._prefers_quicklook_evidence(message.message)
        )
        explicit_artifact_request = PlanActFlow._requests_downloadable_result(
            message.message
        )
        if explicit_artifact_request or (
            dataset_intent == "visualization" and not prefer_quicklook_evidence
        ):
            artifact_policy = "required"
        elif dataset_intent in {"file_preview", "visualization"}:
            artifact_policy = "capability"
        else:
            artifact_policy = "optional"
        uses_chinese = any("\u3400" <= character <= "\u9fff" for character in message.message)
        if uses_chinese:
            title = f"{dataset_name}分析"
            progress = "正在快速分析当前数据集…"
            language = "zh"
        else:
            title = f"Analyze {dataset_name}"
            progress = "Analyzing the current dataset…"
            language = "en"

        return Plan(
            title=title[:80],
            goal=message.message,
            language=language,
            message=progress,
            steps=[
                Step(
                    id="dataset-fast-path",
                    agent="execution",
                    inputs={
                        # Keep the execution mode stable because it also selects the
                        # bounded dataset tool scope. The separate intent tells the
                        # executor whether a quicklook result may end the turn.
                        "execution_mode": "dataset_fast_path",
                        "dataset_intent": dataset_intent,
                        "requested_dimensions": requested_dimensions,
                        "prefer_quicklook_evidence": prefer_quicklook_evidence,
                        **(
                            {
                                "target_file": target_file.path,
                                "target_filename": PlanActFlow._dataset_file_basename(
                                    target_file
                                ),
                            }
                            if target_file
                            else {}
                        ),
                        # Preserve the exact wording independently of the concise
                        # UI description. The executor treats this field as the
                        # authoritative analysis question instead of attempting to
                        # reconstruct it from a generic step label.
                        "user_question": message.message,
                        "execution_guidance": intent_config["instruction"],
                        "require_model_answer": dataset_intent not in {"catalog_description", "catalog_metadata", "file_preview", "inventory"},
                        "require_evidence": True,
                        "require_method_and_limitations": dataset_intent not in {"catalog_description", "catalog_metadata", "file_preview", "inventory"},
                        "require_downloadable_result": artifact_policy in {"required", "capability"},
                        "artifact_policy": artifact_policy,
                        "include_archive_tree": intent_config["include_archive_tree"],
                        # Only an unconstrained broad quicklook can finish without
                        # a second model decision. A request for named dimensions,
                        # metrics, comparisons, or explanations must inspect the
                        # compact manifest evidence and answer every requested part.
                        "allow_terminal_quicklook": allow_terminal_quicklook,
                    },
                    # Plan descriptions are streamed into the conversation UI. Keep
                    # them brief and Chinese; internal execution detail belongs above.
                    description=intent_config["description"],
                )
            ],
        )

    @staticmethod
    def _dataset_request_intent(user_message: str) -> str:
        """Classify only the behavior needed by the dataset one-step executor.

        This is deliberately a small, generic router rather than a dataset-specific
        rule. Ambiguous requests remain normal model-assisted analysis, so an
        unrelated user question is never silently converted into visualization.
        """
        normalized = " ".join((user_message or "").casefold().split())
        file_structure_markers = (
            "包含哪些文件",
            "都有哪些文件",
            "有哪些文件",
            "文件列表",
            "文件清单",
            "文件组织",
            "组织结构",
            "目录结构",
            "目录树",
            "文件树",
            "解压后",
            "解压以后",
            "压缩包内容",
            "压缩包里",
            "包内文件",
            "archive contents",
            "inside the archive",
            "inside archive",
            "file list",
            "which files",
            "what files",
            "directory structure",
            "folder structure",
            "directory tree",
        )
        visualization_markers = (
            "可视化",
            "快速探查",
            "数据探查",
            "数据概览",
            "画图",
            "绘图",
            "图表",
            "折线图",
            "柱状图",
            "散点图",
            "直方图",
            "箱线图",
            "热力图",
            "空间分布图",
            "伪彩色图",
            "伪彩色",
            "假彩色图",
            "假彩色",
            "专题图",
            "等值线图",
            "密度图",
            "小提琴图",
            "visualization",
            "visualisation",
            "visualize",
            "visualise",
            "quicklook",
            "quick look",
            "explore the dataset",
            "dataset overview",
            "plot",
            "chart",
            "graph",
            "heatmap",
            "histogram",
            "boxplot",
            "pseudocolor",
            "pseudo-color",
            "false color",
            "false colour",
            "thematic map",
            "contour plot",
            "density plot",
            "violin plot",
        )
        if any(marker in normalized for marker in file_structure_markers):
            return "inventory"
        if PlanActFlow._is_catalog_description_request(normalized):
            return "catalog_description"
        if PlanActFlow._is_catalog_metadata_request(normalized):
            return "catalog_metadata"
        if any(marker in normalized for marker in visualization_markers):
            return "visualization"
        return "analysis"

    @staticmethod
    def _dataset_file_basename(dataset_file: DatasetFile) -> str:
        return PurePosixPath(dataset_file.path.replace("\\", "/")).name

    @staticmethod
    def _safe_dataset_file_path(dataset_file: DatasetFile) -> str | None:
        raw_path = (dataset_file.path or "").strip().replace("\\", "/")
        if not raw_path or raw_path.startswith("/") or re.match(r"^[a-zA-Z]:/", raw_path):
            return None
        logical_path = PurePosixPath(raw_path)
        if ".." in logical_path.parts:
            return None
        normalized = "/".join(part for part in logical_path.parts if part not in {"", "."})
        return normalized.casefold() or None

    @staticmethod
    def _is_file_reference_character(character: str) -> bool:
        return character.isalnum() or character in "._+-/\\"

    @classmethod
    def _message_contains_file_variant(
        cls,
        normalized_message: str,
        variant: str,
    ) -> bool:
        """Match one inventory path without accepting filename substrings."""
        for match in re.finditer(re.escape(variant), normalized_message):
            prefix = normalized_message[: match.start()]
            suffix = normalized_message[match.end() :]
            if (
                prefix
                and cls._is_file_reference_character(prefix[-1])
                and not any(
                    prefix.endswith(marker)
                    for marker in cls._FILE_REFERENCE_PREFIX_MARKERS
                )
            ):
                continue
            if (
                suffix
                and cls._is_file_reference_character(suffix[0])
                and not any(
                    suffix.startswith(marker)
                    for marker in cls._FILE_REFERENCE_SUFFIX_MARKERS
                )
            ):
                continue
            return True
        return False

    @classmethod
    def _resolve_dataset_file_reference(cls, message: Message) -> DatasetFile | None:
        """Resolve exactly one inventory file named by path or basename.

        Matching is case-insensitive and accepts any logical-path suffix exposed
        to the user. A basename shared by multiple files is deliberately
        ambiguous unless the message includes a unique directory-qualified path.
        """
        normalized_message = (message.message or "").replace("\\", "/").casefold()
        matches: list[tuple[DatasetFile, str]] = []
        for dataset in message.datasets or []:
            for dataset_file in dataset.files or []:
                normalized_path = PlanActFlow._safe_dataset_file_path(dataset_file)
                if not normalized_path:
                    continue
                parts = PurePosixPath(normalized_path).parts
                matched_variants = [
                    "/".join(parts[index:])
                    for index in range(len(parts))
                    if cls._message_contains_file_variant(
                        normalized_message,
                        "/".join(parts[index:]),
                    )
                ]
                if matched_variants:
                    matches.append(
                        (dataset_file, max(matched_variants, key=len))
                    )

        if not matches:
            return None
        directory_qualified = [item for item in matches if "/" in item[1]]
        if directory_qualified:
            # A single qualified path safely disambiguates duplicate basenames.
            if len(directory_qualified) == 1:
                return directory_qualified[0][0]
            return None
        return matches[0][0] if len(matches) == 1 else None

    @classmethod
    def _message_has_explicit_file_reference(cls, user_message: str) -> bool:
        return bool(cls._EXPLICIT_FILE_REFERENCE.search(user_message or ""))

    @classmethod
    def _is_image_dataset_file(cls, dataset_file: DatasetFile) -> bool:
        content_type = (dataset_file.content_type or "").casefold()
        suffix = PurePosixPath(dataset_file.path.replace("\\", "/")).suffix.casefold()
        return content_type.startswith("image/") or suffix in cls._IMAGE_FILE_SUFFIXES

    @classmethod
    def _is_previewable_dataset_file(cls, dataset_file: DatasetFile) -> bool:
        content_type = (dataset_file.content_type or "").casefold()
        suffix = PurePosixPath(dataset_file.path.replace("\\", "/")).suffix.casefold()
        return (
            content_type.startswith(("image/", "text/"))
            or content_type in {"application/json", "application/pdf", "application/xml"}
            or suffix in cls._PREVIEWABLE_FILE_SUFFIXES
        )

    @classmethod
    def _is_file_preview_request(
        cls,
        user_message: str,
        dataset_file: DatasetFile,
        *,
        classified_intent: str,
    ) -> bool:
        normalized = " ".join((user_message or "").casefold().split())
        has_preview_action = any(
            marker in normalized for marker in cls._FILE_PREVIEW_ACTION_MARKERS
        )
        return cls._is_previewable_dataset_file(dataset_file) and has_preview_action

    @staticmethod
    def _is_catalog_metadata_request(user_message: str) -> bool:
        """Recognize only narrow, answerable catalog facts.

        The negative list is intentionally conservative: any request involving
        values, trends, comparisons, scientific interpretation, or file contents
        keeps the professional mounted-data analysis path.
        """

        normalized = " ".join((user_message or "").casefold().split())
        if not normalized or len(normalized) > 160:
            return False
        analytical_markers = (
            "分析", "趋势", "关系", "相关", "比较", "回归", "预测", "质量", "缺失",
            "字段", "波段", "像元", "数值", "平均", "最大值", "最小值", "空间", "时间",
            "年份", "月份", "绘图", "画图", "图表", "内容", "解压", "目录树", "压缩包",
            "包内", "最大", "最小", "排序", "按文件", "统计", "适合", "内存", "转换",
            "兼容", "推荐", "方案",
            "analyze", "analyse", "trend", "relationship", "correlation", "compare",
            "regression", "predict", "quality", "missing", "field", "band", "pixel",
            "mean", "maximum", "minimum", "spatial", "temporal", "plot", "chart",
            "contents", "extract", "directory tree", "archive", "compressed", "uncompressed",
            "largest", "smallest", "sort", "group", "statistics", "suitable", "memory",
            "convert", "conversion", "compatible", "recommend", "unusually",
        )
        if any(marker in normalized for marker in analytical_markers):
            return False
        stripped = normalized.strip(" \t\r\n,，。.!！?？;；:")
        patterns = (
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?(?:这个|该)?数据集(?:总共|一共)?(?:有)?多大(?:呢|吗|是多少)?",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?数据集(?:的)?(?:文件)?(?:总)?大小(?:是多少)?",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?(?:总|合计)(?:文件)?大小(?:是多少)?",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?占用(?:了)?多少(?:磁盘|存储)?空间",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看|统计)?|想知道)?(?:一共|总共)?(?:有)?多少(?:个|份)?文件(?:呢|吗)?",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看|统计)?|想知道)?文件(?:总)?数(?:量)?(?:是多少|有多少)?",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?(?:有|包含|是)?哪些?(?:种)?文件格式",
            r"(?:请(?:告诉我|问)?|帮我(?:看|看看|查看)?|想知道)?文件(?:格式|类型)(?:有哪些|是什么|分组)?",
            r"(?:please )?(?:tell me )?(?:what is )?(?:this |the )?dataset size",
            r"(?:please )?(?:tell me )?how (?:large|big) is (?:this |the )?dataset",
            r"(?:please )?(?:tell me )?how many files(?: are there| does (?:this |the )?dataset contain)?",
            r"(?:please )?(?:tell me )?(?:the )?(?:file count|number of files)",
            r"(?:please )?(?:tell me )?(?:which|what) file (?:formats|types)(?: are there)?",
            r"(?:please )?(?:tell me )?file (?:formats|types)",
        )
        return any(re.fullmatch(pattern, stripped) for pattern in patterns)

    @staticmethod
    def _is_catalog_description_request(user_message: str) -> bool:
        """Recognize narrow purpose/value questions answerable from catalog text.

        This route is intentionally semantic rather than dataset-specific. Mixed
        questions that ask for suitability validation, file-content evidence,
        quality, statistics, comparisons, or a custom method stay on the normal
        mounted-data analysis path.
        """

        normalized = " ".join((user_message or "").casefold().split())
        if not normalized or len(normalized) > 240:
            return False
        dimensions = set(PlanActFlow._dataset_requested_dimensions(normalized))
        if not dimensions or not dimensions <= {"use_cases", "scientific_value"}:
            return False
        analytical_markers = (
            "基于文件", "根据文件", "读取文件", "验证", "核验", "数据质量", "缺失", "异常",
            "统计", "比较", "趋势", "关系", "相关", "回归", "预测", "空间格局", "时间变化",
            "字段", "波段", "像元", "样本", "图表", "绘图", "可视化", "是否适合", "适用性",
            "from the files", "based on the files", "inspect the files", "validate", "verify",
            "data quality", "missing", "anomaly", "statistics", "compare", "trend",
            "relationship", "correlation", "regression", "predict", "spatial pattern",
            "temporal", "field", "band", "pixel", "sample", "plot", "chart",
            "suitable for", "applicability",
        )
        return not any(marker in normalized for marker in analytical_markers)

    @staticmethod
    def _requests_downloadable_result(user_message: str) -> bool:
        normalized = " ".join((user_message or "").casefold().split())
        markers = (
            "下载", "导出", "保存", "生成报告", "分析报告", "输出文件", "生成 csv",
            "生成csv", "生成 json", "生成json", "生成 markdown", "生成markdown",
            "download", "export", "save as", "write a report", "generate a report",
            "csv file", "json file", "markdown file",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _is_broad_quicklook_request(user_message: str) -> bool:
        """Conservatively identify requests satisfied by a generic quicklook.

        The quicklook capability is intentionally terminal only for broad dataset
        overview/visualization requests. Named analytical dimensions require a
        model-authored coverage check after the quicklook manifest is available;
        otherwise a fast response can look successful while silently omitting most
        of the user's question.
        """
        normalized = " ".join((user_message or "").casefold().split())
        if not normalized:
            return False
        if PlanActFlow._message_has_explicit_file_reference(normalized):
            return False
        broad_markers = (
            "数据可视化",
            "可视化",
            "快速探查",
            "数据探查",
            "数据概览",
            "画图",
            "绘图",
            "visualize",
            "visualise",
            "visualization",
            "visualisation",
            "quicklook",
            "quick look",
            "explore the dataset",
            "dataset overview",
        )
        if not any(marker in normalized for marker in broad_markers):
            return False

        requested_dimensions = set(
            PlanActFlow._dataset_requested_dimensions(normalized)
        )
        return requested_dimensions <= {"overview", "visualization"}

    @staticmethod
    def _prefers_quicklook_evidence(user_message: str) -> bool:
        """Prefer deterministic evidence for recognized, quicklook-covered work.

        Quicklook already profiles file structure, distributions, missingness,
        raster spatial zones, descriptive statistics, explicit time dimensions,
        and representative charts. Named transformations and inferential or
        predictive methods remain custom analysis. Unrecognized requests also
        remain custom analysis rather than assuming that quicklook can answer an
        arbitrary calculation. This router describes capability classes only; it
        never keys on a dataset name or value.
        """
        normalized = " ".join((user_message or "").casefold().split())
        if not normalized:
            return False
        if PlanActFlow._message_has_explicit_file_reference(normalized):
            return False
        custom_method_markers = (
            "回归",
            "相关系数",
            "显著性",
            "假设检验",
            "聚类",
            "分类模型",
            "预测",
            "预报",
            "插值",
            "重采样",
            "裁剪",
            "分区统计",
            "主成分",
            "小波",
            "傅里叶",
            "频谱",
            "熵",
            "机器学习",
            "变化检测",
            "伪彩色",
            "假彩色",
            "专题图",
            "等值线",
            "热力图",
            "密度图",
            "小提琴图",
            "regression",
            "correlation coefficient",
            "significance test",
            "hypothesis test",
            "cluster",
            "classification model",
            "forecast",
            "prediction",
            "interpolation",
            "resampling",
            "zonal statistics",
            "principal component",
            "fourier",
            "wavelet",
            "entropy",
            "machine learning",
            "change detection",
            "pseudocolor",
            "pseudo-color",
            "false color",
            "false colour",
            "thematic map",
            "contour plot",
            "heatmap",
            "density plot",
            "violin plot",
        )
        if any(marker in normalized for marker in custom_method_markers):
            return False

        requested_dimensions = set(
            PlanActFlow._dataset_requested_dimensions(normalized)
        )
        # Quicklook is a bounded orientation pass, not an answer engine for
        # named metrics, variables, time ranges, spatial methods, or claims.
        # Those requests must compile a targeted analysis program instead.
        quicklook_evidence_dimensions = {"overview", "visualization"}
        quicklook_modifier_dimensions = {
            "methodology",
            "limitations",
            "interpretation",
        }
        if requested_dimensions == {"question_answering"}:
            return False
        return bool(requested_dimensions & quicklook_evidence_dimensions) and (
            requested_dimensions
            <= quicklook_evidence_dimensions | quicklook_modifier_dimensions
        )

    @staticmethod
    def _dataset_requested_dimensions(user_message: str) -> list[str]:
        """Extract a stable analysis checklist without dataset-specific tuning."""
        normalized = " ".join((user_message or "").casefold().split())
        dimensions: list[str] = []
        marker_groups = (
            (
                "overview",
                (
                    "快速探查",
                    "数据探查",
                    "数据概览",
                    "概览",
                    "概述",
                    "总体情况",
                    "quicklook",
                    "quick look",
                    "explore the dataset",
                    "dataset overview",
                    "overview",
                    "summarize the dataset",
                    "summary of the dataset",
                ),
            ),
            (
                "scientific_value",
                (
                    "科学价值",
                    "研究价值",
                    "学术价值",
                    "数据价值",
                    "价值",
                    "scientific value",
                    "research value",
                    "academic value",
                    "dataset value",
                ),
            ),
            (
                "use_cases",
                (
                    "用途",
                    "用处",
                    "有什么用",
                    "有何用",
                    "应用场景",
                    "可以用来",
                    "可用于",
                    "能用来",
                    "use case",
                    "used for",
                    "potential use",
                    "potential application",
                ),
            ),
            (
                "applicability",
                (
                    "适用性",
                    "适用于",
                    "适合用于",
                    "是否适合",
                    "应用范围",
                    "applicability",
                    "suitable for",
                    "fit for",
                    "appropriate for",
                ),
            ),
            (
                "overall_assessment",
                (
                    "综合评价",
                    "综合评判",
                    "综合评估",
                    "总体评价",
                    "整体评价",
                    "overall assessment",
                    "comprehensive assessment",
                    "comprehensive evaluation",
                    "overall evaluation",
                ),
            ),
            (
                "file_inventory",
                (
                    "哪些文件",
                    "文件列表",
                    "文件清单",
                    "文件组织",
                    "目录结构",
                    "目录树",
                    "压缩包内容",
                    "what files",
                    "file list",
                    "file inventory",
                    "directory structure",
                    "directory tree",
                    "archive contents",
                ),
            ),
            (
                "spatial_pattern",
                (
                    "空间",
                    "地理分布",
                    "区域分布",
                    "spatial",
                    "geographic distribution",
                ),
            ),
            (
                "temporal_trend",
                (
                    "时间",
                    "年度",
                    "年份",
                    "哪一年",
                    "哪个年份",
                    "年际",
                    "月度",
                    "月份",
                    "月际",
                    "趋势",
                    "变化",
                    "temporal",
                    "annual",
                    "yearly",
                    "which year",
                    "monthly",
                    "trend",
                    "time series",
                ),
            ),
            (
                "data_quality",
                (
                    "质量",
                    "缺失",
                    "完整性",
                    "无效值",
                    "nodata",
                    "quality",
                    "missing",
                    "completeness",
                ),
            ),
            (
                "anomaly",
                ("异常", "离群", "极端", "anomal", "outlier", "extreme"),
            ),
            (
                "relationship",
                (
                    "相关",
                    "关系",
                    "回归",
                    "correlat",
                    "relationship",
                    "regression",
                ),
            ),
            (
                "comparison",
                ("对比", "比较", "差异", "compare", "comparison", "difference"),
            ),
            (
                "grouped_analysis",
                ("分组", "按年", "按月", "按区域", "group by", "grouped"),
            ),
            (
                "forecast",
                ("预测", "预报", "forecast", "prediction"),
            ),
            (
                "quantitative_metrics",
                (
                    "量化",
                    "指标",
                    "最大",
                    "最小",
                    "最高",
                    "最低",
                    "峰值",
                    "平均",
                    "均值",
                    "中位数",
                    "标准差",
                    "总量",
                    "metric",
                    "maximum",
                    "minimum",
                    "highest",
                    "lowest",
                    "peak",
                    "mean",
                    "median",
                    "standard deviation",
                ),
            ),
            (
                "visualization",
                (
                    "可视化",
                    "画图",
                    "绘图",
                    "图表",
                    "折线图",
                    "柱状图",
                    "散点图",
                    "直方图",
                    "箱线图",
                    "热力图",
                    "伪彩色图",
                    "伪彩色",
                    "假彩色图",
                    "假彩色",
                    "专题图",
                    "等值线图",
                    "密度图",
                    "小提琴图",
                    "visualiz",
                    "plot",
                    "chart",
                    "graph",
                    "heatmap",
                    "histogram",
                    "boxplot",
                    "pseudocolor",
                    "pseudo-color",
                    "false color",
                    "false colour",
                    "thematic map",
                    "contour plot",
                    "density plot",
                    "violin plot",
                ),
            ),
            (
                "methodology",
                ("方法", "怎么计算", "如何计算", "method", "methodology"),
            ),
            (
                "limitations",
                ("局限", "限制", "不确定性", "limitation", "uncertainty"),
            ),
            (
                "interpretation",
                ("原因", "影响", "说明什么", "解释", "why", "impact", "interpret"),
            ),
        )
        for dimension, markers in marker_groups:
            if any(marker in normalized for marker in markers):
                dimensions.append(dimension)
        return dimensions or ["question_answering"]

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

    @staticmethod
    def _truncate_session_text(value: str, max_bytes: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        suffix = "\n[earlier text truncated]"
        available = max(0, max_bytes - len(suffix.encode("utf-8")))
        return encoded[:available].decode("utf-8", errors="ignore") + suffix

    def _render_session_context(
        self,
        events: list[BaseEvent],
        *,
        current_user_message: str | None = None,
    ) -> str:
        current_event_index: int | None = None
        if current_user_message:
            # The current user event is persisted before the flow starts. Exclude
            # only its latest matching occurrence so an identical older question
            # can still be legitimate follow-up context.
            for index in range(len(events) - 1, -1, -1):
                event = events[index]
                if (
                    isinstance(event, MessageEvent)
                    and event.role == "user"
                    and event.message == current_user_message
                ):
                    current_event_index = index
                    break

        conversation: list[tuple[str, str]] = []
        vision_results: list[str] = []
        analysis_results: list[tuple[str, tuple[str, ...]]] = []
        seen_analysis_results: set[tuple[str, tuple[str, ...]]] = set()
        for index, event in enumerate(events):
            if (
                isinstance(event, MessageEvent)
                and index != current_event_index
                and event.message.strip()
            ):
                conversation.append((event.role, event.message.strip()))
            if not isinstance(event, PlanEvent):
                continue
            for step in event.plan.steps:
                if step.agent == "vision" and step.result:
                    vision_results.append(step.result)
                    continue
                if not step.success or (not step.result and not step.attachments):
                    continue
                result = step.result or "Prior analysis completed."
                attachments = tuple(
                    path for path in step.attachments[:8] if isinstance(path, str) and path
                )
                key = (result, attachments)
                if key in seen_analysis_results:
                    continue
                seen_analysis_results.add(key)
                analysis_results.append(key)

        conversation = conversation[-self.MAX_SESSION_CONTEXT_MESSAGES:]
        rendered_messages_reversed: list[dict[str, str]] = []
        remaining_bytes = self.MAX_SESSION_CONTEXT_BYTES
        # Select newest turns first so a bounded follow-up context does not keep
        # stale history while dropping the immediately preceding answer.
        for role, content in reversed(conversation):
            bounded = self._truncate_session_text(
                content,
                self.MAX_SESSION_CONTEXT_MESSAGE_BYTES,
            )
            entry = {"role": role, "content": bounded}
            entry_size = len(json.dumps(
                entry,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"))
            if entry_size > remaining_bytes:
                break
            rendered_messages_reversed.append(entry)
            remaining_bytes -= entry_size
        rendered_messages = list(reversed(rendered_messages_reversed))

        rendered_vision_results = [
            self._truncate_session_text(
                result,
                self.MAX_SESSION_CONTEXT_MESSAGE_BYTES,
            )
            for result in vision_results[-3:]
        ]
        rendered_analysis_results = [
            (
                self._truncate_session_text(
                    result,
                    self.MAX_SESSION_CONTEXT_MESSAGE_BYTES,
                ),
                attachments,
            )
            for result, attachments in analysis_results[-3:]
        ]
        if (
            not rendered_messages
            and not rendered_vision_results
            and not rendered_analysis_results
        ):
            return ""

        # Historical values are serialized as data and inserted as a
        # HumanMessage by BaseAgent. Never promote user-controlled history into
        # a SystemMessage or concatenate it with trusted instructions.
        payload = {
            "schema": "session_history/v1",
            "messages": rendered_messages,
            "prior_vision_results": rendered_vision_results,
            "prior_analysis_results": [
                {
                    "result": result,
                    "attachments": list(attachments),
                }
                for result, attachments in rendered_analysis_results
            ],
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
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
