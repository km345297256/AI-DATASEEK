from typing import Dict, Any, Optional, List, BinaryIO
import uuid
import httpx
import docker
import socket
import logging
import asyncio
import io
import re
from pathlib import Path, PurePosixPath
from datetime import datetime, UTC
from async_lru import alru_cache
from app.core.config import get_settings
from app.domain.models.tool_result import ToolResult
from app.domain.external.sandbox import Sandbox
from app.domain.models.dataset import DatasetMount, DatasetStorageType
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
from app.infrastructure.external.browser.browser_use_browser import BrowserUseBrowser
from app.domain.external.browser import Browser
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    canonical_host_source as _canonical_host_source,
    docker_host_source_and_candidates as _docker_host_source_and_candidates,
)

logger = logging.getLogger(__name__)
_DOCKER_CREATE_SEMAPHORE = asyncio.Semaphore(1)
_SAFE_DATASET_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SOURCE_COMPONENT = re.compile(r"^dsl_[0-9a-f]{16}$")


def _safe_dataset_component(value: str) -> bool:
    return bool(_SAFE_DATASET_COMPONENT.fullmatch(value)) and value not in {".", ".."}


def _safe_mount_filename(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 255
        and value not in {".", ".."}
        and PurePosixPath(value).name == value
        and "\\" not in value
        and not any(ord(character) < 32 for character in value)
    )


class DockerSandbox(Sandbox):
    def __init__(self, ip: str = None, container_name: str = None, docker_host: str = None):
        """Initialize Docker sandbox and API interaction client"""
        self.client = httpx.AsyncClient(timeout=600)
        self._container_name = container_name
        self._docker_host = docker_host
        self._set_ip(ip)

    def _set_ip(self, ip: str) -> None:
        self.ip = ip
        self.base_url = f"http://{self.ip}:8080"
        self._vnc_url = f"ws://{self.ip}:5901"
        self._cdp_url = f"http://{self.ip}:9222"

    @staticmethod
    def _tool_result_from_response(response, operation: str) -> ToolResult:
        """Normalize sandbox responses while older sandbox images are drained.

        Older images reported a successful HTTP operation as a successful tool
        even when a command returned non-zero or a replacement changed nothing.
        Keep the adapter defensive so agents receive truthful execution state
        during rolling upgrades as well as from current images.
        """
        result = ToolResult(**response.json())
        data = result.data if isinstance(result.data, dict) else {}

        if operation in {"shell_exec", "shell_wait"}:
            returncode = data.get("returncode")
            if data.get("status") == "completed" and returncode not in (None, 0):
                return ToolResult(
                    success=False,
                    message=f"Command failed with return code: {returncode}",
                    data=result.data,
                )

        if operation == "file_replace" and data.get("replaced_count") == 0:
            return ToolResult(
                success=False,
                message="Replacement made no changes: target text was not found",
                data=result.data,
            )

        return result
    
    @property
    def id(self) -> str:
        """Sandbox ID"""
        if not self._container_name:
            return "dev-sandbox"
        return self._container_name
    
    
    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def vnc_url(self) -> str:
        return self._vnc_url

    @staticmethod
    def _get_container_ip(container) -> str:
        """Get container IP address from network settings
        
        Args:
            container: Docker container instance
            
        Returns:
            Container IP address
        """
        # Get container network settings
        network_settings = container.attrs['NetworkSettings']

        # Use .get() to avoid KeyError on newer Docker versions (e.g. Debian 13)
        # where the top-level IPAddress field may be absent when the container
        # is attached to a user-defined network instead of the default bridge.
        ip_address = network_settings.get('IPAddress', '')

        # Fall back to per-network IP when the top-level field is empty
        if not ip_address:
            networks = network_settings.get('Networks', {})
            for network_config in networks.values():
                candidate = network_config.get('IPAddress', '')
                if candidate:
                    ip_address = candidate
                    break

        return ip_address

    @staticmethod
    def _create_task(
        docker_host: str = None,
        network: str = None,
        mounts: Optional[List[DatasetMount]] = None,
    ) -> 'DockerSandbox':
        """Create a new Docker sandbox (static method)
        
        Args:
            image: Docker image name
            name_prefix: Container name prefix
            
        Returns:
            DockerSandbox instance
        """
        # Use configured default values
        settings = get_settings()

        image = settings.sandbox_image
        name_prefix = settings.sandbox_name_prefix
        container_name = f"{name_prefix}-{str(uuid.uuid4())[:8]}"
        
        docker_client = None
        try:
            # Create Docker client. Keep the timeout explicit so create failures
            # are bounded and can be recovered by checking whether Docker created
            # the named container after the client timed out.
            docker_timeout = settings.sandbox_docker_create_timeout_seconds
            docker_client = (
                docker.DockerClient(base_url=docker_host, timeout=docker_timeout)
                if docker_host
                else docker.from_env(timeout=docker_timeout)
            )

            # Prepare container configuration
            container_config = {
                "image": image,
                "name": container_name,
                "detach": True,
                "remove": True,
                "environment": {
                    "SERVICE_TIMEOUT_MINUTES": settings.sandbox_ttl_minutes,
                    "CHROME_ARGS": settings.sandbox_chrome_args,
                    "HTTPS_PROXY": settings.sandbox_https_proxy,
                    "HTTP_PROXY": settings.sandbox_http_proxy,
                    "NO_PROXY": settings.sandbox_no_proxy
                }
            }
            docker_mounts = []
            seen_sources: set[str] = set()
            seen_targets: set[str] = set()
            for mount in mounts or []:
                if not mount.read_only:
                    raise RuntimeError("Dataset mounts must be read-only")
                if not _safe_dataset_component(mount.dataset_id):
                    raise RuntimeError("Invalid dataset mount identity")
                dataset_root = f"/home/ubuntu/datasets/{mount.dataset_id}"
                if mount.storage_type == DatasetStorageType.MANAGED_UPLOAD:
                    if mount.source != settings.dataset_managed_volume:
                        raise RuntimeError("Unapproved managed dataset volume")
                    if mount.target != dataset_root:
                        raise RuntimeError("Invalid managed dataset mount target")
                    volume = docker_client.volumes.get(mount.source)
                    volume_root = volume.attrs.get("Mountpoint")
                    if not volume_root:
                        raise RuntimeError(f"Docker volume {mount.source} has no host mountpoint")
                    target = dataset_root
                    source = str(Path(volume_root) / mount.dataset_id)
                    if target in seen_targets:
                        raise RuntimeError("Duplicate dataset mount target")
                    seen_targets.add(target)
                    docker_mounts.append(docker.types.Mount(
                        target=target,
                        source=source,
                        type="bind",
                        read_only=True,
                    ))
                else:
                    if mount.storage_type != DatasetStorageType.HOST_PATH:
                        raise RuntimeError("Unsupported dataset storage type")
                    if not _SAFE_SOURCE_COMPONENT.fullmatch(mount.source_id):
                        raise RuntimeError("Invalid dataset source identity")
                    if not _safe_mount_filename(mount.display_name):
                        raise RuntimeError("Invalid dataset display filename")
                    target = f"{dataset_root}/sources/{mount.source_id}/{mount.display_name}"
                    if mount.target != target:
                        raise RuntimeError("Invalid dataset mount target")
                    docker_source, candidate_roots = _docker_host_source_and_candidates(
                        mount.source,
                        settings.dataset_host_path_allowlist,
                        settings.dataset_docker_host_root,
                    )
                    source = _canonical_host_source(
                        docker_client,
                        image=image,
                        source=docker_source,
                        candidate_roots=candidate_roots,
                    )
                    if source in seen_sources:
                        raise RuntimeError("Duplicate dataset mount source")
                    if target in seen_targets:
                        raise RuntimeError("Duplicate dataset mount target")
                    seen_sources.add(source)
                    seen_targets.add(target)
                    docker_mounts.append(docker.types.Mount(
                        target=target,
                        source=source,
                        type="bind",
                        read_only=True,
                    ))
            if docker_mounts:
                container_config["mounts"] = docker_mounts
            
            # Add network to container config if configured
            sandbox_network = network if network is not None else settings.sandbox_network
            if sandbox_network:
                container_config["network"] = sandbox_network
            
            # Create container
            try:
                container = docker_client.containers.run(**container_config)
            except Exception:
                container = DockerSandbox._get_container_if_created(docker_client, container_name)
                if not container:
                    raise
                logger.warning(
                    "Docker create request for sandbox %s failed or timed out, but the container exists; adopting it",
                    container_name,
                )
            
            # Get container IP address
            container.reload()  # Refresh container info
            ip_address = DockerSandbox._get_container_ip(container)
            
            # Create and return DockerSandbox instance
            return DockerSandbox(
                ip=ip_address,
                container_name=container_name,
                docker_host=docker_host,
            )
            
        except Exception as e:
            logger.exception("Failed to create Docker sandbox")
            if mounts:
                raise RuntimeError("Failed to create Docker sandbox with the selected read-only dataset") from e
            raise Exception(f"Failed to create Docker sandbox: {str(e)}")
        finally:
            close = getattr(docker_client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _get_container_if_created(docker_client, container_name: str):
        try:
            return docker_client.containers.get(container_name)
        except Exception:
            return None

    async def ensure_sandbox(self) -> None:
        """Start the full legacy profile and wait for every sandbox service."""
        await self._ensure_supervisor_profile("vnc")
        await self._wait_for_supervisor_services(
            required_services={
                "app",
                "xvfb",
                "chrome",
                "socat",
                "x11vnc",
                "websockify",
            },
        )

    async def ensure_api_ready(self) -> None:
        """Wait only for the sandbox API used by dataset and shell tools.

        The supervisor endpoint is served by the API process itself, so a
        successful response is sufficient. GUI services are started lazily on
        first browser-tool or VNC access.
        """
        await self._wait_for_supervisor_services(required_services={"app"})

    async def ensure_browser_ready(self) -> None:
        """Start and wait for the services required by CDP browser tools."""
        await self._ensure_supervisor_profile("browser")
        await self._wait_for_supervisor_services(
            required_services={"app", "xvfb", "chrome", "socat"},
        )

    async def ensure_vnc_ready(self) -> None:
        """Start and wait for the browser-backed interactive VNC viewer."""
        await self._ensure_supervisor_profile("vnc")
        await self._wait_for_supervisor_services(
            required_services={
                "app",
                "xvfb",
                "chrome",
                "socat",
                "x11vnc",
                "websockify",
            },
        )

    async def _ensure_supervisor_profile(self, profile: str) -> None:
        """Start a fixed service profile, with rolling-upgrade compatibility."""
        response = await self.client.post(
            f"{self.base_url}/api/v1/supervisor/ensure",
            json={"profile": profile},
        )
        if response.status_code == 404:
            # Older sandbox images autostart all services and do not expose the
            # profile endpoint. The readiness poll below remains sufficient
            # while those containers are naturally drained.
            logger.info(
                "Sandbox %s uses legacy supervisor startup; waiting for %s services",
                self.id,
                profile,
            )
            return
        response.raise_for_status()
        result = ToolResult(**response.json())
        if not result.success:
            raise RuntimeError(result.message or f"Failed to start {profile} services")

    async def _wait_for_supervisor_services(
        self,
        *,
        required_services: Optional[set[str]],
    ) -> None:
        max_retries = 60  # Maximum number of retries
        retry_interval = 2  # Seconds between retries
        
        for attempt in range(max_retries):
            try:
                response = await self.client.get(f"{self.base_url}/api/v1/supervisor/status")
                response.raise_for_status()
                
                # Parse response as ToolResult
                tool_result = ToolResult(**response.json())
                
                if not tool_result.success:
                    logger.warning(f"Supervisor status check failed: {tool_result.message}")
                    await asyncio.sleep(retry_interval)
                    continue
                
                services = tool_result.data or []
                if not services:
                    logger.warning("No services found in supervisor status")
                    await asyncio.sleep(retry_interval)
                    continue
                
                states = {
                    str(service.get("name", "unknown")): str(service.get("statename", ""))
                    for service in services
                    if isinstance(service, dict)
                }
                names_to_check = set(states) if required_services is None else required_services
                non_running_services = [
                    f"{service_name}({states.get(service_name, 'MISSING')})"
                    for service_name in sorted(names_to_check)
                    if states.get(service_name) != "RUNNING"
                ]
                all_running = not non_running_services
                
                if all_running:
                    logger.info(
                        "Required sandbox services are RUNNING: %s",
                        ", ".join(sorted(names_to_check)),
                    )
                    return  # Success - all services are running
                else:
                    logger.info(f"Waiting for services to start... Non-running: {', '.join(non_running_services)} (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_interval)
                    
            except Exception as e:
                logger.warning(f"Failed to check supervisor status (attempt {attempt + 1}/{max_retries}): {str(e)}")
                await asyncio.sleep(retry_interval)
        
        # If we reach here, we've exhausted all retries
        error_message = f"Sandbox services failed to start after {max_retries} attempts ({max_retries * retry_interval} seconds)"
        logger.error(error_message)
        raise Exception(error_message)

    async def is_available(self, timeout: float = 3.0) -> bool:
        """Return whether the sandbox container still exists and its API is reachable."""
        docker_client = None
        try:
            if self._container_name:
                docker_client = docker.DockerClient(base_url=self._docker_host) if self._docker_host else docker.from_env()
                container = await asyncio.to_thread(docker_client.containers.get, self._container_name)
                await asyncio.to_thread(container.reload)
                if container.status == "paused":
                    logger.info("Sandbox %s is paused", self._container_name)
                    return False
                if container.status != "running":
                    logger.warning("Sandbox %s is not running: %s", self._container_name, container.status)
                    return False
                current_ip = self._get_container_ip(container)
                if current_ip and current_ip != self.ip:
                    logger.info("Sandbox %s IP changed from %s to %s", self._container_name, self.ip, current_ip)
                    self._set_ip(current_ip)

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self.base_url}/api/v1/supervisor/status")
                response.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Sandbox %s is not available at %s: %s", self.id, self.base_url, e)
            return False
        finally:
            close = getattr(docker_client, "close", None)
            if callable(close):
                close()

    async def is_paused(self) -> bool:
        if not self._container_name:
            return False
        try:
            docker_client = docker.DockerClient(base_url=self._docker_host) if self._docker_host else docker.from_env()
            try:
                container = await asyncio.to_thread(docker_client.containers.get, self._container_name)
                await asyncio.to_thread(container.reload)
                return container.status == "paused"
            finally:
                close = getattr(docker_client, "close", None)
                if callable(close):
                    close()
        except Exception as e:
            logger.warning("Failed to check sandbox %s pause state: %s", self.id, e)
            return False

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/exec",
            json={
                "id": session_id,
                "exec_dir": exec_dir,
                "command": command
            }
        )
        return self._tool_result_from_response(response, "shell_exec")

    async def view_shell(self, session_id: str, console: bool = False) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/view",
            json={
                "id": session_id,
                "console": console
            }
        )
        return ToolResult(**response.json())

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/wait",
            json={
                "id": session_id,
                "seconds": seconds
            }
        )
        return self._tool_result_from_response(response, "shell_wait")

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool = True) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/write",
            json={
                "id": session_id,
                "input": input_text,
                "press_enter": press_enter
            }
        )
        return ToolResult(**response.json())

    async def kill_process(self, session_id: str) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/kill",
            json={"id": session_id}
        )
        return ToolResult(**response.json())

    async def file_write(self, file: str, content: str, append: bool = False, 
                        leading_newline: bool = False, trailing_newline: bool = False, 
                        sudo: bool = False) -> ToolResult:
        """Write content to file
        
        Args:
            file: File path
            content: Content to write
            append: Whether to append content
            leading_newline: Whether to add newline before content
            trailing_newline: Whether to add newline after content
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of write operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/write",
            json={
                "file": file,
                "content": content,
                "append": append,
                "leading_newline": leading_newline,
                "trailing_newline": trailing_newline,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_read(self, file: str, start_line: int = None, 
                        end_line: int = None, sudo: bool = False) -> ToolResult:
        """Read file content
        
        Args:
            file: File path
            start_line: Start line number
            end_line: End line number
            sudo: Whether to use sudo privileges
            
        Returns:
            File content
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/read",
            json={
                "file": file,
                "start_line": start_line,
                "end_line": end_line,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())
        
    async def file_exists(self, path: str) -> ToolResult:
        """Check if file exists
        
        Args:
            path: File path
            
        Returns:
            Whether file exists
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/exists",
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_delete(self, path: str) -> ToolResult:
        """Delete file
        
        Args:
            path: File path
            
        Returns:
            Result of delete operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/delete",
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_list(self, path: str) -> ToolResult:
        """List directory contents
        
        Args:
            path: Directory path
            
        Returns:
            List of directory contents
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/list",
            json={"path": path}
        )
        return ToolResult(**response.json())

    async def file_replace(self, file: str, old_str: str, new_str: str, sudo: bool = False) -> ToolResult:
        """Replace string in file
        
        Args:
            file: File path
            old_str: String to replace
            new_str: String to replace with
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of replace operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/replace",
            json={
                "file": file,
                "old_str": old_str,
                "new_str": new_str,
                "sudo": sudo
            }
        )
        return self._tool_result_from_response(response, "file_replace")

    async def file_search(self, file: str, regex: str, sudo: bool = False) -> ToolResult:
        """Search in file content
        
        Args:
            file: File path
            regex: Regular expression
            sudo: Whether to use sudo privileges
            
        Returns:
            Search results
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/search",
            json={
                "file": file,
                "regex": regex,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_find(self, path: str, glob_pattern: str) -> ToolResult:
        """Find files by name pattern
        
        Args:
            path: Search directory path
            glob_pattern: Glob match pattern
            
        Returns:
            List of found files
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/find",
            json={
                "path": path,
                "glob": glob_pattern
            }
        )
        return ToolResult(**response.json())

    async def file_upload(self, file_data: BinaryIO, path: str, filename: str = None) -> ToolResult:
        """Upload file to sandbox
        
        Args:
            file_data: File content as binary stream
            path: Target file path in sandbox
            filename: Original filename (optional)
            
        Returns:
            Upload operation result
        """
        # Prepare form data for upload. Some storage streams expose seek()
        # but are not actually seekable, e.g. MinIO HTTP response streams.
        if isinstance(file_data, (bytes, bytearray)):
            file_data = io.BytesIO(file_data)
        elif hasattr(file_data, "seek"):
            try:
                file_data.seek(0)
            except (OSError, io.UnsupportedOperation):
                if hasattr(file_data, "read"):
                    file_data = io.BytesIO(file_data.read())
        files = {"file": (filename or "upload", file_data, "application/octet-stream")}
        data = {"path": path}
        
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/upload",
            files=files,
            data=data
        )
        return ToolResult(**response.json())

    async def file_download(self, path: str) -> BinaryIO:
        """Download file from sandbox
        
        Args:
            path: File path in sandbox
            
        Returns:
            File content as binary stream
        """
        response = await self.client.get(
            f"{self.base_url}/api/v1/file/download",
            params={"path": path}
        )
        response.raise_for_status()
        
        # Return the response content as a BinaryIO stream
        # TODO: change to real stream
        return io.BytesIO(response.content)
    
    @staticmethod
    @alru_cache(maxsize=128, typed=True)
    async def _resolve_hostname_to_ip(hostname: str) -> str:
        """Resolve hostname to IP address
        
        Args:
            hostname: Hostname to resolve
            
        Returns:
            Resolved IP address, or None if resolution fails
            
        Note:
            This method is cached using LRU cache with a maximum size of 128 entries.
            The cache helps reduce repeated DNS lookups for the same hostname.
        """
        try:
            # First check if hostname is already in IP address format
            try:
                socket.inet_pton(socket.AF_INET, hostname)
                # If successfully parsed, it's an IPv4 address format, return directly
                return hostname
            except OSError:
                # Not a valid IP address format, proceed with DNS resolution
                pass
                
            # Use socket.getaddrinfo for DNS resolution
            addr_info = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
            # Return the first IPv4 address found
            if addr_info and len(addr_info) > 0:
                return addr_info[0][4][0]  # Return sockaddr[0] from (family, type, proto, canonname, sockaddr), which is the IP address
            return None
        except Exception as e:
            # Log error and return None on failure
            logger.error(f"Failed to resolve hostname {hostname}: {str(e)}")
            return None
    
    async def destroy(self) -> bool:
        """Destroy Docker sandbox"""
        try:
            if self.client:
                await self.client.aclose()
            if self._container_name:
                docker_client = (
                    docker.DockerClient(base_url=self._docker_host)
                    if self._docker_host
                    else docker.from_env()
                )
                try:
                    docker_client.containers.get(self._container_name).remove(force=True)
                except docker.errors.NotFound:
                    pass  # container may have already exited with remove=True
                finally:
                    close = getattr(docker_client, "close", None)
                    if callable(close):
                        close()
                await DockerSandbox._update_record_destroyed(self._container_name)
                await DockerSandbox._update_allocation_status(self._container_name, "released")
                from app.infrastructure.external.sandbox.node_health import (
                    clear_session_sandbox_references,
                )

                await clear_session_sandbox_references({self._container_name})
            return True
        except Exception as e:
            logger.error(f"Failed to destroy Docker sandbox: {str(e)}")
            return False

    async def pause(self) -> bool:
        """Pause Docker sandbox to release CPU while preserving process/filesystem state."""
        if not self._container_name:
            return True
        try:
            if self.client:
                await self.client.aclose()
            docker_client = docker.DockerClient(base_url=self._docker_host) if self._docker_host else docker.from_env()
            try:
                container = await asyncio.to_thread(docker_client.containers.get, self._container_name)
                await asyncio.to_thread(container.reload)
                if container.status == "paused":
                    await DockerSandbox._update_record_paused(self._container_name)
                    await DockerSandbox._update_allocation_status(self._container_name, "paused")
                    return True
                if container.status != "running":
                    logger.warning("Sandbox %s cannot be paused from status %s", self._container_name, container.status)
                    return False
                await asyncio.to_thread(container.pause)
                await DockerSandbox._update_record_paused(self._container_name)
                await DockerSandbox._update_allocation_status(self._container_name, "paused")
                logger.info("Paused sandbox %s", self._container_name)
                return True
            finally:
                close = getattr(docker_client, "close", None)
                if callable(close):
                    close()
        except Exception as e:
            logger.error("Failed to pause Docker sandbox %s: %s", self.id, e)
            return False

    async def resume(self) -> bool:
        """Resume a paused Docker sandbox."""
        if not self._container_name:
            return True
        try:
            docker_client = docker.DockerClient(base_url=self._docker_host) if self._docker_host else docker.from_env()
            try:
                container = await asyncio.to_thread(docker_client.containers.get, self._container_name)
                await asyncio.to_thread(container.reload)
                if container.status == "paused":
                    await asyncio.to_thread(container.unpause)
                    logger.info("Resumed sandbox %s", self._container_name)
                elif container.status != "running":
                    logger.warning("Sandbox %s cannot be resumed from status %s", self._container_name, container.status)
                    return False
                await DockerSandbox._update_record_resumed(self._container_name)
                await DockerSandbox._update_allocation_status(self._container_name, "running")
                self.client = httpx.AsyncClient(timeout=600)
                return True
            finally:
                close = getattr(docker_client, "close", None)
                if callable(close):
                    close()
        except Exception as e:
            logger.error("Failed to resume Docker sandbox %s: %s", self.id, e)
            return False

    @staticmethod
    async def create_record(container_name: str, container_ip: str, status: str = "assigned",
                             session_id: Optional[str] = None, task_id: Optional[str] = None) -> None:
        """Write a sandbox lifecycle record to the database."""
        try:
            from app.infrastructure.models.documents import SandboxRecordDocument
            now = datetime.now(UTC)
            doc = SandboxRecordDocument(
                container_name=container_name,
                container_ip=container_ip,
                session_id=session_id,
                task_id=task_id,
                status=status,
                created_at=now,
                assigned_at=now if status == "assigned" else None,
                last_used_at=now,
            )
            await doc.insert()
        except Exception as e:
            logger.error(f"Failed to create sandbox record for {container_name}: {e}")

    @staticmethod
    async def assign_to_session(container_name: str, session_id: str, task_id: Optional[str]) -> None:
        """Update sandbox record with session/task association."""
        try:
            from app.infrastructure.models.documents import SandboxRecordDocument
            doc = await SandboxRecordDocument.find_one(
                SandboxRecordDocument.container_name == container_name
            )
            if doc:
                doc.session_id = session_id
                if task_id:
                    doc.task_id = task_id
                doc.status = "assigned"
                doc.assigned_at = datetime.now(UTC)
                doc.last_used_at = datetime.now(UTC)
                doc.paused_at = None
                await doc.save()
        except Exception as e:
            logger.error(f"Failed to assign sandbox {container_name} to session {session_id}: {e}")

    @staticmethod
    async def _update_record_destroyed(container_name: str) -> None:
        try:
            from app.infrastructure.models.documents import SandboxRecordDocument
            doc = await SandboxRecordDocument.find_one(
                SandboxRecordDocument.container_name == container_name
            )
            if doc:
                doc.status = "destroyed"
                doc.destroyed_at = datetime.now(UTC)
                await doc.save()
        except Exception as e:
            logger.error(f"Failed to update destroyed record for {container_name}: {e}")

    @staticmethod
    async def _update_record_paused(container_name: str) -> None:
        try:
            from app.infrastructure.models.documents import SandboxRecordDocument
            doc = await SandboxRecordDocument.find_one(
                SandboxRecordDocument.container_name == container_name
            )
            if doc:
                doc.status = "paused"
                now = datetime.now(UTC)
                doc.paused_at = now
                doc.last_used_at = now
                await doc.save()
        except Exception as e:
            logger.error(f"Failed to update paused record for {container_name}: {e}")

    @staticmethod
    async def _update_record_resumed(container_name: str) -> None:
        try:
            from app.infrastructure.models.documents import SandboxRecordDocument
            doc = await SandboxRecordDocument.find_one(
                SandboxRecordDocument.container_name == container_name
            )
            if doc:
                doc.status = "assigned" if doc.session_id else "warm"
                doc.last_used_at = datetime.now(UTC)
                doc.paused_at = None
                await doc.save()
        except Exception as e:
            logger.error(f"Failed to update resumed record for {container_name}: {e}")

    @staticmethod
    async def _update_allocation_status(container_name: str, status: str) -> None:
        try:
            from app.domain.models.execution_node import SandboxAllocationStatus
            from app.infrastructure.models.documents import SandboxAllocationDocument
            doc = await SandboxAllocationDocument.find_one(
                SandboxAllocationDocument.sandbox_id == container_name,
                SandboxAllocationDocument.status != SandboxAllocationStatus.RELEASED,
            )
            if doc:
                doc.status = SandboxAllocationStatus(status)
                doc.updated_at = datetime.now(UTC)
                await doc.save()
        except Exception as e:
            logger.error(f"Failed to update allocation status for {container_name}: {e}")
    
    async def get_browser(self) -> Browser:
        """Get browser instance

        Returns a browser implementation connected to the sandbox's Chrome via CDP.
        The concrete implementation is selected by the BROWSER_ENGINE setting:
          - "playwright"   → PlaywrightBrowser
          - "browser_use"  → BrowserUseBrowser  (default)
        """
        settings = get_settings()
        engine = (settings.browser_engine or "browser_use").lower().strip()
        if engine == "browser_use":
            logger.info("Using BrowserUseBrowser engine for CDP URL: %s", self.cdp_url)
            return BrowserUseBrowser(self.cdp_url)
        logger.info("Using PlaywrightBrowser engine for CDP URL: %s", self.cdp_url)
        return PlaywrightBrowser(self.cdp_url)

    @staticmethod
    @alru_cache(maxsize=128, typed=True)
    async def _resolve_hostname_to_ip(hostname: str) -> str:
        """Resolve hostname to IP address
        
        Args:
            hostname: Hostname to resolve
            
        Returns:
            Resolved IP address, or None if resolution fails
            
        Note:
            This method is cached using LRU cache with a maximum size of 128 entries.
            The cache helps reduce repeated DNS lookups for the same hostname.
        """
        try:
            # First check if hostname is already in IP address format
            try:
                socket.inet_pton(socket.AF_INET, hostname)
                # If successfully parsed, it's an IPv4 address format, return directly
                return hostname
            except OSError:
                # Not a valid IP address format, proceed with DNS resolution
                pass
                
            # Use socket.getaddrinfo for DNS resolution
            addr_info = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
            # Return the first IPv4 address found
            if addr_info and len(addr_info) > 0:
                return addr_info[0][4][0]  # Return sockaddr[0] from (family, type, proto, canonname, sockaddr), which is the IP address
            return None
        except Exception as e:
            # Log error and return None on failure
            logger.error(f"Failed to resolve hostname {hostname}: {str(e)}")
            return None

    @classmethod
    async def create(cls, mounts: Optional[List[DatasetMount]] = None) -> Sandbox:
        """Create a new sandbox instance
        
        Returns:
            New sandbox instance
        """
        settings = get_settings()

        if settings.sandbox_address and settings.sandbox_isolation == "shared":
            # Chrome CDP needs IP address
            ip = await cls._resolve_hostname_to_ip(settings.sandbox_address)
            return DockerSandbox(ip=ip)
    
        async with _DOCKER_CREATE_SEMAPHORE:
            return await asyncio.to_thread(DockerSandbox._create_task, None, None, mounts)

    @classmethod
    async def create_on_host(
        cls,
        docker_host: str,
        network: str = None,
        mounts: Optional[List[DatasetMount]] = None,
    ) -> Sandbox:
        async with _DOCKER_CREATE_SEMAPHORE:
            return await asyncio.to_thread(DockerSandbox._create_task, docker_host, network, mounts)
    
    @classmethod
    async def get(cls, id: str) -> Sandbox:
        """Get sandbox by ID
        
        Args:
            id: Sandbox ID
            
        Returns:
            Sandbox instance
        """
        settings = get_settings()
        if settings.sandbox_address and settings.sandbox_isolation == "shared":
            ip = await cls._resolve_hostname_to_ip(settings.sandbox_address)
            return DockerSandbox(ip=ip, container_name=id)

        docker_client = docker.from_env()
        try:
            container = docker_client.containers.get(id)
            container.reload()

            ip_address = cls._get_container_ip(container)
            logger.info(f"IP address: {ip_address}")
            return DockerSandbox(ip=ip_address, container_name=container.name)
        finally:
            docker_client.close()

    @classmethod
    async def get_on_host(cls, id: str, docker_host: str) -> Sandbox:
        docker_client = docker.DockerClient(base_url=docker_host)
        try:
            container = docker_client.containers.get(id)
            container.reload()

            ip_address = cls._get_container_ip(container)
            logger.info(f"Remote sandbox IP address: {ip_address}")
            return DockerSandbox(ip=ip_address, container_name=container.name, docker_host=docker_host)
        finally:
            docker_client.close()
