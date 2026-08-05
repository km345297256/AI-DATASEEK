import json

from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.message import Message
from app.domain.models.event import MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, DoneEvent
from app.domain.models.file import FileInfo
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.tools.browser import BrowserToolkit
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.vision import VisionAgent
from app.domain.services.prompts.planner import CREATE_PLAN_PROMPT, UPDATE_PLAN_PROMPT
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def test_step_defaults_to_execution():
    step = Step()
    assert step.agent == "execution"
    assert step.inputs == {}
    assert step.outputs == {}


def test_plan_dump_json_keeps_steps():
    plan = Plan(goal="g", language="zh", steps=[Step(description="look at image", agent="vision")])
    dumped = plan.dump_json()
    assert '"agent":"vision"' in dumped
    assert '"goal":"g"' in dumped


def test_planner_prompt_mentions_vision_agent():
    assert "available SubAgent keys" in CREATE_PLAN_PROMPT
    assert "agent?: string" in UPDATE_PLAN_PROMPT
    assert "uploaded_file_id:" in CREATE_PLAN_PROMPT
    assert "Never create steps to search for uploaded_file_id files" in CREATE_PLAN_PROMPT
    assert PlannerAgent.bind_tools is False


def test_step_normalizes_success_status():
    step = Step(status="success")

    assert step.status == ExecutionStatus.COMPLETED


def test_planner_sanitizes_invalid_status_values():
    planner = object.__new__(PlannerAgent)
    payload = planner._sanitize_plan_payload(
        {
            "status": "error",
            "steps": [
                {"id": "1", "description": "bad status", "status": "error"},
                {"id": "2", "description": "valid status", "status": "completed"},
                {"id": "3", "description": "success status", "status": "success"},
            ],
        }
    )

    plan = Plan.model_validate(payload)

    assert plan.status == "failed"
    assert plan.steps[0].status == "failed"
    assert plan.steps[1].status == "completed"
    assert plan.steps[2].status == "completed"


def test_planner_update_never_accepts_running_steps():
    planner = object.__new__(PlannerAgent)
    current_plan = Plan(
        steps=[
            Step(id="1", description="done vision", agent="vision", status=ExecutionStatus.COMPLETED),
            Step(id="2", description="pending work"),
        ]
    )
    new_steps = [
        Step(id="1", description="regressed vision", agent="vision", status=ExecutionStatus.RUNNING),
        Step(id="3", description="new visual check", agent="vision", status=ExecutionStatus.RUNNING),
    ]

    sanitized_steps = planner._sanitize_updated_steps(new_steps, current_plan)

    assert [step.id for step in sanitized_steps] == ["3"]
    assert sanitized_steps[0].status == ExecutionStatus.PENDING


def test_planner_filters_internal_skill_loading_steps():
    planner = object.__new__(PlannerAgent)
    steps = [
        Step(id="1", description="加载 research-data-extractor 技能说明，了解完整工作流程"),
        Step(id="2", description="根据数据集内容生成探查清单"),
    ]

    filtered_steps = planner._remove_internal_skill_steps(steps)

    assert [step.id for step in filtered_steps] == ["2"]


def test_planner_update_does_not_readd_completed_steps_as_pending():
    planner = object.__new__(PlannerAgent)
    current_plan = Plan(
        steps=[
            Step(id="1", description="加载技能说明", status=ExecutionStatus.COMPLETED),
            Step(id="2", description="执行数据处理"),
        ]
    )
    new_steps = [
        Step(id="1", description="加载技能说明"),
        Step(id="2", description="执行数据处理"),
    ]

    sanitized_steps = planner._sanitize_updated_steps(new_steps, current_plan)
    updated_steps = planner._dedupe_plan_steps([current_plan.steps[0], *sanitized_steps])

    assert [step.id for step in updated_steps] == ["1", "2"]
    assert updated_steps[0].status == ExecutionStatus.COMPLETED
    assert updated_steps[1].status == ExecutionStatus.PENDING


def test_planner_prompt_includes_uploaded_file_ids():
    import asyncio

    captured = {}
    planner = object.__new__(PlannerAgent)

    async def fake_reset_context():
        captured["reset"] = True

    async def fake_execute(prompt):
        captured["prompt"] = prompt
        if False:
            yield None

    planner.reset_context = fake_reset_context
    planner.execute = fake_execute

    async def run():
        async for _ in planner.create_plan(Message(message="识别图片", attachment_file_ids=["file-1"])):
            pass

    asyncio.run(run())

    assert captured["reset"] is True
    assert "uploaded_file_id:file-1" in captured["prompt"]


def test_vision_overrides_use_vision_model_settings(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("API_KEY", "default-key")
    monkeypatch.setenv("VISION_MODEL_BASE", "https://vision.example.test/v1")
    monkeypatch.setenv("VISION_MODEL_API_KEY", "vision-key")
    monkeypatch.setenv("VISION_MODEL_NAME", "qwen3.7-plus")
    monkeypatch.setenv("VISION_MODEL_PROVIDER", "openai")
    get_settings.cache_clear()
    try:
        overrides = VisionAgent._build_vision_overrides({"model_name": "text-model"})
    finally:
        get_settings.cache_clear()

    assert overrides["model_name"] == "qwen3.7-plus"
    assert overrides["model_provider"] == "openai"
    assert overrides["api_base"] == "https://vision.example.test/v1"
    assert overrides["api_key"] == "vision-key"


def test_vision_base_url_normalizes_root_to_v1():
    assert VisionAgent._normalize_openai_base_url("https://api.vectorengine.cn") == "https://api.vectorengine.cn/v1"
    assert VisionAgent._normalize_openai_base_url("https://api.vectorengine.cn/v1") == "https://api.vectorengine.cn/v1"


def test_image_attachment_forces_vision_step():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(id="1", description="answer the user")])

    flow._ensure_vision_step_for_image_message(
        Message(
            message="这个图片的内容是什么",
            attachments=["/home/ubuntu/upload/image.png"],
            attachment_file_ids=["file-1"],
            attachment_file_infos=[FileInfo(file_id="file-1", filename="image.png", content_type="image/png")],
        )
    )

    assert flow.plan.steps[0].agent == "vision"
    assert flow.plan.steps[0].inputs["attachments"] == ["file-1"]
    assert flow.plan.steps[1].agent == "execution"


def test_existing_vision_step_gets_real_attachment_ids():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(
        steps=[
            Step(
                id="1",
                description="vision",
                agent="vision",
                inputs={"attachments": "用户提供的图片附件"},
            )
        ]
    )

    flow._ensure_vision_step_for_image_message(
        Message(
            message="识别图片",
            attachment_file_ids=["file-1"],
            attachment_file_infos=[FileInfo(file_id="file-1", filename="image.png", content_type="image/png")],
        )
    )

    assert flow.plan.steps[0].inputs["attachments"] == ["file-1"]


def test_non_image_file_id_does_not_force_default_vision_step():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(id="1", description="analyze zip data", agent="execution")])

    flow._ensure_vision_step_for_image_message(
        Message(
            message="分析这个压缩包里的 NDVI",
            attachments=["/home/ubuntu/upload/landsat.zip"],
            attachment_file_ids=["zip-1"],
            attachment_file_infos=[
                FileInfo(file_id="zip-1", filename="landsat.zip", content_type="application/zip")
            ],
        )
    )

    assert len(flow.plan.steps) == 1
    assert flow.plan.steps[0].agent == "execution"


def test_completed_vision_only_plan_skips_executor_summary():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(
        steps=[
            Step(
                id="1",
                description="vision",
                agent="vision",
                status=ExecutionStatus.COMPLETED,
                success=True,
            )
        ]
    )

    assert flow._should_complete_after_vision_step() is True


def test_completed_plan_finalizes_running_steps_before_final_plan_event():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(
        steps=[
            Step(id="1", description="completed", status=ExecutionStatus.COMPLETED),
            Step(id="2", description="stale visual check", agent="vision", status=ExecutionStatus.RUNNING),
        ]
    )

    flow._finalize_incomplete_steps()

    assert flow.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert flow.plan.steps[1].status == ExecutionStatus.COMPLETED
    assert flow.plan.steps[1].success is True


def test_completed_plan_finalizes_pending_steps_before_final_plan_event():
    flow = object.__new__(PlanActFlow)
    flow.plan = Plan(
        steps=[
            Step(id="1", description="stale pending visual check", agent="vision", status=ExecutionStatus.PENDING),
        ]
    )

    flow._finalize_incomplete_steps()

    assert flow.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert flow.plan.steps[0].success is True


def test_agent_runner_does_not_interrupt_current_flow_when_new_input_arrives():
    import asyncio

    class FakeInputStream:
        def __init__(self):
            self._popped = False
            self.pending_next_message = False

        async def is_empty(self):
            if not self._popped:
                return False
            return not self.pending_next_message

        async def pop(self):
            if not self._popped:
                self._popped = True
                return "message-1", MessageEvent(message="看图", role="user").model_dump_json()
            return None, None

    class FakeOutputStream:
        def __init__(self):
            self.events = []

        async def put(self, event_json):
            event_id = f"event-{len(self.events) + 1}"
            self.events.append(event_json)
            return event_id

    class FakeTask:
        def __init__(self):
            self.input_stream = FakeInputStream()
            self.output_stream = FakeOutputStream()

    class FakeSandbox:
        async def ensure_sandbox(self):
            return None

    class FakeSessionRepository:
        def __init__(self):
            self.added_events = []

        async def add_event(self, session_id, event):
            self.added_events.append(event.model_copy(deep=True))

        async def update_status(self, session_id, status):
            return None

        async def update_title(self, session_id, title):
            return None

        async def update_latest_message(self, session_id, message, timestamp):
            return None

        async def increment_unread_message_count(self, session_id):
            return None

        async def get_events(self, session_id):
            return []

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._sandbox = FakeSandbox()
    runner._session_repository = FakeSessionRepository()
    runner._generated_files = []
    runner._artifact_baseline_paths = set()

    async def noop(*args, **kwargs):
        return None

    async def fake_run_flow(message):
        step = Step(id="vision-1", description="Analyze the uploaded image attachment(s) and answer the user's question.", agent="vision")
        task.input_stream.pending_next_message = True
        step.status = ExecutionStatus.RUNNING
        yield StepEvent(status=StepStatus.STARTED, step=step)
        step.status = ExecutionStatus.COMPLETED
        yield StepEvent(status=StepStatus.COMPLETED, step=step)
        task.input_stream.pending_next_message = False
        yield DoneEvent()

    runner._capture_artifact_baseline = noop
    runner._sync_message_attachments_to_sandbox = noop
    runner._initialize_mcp_tool = noop
    runner._run_flow = fake_run_flow

    task = FakeTask()
    asyncio.run(runner.run(task))

    statuses = [
        event.step.status
        for event in runner._session_repository.added_events
        if isinstance(event, StepEvent)
    ]
    assert statuses == [ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED]


def test_session_context_includes_previous_vision_results():
    flow = object.__new__(PlanActFlow)
    event = PlanEvent(
        status=PlanStatus.COMPLETED,
        plan=Plan(
            steps=[
                Step(
                    id="1",
                    description="vision",
                    agent="vision",
                    result="图片中包含一张温度分布图。",
                )
            ]
        ),
    )

    context = flow._render_session_context([event])

    payload = json.loads(context)
    assert payload["schema"] == "session_history/v1"
    assert payload["prior_vision_results"] == ["图片中包含一张温度分布图。"]


def test_dynamic_system_prompt_excludes_untrusted_session_context():
    flow = object.__new__(PlanActFlow)
    flow._session_id = "session-123"
    flow.active_skill_context = "skill-context"
    flow.session_context = "session-context"

    prompt = flow._dynamic_system_prompt()

    assert "<runtime_context>" in prompt
    assert "Current session ID: session-123" in prompt
    assert "Current local time:" in prompt
    assert "Current UTC time:" in prompt
    assert "skill-context" in prompt
    assert "session-context" not in prompt
    assert flow._dynamic_user_context() == "session-context"


def test_docker_sandbox_file_upload_rewinds_stream(monkeypatch):
    import io

    captured = {}

    class FakeClient:
        async def post(self, url, files=None, data=None):
            file_obj = files["file"][1]
            captured["position"] = file_obj.tell()
            captured["content"] = file_obj.read()

            class Response:
                def json(self):
                    return {"success": True}

            return Response()

    sandbox = DockerSandbox(ip="127.0.0.1")
    sandbox.client = FakeClient()
    stream = io.BytesIO(b"image-bytes")
    stream.read()

    import asyncio

    result = asyncio.run(sandbox.file_upload(stream, "/home/ubuntu/upload/a.png", filename="a.png"))

    assert result.success is True
    assert captured["position"] == 0
    assert captured["content"] == b"image-bytes"


def test_tool_filters_unexpected_model_arguments():
    import asyncio

    class FakeBrowser:
        async def view_page(self):
            return {"ok": True}

    toolkit = BrowserToolkit(FakeBrowser())
    tool = toolkit.get_tool("browser_view")

    result = asyncio.run(tool.ainvoke({"id": "call-1", "args": {"url": "http://127.0.0.1:8080/docs"}}))

    assert result.artifact == {"ok": True}


def test_unsynced_message_attachment_is_not_dropped(monkeypatch):
    import asyncio

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"

    async def fake_sync_file_to_sandbox(file_id):
        return None

    class FakeSessionRepository:
        async def add_file(self, session_id, file_info):
            raise AssertionError("unsynced attachment should not be added as session file")

    runner._sync_file_to_sandbox = fake_sync_file_to_sandbox
    runner._session_repository = FakeSessionRepository()
    runner._session_id = "session"

    original = FileInfo(file_id="file-1", filename="image.png")
    event = MessageEvent(message="看图", role="user", attachments=[original])

    asyncio.run(runner._sync_message_attachments_to_sandbox(event))

    assert event.attachments == [original]


def test_upload_file_to_storage_wraps_bytes():
    import asyncio

    captured = {}
    runner = object.__new__(AgentTaskRunner)
    runner._user_id = "user"

    class FakeStorage:
        async def upload_file(self, file_data, filename, user_id, content_type=None, metadata=None):
            captured["has_read"] = callable(getattr(file_data, "read", None))
            captured["content"] = file_data.read()
            return FileInfo(file_id="file-1", filename=filename)

    runner._file_storage = FakeStorage()

    result = asyncio.run(runner._upload_file_to_storage(b"screenshot", "screenshot.png"))

    assert result.file_id == "file-1"
    assert captured == {"has_read": True, "content": b"screenshot"}


def test_vision_agent_builds_image_block_from_storage(monkeypatch):
    import asyncio
    import io

    monkeypatch.setenv("API_KEY", "default-key")

    class FakeStorage:
        async def download_file(self, file_id, user_id=None):
            assert file_id == "file-1"
            assert user_id == "user-1"
            return io.BytesIO(b"image-bytes"), FileInfo(
                file_id=file_id,
                filename="image.png",
                content_type="image/png",
            )

    agent = VisionAgent(
        agent_id="agent",
        agent_repository=None,
        tools=[],
        file_storage=FakeStorage(),
        user_id="user-1",
    )

    blocks = asyncio.run(
        agent._build_storage_image_blocks(Message(message="看图", attachment_file_ids=["file-1"]))
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_plan_act_flow_passes_file_storage_only_to_vision(monkeypatch):
    captured = {}

    class FakeToolkit:
        def __init__(self, *args, **kwargs):
            pass

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            captured.setdefault(self.__class__.__name__, kwargs)

    class FakePlanner(FakeAgent):
        pass

    class FakeExecutor(FakeAgent):
        pass

    class FakeVision(FakeAgent):
        pass

    class FakeSkillRegistry:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("app.domain.services.flows.plan_act.ShellToolkit", FakeToolkit)
    monkeypatch.setattr("app.domain.services.flows.plan_act.BrowserToolkit", FakeToolkit)
    monkeypatch.setattr("app.domain.services.flows.plan_act.FileToolkit", FakeToolkit)
    monkeypatch.setattr("app.domain.services.flows.plan_act.MessageToolkit", FakeToolkit)
    monkeypatch.setattr("app.domain.services.flows.plan_act.SkillToolkit", FakeToolkit)
    monkeypatch.setattr("app.domain.services.flows.plan_act.SkillRegistry", FakeSkillRegistry)
    monkeypatch.setattr("app.domain.services.flows.plan_act.PlannerAgent", FakePlanner)
    monkeypatch.setattr("app.domain.services.flows.plan_act.ExecutionAgent", FakeExecutor)
    monkeypatch.setattr("app.domain.services.flows.plan_act.VisionAgent", FakeVision)

    file_storage = object()
    PlanActFlow(
        agent_id="agent",
        user_id="user",
        agent_repository=object(),
        session_id="session",
        session_repository=object(),
        sandbox=object(),
        browser=object(),
        mcp_tool=object(),
        file_storage=file_storage,
    )

    assert "file_storage" not in captured["FakePlanner"]
    assert "file_storage" not in captured["FakeExecutor"]
    assert captured["FakeVision"]["file_storage"] is file_storage
    assert captured["FakeVision"]["user_id"] == "user"
