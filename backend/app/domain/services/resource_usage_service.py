import asyncio
import os
import resource
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from typing import Any, Dict, Optional

import httpx

from app.core.config import get_settings
from app.domain.models.execution_node import ExecutionNodeStatus, ExecutionNodeType
from app.infrastructure.external.sandbox.container_identity import is_sandbox_container_name
from app.infrastructure.external.sandbox.node_health import resolve_node_credential
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.models.documents import APIKeyDocument, ExecutionNodeDocument, SandboxRecordDocument, SessionDocument, TokenUsageDocument, UserDocument


class ResourceUsageService:
    async def get_overview(
        self,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        include_sandboxes: bool = False,
    ) -> Dict[str, Any]:
        token_usage, auth_usage, server_usage, execution_nodes_usage, worker_sandbox_usage = await asyncio.gather(
            self._get_token_usage(start_at=start_at, end_at=end_at),
            self._get_auth_usage(),
            self._get_server_usage(include_sandboxes=include_sandboxes),
            self._get_execution_nodes_usage(),
            self._get_worker_sandbox_usage() if include_sandboxes else asyncio.sleep(0, result=[]),
        )
        records = token_usage.pop("records")
        user_names = await self._get_user_names(records)
        sandbox_usage = [
            *server_usage.get("docker", {}).get("sandboxes", []),
            *worker_sandbox_usage,
        ]
        return {
            "token_usage": token_usage,
            "token_usage_by_user": self._aggregate_token_usage_by_dimension(records, "user_id", labels=user_names),
            "token_usage_by_workspace": self._aggregate_token_usage_by_dimension(records, "workspace_id"),
            "auth_usage": auth_usage,
            "server_usage": server_usage,
            "sandbox_usage": sandbox_usage,
            "execution_nodes_usage": execution_nodes_usage,
            "generated_at": datetime.now(UTC),
        }

    async def _get_token_usage(
        self,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        query = TokenUsageDocument.find()
        if start_at:
            query = query.find(TokenUsageDocument.created_at >= start_at)
        if end_at:
            query = query.find(TokenUsageDocument.created_at <= end_at)
        docs = await query.to_list()
        return {
            "record_count": len(docs),
            "prompt_tokens": sum(doc.prompt_tokens for doc in docs),
            "completion_tokens": sum(doc.completion_tokens for doc in docs),
            "total_tokens": sum(doc.total_tokens for doc in docs),
            "by_model": self._aggregate_token_usage_by_model(docs),
            "records": docs,
        }

    async def _get_auth_usage(self) -> Dict[str, Any]:
        users = await UserDocument.find().to_list()
        api_keys = await APIKeyDocument.find().to_list()
        sessions = await SessionDocument.find().to_list()
        return {
            "users_total": len(users),
            "users_active": sum(1 for user in users if user.is_active),
            "api_keys_total": len(api_keys),
            "api_keys_active": sum(1 for key in api_keys if key.status == "active"),
            "sessions_total": len(sessions),
        }

    async def _get_server_usage(self, *, include_sandboxes: bool = False) -> Dict[str, Any]:
        disk = shutil.disk_usage("/")
        usage = resource.getrusage(resource.RUSAGE_SELF)
        load_average = os.getloadavg() if hasattr(os, "getloadavg") else None
        docker_usage, file_storage_usage = await asyncio.gather(
            asyncio.to_thread(self._get_docker_usage, include_sandboxes=include_sandboxes),
            self._get_file_storage_usage(),
        )
        if include_sandboxes and docker_usage.get("available"):
            docker_usage["sandboxes"] = await self._attach_local_sandbox_lifecycle(docker_usage.get("sandboxes") or [])
        return {
            "cpu": {
                "load_average": load_average,
                "process_user_seconds": usage.ru_utime,
                "process_system_seconds": usage.ru_stime,
            },
            "memory": {
                "process_max_rss_kb": usage.ru_maxrss,
            },
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "used_percent": round((disk.used / disk.total) * 100, 2) if disk.total else 0,
            },
            "docker": docker_usage,
            "file_storage": file_storage_usage,
        }

    async def _get_file_storage_usage(self) -> Dict[str, Any]:
        try:
            storage = get_file_storage()
            usage = await storage.storage_usage() if hasattr(storage, "storage_usage") else {}
            health = await storage.health_check() if hasattr(storage, "health_check") else {}
            return {
                **usage,
                "available": health.get("available"),
                "health": health,
                "usage": usage,
            }
        except Exception:
            return {
                "available": False,
                "error": "File storage metrics are unavailable",
            }

    def _get_docker_usage(self, *, include_sandboxes: bool = False) -> Dict[str, Any]:
        try:
            import docker

            name_prefix = get_settings().sandbox_name_prefix
            client = docker.from_env(timeout=3)
            try:
                containers = client.containers.list(all=True)
                sandbox_containers = [
                    container
                    for container in containers
                    if is_sandbox_container_name(container.name, name_prefix)
                ]
                sandboxes = self._get_local_sandbox_container_usage(client) if include_sandboxes else []
                return {
                    "available": True,
                    "containers_total": len(containers),
                    "containers_running": sum(1 for container in containers if container.status == "running"),
                    "sandbox_containers_total": len(sandbox_containers),
                    "sandbox_containers_running": sum(1 for container in sandbox_containers if container.status == "running"),
                    "sandbox_cpu_percent": round(sum(item.get("cpu_percent") or 0 for item in sandboxes), 2),
                    "sandbox_memory_bytes": sum(item.get("memory_used_bytes") or 0 for item in sandboxes),
                    "sandbox_disk_bytes": sum(item.get("disk_used_bytes") or 0 for item in sandboxes),
                    "sandboxes": sandboxes,
                }
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        except Exception:
            return {
                "available": False,
                "error": "Docker metrics are unavailable",
            }

    def _get_local_sandbox_container_usage(self, client) -> list[Dict[str, Any]]:
        name_prefix = get_settings().sandbox_name_prefix
        containers = client.api.containers(all=True, size=True)
        sandbox_containers = [
            container
            for container in containers
            if any(
                is_sandbox_container_name(name, name_prefix)
                for name in container.get("Names", [])
            )
        ]
        def build_item(container: Dict[str, Any]) -> Dict[str, Any]:
            name = self._container_display_name(container)
            container_id = container.get("Id")
            item: Dict[str, Any] = {
                "id": container_id,
                "name": name,
                "docker_status": container.get("State") or container.get("Status"),
                "status": container.get("State") or container.get("Status"),
                "image": container.get("Image"),
                "created": container.get("Created"),
                "created_at": None,
                "last_used_at": None,
                "cpu_percent": None,
                "memory_used_bytes": None,
                "memory_limit_bytes": None,
                "memory_percent": None,
                "disk_used_bytes": container.get("SizeRw"),
                "disk_rootfs_bytes": container.get("SizeRootFs"),
                "network_rx_bytes": None,
                "network_tx_bytes": None,
                "block_read_bytes": None,
                "block_write_bytes": None,
            }
            if container_id and item["status"] == "running":
                try:
                    stats = client.api.stats(container_id, stream=False)
                    item.update(self._parse_container_stats(stats))
                except Exception:
                    item["stats_error"] = "Sandbox metrics are unavailable"
            return item

        if not sandbox_containers:
            return []

        max_workers = min(8, len(sandbox_containers))
        usage: list[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(build_item, container) for container in sandbox_containers]
            for future in as_completed(futures):
                usage.append(future.result())
        return sorted(usage, key=lambda item: item.get("name") or "")

    async def _attach_local_sandbox_lifecycle(self, sandboxes: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        records = await SandboxRecordDocument.find().to_list()
        records_by_name = {record.container_name: record for record in records}
        seen = {sandbox.get("name") for sandbox in sandboxes}
        for sandbox in sandboxes:
            name = sandbox.get("name")
            record = records_by_name.get(name or "")
            if not record:
                sandbox["lifecycle_status"] = self._sandbox_lifecycle_from_docker_status(sandbox.get("docker_status"))
                continue
            sandbox["lifecycle_status"] = record.status
            sandbox["status"] = record.status
            sandbox["session_id"] = record.session_id
            sandbox["task_id"] = record.task_id
            sandbox["created_at"] = record.created_at
            sandbox["last_used_at"] = record.last_used_at or record.paused_at or record.assigned_at or record.created_at
            sandbox["assigned_at"] = record.assigned_at
            sandbox["paused_at"] = record.paused_at
            sandbox["destroyed_at"] = record.destroyed_at

        for record in records:
            if record.status != "destroyed" or record.container_name in seen:
                continue
            sandboxes.append(
                {
                    "id": record.container_name,
                    "name": record.container_name,
                    "status": "destroyed",
                    "lifecycle_status": "destroyed",
                    "docker_status": "removed",
                    "image": None,
                    "created": None,
                    "created_at": record.created_at,
                    "last_used_at": record.last_used_at or record.destroyed_at or record.paused_at or record.assigned_at or record.created_at,
                    "assigned_at": record.assigned_at,
                    "paused_at": record.paused_at,
                    "destroyed_at": record.destroyed_at,
                    "session_id": record.session_id,
                    "task_id": record.task_id,
                    "cpu_percent": None,
                    "memory_used_bytes": None,
                    "memory_limit_bytes": None,
                    "memory_percent": None,
                    "disk_used_bytes": None,
                    "disk_rootfs_bytes": None,
                    "network_rx_bytes": None,
                    "network_tx_bytes": None,
                    "block_read_bytes": None,
                    "block_write_bytes": None,
                }
            )
        return sorted(sandboxes, key=self._sandbox_created_sort_key, reverse=True)

    def _sandbox_created_sort_key(self, item: Dict[str, Any]) -> float:
        value = item.get("created_at") or item.get("created")
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    def _sandbox_lifecycle_from_docker_status(self, docker_status: Optional[str]) -> str:
        if docker_status == "paused":
            return "paused"
        if docker_status == "running":
            return "running"
        return "destroyed"

    def _parse_container_stats(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        memory = stats.get("memory_stats") or {}
        memory_used = memory.get("usage")
        memory_limit = memory.get("limit")
        networks = stats.get("networks") or {}
        block_io = stats.get("blkio_stats") or {}
        return {
            "cpu_percent": self._container_cpu_percent(stats),
            "memory_used_bytes": memory_used,
            "memory_limit_bytes": memory_limit,
            "memory_percent": round((memory_used / memory_limit) * 100, 2)
            if isinstance(memory_used, (int, float)) and isinstance(memory_limit, (int, float)) and memory_limit
            else None,
            "network_rx_bytes": sum((iface or {}).get("rx_bytes") or 0 for iface in networks.values()),
            "network_tx_bytes": sum((iface or {}).get("tx_bytes") or 0 for iface in networks.values()),
            "block_read_bytes": self._sum_blkio_bytes(block_io, "Read"),
            "block_write_bytes": self._sum_blkio_bytes(block_io, "Write"),
        }

    def _container_cpu_percent(self, stats: Dict[str, Any]) -> Optional[float]:
        cpu_stats = stats.get("cpu_stats") or {}
        precpu_stats = stats.get("precpu_stats") or {}
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        precpu_usage = precpu_stats.get("cpu_usage") or {}
        cpu_delta = (cpu_usage.get("total_usage") or 0) - (precpu_usage.get("total_usage") or 0)
        system_delta = (cpu_stats.get("system_cpu_usage") or 0) - (precpu_stats.get("system_cpu_usage") or 0)
        online_cpus = cpu_stats.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or os.cpu_count() or 1
        if cpu_delta <= 0 or system_delta <= 0:
            return 0.0
        return round((cpu_delta / system_delta) * online_cpus * 100, 2)

    def _sum_blkio_bytes(self, block_io: Dict[str, Any], operation: str) -> int:
        total = 0
        for entry in block_io.get("io_service_bytes_recursive") or []:
            if (entry.get("op") or "").lower() == operation.lower():
                total += int(entry.get("value") or 0)
        return total

    def _container_display_name(self, container: Dict[str, Any]) -> str:
        names = container.get("Names") or []
        if names:
            return str(names[0]).lstrip("/")
        return str(container.get("Id") or "")[:12]

    async def _get_execution_nodes_usage(self) -> list[Dict[str, Any]]:
        nodes = await ExecutionNodeDocument.find().to_list()
        nodes = [node for node in nodes if node.status != ExecutionNodeStatus.DELETED]
        return [
            {
                "id": node.node_id,
                "name": node.name,
                "type": node.type,
                "status": node.status,
                "enabled": node.enabled,
                "base_url": None,
                "capacity": node.capacity.model_dump(),
                "health": node.health.model_dump(exclude={"raw"}),
                "last_checked_at": node.last_checked_at,
                "last_heartbeat_at": node.last_heartbeat_at,
                "failure_reason": (
                    "Execution node health check failed"
                    if node.failure_reason
                    else None
                ),
            }
            for node in nodes
        ]

    async def _get_worker_sandbox_usage(self) -> list[Dict[str, Any]]:
        nodes = await ExecutionNodeDocument.find(
            ExecutionNodeDocument.type == ExecutionNodeType.WORKER_AGENT,
            ExecutionNodeDocument.status != ExecutionNodeStatus.DELETED,
        ).to_list()
        usage: list[Dict[str, Any]] = []
        for node in nodes:
            if not node.enabled or node.status == ExecutionNodeStatus.DISABLED:
                continue
            if not node.base_url:
                continue
            token = await resolve_node_credential(node.credential_ref)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(f"{node.base_url.rstrip('/')}/sandboxes", headers=headers)
                    response.raise_for_status()
                    payload = response.json()
            except Exception:
                usage.append({
                    "id": None,
                    "name": node.name,
                    "status": "unavailable",
                    "node_id": node.node_id,
                    "node_name": node.name,
                    "source": "worker_agent",
                    "stats_error": "Worker sandbox metrics are unavailable",
                })
                continue
            if not isinstance(payload, list):
                continue
            for sandbox in payload:
                if not isinstance(sandbox, dict):
                    continue
                usage.append(self._worker_sandbox_usage_item(node, sandbox))
        return usage

    def _worker_sandbox_usage_item(self, node: ExecutionNodeDocument, sandbox: Dict[str, Any]) -> Dict[str, Any]:
        raw_usage = sandbox.get("usage") if isinstance(sandbox.get("usage"), dict) else {}
        return {
            "id": sandbox.get("id") or sandbox.get("sandbox_id"),
            "name": sandbox.get("name") or sandbox.get("id") or sandbox.get("sandbox_id") or "worker-sandbox",
            "status": sandbox.get("status") or "running",
            "image": sandbox.get("image"),
            "created": sandbox.get("created") or sandbox.get("created_at"),
            "node_id": node.node_id,
            "node_name": node.name,
            "source": "worker_agent",
            "cpu_percent": raw_usage.get("cpu_percent") or sandbox.get("cpu_percent"),
            "memory_used_bytes": raw_usage.get("memory_used_bytes") or sandbox.get("memory_used_bytes"),
            "memory_limit_bytes": raw_usage.get("memory_limit_bytes") or sandbox.get("memory_limit_bytes"),
            "memory_percent": raw_usage.get("memory_percent") or sandbox.get("memory_percent"),
            "disk_used_bytes": raw_usage.get("disk_used_bytes") or sandbox.get("disk_used_bytes"),
            "disk_rootfs_bytes": raw_usage.get("disk_rootfs_bytes") or sandbox.get("disk_rootfs_bytes"),
            "network_rx_bytes": raw_usage.get("network_rx_bytes") or sandbox.get("network_rx_bytes"),
            "network_tx_bytes": raw_usage.get("network_tx_bytes") or sandbox.get("network_tx_bytes"),
            "block_read_bytes": raw_usage.get("block_read_bytes") or sandbox.get("block_read_bytes"),
            "block_write_bytes": raw_usage.get("block_write_bytes") or sandbox.get("block_write_bytes"),
        }

    async def _get_user_names(self, docs: list[TokenUsageDocument]) -> dict[str, str]:
        user_ids = {doc.user_id for doc in docs if doc.user_id}
        if not user_ids:
            return {}
        users = await UserDocument.find({"user_id": {"$in": list(user_ids)}}).to_list()
        return {user.user_id: user.fullname for user in users}

    def _aggregate_token_usage_by_model(self, docs: list[TokenUsageDocument]) -> list[Dict[str, Any]]:
        by_model: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            key = doc.model_name or "unknown"
            item = by_model.setdefault(
                key,
                {
                    "model_name": key,
                    "record_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            item["record_count"] += 1
            item["prompt_tokens"] += doc.prompt_tokens
            item["completion_tokens"] += doc.completion_tokens
            item["total_tokens"] += doc.total_tokens
        return sorted(by_model.values(), key=lambda item: item["total_tokens"], reverse=True)

    def _aggregate_token_usage_by_dimension(
        self,
        docs: list[TokenUsageDocument],
        field_name: str,
        labels: Optional[dict[str, str]] = None,
    ) -> list[Dict[str, Any]]:
        labels = labels or {}
        aggregated: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            key = getattr(doc, field_name) or "unknown"
            item = aggregated.setdefault(
                key,
                {
                    "key": key,
                    "label": labels.get(key, key),
                    "record_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            item["record_count"] += 1
            item["prompt_tokens"] += doc.prompt_tokens
            item["completion_tokens"] += doc.completion_tokens
            item["total_tokens"] += doc.total_tokens
        return sorted(aggregated.values(), key=lambda item: item["total_tokens"], reverse=True)


def get_resource_usage_service() -> ResourceUsageService:
    return ResourceUsageService()
