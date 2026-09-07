from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginRuntimeError,
    PluginRuntimeProtocolError,
    PluginRuntimeRPCError,
    PluginRuntimeUnavailableError,
)


logger = logging.getLogger(__name__)

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_HOST_VERSION = "4.0.2"
_SAFE_ENVIRONMENT_KEYS = frozenset({
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERPROFILE",
    "WINDIR",
})


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[5]


def default_plugin_host_path() -> Path:
    """Locate the checked-in Node host without depending on the process cwd."""
    packaged = Path("/opt/ai-dataseek/plugin-host/dist/index.js")
    if packaged.is_file():
        return packaged
    return _repository_root() / "plugin-host" / "dist" / "index.js"


def default_tool_plugins_directory() -> Path:
    configured = os.getenv("TOOL_PLUGINS_DIR")
    if configured:
        return Path(configured)
    packaged = Path("/opt/ai-dataseek/tools")
    if packaged.is_dir():
        return packaged
    return _repository_root() / "tools"


def default_execution_contract_directory() -> Path:
    """Locate the source contract shared with the sandbox tool runner."""
    packaged = Path("/opt/ai-dataseek/sandbox-contract")
    if packaged.is_dir():
        return packaged
    return _repository_root() / "sandbox"


def _clean_host_environment() -> dict[str, str]:
    """Keep model, database and service credentials out of the Node child."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENVIRONMENT_KEYS
    }
    environment.update({
        "NODE_ENV": "production",
        "NO_COLOR": "1",
    })
    return environment


class NodePluginRuntime:
    """Supervise the Cordis Node host over newline-delimited JSON-RPC 2.0.

    The host is a catalog/control plane only. Domain tool execution continues to
    use the existing sandbox boundary. A failed host immediately invalidates the
    cached generation, so newly-created toolkits expose no plugin tools.
    """

    def __init__(
        self,
        *,
        host_path: str | Path,
        tools_dir: str | Path,
        execution_contract_dir: str | Path | None = None,
        node_executable: str = "node",
        request_timeout_seconds: float = 5.0,
        startup_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 3.0,
        max_response_frame_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.host_path = Path(host_path).expanduser().resolve()
        self.tools_dir = Path(tools_dir).expanduser().resolve()
        self.execution_contract_dir = (
            Path(execution_contract_dir).expanduser().resolve()
            if execution_contract_dir is not None
            else None
        )
        self.node_executable = node_executable
        self.request_timeout_seconds = max(0.05, float(request_timeout_seconds))
        self.startup_timeout_seconds = max(
            self.request_timeout_seconds,
            float(startup_timeout_seconds),
        )
        self.shutdown_timeout_seconds = max(0.05, float(shutdown_timeout_seconds))
        self.max_response_frame_bytes = max(64 * 1024, int(max_response_frame_bytes))

        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = 0
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._snapshot = PluginCatalogSnapshot.unavailable()
        self._healthy = False
        self._stopping = False
        self._last_error: str | None = None

    @property
    def healthy(self) -> bool:
        process = self._process
        return bool(
            self._healthy
            and process is not None
            and process.returncode is None
        )

    @property
    def current_snapshot(self) -> PluginCatalogSnapshot:
        # Assignment of the immutable Pydantic model is atomic. Never hand a
        # stale generation to a new toolkit after the host becomes unhealthy.
        if not self.healthy:
            return PluginCatalogSnapshot.unavailable()
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def start(self) -> PluginCatalogSnapshot:
        """Start and prime the host once; repeated calls are idempotent."""
        async with self._lifecycle_lock:
            if self.healthy:
                return self._snapshot

            await self._stop_locked(graceful=False)
            if not self.host_path.is_file():
                error = PluginRuntimeUnavailableError("Cordis plugin host is not installed")
                self._invalidate(error)
                raise error
            if not self.tools_dir.is_dir():
                error = PluginRuntimeUnavailableError("Tool plugin directory is unavailable")
                self._invalidate(error)
                raise error
            if (
                self.execution_contract_dir is not None
                and not self.execution_contract_dir.is_dir()
            ):
                error = PluginRuntimeUnavailableError(
                    "Sandbox execution contract is unavailable"
                )
                self._invalidate(error)
                raise error

            self._stopping = False
            try:
                arguments = [
                    self.node_executable,
                    str(self.host_path),
                    "--tools-dir",
                    str(self.tools_dir),
                ]
                if self.execution_contract_dir is not None:
                    arguments.extend([
                        "--execution-contract-dir",
                        str(self.execution_contract_dir),
                    ])
                self._process = await asyncio.create_subprocess_exec(
                    *arguments,
                    cwd=str(self.host_path.parent),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_clean_host_environment(),
                    limit=self.max_response_frame_bytes + 1,
                )
                self._stdout_task = asyncio.create_task(
                    self._read_stdout(),
                    name="cordis-plugin-host-stdout",
                )
                self._stderr_task = asyncio.create_task(
                    self._drain_stderr(),
                    name="cordis-plugin-host-stderr",
                )
                snapshot = await asyncio.wait_for(
                    self._bootstrap(),
                    timeout=self.startup_timeout_seconds,
                )
            except asyncio.CancelledError:
                self._invalidate(PluginRuntimeUnavailableError("Cordis plugin host startup cancelled"))
                await self._stop_locked(graceful=False)
                raise
            except Exception as exc:
                error = self._runtime_error("Cordis plugin host failed to start", exc)
                self._invalidate(error)
                await self._stop_locked(graceful=False)
                raise error from exc

            self._snapshot = snapshot
            self._healthy = True
            self._last_error = None
            logger.info(
                "Cordis plugin host started revision=%s plugins=%d tools=%d",
                snapshot.revision[:12],
                snapshot.plugin_count,
                snapshot.tool_count,
            )
            return snapshot

    async def reload(self) -> PluginCatalogSnapshot:
        """Atomically install one freshly validated host generation."""
        await self.start()
        async with self._lifecycle_lock:
            return await self._refresh_locked("plugins.reload")

    async def snapshot(self) -> PluginCatalogSnapshot:
        """Refresh the cache from the live host and return that generation."""
        await self.start()
        async with self._lifecycle_lock:
            return await self._refresh_locked("catalog.snapshot")

    async def shutdown(self) -> None:
        """Stop the child and release all tasks; safe to call more than once."""
        async with self._lifecycle_lock:
            await self._stop_locked(graceful=True)

    async def _bootstrap(self) -> PluginCatalogSnapshot:
        health = await self._request("host.health", require_healthy=False)
        self._validate_health(health)
        payload = await self._request("catalog.snapshot", require_healthy=False)
        snapshot = self._parse_snapshot(payload)
        for field in (
            "version",
            "revision",
            "manifest_digest",
            "execution_bundle_digest",
            "plugin_count",
            "tool_count",
        ):
            if health[field] != getattr(snapshot, field):
                raise PluginRuntimeProtocolError(
                    "Cordis health and catalog generations do not match"
                )
        return snapshot

    async def _refresh_locked(self, method: str) -> PluginCatalogSnapshot:
        try:
            payload = await self._request(method)
            snapshot = self._parse_snapshot(payload)
        except asyncio.CancelledError:
            if method == "plugins.reload":
                # The Node side may finish and commit after its caller is
                # cancelled. Its generation is then unknowable here, so never
                # keep advertising the Python cache as healthy.
                error = PluginRuntimeUnavailableError(
                    "Cordis plugin reload was cancelled"
                )
                self._invalidate(error)
                await self._stop_locked(graceful=False)
            raise
        except PluginRuntimeRPCError as exc:
            # A rejected candidate (for example an invalid manifest during
            # reload) is an application-level response from a healthy host.
            # Keep serving the last fully validated generation and retain the
            # safe host error until a later reload successfully commits. A
            # routine catalog read must neither clear nor replace that state.
            if method == "plugins.reload":
                self._last_error = str(exc)
            raise
        except Exception as exc:
            error = self._runtime_error(f"Cordis plugin host {method} failed", exc)
            self._invalidate(error)
            await self._stop_locked(graceful=False)
            raise error from exc

        self._snapshot = snapshot
        self._healthy = True
        if method == "plugins.reload":
            self._last_error = None
        return snapshot

    async def _request(
        self,
        method: str,
        *,
        require_healthy: bool = True,
    ) -> Any:
        process = self._process
        if (
            process is None
            or process.returncode is not None
            or process.stdin is None
            or (require_healthy and not self._healthy)
        ):
            raise PluginRuntimeUnavailableError("Cordis plugin host is unavailable")

        loop = asyncio.get_running_loop()
        self._next_request_id += 1
        request_id = self._next_request_id
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {},
        }
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await asyncio.wait_for(
                    process.stdin.drain(),
                    timeout=self.request_timeout_seconds,
                )
            return await asyncio.wait_for(
                future,
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise PluginRuntimeUnavailableError(
                f"Cordis plugin host timed out during {method}"
            ) from exc
        except (BrokenPipeError, ConnectionError) as exc:
            raise PluginRuntimeUnavailableError("Cordis plugin host pipe closed") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: PluginRuntimeError | None = None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    # StreamReader.readline() translates LimitOverrunError to
                    # ValueError. Treat it as an oversized protocol frame, not
                    # as a generic child-process failure.
                    failure = PluginRuntimeProtocolError(
                        "Cordis plugin host response exceeded the frame limit"
                    )
                    break
                if not line:
                    break
                if len(line) > self.max_response_frame_bytes:
                    failure = PluginRuntimeProtocolError(
                        "Cordis plugin host response exceeded the frame limit"
                    )
                    break
                try:
                    response = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    failure = PluginRuntimeProtocolError(
                        "Cordis plugin host wrote an invalid JSON-RPC frame"
                    )
                    break
                if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                    failure = PluginRuntimeProtocolError(
                        "Cordis plugin host wrote an invalid JSON-RPC response"
                    )
                    break
                request_id = response.get("id")
                if isinstance(request_id, bool) or not isinstance(request_id, int):
                    failure = PluginRuntimeProtocolError(
                        "Cordis plugin host response has no integer request id"
                    )
                    break
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    logger.warning("Ignoring unmatched Cordis JSON-RPC response id=%s", request_id)
                    continue
                error_payload = response.get("error")
                if error_payload is not None:
                    if not isinstance(error_payload, dict):
                        future.set_exception(PluginRuntimeProtocolError(
                            "Cordis plugin host returned an invalid error response"
                        ))
                        continue
                    future.set_exception(PluginRuntimeRPCError(
                        error_payload.get("code"),
                        str(error_payload.get("message") or "Cordis plugin host request failed"),
                    ))
                elif "result" in response:
                    future.set_result(response["result"])
                else:
                    future.set_exception(PluginRuntimeProtocolError(
                        "Cordis plugin host response has neither result nor error"
                    ))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            failure = self._runtime_error("Cordis plugin host stdout reader failed", exc)
        finally:
            if not self._stopping:
                self._invalidate(
                    failure
                    or PluginRuntimeUnavailableError("Cordis plugin host exited unexpectedly")
                )

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                # Read bounded chunks rather than lines: stderr is diagnostic,
                # and an accidentally huge unbroken log line must never stop
                # the drain and deadlock the child.
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                # stderr belongs to a child process and is not a trusted data
                # channel. Even a future dependency could print host paths or
                # credentials, so production logs retain only volume metadata.
                logger.warning(
                    "Cordis plugin host emitted diagnostic_bytes=%d",
                    len(chunk),
                )
        except asyncio.CancelledError:
            return
        except Exception as error:
            logger.warning(
                "Cordis plugin host stderr drain failed error_type=%s",
                type(error).__name__,
            )

    async def _stop_locked(self, *, graceful: bool) -> None:
        process = self._process
        stdout_task = self._stdout_task
        stderr_task = self._stderr_task
        self._stopping = True

        if process is not None and process.returncode is None:
            if graceful and self._healthy:
                try:
                    await asyncio.wait_for(
                        self._request("shutdown"),
                        timeout=self.shutdown_timeout_seconds,
                    )
                except Exception:
                    logger.warning("Cordis plugin host did not acknowledge shutdown")
            if process.stdin is not None:
                process.stdin.close()
                wait_closed = getattr(process.stdin, "wait_closed", None)
                if callable(wait_closed):
                    try:
                        await asyncio.wait_for(
                            wait_closed(),
                            timeout=self.shutdown_timeout_seconds,
                        )
                    except (asyncio.TimeoutError, BrokenPipeError, ConnectionError):
                        pass
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=self.shutdown_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(
                            process.wait(),
                            timeout=self.shutdown_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        logger.error("Cordis plugin host did not exit after kill")

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in (stdout_task, stderr_task)
            if task is not None and task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._process = None
        self._stdout_task = None
        self._stderr_task = None
        self._healthy = False
        self._snapshot = PluginCatalogSnapshot.unavailable()
        self._stopping = False
        self._reject_pending(PluginRuntimeUnavailableError("Cordis plugin host stopped"))

    def _invalidate(self, error: PluginRuntimeError) -> None:
        self._healthy = False
        self._snapshot = PluginCatalogSnapshot.unavailable()
        self._last_error = str(error)
        self._reject_pending(error)

    def _reject_pending(self, error: PluginRuntimeError) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _runtime_error(prefix: str, exc: Exception) -> PluginRuntimeError:
        if isinstance(exc, PluginRuntimeError):
            return exc
        if isinstance(exc, asyncio.TimeoutError):
            return PluginRuntimeUnavailableError(f"{prefix}: request timed out")
        return PluginRuntimeUnavailableError(f"{prefix}: {type(exc).__name__}")

    @staticmethod
    def _validate_health(payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise PluginRuntimeProtocolError(
                "Cordis plugin host health response is invalid"
            )
        if payload.get("status") != "ok" or payload.get("engine") != "cordis":
            raise PluginRuntimeProtocolError(
                "Cordis plugin host health response is invalid"
            )
        if payload.get("version") != _SUPPORTED_HOST_VERSION:
            raise PluginRuntimeProtocolError(
                "Cordis plugin host health response is invalid"
            )
        for field in ("revision", "manifest_digest", "execution_bundle_digest"):
            value = payload.get(field)
            if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
                raise PluginRuntimeProtocolError(
                    "Cordis plugin host health response is invalid"
                )
        for field in ("plugin_count", "tool_count"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PluginRuntimeProtocolError(
                    "Cordis plugin host health response is invalid"
                )

    @staticmethod
    def _parse_snapshot(payload: Any) -> PluginCatalogSnapshot:
        if not isinstance(payload, Mapping):
            raise PluginRuntimeProtocolError("Cordis catalog snapshot must be an object")
        if payload.get("engine") != "cordis":
            raise PluginRuntimeProtocolError("Cordis catalog snapshot has an invalid engine")
        if payload.get("version") != _SUPPORTED_HOST_VERSION:
            raise PluginRuntimeProtocolError(
                "Cordis catalog snapshot has an unsupported version"
            )
        for field in ("revision", "manifest_digest", "execution_bundle_digest"):
            value = payload.get(field)
            if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
                raise PluginRuntimeProtocolError(
                    f"Cordis catalog snapshot has an invalid {field}"
                )
        try:
            return PluginCatalogSnapshot.model_validate(dict(payload))
        except (TypeError, ValueError, ValidationError) as exc:
            raise PluginRuntimeProtocolError(
                "Cordis catalog snapshot failed validation"
            ) from exc
