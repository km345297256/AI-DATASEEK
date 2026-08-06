from datetime import datetime, UTC
import os
import shutil
import time
from typing import Optional

import anyio
import docker
import httpx

from app.core.config import get_settings
from app.domain.models.execution_node import (
    ExecutionNodeAuthType,
    ExecutionNodeCapacity,
    ExecutionNodeHealth,
    ExecutionNodeStatus,
    ExecutionNodeType,
    SandboxAllocationStatus,
)
from app.infrastructure.external.sandbox.container_identity import is_sandbox_container_name
from app.infrastructure.models.documents import (
    ExecutionNodeDocument,
    NodeCredentialDocument,
    SandboxAllocationDocument,
    SandboxRecordDocument,
    SessionDocument,
)


LOCAL_DEFAULT_NODE_ID = "local-default"
RESOURCE_CONFIG_MANAGED_KEY = "admin_resource_configured"
WARM_POOL_TARGET_KEY = "warm_pool_target"
PAUSED_RECLAIM_MINUTES_KEY = "paused_sandbox_destroy_after_minutes"


def normalized_warm_pool_target(value: object, max_sandboxes: object) -> int:
    """Keep warm capacity bounded while reserving one slot for mounted data."""

    try:
        maximum = max(1, int(max_sandboxes))
    except (TypeError, ValueError):
        maximum = 1
    try:
        target = max(0, int(value))
    except (TypeError, ValueError):
        target = 0
    return min(target, 16, max(0, maximum - 1))


def _read_cpu_times() -> Optional[tuple[int, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as stat_file:
            first_line = stat_file.readline().strip()
    except OSError:
        return None
    parts = first_line.split()
    if not parts or parts[0] != "cpu":
        return None
    values = [int(value) for value in parts[1:] if value.isdigit()]
    if not values:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def _host_cpu_percent(sample_seconds: float = 0.1) -> Optional[float]:
    first = _read_cpu_times()
    if not first:
        load_average = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
        cpu_cores = os.cpu_count() or 1
        return round(min(100.0, max(0.0, (load_average / cpu_cores) * 100)), 2) if load_average is not None else None
    time.sleep(sample_seconds)
    second = _read_cpu_times()
    if not second:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)


def _host_memory() -> dict[str, Optional[int]]:
    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as meminfo_file:
            for line in meminfo_file:
                key, _, value = line.partition(":")
                amount = value.strip().split()[0] if value.strip() else ""
                if amount.isdigit():
                    meminfo[key] = int(amount) * 1024
    except OSError:
        return {"memory_total_bytes": None, "memory_available_bytes": None, "memory_used_bytes": None}

    total = meminfo.get("MemTotal")
    available = meminfo.get("MemAvailable")
    used = max(0, total - available) if total is not None and available is not None else None
    return {
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "memory_used_bytes": used,
    }


def _host_metrics() -> dict:
    disk = shutil.disk_usage("/")
    return {
        "cpu_percent": _host_cpu_percent(),
        "cpu_cores": os.cpu_count(),
        **_host_memory(),
        "disk_total_bytes": disk.total,
        "disk_used_bytes": disk.used,
        "disk_free_bytes": disk.free,
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }


def _update_capacity_from_usage(doc: ExecutionNodeDocument, usage: dict) -> None:
    cpu_cores = usage.get("cpu_cores")
    memory_total = usage.get("memory_total_bytes")
    disk_total = usage.get("disk_total_bytes")
    if not doc.capacity.cpu_cores and isinstance(cpu_cores, (int, float)):
        doc.capacity.cpu_cores = float(cpu_cores)
    if not doc.capacity.memory_bytes and isinstance(memory_total, int):
        doc.capacity.memory_bytes = memory_total
    if not doc.capacity.disk_bytes and isinstance(disk_total, int):
        doc.capacity.disk_bytes = disk_total


def _local_sandbox_container_states() -> Optional[dict[str, str]]:
    client = None
    try:
        name_prefix = get_settings().sandbox_name_prefix
        client = docker.from_env()
        containers = client.containers.list(all=True)
        return {
            container.name: container.status
            for container in containers
            if is_sandbox_container_name(container.name, name_prefix)
        }
    except Exception:
        return None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


async def ensure_local_default_node() -> ExecutionNodeDocument:
    settings = get_settings()
    doc = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.node_id == LOCAL_DEFAULT_NODE_ID)
    if not doc:
        doc = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.name == "local-default")
    now = datetime.now(UTC)
    if doc:
        changed = False
        runtime_config = dict(doc.runtime_config or {})
        admin_managed = bool(runtime_config.get(RESOURCE_CONFIG_MANAGED_KEY))
        if doc.node_id != LOCAL_DEFAULT_NODE_ID:
            doc.node_id = LOCAL_DEFAULT_NODE_ID
            changed = True
        if doc.status == ExecutionNodeStatus.DELETED:
            doc.status = ExecutionNodeStatus.UNKNOWN
            doc.enabled = True
            changed = True
        if doc.name != "local-default":
            doc.name = "local-default"
            changed = True
        if not doc.description:
            doc.description = "当前 backend 所在服务器的本地 Docker 执行环境"
            changed = True
        configured_capacity = None if admin_managed else settings.sandbox_max_concurrent
        if configured_capacity is not None:
            configured_capacity = max(1, int(configured_capacity))
            if doc.capacity.max_sandboxes != configured_capacity:
                doc.capacity.max_sandboxes = configured_capacity
                changed = True
        configured_paused_ttl = None if admin_managed else settings.sandbox_paused_destroy_after_minutes
        if configured_paused_ttl is not None:
            configured_paused_ttl = max(1, int(configured_paused_ttl))
            if runtime_config.get(PAUSED_RECLAIM_MINUTES_KEY) != configured_paused_ttl:
                runtime_config[PAUSED_RECLAIM_MINUTES_KEY] = configured_paused_ttl
                doc.runtime_config = runtime_config
                changed = True
        warm_source = (
            runtime_config.get(WARM_POOL_TARGET_KEY, 0)
            if admin_managed
            else settings.sandbox_pool_size
        )
        configured_warm_target = normalized_warm_pool_target(
            warm_source,
            doc.capacity.max_sandboxes,
        )
        if runtime_config.get(WARM_POOL_TARGET_KEY) != configured_warm_target:
            runtime_config[WARM_POOL_TARGET_KEY] = configured_warm_target
            doc.runtime_config = runtime_config
            changed = True
        if changed:
            doc.updated_at = now
            await doc.save()
        return doc

    maximum = max(1, int(settings.sandbox_max_concurrent or 1))
    runtime_config = {
        WARM_POOL_TARGET_KEY: normalized_warm_pool_target(
            settings.sandbox_pool_size,
            maximum,
        ),
    }
    if settings.sandbox_paused_destroy_after_minutes is not None:
        runtime_config[PAUSED_RECLAIM_MINUTES_KEY] = max(
            1,
            int(settings.sandbox_paused_destroy_after_minutes),
        )
    capacity = ExecutionNodeCapacity(
        max_sandboxes=maximum,
    )
    doc = ExecutionNodeDocument(
        node_id=LOCAL_DEFAULT_NODE_ID,
        name="local-default",
        description="当前 backend 所在服务器的本地 Docker 执行环境",
        type=ExecutionNodeType.LOCAL_DOCKER,
        status=ExecutionNodeStatus.UNKNOWN,
        enabled=True,
        runtime_config=runtime_config,
        capacity=capacity,
    )
    await doc.insert()
    return doc


def normalize_docker_host(value: Optional[str]) -> str:
    if not value or not value.strip():
        raise ValueError("Remote Docker base_url is required, for example tcp://10.0.82.238:2375")
    host = value.strip()
    if host.startswith("http://"):
        host = f"tcp://{host.removeprefix('http://')}"
    elif host.startswith("https://"):
        host = f"tcp://{host.removeprefix('https://')}"
    elif "://" not in host:
        host = f"tcp://{host}"
    if host.startswith("tcp://") and ":" not in host.removeprefix("tcp://"):
        host = f"{host}:2375"
    return host


async def resolve_node_credential(credential_ref: Optional[str]) -> Optional[str]:
    if not credential_ref:
        return None
    stored = await NodeCredentialDocument.find_one({"credential_ref": credential_ref})
    if stored:
        return stored.secret_value
    return os.getenv(credential_ref)


async def execution_node_auth_headers(doc: ExecutionNodeDocument) -> dict[str, str]:
    if doc.auth_type == ExecutionNodeAuthType.BEARER:
        if not doc.credential_ref:
            raise ValueError("credential_ref is required when auth_type is bearer")
        token = await resolve_node_credential(doc.credential_ref)
        if not token:
            raise ValueError(f"Node credential is not set: {doc.credential_ref}")
        return {"Authorization": f"Bearer {token}"}
    return {}


async def check_execution_node(doc: ExecutionNodeDocument) -> None:
    doc.status = ExecutionNodeStatus.CHECKING
    doc.last_checked_at = datetime.now(UTC)
    doc.failure_reason = None
    await doc.save()

    try:
        if doc.type == ExecutionNodeType.LOCAL_DOCKER:
            await _check_local_docker_node(doc)
            return

        if doc.type == ExecutionNodeType.WORKER_AGENT:
            await _check_worker_agent_node(doc)
            return

        if doc.type == ExecutionNodeType.REMOTE_DOCKER:
            await _check_remote_docker_node(doc)
            return

        doc.status = ExecutionNodeStatus.UNKNOWN if doc.enabled else ExecutionNodeStatus.DISABLED
        doc.failure_reason = f"Health check for node type {doc.type} is not implemented yet"
    except Exception as exc:
        doc.status = ExecutionNodeStatus.UNHEALTHY if doc.enabled else ExecutionNodeStatus.DISABLED
        doc.failure_reason = str(exc)
    finally:
        doc.last_checked_at = datetime.now(UTC)
        doc.updated_at = datetime.now(UTC)
        await doc.save()


async def mark_execution_node_unhealthy(node_id: str, reason: str) -> None:
    doc = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.node_id == node_id)
    if not doc:
        return
    doc.status = ExecutionNodeStatus.UNHEALTHY if doc.enabled else ExecutionNodeStatus.DISABLED
    doc.failure_reason = reason
    doc.last_checked_at = datetime.now(UTC)
    doc.updated_at = datetime.now(UTC)
    await doc.save()


async def _check_local_docker_node(doc: ExecutionNodeDocument) -> None:
    metrics = _host_metrics()
    container_states = await anyio.to_thread.run_sync(_local_sandbox_container_states)
    sandbox_records = await SandboxRecordDocument.find(
        {"status": {"$in": ["warm", "assigned", "paused", "destroyed"]}}
    ).to_list()

    if container_states is None:
        running_sandboxes = sum(1 for record in sandbox_records if record.status == "assigned")
        paused_sandboxes = sum(1 for record in sandbox_records if record.status == "paused")
        warm_sandboxes = sum(1 for record in sandbox_records if record.status == "warm")
        assigned_sandboxes = sum(1 for record in sandbox_records if record.status == "assigned")
        destroyed_sandboxes = sum(1 for record in sandbox_records if record.status == "destroyed")
        container_states = {}
    else:
        await _reconcile_local_sandbox_lifecycle(
            node_id=doc.node_id,
            container_states=container_states,
            sandbox_records=sandbox_records,
        )
        warm_sandboxes = sum(
            1
            for record in sandbox_records
            if record.status == "warm" and container_states.get(record.container_name) == "running"
        )
        assigned_sandboxes = sum(
            1
            for record in sandbox_records
            if record.status == "assigned" and container_states.get(record.container_name) == "running"
        )
        # Capacity is a Docker runtime property, not a database-record property.
        # Count warm and temporarily untracked containers as well so prewarming
        # and crash recovery cannot silently overcommit the host.
        running_sandboxes = sum(
            1 for status in container_states.values() if status == "running"
        )
        paused_sandboxes = sum(1 for status in container_states.values() if status == "paused")
        destroyed_sandboxes = sum(1 for record in sandbox_records if record.status == "destroyed")
    doc.health = ExecutionNodeHealth(
        running_sandboxes=running_sandboxes,
        warm_sandboxes=warm_sandboxes,
        assigned_sandboxes=assigned_sandboxes,
        paused_sandboxes=paused_sandboxes,
        destroyed_sandboxes=destroyed_sandboxes,
        cpu_percent=metrics.get("cpu_percent"),
        memory_used_bytes=metrics.get("memory_used_bytes"),
        disk_used_bytes=metrics.get("disk_used_bytes"),
        raw={
            "cpu_cores": metrics.get("cpu_cores"),
            "memory_total_bytes": metrics.get("memory_total_bytes"),
            "memory_available_bytes": metrics.get("memory_available_bytes"),
            "disk_total_bytes": metrics.get("disk_total_bytes"),
            "disk_free_bytes": metrics.get("disk_free_bytes"),
            "load_average": metrics.get("load_average"),
            "sandbox_containers_total": len(container_states),
        },
    )
    _update_capacity_from_usage(doc, metrics)
    doc.status = ExecutionNodeStatus.HEALTHY if doc.enabled else ExecutionNodeStatus.DISABLED
    doc.last_heartbeat_at = datetime.now(UTC)


async def _reconcile_local_sandbox_lifecycle(
    *,
    node_id: str,
    container_states: dict[str, str],
    sandbox_records: list,
) -> None:
    """Converge persisted lifecycle state with the local Docker daemon.

    Docker containers use ``remove=True`` and can disappear after a crash or
    sandbox timeout without giving the backend a final callback.  Leaving those
    allocations in ``running`` makes capacity/admin state permanently stale and
    leaves sessions pointing at containers that can never be restored.
    """

    now = datetime.now(UTC)
    active_allocations = await SandboxAllocationDocument.find(
        SandboxAllocationDocument.node_id == node_id,
        SandboxAllocationDocument.status != SandboxAllocationStatus.RELEASED,
    ).to_list()
    records_by_id = {record.container_name: record for record in sandbox_records}
    missing_ids: set[str] = set()
    for allocation in active_allocations:
        state = container_states.get(allocation.sandbox_id)
        record = records_by_id.get(allocation.sandbox_id)
        if state in {"created", "restarting"}:
            # Do not release a container while Docker is still bringing it up.
            # The next health tick will converge it once the state stabilizes.
            continue
        if state in {"running", "paused"}:
            expected_allocation_status = (
                SandboxAllocationStatus.PAUSED
                if state == "paused"
                else SandboxAllocationStatus.RUNNING
            )
            if allocation.status != expected_allocation_status:
                allocation.status = expected_allocation_status
                allocation.updated_at = now
                await allocation.save()
            if record:
                expected_record_status = (
                    "paused"
                    if state == "paused"
                    else ("assigned" if allocation.session_id else "warm")
                )
                if record.status != expected_record_status:
                    record.status = expected_record_status
                    if state == "paused" and not record.paused_at:
                        record.paused_at = now
                    elif state == "running":
                        record.paused_at = None
                    await record.save()
            continue
        allocation.status = SandboxAllocationStatus.RELEASED
        allocation.updated_at = now
        allocation.failure_reason = allocation.failure_reason or (
            "Sandbox container no longer exists"
            if state is None
            else f"Sandbox container is not active ({state})"
        )
        await allocation.save()
        missing_ids.add(allocation.sandbox_id)
        if record and record.status != "destroyed":
            record.status = "destroyed"
            record.destroyed_at = now
            record.last_used_at = now
            await record.save()

    await clear_session_sandbox_references(missing_ids, now=now)


async def clear_session_sandbox_references(
    sandbox_ids: set[str] | list[str],
    *,
    now: datetime | None = None,
) -> None:
    """Clear persisted session pointers after a sandbox is definitively gone."""

    normalized_ids = sorted({sandbox_id for sandbox_id in sandbox_ids if sandbox_id})
    if not normalized_ids:
        return
    sessions = await SessionDocument.find(
        {"sandbox_id": {"$in": normalized_ids}}
    ).to_list()
    changed_at = now or datetime.now(UTC)
    for session in sessions:
        session.sandbox_id = None
        session.sandbox_dataset_ids = []
        # Task instances are process-local. If the backing container vanished,
        # a persisted task id cannot be resumed safely after reconciliation.
        session.task_id = None
        session.updated_at = changed_at
        await session.save()


async def _check_worker_agent_node(doc: ExecutionNodeDocument) -> None:
    if not doc.base_url:
        raise ValueError("Worker agent base_url is required")
    headers = await execution_node_auth_headers(doc)
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{doc.base_url.rstrip('/')}/health", headers=headers)
        response.raise_for_status()
        payload = response.json()
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    doc.health = ExecutionNodeHealth(
        running_sandboxes=int(usage.get("running_sandboxes") or 0),
        warm_sandboxes=int(usage.get("warm_sandboxes") or 0),
        assigned_sandboxes=int(usage.get("assigned_sandboxes") or 0),
        paused_sandboxes=int(usage.get("paused_sandboxes") or 0),
        cpu_percent=usage.get("cpu_percent"),
        memory_used_bytes=usage.get("memory_used_bytes"),
        disk_used_bytes=usage.get("disk_used_bytes"),
        raw=payload if isinstance(payload, dict) else {"response": payload},
    )
    _update_capacity_from_usage(doc, usage)
    doc.status = ExecutionNodeStatus.HEALTHY if doc.enabled else ExecutionNodeStatus.DISABLED
    doc.last_heartbeat_at = datetime.now(UTC)


async def _check_remote_docker_node(doc: ExecutionNodeDocument) -> None:
    if doc.auth_type not in {ExecutionNodeAuthType.NONE, ExecutionNodeAuthType.DOCKER_TLS}:
        raise ValueError("Remote Docker only supports auth_type none or docker_tls")
    docker_host = normalize_docker_host(doc.base_url)
    tls_config = None
    if doc.auth_type == ExecutionNodeAuthType.DOCKER_TLS:
        raise ValueError("Remote Docker TLS credential loading is not implemented; use worker_agent for production nodes")

    def inspect() -> tuple[dict, list]:
        client = docker.DockerClient(base_url=docker_host, tls=tls_config, timeout=5)
        try:
            client.ping()
            info = client.info()
            containers = client.containers.list(all=True)
            return info, containers
        finally:
            client.close()

    info, containers = await anyio.to_thread.run_sync(inspect)
    runtime_config = doc.runtime_config if isinstance(doc.runtime_config, dict) else {}
    name_prefix = runtime_config.get("name_prefix") or get_settings().sandbox_name_prefix
    sandbox_containers = [
        container
        for container in containers
        if is_sandbox_container_name(container.name, name_prefix)
    ]
    running_sandboxes = sum(1 for container in sandbox_containers if container.status == "running")
    paused_sandboxes = sum(1 for container in sandbox_containers if container.status == "paused")
    memory_total = info.get("MemTotal")
    memory_used = None
    if isinstance(memory_total, int):
        memory_used = max(0, memory_total - int(info.get("MemAvailable") or 0))
    doc.base_url = docker_host
    doc.health = ExecutionNodeHealth(
        running_sandboxes=running_sandboxes,
        paused_sandboxes=paused_sandboxes,
        cpu_percent=None,
        memory_used_bytes=memory_used,
        disk_used_bytes=None,
        raw={
            "docker_host": docker_host,
            "server_version": info.get("ServerVersion"),
            "containers": info.get("Containers"),
            "containers_running": info.get("ContainersRunning"),
            "images": info.get("Images"),
            "operating_system": info.get("OperatingSystem"),
            "architecture": info.get("Architecture"),
            "ncpu": info.get("NCPU"),
            "mem_total": memory_total,
            "sandbox_containers": len(sandbox_containers),
        },
    )
    if not doc.capacity.cpu_cores and info.get("NCPU"):
        doc.capacity.cpu_cores = float(info["NCPU"])
    if not doc.capacity.memory_bytes and isinstance(memory_total, int):
        doc.capacity.memory_bytes = memory_total
    doc.status = ExecutionNodeStatus.HEALTHY if doc.enabled else ExecutionNodeStatus.DISABLED
    doc.last_heartbeat_at = datetime.now(UTC)
