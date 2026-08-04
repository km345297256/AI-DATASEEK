from typing import Optional, Type, Sequence
import logging
from datetime import datetime, UTC

import httpx

from app.domain.external.sandbox import Sandbox
from app.domain.models.execution_node import ExecutionNodeCapacity, ExecutionNodeStatus, ExecutionNodeType, SandboxAllocationStatus
from app.core.config import get_settings
from app.domain.models.session import Session
from app.domain.models.dataset import DatasetMount
from app.infrastructure.models.documents import ExecutionNodeDocument, SandboxAllocationDocument
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID, check_execution_node, ensure_local_default_node, mark_execution_node_unhealthy, resolve_node_credential

logger = logging.getLogger(__name__)


async def _ensure_local_default_node() -> None:
    try:
        await ensure_local_default_node()
    except Exception as exc:
        logger.warning("Failed to ensure local default execution node: %s", exc)


async def _list_execution_node_candidates(required_node_ids: set[str] | None = None) -> list[ExecutionNodeDocument]:
    await _ensure_local_default_node()
    candidates = await ExecutionNodeDocument.find(
        ExecutionNodeDocument.enabled == True,  # noqa: E712
        {"status": {"$in": [ExecutionNodeStatus.HEALTHY, ExecutionNodeStatus.UNKNOWN, ExecutionNodeStatus.CHECKING]}},
        {"type": {"$in": [ExecutionNodeType.LOCAL_DOCKER, ExecutionNodeType.REMOTE_DOCKER, ExecutionNodeType.WORKER_AGENT]}},
    ).to_list()
    for node in candidates:
        await check_execution_node(node)
    candidates = [
        node for node in candidates
        if node.enabled
        and node.status in {ExecutionNodeStatus.HEALTHY, ExecutionNodeStatus.UNKNOWN}
        and (required_node_ids is None or node.node_id in required_node_ids)
    ]
    if not candidates:
        raise RuntimeError("No enabled execution node is available for sandbox allocation")

    def score(node: ExecutionNodeDocument) -> tuple[int, int, str]:
        capacity = max(1, node.capacity.max_sandboxes)
        running = max(0, node.health.running_sandboxes)
        available = capacity - running
        health_bonus = 1 if node.status == ExecutionNodeStatus.HEALTHY else 0
        return (available, health_bonus, node.node_id)

    candidates = sorted(candidates, key=score, reverse=True)
    if candidates[0].capacity.max_sandboxes <= candidates[0].health.running_sandboxes:
        raise RuntimeError("All execution nodes are at sandbox capacity")
    return [node for node in candidates if node.capacity.max_sandboxes > node.health.running_sandboxes]


async def _select_execution_node() -> ExecutionNodeDocument:
    return (await _list_execution_node_candidates())[0]


def _node_docker_host(node: ExecutionNodeDocument) -> Optional[str]:
    if node.type == ExecutionNodeType.LOCAL_DOCKER:
        return None
    return node.base_url


class NodeBoundSandbox:
    """Internal wrapper keeping the scheduler node with the allocated sandbox."""

    def __init__(self, sandbox: Sandbox, node: ExecutionNodeDocument):
        self.sandbox = sandbox
        self.node = node

    def __getattr__(self, name: str):
        return getattr(self.sandbox, name)

    @property
    def id(self) -> str:
        return self.sandbox.id

    @property
    def vnc_url(self) -> str:
        return self.sandbox.vnc_url

    @property
    def cdp_url(self) -> str:
        return self.sandbox.cdp_url

    @property
    def base_url(self) -> str:
        return getattr(self.sandbox, "base_url", "")


async def _worker_headers(node: ExecutionNodeDocument) -> dict[str, str]:
    if not node.credential_ref:
        return {}
    token = await resolve_node_credential(node.credential_ref)
    if not token:
        raise RuntimeError(f"Node credential is not set: {node.credential_ref}")
    return {"Authorization": f"Bearer {token}"}


class WorkerAgentSandbox(DockerSandbox):
    def __init__(self, sandbox_id: str, api_url: str, vnc_url: str, cdp_url: str, worker_url: str, headers: Optional[dict[str, str]] = None):
        self._worker_url = worker_url.rstrip("/")
        self._worker_headers = headers or {}
        self.client = httpx.AsyncClient(timeout=600)
        self._container_name = sandbox_id
        self._docker_host = None
        self.ip = api_url.split("://", 1)[-1].split(":", 1)[0]
        self.base_url = api_url
        self._vnc_url = vnc_url
        self._cdp_url = cdp_url

    async def destroy(self) -> bool:
        try:
            response = await self.client.delete(f"{self._worker_url}/sandboxes/{self.id}", headers=self._worker_headers)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Failed to destroy worker sandbox %s: %s", self.id, exc)
            return False

    async def is_paused(self) -> bool:
        return False

    async def pause(self) -> bool:
        logger.info("Worker sandbox %s pause is not supported by this backend runtime", self.id)
        return False

    async def resume(self) -> bool:
        return True


def _worker_sandbox_from_payload(payload: dict, worker_url: str, headers: Optional[dict[str, str]] = None) -> WorkerAgentSandbox:
    return WorkerAgentSandbox(
        sandbox_id=payload["id"],
        api_url=payload["api_url"],
        vnc_url=payload["vnc_url"],
        cdp_url=payload["cdp_url"],
        worker_url=worker_url,
        headers=headers,
    )


async def _create_worker_sandbox(
    node: ExecutionNodeDocument,
    session: Optional[Session],
    mounts: Sequence[DatasetMount] | None = None,
) -> WorkerAgentSandbox:
    if not node.base_url:
        raise RuntimeError(f"Worker node {node.node_id} is missing base_url")
    settings = get_settings()
    payload = {
        "session_id": session.id if session else None,
        "image": node.runtime_config.get("image") or settings.sandbox_image,
        "name_prefix": node.runtime_config.get("name_prefix") or settings.sandbox_name_prefix,
        "network": node.runtime_config.get("network") or settings.sandbox_network,
        "public_base_url": node.runtime_config.get("public_base_url") or node.base_url,
        "environment": node.runtime_config.get("environment") or {},
        "mounts": [item.model_dump(mode="json") for item in mounts or []],
    }
    headers = await _worker_headers(node)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{node.base_url.rstrip('/')}/sandboxes", json=payload, headers=headers)
        response.raise_for_status()
        return _worker_sandbox_from_payload(response.json(), node.base_url, headers)


async def _get_worker_sandbox(node: ExecutionNodeDocument, sandbox_id: str) -> WorkerAgentSandbox:
    if not node.base_url:
        raise RuntimeError(f"Worker node {node.node_id} is missing base_url")
    headers = await _worker_headers(node)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{node.base_url.rstrip('/')}/sandboxes/{sandbox_id}",
            headers=headers,
            params={"public_base_url": node.runtime_config.get("public_base_url") or node.base_url},
        )
        response.raise_for_status()
        return _worker_sandbox_from_payload(response.json(), node.base_url, headers)


async def _find_worker_sandbox_by_id(sandbox_id: str) -> tuple[Optional[ExecutionNodeDocument], Optional[WorkerAgentSandbox]]:
    nodes = await ExecutionNodeDocument.find(
        ExecutionNodeDocument.enabled == True,  # noqa: E712
        ExecutionNodeDocument.type == ExecutionNodeType.WORKER_AGENT,
    ).to_list()
    for node in nodes:
        if not node.base_url:
            continue
        try:
            sandbox = await _get_worker_sandbox(node, sandbox_id)
            return node, sandbox
        except Exception as exc:
            logger.debug("Worker node %s does not have sandbox %s: %s", node.node_id, sandbox_id, exc)
    return None, None


async def _assign_worker_sandbox(sandbox: WorkerAgentSandbox, session: Session, task_id: Optional[str]) -> WorkerAgentSandbox:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{sandbox._worker_url}/sandboxes/{sandbox.id}/assign",
            json={"session_id": session.id, "task_id": task_id},
            headers=sandbox._worker_headers,
        )
        if response.status_code == 404:
            fallback = await client.get(f"{sandbox._worker_url}/sandboxes/{sandbox.id}", headers=sandbox._worker_headers)
            if fallback.status_code < 400:
                logger.warning(
                    "Worker sandbox %s does not support assign endpoint; using existing sandbox response",
                    sandbox.id,
                )
                return _worker_sandbox_from_payload(fallback.json(), sandbox._worker_url, sandbox._worker_headers)
        response.raise_for_status()
        return _worker_sandbox_from_payload(response.json(), sandbox._worker_url, sandbox._worker_headers)


async def _restore_allocation_for_sandbox(sandbox_id: str) -> Optional[SandboxAllocationDocument]:
    return await SandboxAllocationDocument.find_one(
        SandboxAllocationDocument.sandbox_id == sandbox_id,
        SandboxAllocationDocument.status != SandboxAllocationStatus.RELEASED,
    )


async def _restore_node_for_sandbox(sandbox_id: str) -> Optional[ExecutionNodeDocument]:
    allocation = await _restore_allocation_for_sandbox(sandbox_id)
    if allocation:
        node = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.node_id == allocation.node_id)
        if node:
            return node
    return None


def _worker_sandbox_from_allocation(
    allocation: SandboxAllocationDocument,
    worker_url: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> WorkerAgentSandbox:
    if not allocation.api_url or not allocation.vnc_url or not allocation.cdp_url:
        raise RuntimeError(f"Sandbox allocation {allocation.sandbox_id} is missing runtime URLs")
    return WorkerAgentSandbox(
        sandbox_id=allocation.sandbox_id,
        api_url=allocation.api_url,
        vnc_url=allocation.vnc_url,
        cdp_url=allocation.cdp_url,
        worker_url=worker_url or allocation.api_url,
        headers=headers,
    )


async def _upsert_allocation(
    *,
    sandbox: Sandbox,
    node_id: str,
    session: Optional[Session] = None,
    task_id: Optional[str] = None,
    status: SandboxAllocationStatus = SandboxAllocationStatus.ALLOCATED,
) -> None:
    try:
        doc = await SandboxAllocationDocument.find_one(
            SandboxAllocationDocument.sandbox_id == sandbox.id,
            SandboxAllocationDocument.status != SandboxAllocationStatus.RELEASED,
        )
        is_new = doc is None
        if not doc:
            doc = SandboxAllocationDocument(
                node_id=node_id,
                sandbox_id=sandbox.id,
                status=status,
                api_url=getattr(sandbox, "base_url", None),
                vnc_url=getattr(sandbox, "vnc_url", None),
                cdp_url=getattr(sandbox, "cdp_url", None),
            )
        doc.node_id = node_id
        doc.api_url = getattr(sandbox, "base_url", None)
        doc.vnc_url = getattr(sandbox, "vnc_url", None)
        doc.cdp_url = getattr(sandbox, "cdp_url", None)
        if session:
            doc.session_id = session.id
            doc.user_id = session.user_id
        if task_id is not None:
            doc.task_id = task_id
        doc.status = status
        doc.updated_at = datetime.now(UTC)
        if is_new:
            await doc.insert()
        else:
            await doc.save()
    except Exception as exc:
        logger.warning("Failed to upsert sandbox allocation for %s: %s", sandbox.id, exc)


class LocalDockerRuntime:
    async def allocate(self, session: Optional[Session] = None, dataset_ids: Sequence[str] | None = None) -> Sandbox:
        required_node_ids = None
        dataset_service = None
        if dataset_ids:
            from app.application.services.data_center_dataset_service import DataCenterDatasetService

            dataset_service = DataCenterDatasetService()
            required_node_ids = await dataset_service.candidate_node_ids(
                dataset_ids,
                user_id=session.user_id if session else None,
            )
        last_error: Optional[Exception] = None
        candidates = (
            await _list_execution_node_candidates(required_node_ids)
            if required_node_ids is not None
            else await _list_execution_node_candidates()
        )
        for node in candidates:
            try:
                mounts = (
                    await dataset_service.resolve_mounts(
                        dataset_ids,
                        node.node_id,
                        user_id=session.user_id if session else None,
                    )
                    if dataset_service
                    else []
                )
                return await self._allocate_on_node(node, session, mounts)
            except Exception as exc:
                last_error = exc
                logger.warning("Sandbox allocation failed on node %s: %s", node.node_id, exc)
                # A missing/rejected user-provided dataset path does not mean
                # the execution node itself is unhealthy. Marking it unhealthy
                # here would let one bad submission disrupt other users.
                if dataset_service is None:
                    await mark_execution_node_unhealthy(node.node_id, f"Sandbox allocation failed: {exc}")
        raise RuntimeError(f"Failed to allocate sandbox on all execution nodes: {last_error}")

    async def _allocate_on_node(
        self,
        node: ExecutionNodeDocument,
        session: Optional[Session] = None,
        mounts: Sequence[DatasetMount] | None = None,
    ) -> Sandbox:
        docker_host = _node_docker_host(node)
        if node.type == ExecutionNodeType.WORKER_AGENT:
            sandbox = (
                await _create_worker_sandbox(node, session, mounts)
                if mounts
                else await _create_worker_sandbox(node, session)
            )
        else:
            if docker_host:
                sandbox = (
                    await DockerSandbox.create_on_host(docker_host, node.runtime_config.get("network"), list(mounts))
                    if mounts
                    else await DockerSandbox.create_on_host(docker_host, node.runtime_config.get("network"))
                )
            else:
                sandbox = await DockerSandbox.create(list(mounts)) if mounts else await DockerSandbox.create()
        if session and node.type != ExecutionNodeType.WORKER_AGENT:
            await DockerSandbox.create_record(
                sandbox.id,
                sandbox.ip,
                status="assigned",
                session_id=session.id,
            )
        if session:
            await _upsert_allocation(sandbox=sandbox, node_id=node.node_id, session=session)
        return NodeBoundSandbox(sandbox, node)

    async def restore(self, sandbox_id: str) -> Sandbox:
        allocation = await _restore_allocation_for_sandbox(sandbox_id)
        node = None
        if allocation:
            node = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.node_id == allocation.node_id)
        if node and node.type == ExecutionNodeType.WORKER_AGENT:
            return await _get_worker_sandbox(node, sandbox_id)
        if node and node.type == ExecutionNodeType.REMOTE_DOCKER and node.base_url:
            return await DockerSandbox.get_on_host(sandbox_id, node.base_url)
        if allocation and allocation.node_id == LOCAL_DEFAULT_NODE_ID:
            return await DockerSandbox.get(sandbox_id)
        if allocation and allocation.api_url and allocation.vnc_url and allocation.cdp_url:
            headers = await _worker_headers(node) if node else {}
            logger.warning(
                "Restoring sandbox %s from allocation URLs because node %s is unavailable or unsupported",
                sandbox_id,
                allocation.node_id,
            )
            return _worker_sandbox_from_allocation(
                allocation,
                worker_url=node.base_url if node and node.base_url else allocation.api_url,
                headers=headers,
            )
        if allocation:
            raise RuntimeError(f"Sandbox {sandbox_id} allocation cannot be restored from node {allocation.node_id}")
        worker_node, worker_sandbox = await _find_worker_sandbox_by_id(sandbox_id)
        if worker_node and worker_sandbox:
            await _upsert_allocation(
                sandbox=worker_sandbox,
                node_id=worker_node.node_id,
                status=SandboxAllocationStatus.RUNNING,
            )
            return worker_sandbox
        return await DockerSandbox.get(sandbox_id)

    async def assign(self, sandbox: Sandbox, session: Session, task_id: Optional[str] = None) -> None:
        node = sandbox.node if isinstance(sandbox, NodeBoundSandbox) else None
        raw_sandbox = sandbox.sandbox if isinstance(sandbox, NodeBoundSandbox) else sandbox
        if isinstance(raw_sandbox, WorkerAgentSandbox):
            raw_sandbox = await _assign_worker_sandbox(raw_sandbox, session, task_id)
        else:
            await DockerSandbox.assign_to_session(raw_sandbox.id, session.id, task_id)
        if not node:
            node = await _restore_node_for_sandbox(raw_sandbox.id)
        await _upsert_allocation(
            sandbox=raw_sandbox,
            node_id=node.node_id if node else LOCAL_DEFAULT_NODE_ID,
            session=session,
            task_id=task_id,
            status=SandboxAllocationStatus.RUNNING if task_id else SandboxAllocationStatus.ALLOCATED,
        )


class SandboxClassRuntime:
    def __init__(self, sandbox_cls: Type[Sandbox]):
        self._sandbox_cls = sandbox_cls

    async def allocate(self, session: Optional[Session] = None, dataset_ids: Sequence[str] | None = None) -> Sandbox:
        if dataset_ids and self._sandbox_cls is not DockerSandbox:
            raise RuntimeError("The configured sandbox runtime does not support dataset mounts")
        mounts = []
        if dataset_ids:
            from app.application.services.data_center_dataset_service import DataCenterDatasetService

            service = DataCenterDatasetService()
            mounts = await service.resolve_mounts(
                dataset_ids,
                LOCAL_DEFAULT_NODE_ID,
                user_id=session.user_id if session else None,
            )
        sandbox = await self._sandbox_cls.create(mounts=mounts) if mounts else await self._sandbox_cls.create()
        if isinstance(sandbox, DockerSandbox) and session:
            await DockerSandbox.create_record(
                sandbox.id,
                sandbox.ip,
                status="assigned",
                session_id=session.id,
            )
            await _upsert_allocation(sandbox=sandbox, node_id=LOCAL_DEFAULT_NODE_ID, session=session)
        return sandbox

    async def restore(self, sandbox_id: str) -> Sandbox:
        return await self._sandbox_cls.get(sandbox_id)

    async def assign(self, sandbox: Sandbox, session: Session, task_id: Optional[str] = None) -> None:
        if isinstance(sandbox, DockerSandbox):
            await DockerSandbox.assign_to_session(sandbox.id, session.id, task_id)
            await _upsert_allocation(
                sandbox=sandbox,
                node_id=LOCAL_DEFAULT_NODE_ID,
                session=session,
                task_id=task_id,
                status=SandboxAllocationStatus.RUNNING if task_id else SandboxAllocationStatus.ALLOCATED,
            )


def get_default_sandbox_runtime(sandbox_cls: Type[Sandbox] = DockerSandbox):
    if sandbox_cls is DockerSandbox:
        return LocalDockerRuntime()
    return SandboxClassRuntime(sandbox_cls)
