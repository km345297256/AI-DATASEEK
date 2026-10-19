"""Task-isolated JupyterLab lifecycle and code handoff service."""
from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import docker
import httpx
from docker.types import Mount

from app.core.config import get_settings
from app.infrastructure.models.documents import JupyterSessionDocument


class JupyterService:
    """Creates one private Jupyter container and work volume per Agent session."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _name(session_id: str) -> str:
        return f"{get_settings().jupyter_name_prefix}-{session_id}"

    @staticmethod
    def _volume(session_id: str) -> str:
        return f"{get_settings().jupyter_name_prefix}-work-{session_id}"

    @staticmethod
    def proxy_base_path(session_id: str) -> str:
        return f"/api/v1/sessions/{session_id}/jupyter-proxy/"

    @staticmethod
    def _visible_dataset_target(destination: str, index: int, total: int) -> str:
        """Expose a read-only Sandbox mount inside Jupyter's file browser."""
        display_name = PurePosixPath(destination).name
        if total == 1 and "." not in display_name:
            display_name = "current"
        elif total > 1:
            display_name = f"source-{index + 1}-{display_name}"
        return f"/home/jovyan/work/datasets/{display_name}"

    async def open_notebook(self, *, session_id: str, user_id: str, code: str, language: str, sandbox_id: str | None) -> dict[str, Any]:
        if language.lower() not in {"python", "py", "python3"}:
            raise ValueError("Only Python code can be opened in Jupyter")
        if not code.strip() or len(code) > 200_000:
            raise ValueError("Code must contain between 1 and 200000 characters")

        async with self._lock(session_id):
            document = await JupyterSessionDocument.find_one(
                JupyterSessionDocument.session_id == session_id,
                JupyterSessionDocument.user_id == user_id,
            )
            if document is None:
                document = await self._create(session_id, user_id, sandbox_id)
            elif not await self._ensure_running(document, sandbox_id=sandbox_id):
                await document.delete()
                document = await self._create(session_id, user_id, sandbox_id)
            await self._append_cell(document, code)
            document.last_used_at = datetime.now(UTC)
            await document.save()
            browser_url = f"http://{document.container_name}:8888{self.proxy_base_path(session_id)}lab/tree/DataSeek.ipynb"
            await self._wait_ready(browser_url)
            return {
                "notebook_path": "DataSeek.ipynb",
                "container_name": document.container_name,
                "browser_url": browser_url,
            }

    async def prewarm(self, *, session_id: str, user_id: str, sandbox_id: str | None) -> None:
        """Start the session-owned JupyterLab before the user opens a code cell.

        This is intentionally best-effort and does not append a notebook cell.
        A changed Sandbox ID means its read-only dataset mounts changed, so the
        previous runtime is discarded rather than leaking stale mounted data.
        """
        if not sandbox_id:
            return
        async with self._lock(session_id):
            document = await JupyterSessionDocument.find_one(
                JupyterSessionDocument.session_id == session_id,
                JupyterSessionDocument.user_id == user_id,
            )
            if document is None:
                document = await self._create(session_id, user_id, sandbox_id)
            elif not await self._ensure_running(document, sandbox_id=sandbox_id):
                # _create removes the stale container after validating its
                # ownership. Deleting only the record here avoids re-entering
                # this session's lifecycle lock.
                await document.delete()
                document = await self._create(session_id, user_id, sandbox_id)
            document.last_used_at = datetime.now(UTC)
            await document.save()
            await self._wait_ready(
                f"http://{document.container_name}:8888{self.proxy_base_path(session_id)}lab"
            )

    async def _ensure_running(self, document: JupyterSessionDocument, *, sandbox_id: str | None = None) -> bool:
        def ensure() -> bool:
            client = docker.from_env(timeout=60)
            try:
                try:
                    container = client.containers.get(document.container_name)
                except docker.errors.NotFound:
                    return False
                labels = container.labels or {}
                if labels.get("ai-dataseek.session_id") != document.session_id or labels.get("ai-dataseek.user_id") != document.user_id:
                    raise RuntimeError("Jupyter container identity mismatch")
                if labels.get("ai-dataseek.jupyter_proxy") != "v1":
                    return False
                if labels.get("ai-dataseek.jupyter_layout") != "visible-datasets-v1":
                    return False
                if sandbox_id and labels.get("ai-dataseek.sandbox_id") != sandbox_id:
                    return False
                container.reload()
                if container.status != "running":
                    container.start()
                    container.reload()
                return container.status == "running"
            finally:
                client.close()

        return await asyncio.to_thread(ensure)

    @staticmethod
    async def _wait_ready(url: str) -> None:
        async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
            for _ in range(20):
                try:
                    response = await client.get(url)
                    if response.status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise RuntimeError("JupyterLab did not become ready")

    async def _create(self, session_id: str, user_id: str, sandbox_id: str | None) -> JupyterSessionDocument:
        token = secrets.token_urlsafe(32)
        container_name = self._name(session_id)
        volume_name = self._volume(session_id)
        image = self._settings.jupyter_image
        network = self._settings.sandbox_network
        proxy_base_path = self.proxy_base_path(session_id)

        def create_container() -> None:
            client = docker.from_env(timeout=60)
            try:
                try:
                    container = client.containers.get(container_name)
                    container.reload()
                    labels = container.labels or {}
                    if labels.get("ai-dataseek.session_id") != session_id or labels.get("ai-dataseek.user_id") != user_id:
                        raise RuntimeError("Jupyter container identity mismatch")
                    # A database record is required to recover its token. A
                    # container without the matching record is stale and must
                    # never be adopted into another authorization context.
                    container.remove(force=True)
                except docker.errors.NotFound:
                    pass
                try:
                    client.volumes.create(name=volume_name, labels={"ai-dataseek.session_id": session_id, "ai-dataseek.kind": "jupyter-work"})
                except docker.errors.APIError:
                    volume = client.volumes.get(volume_name)
                    labels = volume.attrs.get("Labels") or {}
                    if labels.get("ai-dataseek.session_id") != session_id or labels.get("ai-dataseek.kind") != "jupyter-work":
                        raise RuntimeError("Jupyter work volume identity mismatch")
                mounts = [Mount(target="/home/jovyan/work", source=volume_name, type="volume")]
                dataset_mounts: list[tuple[str, str, str]] = []
                if sandbox_id:
                    sandbox = client.containers.get(sandbox_id)
                    for mount in sandbox.attrs.get("Mounts", []):
                        destination = str(mount.get("Destination", ""))
                        if not destination.startswith("/home/ubuntu/datasets/"):
                            continue
                        source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
                        if not source:
                            continue
                        mount_type = mount.get("Type", "bind")
                        dataset_mounts.append((destination, source, mount_type))
                        # Preserve the Sandbox logical path so Agent-generated
                        # code runs unchanged after being handed to Jupyter.
                        mounts.append(Mount(target=destination, source=source, type=mount_type, read_only=True))
                for index, (destination, source, mount_type) in enumerate(dataset_mounts):
                    mounts.append(Mount(
                        target=self._visible_dataset_target(destination, index, len(dataset_mounts)),
                        source=source,
                        type=mount_type,
                        read_only=True,
                    ))
                client.containers.run(
                    image=image,
                    entrypoint=["bash", "-lc"],
                    command=["chown -R 1000:1000 /home/jovyan/work"],
                    user="root",
                    remove=True,
                    mounts=[mounts[0]],
                )
                client.containers.run(
                    image=image,
                    name=container_name,
                    detach=True,
                    network=network,
                    environment={"JUPYTER_TOKEN": token},
                    command=[
                        f"--IdentityProvider.token={token}",
                        f"--ServerApp.base_url={proxy_base_path}",
                        "--ServerApp.trust_xheaders=True",
                    ],
                    mounts=mounts,
                    labels={
                        "ai-dataseek.session_id": session_id,
                        "ai-dataseek.user_id": user_id,
                        "ai-dataseek.kind": "jupyter",
                        "ai-dataseek.jupyter_proxy": "v1",
                        "ai-dataseek.jupyter_layout": "visible-datasets-v1",
                        "ai-dataseek.sandbox_id": sandbox_id or "",
                    },
                    mem_limit=self._settings.jupyter_memory_limit,
                    nano_cpus=self._settings.jupyter_nano_cpus,
                    pids_limit=self._settings.jupyter_pids_limit,
                    network_disabled=self._settings.jupyter_network_disabled,
                    cap_drop=["ALL"],
                )
            finally:
                client.close()

        await asyncio.to_thread(create_container)
        document = JupyterSessionDocument(session_id=session_id, user_id=user_id, container_name=container_name, token=token, work_volume=volume_name)
        await document.insert()
        return document

    async def _append_cell(self, document: JupyterSessionDocument, code: str) -> None:
        """Use nbformat inside the isolated container, never write a host path."""
        escaped = repr(code)
        program = (
            "import nbformat as n; from pathlib import Path; "
            "p=Path('/home/jovyan/work/DataSeek.ipynb'); "
            "nb=n.read(p, as_version=4) if p.exists() else n.v4.new_notebook(); "
            f"code={escaped}; "
            "\nif not nb.cells or nb.cells[-1].get('cell_type') != 'code' or nb.cells[-1].get('source') != code: "
            "\n    nb.cells.append(n.v4.new_code_cell(code)); "
            "\nn.write(nb,p)"
        )

        def append() -> None:
            client = docker.from_env(timeout=60)
            try:
                container = client.containers.get(document.container_name)
                container.reload()
                if container.status != "running":
                    raise RuntimeError("Jupyter container stopped before the notebook could be updated")
                try:
                    result = container.exec_run(["python", "-c", program], user="jovyan")
                except docker.errors.APIError as exc:
                    if getattr(exc, "status_code", None) == 409:
                        raise RuntimeError("Jupyter container stopped before the notebook could be updated") from exc
                    raise
                if result.exit_code != 0:
                    raise RuntimeError("Failed to write the Jupyter notebook")
            finally:
                client.close()

        await asyncio.to_thread(append)

    async def proxy_target(self, *, session_id: str, user_id: str) -> tuple[str, str]:
        """Return an authenticated internal origin for the task-owned runtime."""
        document = await JupyterSessionDocument.find_one(
            JupyterSessionDocument.session_id == session_id,
            JupyterSessionDocument.user_id == user_id,
        )
        if document is None or not await self._ensure_running(document):
            raise RuntimeError("Jupyter runtime is not available")
        document.last_used_at = datetime.now(UTC)
        await document.save()
        return f"http://{document.container_name}:8888", document.token

    async def delete(self, *, session_id: str, user_id: str) -> None:
        async with self._lock(session_id):
            document = await JupyterSessionDocument.find_one(
                JupyterSessionDocument.session_id == session_id,
                JupyterSessionDocument.user_id == user_id,
            )
            if document is None:
                return

            def remove() -> None:
                client = docker.from_env(timeout=60)
                try:
                    try:
                        container = client.containers.get(document.container_name)
                        labels = container.labels or {}
                        if labels.get("ai-dataseek.session_id") == session_id and labels.get("ai-dataseek.user_id") == user_id:
                            container.remove(force=True)
                    except docker.errors.NotFound:
                        pass
                    try:
                        volume = client.volumes.get(document.work_volume)
                        if (volume.attrs.get("Labels") or {}).get("ai-dataseek.session_id") == session_id:
                            volume.remove(force=True)
                    except docker.errors.NotFound:
                        pass
                finally:
                    client.close()

            await asyncio.to_thread(remove)
            await document.delete()

    async def reap_idle(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=self._settings.jupyter_ttl_minutes)
        documents = await JupyterSessionDocument.find(
            JupyterSessionDocument.last_used_at < cutoff,
        ).to_list()
        for document in documents:
            await self.delete(session_id=document.session_id, user_id=document.user_id)
        return len(documents)
