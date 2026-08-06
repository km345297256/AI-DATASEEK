import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.execution_node import (
    ExecutionNodeCapacity,
    ExecutionNodeHealth,
    ExecutionNodeStatus,
    ExecutionNodeType,
)
from app.domain.models.session import Session
from app.domain.external.sandbox_runtime import SandboxNotFoundError
from app.domain.external.task import TaskInputClosedError
from app.domain.services.agent_domain_service import AgentDomainService
from app.application.services.agent_service import AgentService
from app.application.services.resource_configuration_service import ResourceConfigurationService
from app.infrastructure.external.sandbox import node_health, runtime as runtime_module
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool
from app.infrastructure.external.task.redis_task import RedisStreamTask


@pytest.mark.asyncio
async def test_capacity_wait_retries_until_a_slot_is_available(monkeypatch):
    node = SimpleNamespace(node_id="local-default")
    attempts = 0

    async def list_candidates():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise runtime_module.SandboxCapacityError(
                "All execution nodes are at sandbox capacity"
            )
        return [node]

    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", list_candidates)
    monkeypatch.setattr(
        runtime_module,
        "get_settings",
        lambda: SimpleNamespace(
            sandbox_capacity_wait_seconds=0.1,
            sandbox_capacity_poll_seconds=0.001,
        ),
    )

    assert await runtime_module._wait_for_execution_node_candidates() == [node]
    assert attempts == 2


@pytest.mark.asyncio
async def test_local_allocations_share_one_admission_critical_section(monkeypatch):
    runtime = runtime_module.LocalDockerRuntime()
    node = SimpleNamespace(
        node_id="local-default",
        type=ExecutionNodeType.LOCAL_DOCKER,
        runtime_config={},
    )
    monkeypatch.setattr(runtime_module, "_SANDBOX_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime_module,
        "_wait_for_execution_node_candidates",
        AsyncMock(return_value=[node]),
    )
    active = 0
    max_active = 0

    async def allocate_on_node(_node, _session, _mounts):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return SimpleNamespace(id="sandbox")

    runtime._allocate_on_node = allocate_on_node
    await asyncio.gather(
        runtime.allocate(Session(id="s1", user_id="u1", agent_id="a1")),
        runtime.allocate(Session(id="s2", user_id="u2", agent_id="a1")),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_unavailable_resumed_sandbox_is_retired_before_replacement():
    order: list[str] = []

    class OldSandbox:
        id = "sandbox-old"

        async def is_paused(self):
            return True

        async def resume(self):
            order.append("resume-old")
            return True

        async def ensure_api_ready(self):
            raise RuntimeError("not ready")

        async def destroy(self):
            order.append("destroy-old")
            return True

    class NewSandbox:
        id = "sandbox-new"

        async def ensure_api_ready(self):
            order.append("ready-new")

        async def get_browser(self):
            return object()

    class FakeRuntime:
        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-old"
            return OldSandbox()

        async def allocate(self, session=None):
            order.append("allocate-new")
            return NewSandbox()

        async def assign(self, sandbox, session, task_id=None):
            return None

    class FakeTask:
        id = "task-new"

        @classmethod
        def create(cls, _runner):
            return cls()

    class FakeRepository:
        async def save(self, _session):
            return None

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )
    session = Session(
        id="session",
        user_id="user",
        agent_id="agent",
        sandbox_id="sandbox-old",
    )

    task = await service._create_task(session)

    assert task.id == "task-new"
    assert order.index("destroy-old") < order.index("allocate-new")
    assert session.sandbox_id == "sandbox-new"


@pytest.mark.asyncio
async def test_replacement_stops_when_old_sandbox_cannot_be_released():
    allocated = False

    class OldSandbox:
        id = "sandbox-old"

        async def is_paused(self):
            return True

        async def resume(self):
            return True

        async def ensure_api_ready(self):
            raise RuntimeError("not ready")

        async def destroy(self):
            return False

        async def pause(self):
            return False

    class FakeRuntime:
        async def restore(self, _sandbox_id):
            return OldSandbox()

        async def allocate(self, session=None):
            nonlocal allocated
            allocated = True
            return SimpleNamespace(id="should-not-exist")

    class FakeRepository:
        async def save(self, _session):
            return None

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )

    with pytest.raises(RuntimeError, match="could not be safely released"):
        await service._create_task(Session(
            id="session",
            user_id="user",
            agent_id="agent",
            sandbox_id="sandbox-old",
        ))

    assert allocated is False


@pytest.mark.asyncio
async def test_capacity_timeout_does_not_retire_paused_session_sandbox():
    retired: list[str] = []
    allocated = False

    class PausedSandbox:
        id = "sandbox-paused"

        async def is_paused(self):
            return True

        async def resume(self):
            raise runtime_module.SandboxCapacityError("busy")

        async def destroy(self):
            retired.append("destroy")
            return True

        async def pause(self):
            retired.append("pause")
            return True

    class FakeRuntime:
        async def restore(self, _sandbox_id):
            return PausedSandbox()

        async def allocate(self, session=None):
            nonlocal allocated
            allocated = True
            return SimpleNamespace(id="replacement")

    class FakeRepository:
        async def save(self, _session):
            return None

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )
    session = Session(
        id="session",
        user_id="user",
        agent_id="agent",
        sandbox_id="sandbox-paused",
    )

    with pytest.raises(runtime_module.SandboxCapacityError, match="busy"):
        await service._create_task(session)

    assert session.sandbox_id == "sandbox-paused"
    assert retired == []
    assert allocated is False


@pytest.mark.asyncio
async def test_missing_sandbox_is_idempotently_replaced_for_dataset_remount():
    class NewSandbox:
        id = "sandbox-new"

        async def ensure_api_ready(self):
            return None

        async def get_browser(self):
            return object()

    class FakeRuntime:
        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-missing"
            raise SandboxNotFoundError("gone")

        async def allocate(self, session=None, dataset_ids=None):
            assert dataset_ids == ["dataset-new"]
            return NewSandbox()

        async def assign(self, sandbox, session, task_id=None):
            return None

    class FakeTask:
        id = "task-new"

        @classmethod
        def create(cls, _runner):
            return cls()

    class FakeRepository:
        async def save(self, _session):
            return None

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )
    session = Session(
        id="session",
        user_id="user",
        agent_id="agent",
        sandbox_id="sandbox-missing",
        sandbox_dataset_ids=["dataset-old"],
    )

    task = await service._create_task(session, ["dataset-new"])

    assert task.id == "task-new"
    assert session.sandbox_id == "sandbox-new"
    assert session.sandbox_dataset_ids == ["dataset-new"]


@pytest.mark.asyncio
async def test_local_health_counts_running_containers_without_records(monkeypatch):
    class EmptyQuery:
        async def to_list(self):
            return []

    class EmptyDocument:
        node_id = "node_id"
        status = "status"

        @classmethod
        def find(cls, *_args, **_kwargs):
            return EmptyQuery()

    monkeypatch.setattr(node_health, "SandboxRecordDocument", EmptyDocument)
    monkeypatch.setattr(node_health, "SandboxAllocationDocument", EmptyDocument)
    monkeypatch.setattr(
        node_health,
        "_local_sandbox_container_states",
        lambda: {
            "ai-dataseek-sandbox-untracked": "running",
            "ai-dataseek-sandbox-paused": "paused",
        },
    )
    monkeypatch.setattr(
        node_health,
        "_host_metrics",
        lambda: {
            "cpu_percent": 1.0,
            "cpu_cores": 8,
            "memory_used_bytes": 1,
            "memory_total_bytes": 2,
            "disk_used_bytes": 1,
            "disk_total_bytes": 2,
            "disk_free_bytes": 1,
            "memory_available_bytes": 1,
            "load_average": (0.0, 0.0, 0.0),
        },
    )
    node = SimpleNamespace(
        node_id="local-default",
        enabled=True,
        capacity=ExecutionNodeCapacity(max_sandboxes=2),
        health=ExecutionNodeHealth(),
    )

    await node_health._check_local_docker_node(node)

    assert node.health.running_sandboxes == 1
    assert node.health.paused_sandboxes == 1
    assert node.health.raw["sandbox_containers_total"] == 2


@pytest.mark.asyncio
async def test_local_reconcile_does_not_destroy_foreign_node_records(monkeypatch):
    class FakeRecord:
        def __init__(self, sandbox_id):
            self.container_name = sandbox_id
            self.status = "assigned"
            self.destroyed_at = None
            self.last_used_at = None
            self.saved = False

        async def save(self):
            self.saved = True

    local_record = FakeRecord("sandbox-local-missing")
    foreign_record = FakeRecord("sandbox-foreign")
    allocation = SimpleNamespace(
        sandbox_id="sandbox-local-missing",
        session_id="session-local",
        status=runtime_module.SandboxAllocationStatus.RUNNING,
        updated_at=None,
        failure_reason=None,
        save=AsyncMock(),
    )
    session = SimpleNamespace(
        sandbox_id="sandbox-local-missing",
        sandbox_dataset_ids=["dataset"],
        task_id="task",
        updated_at=None,
        save=AsyncMock(),
    )

    class Query:
        def __init__(self, values):
            self.values = values

        async def to_list(self):
            return self.values

    class FakeAllocationDocument:
        node_id = "node_id"
        status = "status"

        @classmethod
        def find(cls, *_args):
            return Query([allocation])

    class FakeSessionDocument:
        @classmethod
        def find(cls, *_args):
            return Query([session])

    monkeypatch.setattr(node_health, "SandboxAllocationDocument", FakeAllocationDocument)
    monkeypatch.setattr(node_health, "SessionDocument", FakeSessionDocument)

    await node_health._reconcile_local_sandbox_lifecycle(
        node_id="local-default",
        container_states={},
        sandbox_records=[local_record, foreign_record],
    )

    assert local_record.status == "destroyed"
    assert foreign_record.status == "assigned"
    assert foreign_record.saved is False
    assert allocation.status == runtime_module.SandboxAllocationStatus.RELEASED
    assert session.sandbox_id is None
    assert session.sandbox_dataset_ids == []
    assert session.task_id is None


@pytest.mark.asyncio
async def test_api_and_vnc_readiness_use_independent_service_sets():
    sandbox = DockerSandbox.__new__(DockerSandbox)
    observed: list[set[str] | None] = []
    profiles: list[str] = []

    async def capture(*, required_services):
        observed.append(required_services)

    async def capture_profile(profile):
        profiles.append(profile)

    sandbox._wait_for_supervisor_services = capture
    sandbox._ensure_supervisor_profile = capture_profile

    await sandbox.ensure_sandbox()
    await sandbox.ensure_api_ready()
    await sandbox.ensure_browser_ready()
    await sandbox.ensure_vnc_ready()

    assert observed == [
        {"app", "xvfb", "chrome", "socat", "x11vnc", "websockify"},
        {"app"},
        {"app", "xvfb", "chrome", "socat"},
        {"app", "xvfb", "chrome", "socat", "x11vnc", "websockify"},
    ]
    assert profiles == ["vnc", "browser", "vnc"]


@pytest.mark.asyncio
async def test_deployment_warm_pool_target_tracks_environment_and_reserves_capacity(monkeypatch):
    class FakeNode:
        node_id = "local-default"
        name = "local-default"
        description = "local"
        status = ExecutionNodeStatus.HEALTHY
        enabled = True
        capacity = ExecutionNodeCapacity(max_sandboxes=3)
        runtime_config = {node_health.WARM_POOL_TARGET_KEY: 0}
        updated_at = None
        saved = False

        async def save(self):
            self.saved = True

    node = FakeNode()

    class FakeExecutionNodeDocument:
        node_id = "node_id"
        name = "name"

        @classmethod
        async def find_one(cls, *_args):
            return node

    monkeypatch.setattr(node_health, "ExecutionNodeDocument", FakeExecutionNodeDocument)
    monkeypatch.setattr(
        node_health,
        "get_settings",
        lambda: SimpleNamespace(
            sandbox_max_concurrent=3,
            sandbox_paused_destroy_after_minutes=None,
            sandbox_pool_size=3,
        ),
    )

    result = await node_health.ensure_local_default_node()

    assert result.runtime_config[node_health.WARM_POOL_TARGET_KEY] == 2
    assert result.saved is True


@pytest.mark.asyncio
async def test_admin_managed_resource_config_is_not_overwritten_by_environment(monkeypatch):
    class FakeNode:
        node_id = "local-default"
        name = "local-default"
        description = "local"
        status = ExecutionNodeStatus.HEALTHY
        enabled = True
        capacity = ExecutionNodeCapacity(max_sandboxes=5)
        runtime_config = {
            node_health.RESOURCE_CONFIG_MANAGED_KEY: True,
            node_health.WARM_POOL_TARGET_KEY: 1,
            node_health.PAUSED_RECLAIM_MINUTES_KEY: 45,
        }
        updated_at = None
        saved = False

        async def save(self):
            self.saved = True

    node = FakeNode()

    class FakeExecutionNodeDocument:
        node_id = "node_id"
        name = "name"

        @classmethod
        async def find_one(cls, *_args):
            return node

    monkeypatch.setattr(node_health, "ExecutionNodeDocument", FakeExecutionNodeDocument)
    monkeypatch.setattr(
        node_health,
        "get_settings",
        lambda: SimpleNamespace(
            sandbox_max_concurrent=2,
            sandbox_paused_destroy_after_minutes=30,
            sandbox_pool_size=0,
        ),
    )

    result = await node_health.ensure_local_default_node()

    assert result.capacity.max_sandboxes == 5
    assert result.runtime_config[node_health.PAUSED_RECLAIM_MINUTES_KEY] == 45
    assert result.runtime_config[node_health.WARM_POOL_TARGET_KEY] == 1
    assert result.saved is False


@pytest.mark.asyncio
async def test_resource_configuration_update_persists_and_resizes_pool(monkeypatch):
    calls = []

    class FakeNode:
        capacity = ExecutionNodeCapacity(max_sandboxes=2)
        runtime_config = {
            node_health.WARM_POOL_TARGET_KEY: 0,
            node_health.PAUSED_RECLAIM_MINUTES_KEY: 30,
        }
        health = ExecutionNodeHealth()
        updated_at = None

        async def save(self):
            calls.append("save")

    node = FakeNode()
    service_module = __import__(
        "app.application.services.resource_configuration_service",
        fromlist=["resource_configuration_service"],
    )

    async def ensure_node():
        return node

    async def resize(target):
        calls.append(("resize", target))

    async def check(_node):
        return None

    monkeypatch.setattr(service_module, "ensure_local_default_node", ensure_node)
    monkeypatch.setattr(service_module, "configure_sandbox_pool", resize)
    monkeypatch.setattr(service_module, "check_execution_node", check)

    result = await ResourceConfigurationService().update(
        sandbox_max_concurrent=4,
        sandbox_pool_size=1,
        sandbox_paused_destroy_after_minutes=45,
    )

    assert node.capacity.max_sandboxes == 4
    assert node.runtime_config[node_health.RESOURCE_CONFIG_MANAGED_KEY] is True
    assert node.runtime_config[node_health.WARM_POOL_TARGET_KEY] == 1
    assert node.runtime_config[node_health.PAUSED_RECLAIM_MINUTES_KEY] == 45
    assert calls == ["save", ("resize", 1)]
    assert result["sandbox_max_concurrent"] == 4
    assert result["configuration_source"] == "admin"


@pytest.mark.asyncio
async def test_sandbox_pool_resize_retires_surplus_warm_sandboxes():
    class FakeSandbox:
        def __init__(self, sandbox_id):
            self.id = sandbox_id
            self.destroyed = False

        async def destroy(self):
            self.destroyed = True
            return True

    pool = SandboxPool(pool_size=2)
    first = FakeSandbox("warm-1")
    second = FakeSandbox("warm-2")
    await pool._pool.put(first)
    await pool._pool.put(second)

    await pool.resize(1)

    assert pool.target_size == 1
    assert pool.warm_count == 1
    assert sum(item.destroyed for item in (first, second)) == 1


@pytest.mark.asyncio
async def test_vnc_url_uses_vnc_specific_readiness_check():
    calls: list[str] = []

    class FakeSandbox:
        vnc_url = "ws://sandbox:5901"

        async def ensure_vnc_ready(self):
            calls.append("vnc-ready")

        async def ensure_browser_ready(self):
            raise AssertionError("VNC retrieval must not use CDP readiness")

    class FakeRuntime:
        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-1"
            return FakeSandbox()

    class FakeSessionRepository:
        async def find_by_id(self, session_id):
            return Session(
                id=session_id,
                user_id="user-1",
                agent_id="agent-1",
                sandbox_id="sandbox-1",
            )

    service = AgentService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )

    assert await service.get_vnc_url("session-1") == "ws://sandbox:5901"
    assert calls == ["vnc-ready"]


@pytest.mark.asyncio
async def test_delete_session_destroys_sandbox_before_repository_record():
    calls: list[str] = []
    session = Session(
        id="session-delete",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-delete",
    )

    class FakeSandbox:
        async def destroy(self):
            calls.append("destroy")
            return True

    class FakeRuntime:
        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-delete"
            calls.append("restore")
            return FakeSandbox()

    class FakeRepository:
        async def find_owned_by_id_and_user_id(self, session_id, user_id):
            assert (session_id, user_id) == ("session-delete", "user-1")
            return session

        async def find_by_id(self, session_id):
            assert session_id == "session-delete"
            return session

        async def delete(self, session_id):
            assert session_id == "session-delete"
            calls.append("delete")

    service = AgentService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=FakeRuntime(),
    )

    await service.delete_session("session-delete", "user-1")

    assert calls == ["restore", "destroy", "delete"]


@pytest.mark.asyncio
async def test_task_close_decision_is_atomic_with_new_input():
    class EmptyQueue:
        async def is_empty(self):
            return True

        async def put(self, _message):
            raise AssertionError("closed task must not enqueue")

    task = RedisStreamTask.__new__(RedisStreamTask)
    task._id = "task-closing"
    task._input_stream = EmptyQueue()
    task._input_lifecycle_lock = asyncio.Lock()
    task._closing = False
    task._closed = asyncio.Event()

    assert await task.pop_input_or_close() == (None, None)
    with pytest.raises(TaskInputClosedError):
        await task.enqueue_input("late message")


@pytest.mark.asyncio
async def test_task_cancel_removes_input_that_finishes_queueing_during_shutdown():
    put_started = asyncio.Event()
    allow_put_to_finish = asyncio.Event()
    deleted: list[str] = []

    class RacingQueue:
        async def put(self, _message):
            put_started.set()
            await allow_put_to_finish.wait()
            return "message-1"

        async def delete_message(self, event_id):
            deleted.append(event_id)
            return True

    task = RedisStreamTask.__new__(RedisStreamTask)
    task._id = "task-racing-close"
    task._input_stream = RacingQueue()
    task._input_lifecycle_lock = asyncio.Lock()
    task._closing = False
    task._closed = asyncio.Event()
    execution_task = asyncio.create_task(asyncio.sleep(60))
    task._execution_task = execution_task

    enqueue = asyncio.create_task(task.enqueue_input("late message"))
    await put_started.wait()
    assert task.cancel() is True
    allow_put_to_finish.set()

    with pytest.raises(TaskInputClosedError):
        await enqueue
    with pytest.raises(asyncio.CancelledError):
        await execution_task
    assert deleted == ["message-1"]


@pytest.mark.asyncio
async def test_legacy_local_restore_recreates_allocation_and_capacity_binding(monkeypatch):
    class FakeSandbox:
        id = "legacy-sandbox"
        base_url = "http://sandbox"
        vnc_url = "ws://sandbox/vnc"
        cdp_url = "ws://sandbox/cdp"

        async def is_paused(self):
            return True

    node = SimpleNamespace(node_id="local-default")
    upserts = []

    monkeypatch.setattr(
        runtime_module,
        "_restore_allocation_for_sandbox",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        runtime_module,
        "_find_worker_sandbox_by_id",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        runtime_module.DockerSandbox,
        "get",
        AsyncMock(return_value=FakeSandbox()),
    )
    monkeypatch.setattr(
        runtime_module,
        "ensure_local_default_node",
        AsyncMock(return_value=node),
    )

    async def capture_upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(runtime_module, "_upsert_allocation", capture_upsert)

    restored = await runtime_module.LocalDockerRuntime().restore("legacy-sandbox")

    assert isinstance(restored, runtime_module.NodeBoundSandbox)
    assert restored.node is node
    assert upserts[0]["node_id"] == "local-default"
    assert upserts[0]["status"] == runtime_module.SandboxAllocationStatus.PAUSED


@pytest.mark.asyncio
async def test_concurrent_session_bootstraps_create_only_one_task():
    stored = Session(id="session", user_id="user", agent_id="agent")
    create_count = 0

    class FakeInputStream:
        def __init__(self):
            self.messages = []

        async def put(self, message):
            self.messages.append(message)
            return f"{len(self.messages)}-0"

    class FakeTask:
        registry = {}

        def __init__(self):
            self.id = "task-one"
            self.input_stream = FakeInputStream()
            self.done = True
            self.accepting_input = True
            self.registry[self.id] = self

        async def run(self):
            self.done = False

        @classmethod
        def get(cls, task_id):
            return cls.registry.get(task_id)

    class FakeRepository:
        async def find_by_id_and_user_id(self, _session_id, _user_id):
            return stored.model_copy(deep=True)

        async def claim_client_message_id(self, _session_id, _message_id):
            return True

        async def update_status(self, *_args):
            return None

        async def get_events(self, _session_id):
            return []

        async def save(self, session):
            nonlocal stored
            stored = session.model_copy(deep=True)

        async def update_latest_message(self, *_args):
            return None

        async def add_event(self, *_args):
            return None

        async def release_client_message_id(self, *_args):
            return None

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeRepository(),
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=object(),
    )

    async def create_task(session, _dataset_ids=None):
        nonlocal create_count
        create_count += 1
        await asyncio.sleep(0.01)
        task = FakeTask()
        session.task_id = task.id
        await service._session_repository.save(session)
        return task

    service._create_task = create_task

    first, second = await asyncio.gather(
        service._bootstrap_chat_task(
            stored.model_copy(deep=True),
            "user",
            "first",
            None,
            None,
            None,
            None,
            None,
            False,
            "message-1",
        ),
        service._bootstrap_chat_task(
            stored.model_copy(deep=True),
            "user",
            "second",
            None,
            None,
            None,
            None,
            None,
            False,
            "message-2",
        ),
    )

    assert first is second
    assert create_count == 1
    assert len(first.input_stream.messages) == 2
