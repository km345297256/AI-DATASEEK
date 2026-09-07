"""Offline vertical coverage for the Phase 1 Cordis execution boundary."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import uuid

import pytest

from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.interceptors import ToolTracePhase
from app.domain.services.tools.plugin import PluginToolkit
from app.domain.services.tools.registry import ToolRegistry
from app.infrastructure.external.plugins import NodePluginRuntime
from app.infrastructure.external.plugins.node_runtime import default_plugin_host_path
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _LocalShellClient:
    """Exercise DockerSandbox's HTTP adapter against the real tool runner."""

    def __init__(self, *, tools_dir: Path, contract_dir: Path):
        self.tools_dir = tools_dir
        self.contract_dir = contract_dir
        self.requests: list[tuple[str, dict, float | None]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict,
        timeout: float | None = None,
    ) -> _Response:
        self.requests.append((url, json, timeout))
        if url.endswith("/api/v1/shell/release"):
            return _Response({
                "success": True,
                "message": "Shell session released",
                "data": {"status": "released", "returncode": 0},
            })
        if not url.endswith("/api/v1/shell/exec"):
            raise AssertionError(f"unexpected sandbox operation: {url}")

        command = shlex.split(json["command"])
        assert command[:3] == ["ai-dataseek-tool", "run", "vertical_echo"]
        runner = self.contract_dir / "scripts" / "tool_plugin_runner.py"
        environment = os.environ.copy()
        environment.update({
            "AI_DATASEEK_TOOLS_DIR": str(self.tools_dir),
            "AI_DATASEEK_EXECUTION_CONTRACT_DIR": str(self.contract_dir),
        })
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(runner),
            *command[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        output = (stdout + stderr).decode("utf-8", errors="replace")
        return _Response({
            "success": process.returncode == 0,
            "message": "Command completed",
            "data": {
                "status": "completed",
                "returncode": process.returncode,
                "output": output,
            },
        })


class _TraceSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _sandbox_contract_directory() -> Path:
    repository_contract = Path(__file__).resolve().parents[2] / "sandbox"
    if repository_contract.is_dir():
        return repository_contract
    return Path("/opt/ai-dataseek/sandbox-contract")


def _write_plugin(tools_dir: Path) -> None:
    plugin_dir = tools_dir / "vertical"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "handler.py").write_text(
        """\
import json
import sys


def build_command(name, arguments):
    assert name == "vertical_echo"
    payload = json.dumps(
        {"echo": arguments["value"], "length": len(arguments["value"])},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [sys.executable, "-c", f"print({payload!r})"]
""",
        encoding="utf-8",
    )
    (plugin_dir / "manifest.json").write_text(
        json.dumps({
            "plugin": "vertical",
            "version": "1.0.0",
            "handler": "handler.py",
            "tools": [{
                "contract_version": 2,
                "name": "vertical_echo",
                "description": "Echo one bounded offline integration fixture.",
                "parameters": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "maxLength": 64},
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "echo": {"type": "string"},
                        "length": {"type": "integer"},
                    },
                    "required": ["echo", "length"],
                    "additionalProperties": False,
                },
                "execution": {
                    "timeout_seconds": 10,
                    "cancellable": True,
                    "concurrency": "exclusive",
                    "effects": ["sandbox_read"],
                    "permissions": [],
                },
                "presentation": {
                    "kind": "generic",
                    "title": "Vertical result",
                },
                "scopes": [],
                "timeout_seconds": 10,
            }],
        }, separators=(",", ":")),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cordis_contract_runs_through_plan_act_pipeline_and_shell_adapter(
    tmp_path: Path,
):
    """One real descriptor generation reaches execution and release unchanged."""
    tools_dir = tmp_path / "tools"
    _write_plugin(tools_dir)
    contract_dir = _sandbox_contract_directory()
    assert contract_dir.is_dir()
    assert NODE is not None
    runtime = NodePluginRuntime(
        host_path=default_plugin_host_path(),
        tools_dir=tools_dir,
        execution_contract_dir=contract_dir,
        node_executable=NODE,
        request_timeout_seconds=2,
        startup_timeout_seconds=10,
        shutdown_timeout_seconds=2,
    )

    client = _LocalShellClient(tools_dir=tools_dir, contract_dir=contract_dir)
    sandbox = object.__new__(DockerSandbox)
    sandbox.base_url = "http://offline-sandbox:8080"
    sandbox.client = client
    trace_sink = _TraceSink()

    try:
        snapshot = await runtime.start()
        assert snapshot.engine == "cordis"
        assert snapshot.plugin_count == snapshot.tool_count == 1
        descriptor = snapshot.tools[0]
        assert descriptor.contract_version == 2
        assert descriptor.execution.cancellable is True
        assert descriptor.execution.effects == ("sandbox_read",)
        assert descriptor.presentation.kind == "generic"

        toolkit = PluginToolkit(
            sandbox,
            session_id="vertical-session",
            plugin_runtime=runtime,
        )
        flow = object.__new__(PlanActFlow)
        flow._agent_id = "vertical-agent"
        flow._session_id = "vertical-session"
        flow._user_id = "vertical-user"
        flow.plugin_toolkit = toolkit
        flow._non_plugin_toolkits = []
        flow._tool_registry = ToolRegistry([toolkit])
        flow._tool_execution_disposer = None
        flow.configure_tool_execution(trace_sink=trace_sink)

        tool = toolkit.get_tool("vertical_echo")
        assert tool is not None
        assert tool.presentation == {
            "kind": "generic",
            "title": "Vertical result",
            "description": None,
        }
        message = await tool.ainvoke({
            "name": "vertical_echo",
            "id": "vertical-call",
            "args": {"value": "offline-fixture"},
        })

        assert message.artifact.success is True
        assert message.artifact.data["result"] == {
            "echo": "offline-fixture",
            "length": 15,
        }
        assert "--catalog-manifest-digest" in client.requests[0][1]["command"]
        assert snapshot.manifest_digest in client.requests[0][1]["command"]
        assert "--execution-bundle-digest" in client.requests[0][1]["command"]
        assert snapshot.execution_bundle_digest in client.requests[0][1]["command"]

        assert [request[0].rsplit("/", 1)[-1] for request in client.requests] == [
            "exec",
            "release",
        ]
        execution_id = client.requests[0][1]["id"]
        assert str(uuid.UUID(execution_id)) == execution_id
        assert client.requests[1][1]["id"] == execution_id

        assert [event.phase for event in trace_sink.events] == [
            ToolTracePhase.STARTED,
            ToolTracePhase.SUCCEEDED,
        ]
        completed = trace_sink.events[-1]
        assert completed.policy_decision == "allow"
        assert completed.timeout_seconds == 10
        assert completed.cancellable is True
        assert completed.effects == ("sandbox_read",)
        assert completed.attributes["catalog_revision"] == snapshot.revision
    finally:
        await runtime.shutdown()
