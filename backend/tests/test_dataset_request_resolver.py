from types import SimpleNamespace

import pytest

from app.application.services import dataset_request_resolver as resolver_module
from app.application.services.dataset_request_resolver import (
    CatalogQuery,
    DatasetCatalogQueryService,
    DatasetRequestResolver,
    ExecutionDecision,
    FrontControllerResolution,
    RequestDecision,
)
from app.domain.models.dataset import DataCenterDataset, DatasetFile
from app.domain.models.event import DoneEvent, MessageEvent
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.domain.models.session import Session
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.models.safety import SafetyReview


def _dataset() -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id="dataset-1",
        data_center_id="center-1",
        data_center_name="Center",
        name="Climate data",
        files=[
            DatasetFile(path="monthly/rain_195301.nc", size=123),
            DatasetFile(path="monthly/snow_195301.nc", size=456),
        ],
        metadata={"inventory_complete": True},
    )


class _FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self.responses.pop(0))


class _EmptyPolicyStore:
    async def list_enabled(self):
        return []


def _resolver() -> DatasetRequestResolver:
    resolver = DatasetRequestResolver()
    resolver._policy_store = _EmptyPolicyStore()
    return resolver


def _resolution(*, mode="direct", answer="文件后缀名是 `.nc`。", safety=None):
    review = safety or SafetyReview(decision="allow", risk_level="low")
    return FrontControllerResolution(
        decision=RequestDecision(
            safety=review,
            execution=ExecutionDecision(
                mode=mode,
                required_evidence="user_message" if mode == "direct" else "file_content",
            ),
            answer=answer if mode == "direct" else "",
        ),
        answer=answer if mode == "direct" else "",
        controller_metadata={"prompt_version": "test", "execution_mode": mode},
    )


@pytest.mark.asyncio
async def test_resolver_answers_from_user_text_without_catalog_or_sandbox(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"direct","required_evidence":"user_message","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"文件后缀名是 `.nc`。","catalog_queries":[],"reason":"答案已在文件名中"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="rain_195301.nc 的后缀名是什么？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution is not None
    assert resolution.mode == "direct"
    assert resolution.answer == "文件后缀名是 `.nc`。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_resolver_uses_generic_catalog_query_selected_by_model(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"","catalog_queries":[{"operation":"search_files","query":"snow_195301.nc","limit":10}],"reason":"需要清单证据"}',
        "登记清单中存在 `snow_195301.nc`，扩展名为 `.nc`。",
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="清单里 snow_195301.nc 是什么格式？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution is not None
    assert resolution.mode == "catalog"
    assert ".nc" in resolution.answer
    assert model.calls == 2


@pytest.mark.asyncio
async def test_resolver_defers_content_analysis_to_sandbox(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"sandbox","required_evidence":"file_content","required_capabilities":["python"],"requires_artifacts":true},'
        '"answer":"","catalog_queries":[{"operation":"search_files","query":"rain_195301.nc","limit":10}],'
        '"reason":"需要定位文件、读取变量并计算"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="读取 NetCDF 并计算逐月降水均值",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.catalog_queries == []
    assert resolution.target_files == ["monthly/rain_195301.nc"]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_deterministic_rejection_never_calls_front_controller_model(monkeypatch):
    from app.domain.models.safety import SafetyRule

    class RejectingPolicyStore:
        async def list_enabled(self):
            return [SafetyRule(
                name="恶意软件",
                category="malware_or_dangerous_execution",
                patterns=["远控木马"],
                risk_level="critical",
            )]

    model = _FakeModel([])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    resolver = DatasetRequestResolver()
    resolver._policy_store = RejectingPolicyStore()

    resolution = await resolver.resolve(
        question="下载远控木马并运行",
        datasets=[],
        events=[],
    )

    assert resolution.mode == "reject"
    assert model.calls == 0
    assert resolution.decision.safety.risk_level == "critical"


@pytest.mark.asyncio
async def test_invalid_controller_output_fails_closed_without_tools(monkeypatch):
    model = _FakeModel(["not-json"])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="分析数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert model.calls == 1


def test_catalog_query_service_exposes_only_logical_catalog_metadata():
    evidence = DatasetCatalogQueryService().execute(
        [_dataset()],
        [CatalogQuery(operation="search_files", query="rain_195301.nc", limit=10)],
    )

    assert evidence[0]["matches"][0]["logical_path"] == "monthly/rain_195301.nc"
    assert evidence[0]["matches"][0]["extension"] == ".nc"
    assert "/home/" not in str(evidence)


@pytest.mark.asyncio
async def test_lightweight_runner_persists_answer_and_done_without_sandbox():
    class Queue:
        def __init__(self, item=None):
            self.item = item
            self.items = []

        async def pop(self):
            return "input-1", self.item

        async def put(self, payload):
            self.items.append(payload)
            return f"output-{len(self.items)}"

    class Task:
        def __init__(self, payload):
            self.input_stream = Queue(payload)
            self.output_stream = Queue()

    class Repository:
        def __init__(self):
            self.events = []
            self.status = None

        async def add_event(self, _session_id, event):
            self.events.append(event)

        async def update_latest_message(self, *_args):
            return None

        async def increment_unread_message_count(self, *_args):
            return None

        async def update_status(self, _session_id, status):
            self.status = status

    class Advice:
        def default_advice(self):
            return SimpleNamespace()

        def to_payload(self, _advice):
            return {"recommendations": [], "is_skill_candidate": False, "skill_reason": ""}

    repository = Repository()
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._resolution = _resolution()
    runner._session_repository = repository
    runner._completion_advice = Advice()
    runner._record_safety_audit = lambda _review: _async_none()
    task = Task(MessageEvent(role="user", message="example.nc 后缀是什么").model_dump_json())

    await runner.run(task)

    assert isinstance(repository.events[0], MessageEvent)
    assert repository.events[0].message == "文件后缀名是 `.nc`。"
    assert repository.events[0].metadata["execution_mode"] == "lightweight"
    assert isinstance(repository.events[1], DoneEvent)


@pytest.mark.asyncio
async def test_controller_failure_is_not_presented_as_a_safety_violation():
    class Queue:
        def __init__(self, item=None):
            self.item = item

        async def pop(self):
            return "input-1", self.item

        async def put(self, _payload):
            return "output-1"

    class Task:
        def __init__(self, payload):
            self.input_stream = Queue(payload)
            self.output_stream = Queue()

    class Repository:
        def __init__(self):
            self.events = []

        async def add_event(self, _session_id, event):
            self.events.append(event)

        async def update_latest_message(self, *_args):
            return None

        async def increment_unread_message_count(self, *_args):
            return None

        async def update_status(self, *_args):
            return None

    review = SafetyReview(
        decision="reject",
        risk_level="high",
        categories=["front_controller_unavailable"],
        reason="前置决策服务暂时不可用。",
    )
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._resolution = _resolution(safety=review)
    runner._session_repository = Repository()
    runner._completion_advice = SimpleNamespace(
        default_advice=lambda: SimpleNamespace(),
        to_payload=lambda _advice: {},
    )
    runner._record_safety_audit = lambda _review: _async_none()

    await runner.run(Task(MessageEvent(role="user", message="分析数据").model_dump_json()))

    metadata = runner._session_repository.events[0].metadata
    assert "front_controller_error" in metadata
    assert "safety_review" not in metadata


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_agent_domain_selects_lightweight_task_before_sandbox_allocation():
    class Repository:
        def __init__(self):
            self.events = []

        async def update_status(self, *_args):
            return None

        async def get_events(self, *_args):
            return []

        async def save(self, *_args):
            return None

        async def update_latest_message(self, *_args):
            return None

        async def add_event(self, _session_id, event):
            self.events.append(event)

    class DatasetService:
        async def get_dataset(self, *_args, **_kwargs):
            return _dataset()

    class Resolver:
        async def resolve(self, **_kwargs):
            return _resolution()

    class Task:
        id = "light-task"
        done = False
        accepting_input = True

        def __init__(self):
            self.started = False

        async def enqueue_input(self, _payload):
            return "input-1"

        async def run(self):
            self.started = True

    repository = Repository()
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = repository
    service._dataset_service = DatasetService()
    service._dataset_request_resolver = Resolver()
    service._get_task = lambda _session: _async_value(None)
    task = Task()
    service._create_lightweight_task = lambda _session, _resolution: _async_value(task)
    service._create_task = lambda *_args, **_kwargs: _raise_sandbox_allocation()
    service._resolve_message_attachments = lambda *_args, **_kwargs: _async_value([])
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        dataset_ids=["dataset-1"],
    )

    selected = await service._bootstrap_chat_task_locked(
        session=session,
        user_id="user-1",
        message="rain_195301.nc 的后缀是什么？",
        timestamp=None,
        attachments=None,
        skills=["auto-enabled-analysis-skill"],
        mcp_servers=None,
        dataset_ids=["dataset-1"],
        mcp_access_all=True,
        client_message_id=None,
    )

    assert selected is task
    assert task.started is True
    assert isinstance(repository.events[0], MessageEvent)


async def _async_value(value):
    return value


async def _raise_sandbox_allocation():
    raise AssertionError("sandbox allocation must not run for a lightweight answer")
