from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.external.plugin_runtime import (
    PluginRuntimeError,
    PluginRuntimeProtocolError,
    PluginRuntimeRPCError,
    PluginRuntimeUnavailableError,
)
from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.external.plugins import NodePluginRuntime


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


@pytest.fixture(autouse=True)
def _quiet_fake_host_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    # The fake intentionally emits more than a normal pipe buffer. Keep the
    # live pytest log readable while the supervisor still drains every byte.
    caplog.set_level(
        logging.CRITICAL,
        logger="app.infrastructure.external.plugins.node_runtime",
    )


def _fake_host(tmp_path: Path, *, mode: str = "normal") -> tuple[Path, Path]:
    host_path = tmp_path / "fake-plugin-host.mjs"
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    host_path.write_text(
        f"""
import readline from 'node:readline';

const mode = {mode!r};
const digest = 'a'.repeat(64);
const revision = 'b'.repeat(64);
const hostVersion = mode === 'unsupported-host-version' ? '5.0.0' : '4.0.2';
const description = process.env.DEEPSEEK_API_KEY
  ? 'secret-environment-leaked'
  : 'x'.repeat(96 * 1024);
const snapshot = {{
  engine: 'cordis',
  version: hostVersion,
  revision,
  manifest_digest: digest,
  execution_bundle_digest: digest,
  plugin_count: mode === 'string-count' ? '1' : 1,
  tool_count: mode === 'string-count' ? '1' : 1,
  plugins: [{{
    plugin: 'scientific',
    version: '1.0.0',
    manifest_digest: mode === 'bad-plugin-digest' ? 'not-a-digest' : digest,
    tool_count: mode === 'string-count' ? '1' : 1,
  }}],
  tools: [{{
    contract_version: mode === 'bad-contract-version' ? 1 : 2,
    name: mode === 'bad-tool-name' ? 'scientific.inspect' : 'scientific_inspect',
    description,
    parameters: mode === 'invalid-schema'
      ? {{ type: 'object', required: 'not-an-array' }}
      : mode === 'unresolved-ref'
        ? {{ type: 'object', properties: {{ value: {{ $ref: '#/definitions/missing' }} }} }}
        : mode === 'invalid-pattern'
          ? {{ type: 'object', properties: {{ value: {{ type: 'string', pattern: '[' }} }} }}
          : mode === 'nonportable-pattern'
            ? {{ type: 'object', properties: {{ value: {{ type: 'string', pattern: '\\\\p{{L}}+' }} }} }}
            : mode === 'absolute-self-ref'
              ? {{
                  $id: 'https://schemas.example.test/self.json',
                  type: 'object',
                  definitions: {{ value: {{ type: 'string' }} }},
                  properties: {{
                    value: {{
                      $ref: 'https://schemas.example.test/self.json#/definitions/value',
                    }},
                  }},
                }}
              : mode === 'noncanonical-array-ref'
                ? {{
                    type: 'object',
                    definitions: {{
                      choices: {{
                        anyOf: [{{ type: 'string' }}, {{ type: 'number' }}],
                      }},
                    }},
                    properties: {{
                      value: {{ $ref: '#/definitions/choices/anyOf/01' }},
                    }},
                  }}
              : {{ type: 'object', properties: {{}} }},
    output_schema: null,
    execution: {{
      timeout_seconds: mode === 'string-timeout' ? '120' : 120,
      cancellable: mode === 'string-cancellable' ? 'false' : false,
      concurrency: 'exclusive',
      effects: ['sandbox_read', 'sandbox_write'],
      permissions: [],
    }},
    presentation: {{ kind: 'auto' }},
    scopes: mode === 'duplicate-scope'
      ? ['dataset_fast_path', 'dataset_fast_path']
      : ['dataset_fast_path'],
    timeout_seconds: mode === 'string-timeout' ? '120' : 120,
    plugin: 'scientific',
    version: '1.0.0',
  }}],
}};
if (mode === 'missing-contract-fields') {{
  delete snapshot.tools[0].output_schema;
  delete snapshot.tools[0].execution;
  delete snapshot.tools[0].presentation;
}}

process.stderr.write(
  'diagnostic-Bearer stderr-secret /Users/alice/private-' + 'z'.repeat(128 * 1024) + '\\n'
);
const input = readline.createInterface({{ input: process.stdin, crlfDelay: Infinity }});
let reloadAttempts = 0;
const send = (id, payload) => process.stdout.write(JSON.stringify({{
  jsonrpc: '2.0', id, ...payload,
}}) + '\\n');

for await (const line of input) {{
  const request = JSON.parse(line);
  if (request.method === 'host.health') {{
    if (mode === 'timeout') continue;
    send(request.id, {{ result: {{
      status: 'ok',
      engine: 'cordis',
      version: hostVersion,
      revision,
      manifest_digest: digest,
      execution_bundle_digest: digest,
      plugin_count: 1,
      tool_count: 1,
    }} }});
  }} else if (request.method === 'catalog.snapshot') {{
    send(request.id, {{ result: snapshot }});
  }} else if (request.method === 'plugins.reload') {{
    reloadAttempts += 1;
    if (mode === 'reload-error' && reloadAttempts === 1) {{
      send(request.id, {{ error: {{ code: -32010, message: 'candidate rejected' }} }});
    }} else if (mode === 'slow-reload') {{
      await new Promise((resolve) => setTimeout(resolve, 250));
      send(request.id, {{ result: snapshot }});
    }} else if (mode === 'reload-crash') {{
      process.exit(9);
    }} else {{
      send(request.id, {{ result: snapshot }});
    }}
  }} else if (request.method === 'shutdown') {{
    send(request.id, {{ result: {{ status: 'shutting_down' }} }});
    input.close();
  }}
}}
""",
        encoding="utf-8",
    )
    return host_path, tools_dir


def _runtime(
    host_path: Path,
    tools_dir: Path,
    *,
    max_response_frame_bytes: int = 512 * 1024,
    request_timeout_seconds: float = 1.0,
) -> NodePluginRuntime:
    assert NODE is not None
    return NodePluginRuntime(
        host_path=host_path,
        tools_dir=tools_dir,
        node_executable=NODE,
        request_timeout_seconds=request_timeout_seconds,
        startup_timeout_seconds=max(1.0, request_timeout_seconds * 3),
        shutdown_timeout_seconds=1.0,
        max_response_frame_bytes=max_response_frame_bytes,
    )


@pytest.mark.asyncio
async def test_task_boundary_restarts_an_unhealthy_plugin_runtime():
    runtime = SimpleNamespace(healthy=False, start=AsyncMock())
    service = object.__new__(AgentDomainService)
    service._plugin_runtime = runtime

    await service._ensure_plugin_runtime_ready()

    runtime.start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_task_boundary_remains_fail_closed_when_restart_fails():
    runtime = SimpleNamespace(
        healthy=False,
        start=AsyncMock(side_effect=PluginRuntimeUnavailableError("offline")),
    )
    service = object.__new__(AgentDomainService)
    service._plugin_runtime = runtime

    await service._ensure_plugin_runtime_ready()

    runtime.start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_domain_shutdown_joins_bootstrap_before_task_registry_teardown():
    events: list[str] = []
    started = asyncio.Event()

    async def bootstrap():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("bootstrap-stopped")

    async def prewarm():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("prewarm-stopped")

    class TaskClass:
        @staticmethod
        async def destroy():
            events.append("task-registry-destroyed")

    bootstrap_task = asyncio.create_task(bootstrap())
    prewarm_task = asyncio.create_task(prewarm())
    await started.wait()
    await asyncio.sleep(0)

    service = object.__new__(AgentDomainService)
    service._chat_bootstrap_tasks = {bootstrap_task}
    service._jupyter_prewarm_tasks = {prewarm_task}
    service._task_cls = TaskClass

    await service.shutdown()

    assert events == [
        "bootstrap-stopped",
        "task-registry-destroyed",
        "prewarm-stopped",
    ]


@pytest.mark.asyncio
async def test_runtime_reads_large_catalog_and_scrubs_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    host_path, tools_dir = _fake_host(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-reach-child")
    runtime = _runtime(host_path, tools_dir)

    with caplog.at_level(
        logging.WARNING,
        logger="app.infrastructure.external.plugins.node_runtime",
    ):
        try:
            snapshot = await runtime.start()
            process = runtime._process

            assert runtime.healthy is True
            assert snapshot.tool_count == 1
            assert len(snapshot.tools[0].description) == 96 * 1024
            assert snapshot.tools[0].timeout_seconds == 120
            assert snapshot.tools[0].description != "secret-environment-leaked"
            assert await runtime.start() is snapshot
            assert runtime._process is process
            assert (await runtime.snapshot()).revision == snapshot.revision
        finally:
            await runtime.shutdown()
            await runtime.shutdown()

    assert runtime.healthy is False
    assert runtime.current_snapshot.tool_count == 0
    diagnostics = "\n".join(record.getMessage() for record in caplog.records)
    assert "diagnostic_bytes=" in diagnostics
    assert "stderr-secret" not in diagnostics
    assert "/Users/alice" not in diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "bad-contract-version",
        "missing-contract-fields",
        "bad-tool-name",
        "duplicate-scope",
        "unsupported-host-version",
        "bad-plugin-digest",
        "invalid-schema",
        "unresolved-ref",
        "invalid-pattern",
        "nonportable-pattern",
        "absolute-self-ref",
        "noncanonical-array-ref",
        "string-count",
        "string-timeout",
        "string-cancellable",
    ],
)
async def test_runtime_rejects_incomplete_or_incompatible_v2_contracts(
    tmp_path: Path,
    mode: str,
):
    host_path, tools_dir = _fake_host(tmp_path, mode=mode)
    runtime = _runtime(host_path, tools_dir)

    with pytest.raises(PluginRuntimeProtocolError):
        await runtime.start()

    assert runtime.healthy is False
    assert runtime.current_snapshot.tool_count == 0
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_reload_rpc_error_keeps_last_valid_generation_and_host(
    tmp_path: Path,
):
    host_path, tools_dir = _fake_host(tmp_path, mode="reload-error")
    runtime = _runtime(host_path, tools_dir)

    try:
        original = await runtime.start()
        process = runtime._process

        with pytest.raises(PluginRuntimeRPCError) as caught:
            await runtime.reload()

        assert caught.value.code == -32010
        assert runtime.healthy is True
        assert runtime.current_snapshot is original
        assert runtime._process is process
        assert runtime.last_error == "candidate rejected"
        assert (await runtime.snapshot()).revision == original.revision
        assert runtime.last_error == "candidate rejected"

        reloaded = await runtime.reload()
        assert reloaded.revision == original.revision
        assert runtime.healthy is True
        assert runtime._process is process
        assert runtime.last_error is None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_crashed_host_invalidates_the_published_catalog(tmp_path: Path):
    host_path, tools_dir = _fake_host(tmp_path, mode="reload-crash")
    runtime = _runtime(host_path, tools_dir)
    original = await runtime.start()
    assert original.tool_count == 1

    with pytest.raises(PluginRuntimeError):
        await runtime.reload()

    assert runtime.healthy is False
    assert runtime.current_snapshot.tool_count == 0

    recovered = await runtime.start()
    assert recovered.tool_count == 1
    assert runtime.healthy is True
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_reload_invalidates_unknown_node_generation(tmp_path: Path):
    host_path, tools_dir = _fake_host(tmp_path, mode="slow-reload")
    runtime = _runtime(host_path, tools_dir)
    await runtime.start()

    reload_task = asyncio.create_task(runtime.reload())
    await asyncio.sleep(0.05)
    reload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    assert runtime.healthy is False
    assert runtime.current_snapshot.tool_count == 0
    assert runtime._process is None

    recovered = await runtime.start()
    assert recovered.tool_count == 1
    assert runtime.healthy is True
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_response_frame_limit_is_enforced(tmp_path: Path):
    host_path, tools_dir = _fake_host(tmp_path)
    runtime = _runtime(
        host_path,
        tools_dir,
        max_response_frame_bytes=64 * 1024,
    )

    with pytest.raises(PluginRuntimeProtocolError, match="frame limit"):
        await runtime.start()

    assert runtime.healthy is False
    assert runtime.current_snapshot.tool_count == 0
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_timeout_cleans_up_child_process(tmp_path: Path):
    host_path, tools_dir = _fake_host(tmp_path, mode="timeout")
    runtime = _runtime(
        host_path,
        tools_dir,
        request_timeout_seconds=0.1,
    )

    with pytest.raises(PluginRuntimeError, match="timed out"):
        await runtime.start()

    await asyncio.sleep(0)
    assert runtime.healthy is False
    assert runtime._process is None
    await runtime.shutdown()
