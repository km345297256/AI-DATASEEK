from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.application.services.dataset_request_resolver import (
    ExecutionDecision,
    FrontControllerResolution,
    RequestDecision,
)
from app.domain.models.execution_environment import (
    CordisCatalogIdentity,
    ExecutionEnvironmentSnapshot,
    ModelExecutionIdentity,
    SandboxExecutionIdentity,
    ToolsetExecutionIdentity,
    safe_public_identifier,
    stable_sha256,
)
from app.domain.models.event import MessageEvent
from app.domain.models.message import Message
from app.domain.models.safety import SafetyReview
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services import execution_environment as snapshot_service
from app.domain.services import execution_identity
from app.domain.services.execution_environment import (
    create_agent_execution_snapshot,
    create_lightweight_execution_snapshot,
)
from app.domain.services.execution_identity import (
    ExecutionIdentityConfigurationError,
    private_identity_hmac,
)
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.registry import ToolRegistry
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.models.documents import (
    ExecutionEnvironmentSnapshotDocument,
    SessionEventDocument,
)
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository


def _resolution(*, mode: str = "sandbox") -> FrontControllerResolution:
    return FrontControllerResolution(
        decision=RequestDecision(
            safety=SafetyReview(decision="allow"),
            execution=ExecutionDecision(
                mode=mode,
                required_evidence="file_content" if mode == "sandbox" else "user_message",
            ),
        ),
        answer="ok" if mode == "direct" else "",
        controller_metadata={
            "source": "model",
            "prompt_version": "2026-09-03.2",
            "model_provider": "deepseek",
            "model_name": "deepseek-chat",
            # The builder must ignore arbitrary metadata even if a future
            # resolver accidentally adds private diagnostics here.
            "api_key": "metadata-secret",
            "host_path": "/Users/alice/private-data",
        },
    )


def _catalog(revision_character: str = "a"):
    return SimpleNamespace(
        version="4.0.2",
        revision=revision_character * 64,
        manifest_digest="b" * 64,
        execution_bundle_digest="c" * 64,
        plugin_count=2,
        tool_count=3,
    )


def _agent(
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    *,
    custom_prompt_hmac: str = "f" * 64,
):
    return SimpleNamespace(
        _model_provider=provider,
        _model_name=model,
        _execution_builtin_system_prompt="repository owned prompt",
        _execution_model_configuration={
            "temperature": 0.2,
            "max_tokens": 4096,
            "max_iterations": 40,
            "has_custom_system_prompt": True,
            "custom_prompt_hmac_sha256": custom_prompt_hmac,
            "api_key": "must-not-be-read",
            "api_base": "http://internal.invalid/private",
        },
    )


def _flow(revision_character: str = "a"):
    return SimpleNamespace(
        plugin_toolkit=SimpleNamespace(catalog_snapshot=_catalog(revision_character)),
        planner=_agent(),
        executor=_agent(),
        vision=_agent(model="deepseek-vision"),
        _execution_toolset_identity={
            "policy_version": "tool-policy/registry-0123456789abcdef",
            "policy_digest": "d" * 64,
            "tool_names_digest": "d" * 64,
            "tool_count": 17,
            "mcp_tool_count": 2,
            "mcp_tools_hmac_sha256": "e" * 64,
        },
    )


def _sandbox():
    return SimpleNamespace(
        _execution_runtime_kind="local_docker",
        _image_reference="ai-dataseek-sandbox:latest",
        _image_digest="sha256:" + "e" * 64,
    )


def test_snapshot_fingerprint_is_stable_and_context_independent():
    first = create_agent_execution_snapshot(
        task_id="task-one",
        session_id="session-one",
        flow=_flow(),
        sandbox=_sandbox(),
        dataset_ids=["ds_b", "ds_a"],
        resolution=_resolution(),
        llm_overrides={"max_tokens": 8192},
        requested_mcp_servers=["research-server"],
        requested_skill_count=2,
        trigger_event_seq=3,
    )
    second = create_agent_execution_snapshot(
        task_id="task-two",
        session_id="session-two",
        flow=_flow(),
        sandbox=_sandbox(),
        dataset_ids=["ds_a", "ds_b"],
        resolution=_resolution(),
        llm_overrides={"max_tokens": 8192},
        requested_mcp_servers=["research-server"],
        requested_skill_count=2,
        trigger_event_seq=9,
    )

    assert first.fingerprint == second.fingerprint
    assert first.trigger_event_seq == 3
    assert second.trigger_event_seq == 9
    assert first.task_id != second.task_id
    restored = ExecutionEnvironmentSnapshot.model_validate_json(
        first.model_dump_json()
    )
    assert restored == first

    tampered = first.model_dump(mode="python")
    tampered["sandbox"]["runtime"] = "worker_agent"
    with pytest.raises(ValidationError, match="fingerprint"):
        ExecutionEnvironmentSnapshot.model_validate(tampered)


def test_private_identity_hmac_is_stable_keyed_and_never_returns_plaintext(monkeypatch):
    first_settings = SimpleNamespace(
        execution_snapshot_identity_key="identity-key-a-0123456789abcdef0123456789abcdef",
        api_key="unused-provider-key",
    )
    monkeypatch.setattr(execution_identity, "get_settings", lambda: first_settings)
    first = private_identity_hmac({"prompt": "short private prompt"})
    repeated_after_restart = private_identity_hmac({"prompt": "short private prompt"})
    different_value = private_identity_hmac({"prompt": "another private prompt"})

    assert first == repeated_after_restart
    assert first != different_value
    assert len(first) == 64
    assert "private prompt" not in first

    # A dedicated key is preferred; the API key fallback remains keyed and
    # stable for existing deployments until the dedicated secret is configured.
    monkeypatch.setattr(
        execution_identity,
        "get_settings",
        lambda: SimpleNamespace(
            execution_snapshot_identity_key=None,
            api_key="provider-key-b-0123456789abcdef0123456789abcdef",
        ),
    )
    fallback = private_identity_hmac({"prompt": "short private prompt"})
    assert fallback != first

    monkeypatch.setattr(
        execution_identity,
        "get_settings",
        lambda: SimpleNamespace(execution_snapshot_identity_key=None, api_key=None),
    )
    with pytest.raises(ExecutionIdentityConfigurationError):
        private_identity_hmac("private")


def test_custom_prompt_hmac_changes_environment_without_serializing_prompt(monkeypatch):
    monkeypatch.setattr(
        execution_identity,
        "get_settings",
        lambda: SimpleNamespace(
            execution_snapshot_identity_key="snapshot-key-0123456789abcdef0123456789abcdef",
            api_key=None,
        ),
    )
    first_flow = _flow()
    first_flow.planner = _agent(
        custom_prompt_hmac=private_identity_hmac({"custom_system_prompt": "alpha prompt"})
    )
    second_flow = _flow()
    second_flow.planner = _agent(
        custom_prompt_hmac=private_identity_hmac({"custom_system_prompt": "beta prompt"})
    )

    def build(flow, task_id, mcp_server="research-server"):
        return create_agent_execution_snapshot(
            task_id=task_id,
            session_id="session",
            flow=flow,
            sandbox=_sandbox(),
            dataset_ids=[],
            resolution=_resolution(),
            llm_overrides=None,
            requested_mcp_servers=[mcp_server],
            requested_skill_count=0,
        )

    first = build(first_flow, "task-alpha")
    repeated = build(first_flow, "task-alpha-repeat")
    second = build(second_flow, "task-beta")
    other_mcp = build(first_flow, "task-other-mcp", "other-research-server")

    assert first.fingerprint == repeated.fingerprint
    assert first.fingerprint != second.fingerprint
    assert first.fingerprint != other_mcp.fingerprint
    serialized = first.model_dump_json()
    assert "alpha prompt" not in serialized
    assert "research-server" not in serialized
    assert "beta prompt" not in second.model_dump_json()


def test_snapshot_builder_never_hashes_or_serializes_untrusted_secret_fields(monkeypatch):
    observed_hash_inputs: list[object] = []
    original_stable_sha256 = stable_sha256

    def recording_hash(value: object) -> str:
        observed_hash_inputs.append(value)
        return original_stable_sha256(value)

    monkeypatch.setattr(snapshot_service, "stable_sha256", recording_hash)
    monkeypatch.setattr(
        snapshot_service,
        "get_settings",
        lambda: SimpleNamespace(
            sandbox_image="/Users/alice/private-image",
            dataset_request_resolver_timeout_seconds=8.0,
        ),
    )
    flow = _flow()
    flow.planner = _agent(model="sk-model-field-secret")
    sandbox = SimpleNamespace(
        _execution_runtime_kind="local_docker",
        _image_reference="/Users/alice/private-image",
        _image_digest="sha256:" + "f" * 64,
    )
    snapshot = create_agent_execution_snapshot(
        task_id="task-sensitive-test",
        session_id="session-sensitive-test",
        flow=flow,
        sandbox=sandbox,
        dataset_ids=["ds_safe", "/Users/alice/private-dataset"],
        resolution=_resolution(),
        llm_overrides={
            "api_key": "sk-top-secret",
            "api_base": "http://user:password@internal.invalid/v1",
            "system_prompt": "private prompt secret",
            "max_tokens": 2000,
        },
        requested_mcp_servers=["research-server"],
        requested_skill_count=1,
    )

    serialized = snapshot.model_dump_json()
    hashed_inputs = repr(observed_hash_inputs)
    for forbidden in (
        "sk-top-secret",
        "metadata-secret",
        "must-not-be-read",
        "private prompt secret",
        "/Users/alice",
        "user:password",
        "internal.invalid",
    ):
        assert forbidden not in serialized
        assert forbidden not in hashed_inputs
    assert snapshot.sandbox is not None
    assert snapshot.sandbox.image_reference is None
    assert snapshot.models[1].model == "redacted"


def test_catalog_reload_changes_new_snapshot_but_not_frozen_old_snapshot():
    old = create_agent_execution_snapshot(
        task_id="task-old",
        session_id="session",
        flow=_flow("a"),
        sandbox=_sandbox(),
        dataset_ids=[],
        resolution=_resolution(),
        llm_overrides=None,
        requested_mcp_servers=[],
        requested_skill_count=0,
    )
    new = create_agent_execution_snapshot(
        task_id="task-new",
        session_id="session",
        flow=_flow("f"),
        sandbox=_sandbox(),
        dataset_ids=[],
        resolution=_resolution(),
        llm_overrides=None,
        requested_mcp_servers=[],
        requested_skill_count=0,
    )

    assert old.catalog is not None and old.catalog.revision == "a" * 64
    assert new.catalog is not None and new.catalog.revision == "f" * 64
    assert old.fingerprint != new.fingerprint


def test_lightweight_snapshot_records_front_controller_without_sandbox():
    snapshot = create_lightweight_execution_snapshot(
        task_id="light-task",
        session_id="session",
        resolution=_resolution(mode="direct"),
        llm_overrides={"max_tokens": 8000, "api_key": "secret"},
    )

    assert snapshot.execution_mode == "lightweight"
    assert snapshot.catalog is None
    assert snapshot.sandbox is None
    assert [model.role for model in snapshot.models] == ["front_controller"]
    assert snapshot.models[0].prompt_version == "2026-09-03.2"
    assert "secret" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_lightweight_runner_fails_closed_when_snapshot_cannot_be_persisted():
    repository = SimpleNamespace(
        add_execution_snapshot=AsyncMock(side_effect=RuntimeError("database secret"))
    )
    runner = LightweightTaskRunner(
        session_id="session",
        user_id="user",
        resolution=_resolution(mode="direct"),
        session_repository=repository,
        file_storage=SimpleNamespace(),
        llm_overrides={"api_key": "sk-secret"},
    )

    with pytest.raises(RuntimeError, match="could not be recorded"):
        await runner._record_execution_snapshot("task")
    assert runner._execution_snapshot is None


@pytest.mark.asyncio
async def test_lightweight_runner_rejects_a_changed_trigger_for_the_same_task():
    repository = SimpleNamespace(add_execution_snapshot=AsyncMock())
    runner = LightweightTaskRunner(
        session_id="session",
        user_id="user",
        resolution=_resolution(mode="direct"),
        session_repository=repository,
        file_storage=SimpleNamespace(),
    )

    first = await runner._record_execution_snapshot("task", trigger_event_seq=7)
    same = await runner._record_execution_snapshot("task", trigger_event_seq=7)

    assert same is first
    repository.add_execution_snapshot.assert_awaited_once_with(first)
    with pytest.raises(RuntimeError, match="changed after it was frozen"):
        await runner._record_execution_snapshot("task", trigger_event_seq=8)
    repository.add_execution_snapshot.assert_awaited_once()


def test_lightweight_runner_requires_real_task_id_for_production_snapshot_store():
    runner = LightweightTaskRunner(
        session_id="session",
        user_id="user",
        resolution=_resolution(mode="direct"),
        session_repository=SimpleNamespace(add_execution_snapshot=AsyncMock()),
        file_storage=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="missing its task id"):
        runner._execution_task_id(SimpleNamespace())


@pytest.mark.asyncio
async def test_lightweight_snapshot_is_persisted_before_first_stream_output():
    order: list[str] = []
    snapshots: list[ExecutionEnvironmentSnapshot] = []

    class Queue:
        def __init__(self, item: str | None = None):
            self.item = item

        async def pop(self):
            return "input-1", self.item

        async def put(self, _payload):
            order.append("output")
            assert order[0] == "snapshot"
            return "output-1"

    class TaskDouble:
        id = "task-lightweight"

        def __init__(self):
            self.input_stream = Queue(
                MessageEvent(seq=23, role="user", message="hello").model_dump_json()
            )
            self.output_stream = Queue()

    class Repository:
        async def add_execution_snapshot(self, snapshot):
            order.append("snapshot")
            snapshots.append(snapshot)

        async def add_event(self, *_args):
            return None

        async def update_latest_message(self, *_args):
            return None

        async def increment_unread_message_count(self, *_args):
            return None

        async def update_status(self, *_args):
            return None

    runner = LightweightTaskRunner(
        session_id="session",
        user_id="user",
        resolution=_resolution(mode="direct"),
        session_repository=Repository(),
        file_storage=SimpleNamespace(),
    )
    runner._record_safety_audit = AsyncMock()
    runner._completion_advice = SimpleNamespace(
        default_advice=lambda: SimpleNamespace(),
        to_payload=lambda _advice: {},
    )

    await runner.run(TaskDouble())

    assert order == ["snapshot", "output", "output"]
    assert snapshots[0].trigger_event_seq == 23


@pytest.mark.asyncio
async def test_agent_snapshot_is_frozen_for_task_and_rejects_environment_change():
    """A resumed task cannot silently switch its reproducibility boundary."""
    repository = SimpleNamespace(add_execution_snapshot=AsyncMock())
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id = "session"
    runner._flow = _flow()
    runner._sandbox = _sandbox()
    runner._front_controller_resolution = _resolution()
    runner._llm_overrides = {}
    runner._session_repository = repository
    runner._execution_snapshot = None
    message = Message(message="analyze")

    first = await runner._record_execution_snapshot(
        task_id="task",
        message=message,
        trigger_event_seq=7,
    )
    same = await runner._record_execution_snapshot(
        task_id="task",
        message=message,
        trigger_event_seq=7,
    )
    assert same is first
    repository.add_execution_snapshot.assert_awaited_once_with(first)

    with pytest.raises(RuntimeError, match="changed after it was frozen"):
        await runner._record_execution_snapshot(
            task_id="task",
            message=message,
            trigger_event_seq=8,
        )
    repository.add_execution_snapshot.assert_awaited_once()

    runner._flow._execution_toolset_identity = {
        **runner._flow._execution_toolset_identity,
        "policy_digest": "9" * 64,
    }
    with pytest.raises(RuntimeError, match="changed after it was frozen"):
        await runner._record_execution_snapshot(task_id="task", message=message)
    repository.add_execution_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_initializes_tools_then_persists_snapshot_before_flow_output():
    order: list[str] = []
    recorded_snapshot_arguments: list[dict] = []
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._front_controller_resolution = _resolution()
    runner._record_safety_audit = AsyncMock()
    runner._generated_files = []
    runner._open_analysis_runtime = AsyncMock()  # Independently tested durable admission boundary.

    async def initialize_mcp(*_args, **_kwargs):
        order.append("mcp")

    async def record_snapshot(**kwargs):
        order.append("snapshot")
        recorded_snapshot_arguments.append(kwargs)

    def prepare_environment(_message):
        order.append("skills")

    async def flow_run(_message):
        order.append("flow")
        yield MessageEvent(role="assistant", message="done")

    runner._initialize_mcp_tool = initialize_mcp
    runner._record_execution_snapshot = record_snapshot
    runner._flow = SimpleNamespace(
        run=flow_run,
        prepare_execution_environment=prepare_environment,
    )

    events = [
        event
        async for event in runner._run_flow(
            Message(message="analyze"),
            task_id="task",
            trigger_event_seq=41,
        )
    ]

    assert order == ["mcp", "skills", "snapshot", "flow"]
    assert len(events) == 1
    assert recorded_snapshot_arguments[0]["trigger_event_seq"] == 41


def test_agent_snapshot_records_actual_filtered_skill_state():
    class SkillRegistry:
        def __init__(self, name: str, content: str):
            self.skill = SimpleNamespace(
                id=f"id-{name}",
                name=name,
                scope=SimpleNamespace(value="global"),
                description=f"description-{name}",
                content=content,
                triggers=[],
                scripts=[],
                references=[],
                templates=[],
                priority=0,
                max_context_chars=6000,
                user_id=None,
                owner_user_id=None,
                workspace_id=None,
            )

        def clear_restriction(self):
            return None

        def reload(self):
            return None

        def get_skill(self, name):
            return self.skill if name == self.skill.name else None

        def restrict_to(self, _names):
            return None

    def build(name: str, content: str, task_id: str):
        flow = PlanActFlow.__new__(PlanActFlow)
        flow.skill_registry = SkillRegistry(name, content)
        flow._execution_toolset_identity = dict(_flow()._execution_toolset_identity)
        flow.active_skill_context = ""
        flow.prepare_execution_environment(
            Message(message="analyze", skills=[name, "missing", name])
        )
        return create_agent_execution_snapshot(
            task_id=task_id,
            session_id="session",
            flow=flow,
            sandbox=_sandbox(),
            dataset_ids=[],
            resolution=_resolution(),
            llm_overrides=None,
            requested_mcp_servers=[],
            requested_skill_count=2,
        )

    first = build("private-alpha", "secret skill instructions alpha", "task-skills-a")
    repeated = build("private-alpha", "secret skill instructions alpha", "task-skills-a2")
    second = build("private-beta", "secret skill instructions beta", "task-skills-b")

    assert first.toolset is not None and second.toolset is not None
    assert first.toolset.requested_skill_count == 2
    assert first.toolset.active_skill_count == 1
    assert first.fingerprint == repeated.fingerprint
    assert first.fingerprint != second.fingerprint
    assert first.toolset.active_skill_hmac_sha256 != second.toolset.active_skill_hmac_sha256
    serialized = first.model_dump_json() + second.model_dump_json()
    assert "private-alpha" not in serialized
    assert "private-beta" not in serialized
    assert "secret skill instructions" not in serialized


def test_persisted_toolset_hmac_distinguishes_mcp_contracts_without_plaintext():
    class Toolkit:
        name = "fixture"

        def __init__(self, tool_names):
            self._tools = [
                {"type": "function", "function": {"name": name}}
                for name in tool_names
            ]
            self.tool_execution_pipeline = ToolExecutionPipeline()

        def get_tools(self):
            return list(self._tools)

        def get_tool(self, _name):
            return None

    def captured_identity(mcp_name: str):
        flow = PlanActFlow.__new__(PlanActFlow)
        flow._agent_id = "agent"
        flow._session_id = "session"
        flow._user_id = "user"
        flow.plugin_toolkit = Toolkit([])
        flow.plugin_toolkit.catalog_revision = "a" * 64
        flow.mcp_toolkit = Toolkit([mcp_name])
        flow._non_plugin_toolkits = [flow.mcp_toolkit]
        flow._tool_registry = ToolRegistry([
            flow.plugin_toolkit,
            flow.mcp_toolkit,
        ])
        flow._tool_execution_disposer = None
        flow.configure_tool_execution()
        return flow._execution_toolset_identity

    secret_name = captured_identity("mcp_sk_private_token")
    host_path_name = captured_identity("mcp_Users_alice_private_data")

    assert secret_name != host_path_name
    assert secret_name["mcp_tool_count"] == 1
    serialized = json.dumps([secret_name, host_path_name], sort_keys=True)
    assert "private_token" not in serialized
    assert "Users_alice" not in serialized
    assert len(secret_name["mcp_tools_hmac_sha256"]) == 64


@pytest.mark.asyncio
async def test_mongo_snapshot_write_is_idempotent_and_never_overwrites(monkeypatch):
    snapshot = create_lightweight_execution_snapshot(
        task_id="task",
        session_id="session",
        resolution=_resolution(mode="direct"),
        llm_overrides=None,
    )
    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(
        ExecutionEnvironmentSnapshotDocument,
        "get_pymongo_collection",
        lambda: collection,
    )

    await MongoSessionRepository().add_execution_snapshot(snapshot)

    query, update = collection.update_one.await_args.args
    assert query == {
        "task_id": "task",
        "session_id": "session",
        "fingerprint": snapshot.fingerprint,
        "trigger_event_seq": None,
    }
    assert update == {"$setOnInsert": snapshot.model_dump(mode="python")}
    assert collection.update_one.await_args.kwargs == {"upsert": True}

    # The exact retry matches the existing row and is a successful no-op.
    await MongoSessionRepository().add_execution_snapshot(snapshot)
    assert collection.update_one.await_count == 2

    changed_trigger = create_lightweight_execution_snapshot(
        task_id="task",
        session_id="session",
        resolution=_resolution(mode="direct"),
        llm_overrides=None,
        trigger_event_seq=9,
    )
    assert changed_trigger.fingerprint == snapshot.fingerprint
    collection.update_one.side_effect = DuplicateKeyError("duplicate")
    with pytest.raises(ValueError, match="immutable"):
        await MongoSessionRepository().add_execution_snapshot(changed_trigger)
    assert collection.update_one.await_args.args[0] == {
        "task_id": "task",
        "session_id": "session",
        "fingerprint": snapshot.fingerprint,
        "trigger_event_seq": 9,
    }

    changed_resolution = _resolution(mode="direct")
    changed_resolution.controller_metadata["model_name"] = "deepseek-reasoner"
    changed = create_lightweight_execution_snapshot(
        task_id="task",
        session_id="session",
        resolution=changed_resolution,
        llm_overrides=None,
    )
    assert changed.fingerprint != snapshot.fingerprint
    with pytest.raises(ValueError, match="immutable"):
        await MongoSessionRepository().add_execution_snapshot(changed)
    assert collection.update_one.await_args.args[0] == {
        "task_id": "task",
        "session_id": "session",
        "fingerprint": changed.fingerprint,
        "trigger_event_seq": None,
    }

    # Fingerprints intentionally ignore context, so session_id must remain in
    # the upsert predicate to make cross-session task-id reuse hit the unique
    # task index instead of silently accepting a missing snapshot.
    cross_session = create_lightweight_execution_snapshot(
        task_id="task",
        session_id="other-session",
        resolution=_resolution(mode="direct"),
        llm_overrides=None,
    )
    assert cross_session.fingerprint == snapshot.fingerprint
    with pytest.raises(ValueError, match="immutable"):
        await MongoSessionRepository().add_execution_snapshot(cross_session)
    assert collection.update_one.await_args.args[0]["session_id"] == "other-session"


@pytest.mark.asyncio
async def test_session_delete_cascades_execution_snapshots_before_parent(monkeypatch):
    from app.infrastructure.models.model_trace import ModelTraceDocument
    order: list[str] = []
    collection = SimpleNamespace(
        delete_many=AsyncMock(side_effect=lambda _query: order.append("snapshots"))
    )
    session_document = SimpleNamespace(
        delete=AsyncMock(side_effect=lambda: order.append("session"))
    )
    monkeypatch.setattr(
        ExecutionEnvironmentSnapshotDocument,
        "get_pymongo_collection",
        lambda: collection,
    )
    from app.infrastructure.models.documents import (
        SessionDocument,
        SessionEventReservationDocument,
    )

    reservation_collection = SimpleNamespace(delete_many=AsyncMock())
    trace_collection = SimpleNamespace(delete_many=AsyncMock())
    monkeypatch.setattr(ModelTraceDocument, "get_pymongo_collection", lambda: trace_collection)
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        lambda: reservation_collection,
    )
    event_collection = SimpleNamespace(delete_many=AsyncMock())
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        lambda: event_collection,
    )

    monkeypatch.setattr(
        SessionDocument,
        "find_one",
        AsyncMock(return_value=session_document),
    )

    await MongoSessionRepository().delete("session")

    trace_collection.delete_many.assert_awaited_once_with({"session_id": "session"})
    collection.delete_many.assert_awaited_once_with({"session_id": "session"})
    reservation_collection.delete_many.assert_awaited_once_with(
        {"session_id": "session"}
    )
    event_collection.delete_many.assert_awaited_once_with(
        {"session_id": "session"}
    )
    assert order == ["snapshots", "session"]


def test_docker_image_identity_prefers_inspected_immutable_id():
    container = SimpleNamespace(
        attrs={
            "Config": {"Image": "registry.example/data/sandbox:stable"},
            "Image": "sha256:" + "1" * 64,
        },
    )

    reference, digest = DockerSandbox._get_container_image_identity(
        container,
        fallback_reference="fallback:latest",
    )

    assert reference == "registry.example/data/sandbox:stable"
    assert digest == "sha256:" + "1" * 64


def test_execution_snapshot_nested_contracts_are_frozen_and_strict():
    catalog = CordisCatalogIdentity(
        status="ready",
        engine="cordis",
        version="4.0.2",
        revision="a" * 64,
        manifest_digest="b" * 64,
        execution_bundle_digest="c" * 64,
    )
    sandbox = SandboxExecutionIdentity(
        runtime="local_docker",
        image_reference="sandbox:latest",
        image_digest="d" * 64,
        dataset_mounts_digest=stable_sha256([]),
        configuration_digest="e" * 64,
    )
    model = ModelExecutionIdentity(
        role="planner",
        provider="deepseek",
        model="deepseek-chat",
        configuration_digest="f" * 64,
    )
    toolset = ToolsetExecutionIdentity(
        policy_version="tool-policy/v1",
        policy_digest="1" * 64,
        tool_names_digest="2" * 64,
        tool_count=0,
    )
    snapshot = ExecutionEnvironmentSnapshot.create(
        task_id="task",
        session_id="session",
        execution_mode="agent",
        catalog=catalog,
        sandbox=sandbox,
        models=(model,),
        toolset=toolset,
        captured_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(ValidationError):
        ExecutionEnvironmentSnapshot.model_validate({
            **snapshot.model_dump(mode="python"),
            "api_key": "forbidden",
        })
    with pytest.raises(ValidationError):
        CordisCatalogIdentity(
            status="unavailable",
            engine="cordis",
            version="unavailable",
            revision="a" * 64,
        )
    with pytest.raises(ValidationError):
        ExecutionEnvironmentSnapshot.create(
            task_id="/Users/alice/private-task",
            session_id="session",
            execution_mode="lightweight",
            catalog=None,
            sandbox=None,
            models=(),
        )
    assert safe_public_identifier("/Users/alice/data") == "redacted"
    assert safe_public_identifier("C:/Users/alice/data") == "redacted"
    assert safe_public_identifier("sk-secret-token") == "redacted"
