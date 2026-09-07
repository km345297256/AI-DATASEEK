import asyncio
import base64
import json
import logging
import shlex
import uuid
from unittest.mock import AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginDescriptor,
    PluginToolDefinition,
    ToolExecutionDescriptor,
    ToolPresentationDescriptor,
)
from app.domain.services.tools.plugin import PluginToolkit, default_plugin_directory
from app.domain.services.tools.pipeline import (
    ToolExecutionInterceptor,
    ToolExecutionPipeline,
)
from app.domain.services.tools.interceptors import (
    ToolExecutionTimeoutError,
    ToolTimeoutInterceptor,
)
from app.interfaces.schemas.tool_presentation import (
    normalize_tool_presentation,
    populate_tool_presentation_data,
)


def _tool_names(toolkit: PluginToolkit) -> set[str]:
    return {
        schema["function"]["name"]
        for schema in toolkit.get_tools()
    }


def _contract_toolkit(
    tmp_path,
    sandbox,
    *,
    output_schema=None,
    parameters=None,
) -> PluginToolkit:
    directory = tmp_path / "contract"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({
        "plugin": "contract",
        "version": "1.0.0",
        "tools": [{
            "contract_version": 2,
            "name": "contract_read",
            "description": "Read one contract fixture",
            "parameters": parameters or {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "output_schema": output_schema,
            "execution": {
                "timeout_seconds": 30,
                "cancellable": True,
                "concurrency": "parallel",
                "effects": ["sandbox_read"],
                "permissions": [],
            },
            "presentation": {"kind": "generic", "title": "Contract result"},
            "scopes": [],
            "timeout_seconds": 30,
        }],
    }))
    return PluginToolkit(sandbox, session_id="contract-session", plugins_dir=tmp_path)


def _runtime_snapshot() -> PluginCatalogSnapshot:
    return PluginCatalogSnapshot(
        engine="cordis",
        version="4.0.2",
        revision="a" * 64,
        manifest_digest="b" * 64,
        execution_bundle_digest="d" * 64,
        plugin_count=1,
        tool_count=2,
        plugins=(
            PluginDescriptor(
                plugin="runtime-plugin",
                version="1.0.0",
                manifest_digest="c" * 64,
                tool_count=2,
            ),
        ),
        tools=(
            PluginToolDefinition(
                contract_version=2,
                name="runtime_first",
                description="First runtime tool",
                parameters={"type": "object", "properties": {}},
                output_schema=None,
                execution=ToolExecutionDescriptor(timeout_seconds=120),
                presentation=ToolPresentationDescriptor(),
                scopes=("dataset_fast_path",),
                timeout_seconds=120,
                plugin="runtime-plugin",
                version="1.0.0",
            ),
            PluginToolDefinition(
                contract_version=2,
                name="runtime_second",
                description="Second runtime tool",
                parameters={"type": "object", "properties": {}},
                output_schema=None,
                execution=ToolExecutionDescriptor(),
                presentation=ToolPresentationDescriptor(),
                plugin="runtime-plugin",
                version="1.0.0",
            ),
        ),
    )


class _FakeRuntime:
    def __init__(self):
        self.healthy = True
        self._snapshot = _runtime_snapshot()

    @property
    def current_snapshot(self) -> PluginCatalogSnapshot:
        if not self.healthy:
            return PluginCatalogSnapshot.unavailable()
        return self._snapshot

    def crash(self) -> None:
        self.healthy = False


def test_builtin_plugins_discover_scientific_and_geoscience_tools():
    assert default_plugin_directory().is_dir()
    toolkit = PluginToolkit(
        AsyncMock(),
        session_id="session-1",
    )

    names = _tool_names(toolkit)
    assert len(names) == 280
    assert "scientific_inspect" in names
    assert "scientific_netcdf_visualize" in names
    assert "geoscience_collection_inspect" in names
    assert "geoscience_zonal_statistics" in names
    assert "data_format_inspect" in names
    assert "cf_semantics_validate" in names
    assert "spatial_grid_diagnose" in names
    assert "raster_compatibility_validate" in names
    assert "eo_product_resolve" in names
    assert "artifact_scientific_validate" in names
    assert "workbook_inspect" in names
    assert "tabular_visualize" in names
    assert "document_inspect" in names
    assert "document_visual_validate" in names
    assert "pdf_ocr_text" in names
    assert "presentation_inspect" in names
    assert "hierarchical_array_extract" in names
    assert "geoscience_vector_visualize" in names
    assert "geodata_product_package" in names
    assert "netcdf_multi_file_concat" in names
    assert "raster_calculator" in names
    assert "pxp_inspect" in names
    assert "pxp_extract_wave" in names
    assert "pxp_peak_fit" in names
    assert "pxp_visualize" in names
    assert "pxp_extract_experiment_conditions" in names
    assert "pxp_multipeak_deconvolution" in names
    assert "pxp_reproducible_package" in names
    assert "space_fits_inspect" in names
    assert "space_tle_propagate" in names
    assert "sequence_inspect" in names
    assert "sequence_fastqc_report" in names
    assert "blast_hit_visualize" in names
    assert "sequence_quality_heatmap" in names
    assert names == toolkit.dataset_fast_path_tool_names
    inspect = next(
        item for item in toolkit.get_tools()
        if item["function"]["name"] == "scientific_inspect"
    )
    assert inspect["function"]["parameters"]["required"] == ["input_path"]
    assert "id" not in inspect["function"]["parameters"]["properties"]


def test_runtime_snapshot_is_atomic_ordered_and_never_scans_manifests(tmp_path):
    malformed_plugin = tmp_path / "broken"
    malformed_plugin.mkdir()
    (malformed_plugin / "manifest.json").write_text("not json")
    runtime = _FakeRuntime()

    existing = PluginToolkit(
        AsyncMock(),
        session_id="existing",
        plugins_dir=tmp_path,
        plugin_runtime=runtime,
    )
    assert [item["function"]["name"] for item in existing.get_tools()] == [
        "runtime_first",
        "runtime_second",
    ]
    assert existing.catalog_revision == "a" * 64
    assert existing.dataset_fast_path_tool_names == {"runtime_first"}

    runtime.crash()
    new_toolkit = PluginToolkit(
        AsyncMock(),
        session_id="new",
        plugins_dir=tmp_path,
        plugin_runtime=runtime,
    )
    # Existing tasks retain their immutable generation; new tasks fail closed.
    assert len(existing.get_tools()) == 2
    assert new_toolkit.get_tools() == []
    assert new_toolkit.dataset_fast_path_tool_names == set()


@pytest.mark.asyncio
async def test_plugin_tool_invocation_uses_registry_runner_and_session_id():
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={"status": "completed", "returncode": 0, "output": "{}"},
    )
    toolkit = PluginToolkit(
        sandbox,
        session_id="session-42",
    )

    message = await toolkit.get_tool("scientific_inspect").ainvoke({
        "id": "call-1",
        "args": {"input_path": "/home/ubuntu/datasets/example.nc"},
    })

    assert message.tool_call_id == "call-1"
    assert message.artifact.success is True
    session_id, exec_dir, command = sandbox.exec_command.await_args.args
    assert session_id != "session-42"
    assert str(uuid.UUID(session_id)) == session_id
    sandbox.release_shell.assert_awaited_once_with(session_id)
    assert exec_dir == "/home/ubuntu"
    assert command.startswith("ai-dataseek-tool run scientific_inspect --arguments-base64 ")
    encoded = command.rsplit(" ", 1)[-1]
    payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    assert payload == {"input_path": "/home/ubuntu/datasets/example.nc"}


@pytest.mark.asyncio
async def test_parallel_plugin_calls_use_isolated_sandbox_shell_sessions(tmp_path):
    class ConcurrentSandbox:
        def __init__(self):
            self.execution_sessions = []
            self.both_started = asyncio.Event()

        async def exec_command(self, session_id, _exec_dir, _command):
            self.execution_sessions.append(session_id)
            if len(self.execution_sessions) == 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), timeout=1)
            return ToolResult(
                success=True,
                data={"status": "completed", "returncode": 0, "output": "{}"},
            )

    sandbox = ConcurrentSandbox()
    toolkit = _contract_toolkit(tmp_path, sandbox)

    first, second = await asyncio.gather(
        toolkit.call_tool("contract_read", {"value": 1}),
        toolkit.call_tool("contract_read", {"value": 2}),
    )

    assert first.success is True
    assert second.success is True
    assert len(set(sandbox.execution_sessions)) == 2
    assert all(
        str(uuid.UUID(session_id)) == session_id
        for session_id in sandbox.execution_sessions
    )


@pytest.mark.asyncio
async def test_hanging_plugin_release_cannot_swallow_outer_timeout(tmp_path):
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={"status": "completed", "returncode": 0, "output": "{}"},
    )
    async def hang_release(_session_id):
        await asyncio.Event().wait()

    sandbox.release_shell.side_effect = hang_release
    toolkit = _contract_toolkit(tmp_path, sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        ToolTimeoutInterceptor(
            0.01,
            maximum_timeout_seconds=0.02,
            cancellation_cleanup_timeout_seconds=0.05,
        ),
    ])

    started = asyncio.get_running_loop().time()
    with pytest.raises(ToolExecutionTimeoutError):
        await toolkit.get_tool("contract_read").ainvoke({
            "id": "release-timeout",
            "name": "contract_read",
            "args": {"value": 1},
        })
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2
    sandbox.kill_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_v2_input_contract_rejects_before_sandbox_execution(tmp_path):
    sandbox = AsyncMock()
    toolkit = _contract_toolkit(tmp_path, sandbox)

    result = await toolkit.call_tool("contract_read", {"value": "not-an-integer"})

    assert result.success is False
    assert result.data == {
        "error": "tool_input_contract_violation",
        "path": ["properties", "[field]", "type"],
    }
    sandbox.exec_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_pattern_contract_rejects_oversized_regex_candidates_before_validation(
    tmp_path,
):
    sandbox = AsyncMock()
    toolkit = _contract_toolkit(
        tmp_path,
        sandbox,
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string", "pattern": "^[a-z]+$"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    result = await toolkit.call_tool("contract_read", {"value": "a" * 4097})

    assert result.success is False
    assert result.data == {
        "error": "tool_input_contract_violation",
        "path": ["pattern"],
    }
    sandbox.exec_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "arguments"),
    [
        (
            {
                "type": "object",
                "properties": {"value": {"$ref": "#/definitions/missing"}},
            },
            {"value": 1},
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": "["}},
            },
            {"value": "secret-must-not-be-logged"},
        ),
    ],
)
async def test_schema_evaluation_failures_are_non_retryable_contract_errors(
    tmp_path,
    schema,
    arguments,
    caplog,
):
    sandbox = AsyncMock()
    toolkit = _contract_toolkit(tmp_path, sandbox)
    # Simulate a stale/non-Cordis adapter mutating a descriptor after the
    # catalog boundary; invocation must still fail closed without a traceback
    # or rejected value reaching the model.
    toolkit._definitions["contract_read"]["parameters"] = schema
    toolkit._contract_validators.clear()

    with caplog.at_level(
        logging.WARNING,
        logger="app.domain.services.tools.plugin",
    ):
        result = await toolkit.call_tool("contract_read", arguments)

    assert result.success is False
    assert result.data == {
        "error": "tool_input_contract_invalid",
        "path": [],
    }
    sandbox.exec_command.assert_not_awaited()
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-must-not-be-logged" not in rendered_logs


@pytest.mark.asyncio
async def test_contract_violation_hides_extension_controlled_schema_path(
    tmp_path,
    caplog,
):
    sandbox = AsyncMock()
    toolkit = _contract_toolkit(tmp_path, sandbox)
    hostile_field = "Authorization: Bearer schema-secret /Users/alice/private"
    toolkit._definitions["contract_read"]["parameters"] = {
        "type": "object",
        "properties": {hostile_field: {"type": "integer"}},
        "required": [hostile_field],
        "additionalProperties": False,
    }
    toolkit._contract_validators.clear()

    with caplog.at_level(logging.INFO, logger="app.domain.services.tools.plugin"):
        result = await toolkit.call_tool(
            "contract_read",
            {hostile_field: "rejected-secret-value"},
        )

    assert result.data == {
        "error": "tool_input_contract_violation",
        "path": ["properties", "[field]", "type"],
    }
    serialized = result.model_dump_json() + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "schema-secret" not in serialized
    assert "/Users/alice" not in serialized
    assert "rejected-secret-value" not in serialized


@pytest.mark.asyncio
async def test_v2_valid_output_is_parsed_and_keeps_legacy_envelope(tmp_path):
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={
            "status": "completed",
            "returncode": 0,
            "output": '{"answer":42}',
            "command": "must-not-be-forwarded",
            "session_id": "must-not-be-forwarded",
        },
    )
    toolkit = _contract_toolkit(tmp_path, sandbox, output_schema={
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    })

    result = await toolkit.call_tool("contract_read", {"value": 1})

    assert result.success is True
    assert result.data == {
        "status": "completed",
        "returncode": 0,
        "output": '{"answer":42}',
        "result": {"answer": 42},
    }


@pytest.mark.asyncio
async def test_v2_invalid_output_is_quarantined_from_model_and_card(tmp_path):
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={
            "status": "completed",
            "returncode": 0,
            "output": '{"answer":"secret-invalid-value"}',
        },
    )
    toolkit = _contract_toolkit(tmp_path, sandbox, output_schema={
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    })

    result = await toolkit.call_tool("contract_read", {"value": 1})
    presentation = populate_tool_presentation_data(
        normalize_tool_presentation({"kind": "generic"}),
        result,
    )

    assert result.success is False
    assert result.data["status"] == "contract_rejected"
    assert result.data["contract_error"]["error"] == "tool_output_contract_violation"
    assert "output" not in result.data
    assert "result" not in result.data
    assert "secret-invalid-value" not in result.model_dump_json()
    assert presentation is None


@pytest.mark.asyncio
async def test_v2_oversized_output_is_rejected_without_echo(tmp_path):
    sandbox = AsyncMock()
    oversized = "private" * (2 * 1024 * 1024 // len("private") + 2)
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={"status": "completed", "returncode": 0, "output": oversized},
    )
    toolkit = _contract_toolkit(tmp_path, sandbox)

    result = await toolkit.call_tool("contract_read", {"value": 1})

    assert result.success is False
    assert result.data["status"] == "contract_rejected"
    assert result.data["contract_error"]["error"] == "tool_output_size_limit"
    assert "output" not in result.data
    assert "private" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_v2_transport_failure_never_echoes_encoded_arguments(tmp_path):
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=False,
        message="runner failed",
        data={
            "status": "failed",
            "command": "ai-dataseek-tool --arguments-base64 private-payload",
            "session_id": "private-session",
        },
    )
    toolkit = _contract_toolkit(tmp_path, sandbox)

    result = await toolkit.call_tool("contract_read", {"value": 1})

    assert result == ToolResult(
        success=False,
        message="Plugin tool contract_read transport failed",
        data={"status": "transport_error", "tool": "contract_read"},
    )
    assert "private-payload" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_runtime_tool_invocation_pins_the_sandbox_catalog_generation():
    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={"status": "completed", "returncode": 0, "output": "{}"},
    )
    toolkit = PluginToolkit(
        sandbox,
        session_id="runtime-session",
        plugin_runtime=_FakeRuntime(),
    )

    message = await toolkit.get_tool("runtime_first").ainvoke({
        "id": "runtime-call",
        "args": {"value": 1},
    })

    assert message.artifact.success is True
    command = sandbox.exec_command.await_args.args[2]
    tokens = shlex.split(command)
    assert tokens[:3] == ["ai-dataseek-tool", "run", "runtime_first"]
    digest_index = tokens.index("--catalog-manifest-digest")
    assert tokens[digest_index + 1] == "b" * 64
    bundle_index = tokens.index("--execution-bundle-digest")
    assert tokens[bundle_index + 1] == "d" * 64
    payload_index = tokens.index("--arguments-base64")
    payload = json.loads(
        base64.urlsafe_b64decode(tokens[payload_index + 1]).decode("utf-8")
    )
    assert payload == {"value": 1}


def test_runtime_plugin_names_cannot_shadow_dynamic_agent_tools():
    class DynamicToolkit:
        @staticmethod
        def get_tools():
            return [{
                "type": "function",
                "function": {"name": "runtime_first", "parameters": {}},
            }]

    toolkit = PluginToolkit(
        AsyncMock(),
        session_id="collision-session",
        plugin_runtime=_FakeRuntime(),
    )

    with pytest.raises(ValueError, match="runtime_first"):
        toolkit.assert_no_tool_name_collisions([DynamicToolkit()])


@pytest.mark.asyncio
async def test_plugin_tool_invocation_uses_compatible_execution_pipeline():
    observed = []

    class CaptureInterceptor(ToolExecutionInterceptor):
        async def pre_execute(self, context):
            observed.append((context.tool_name, context.tool_call_id, dict(context.arguments)))

    sandbox = AsyncMock()
    sandbox.exec_command.return_value = ToolResult(
        success=True,
        data={"status": "completed", "returncode": 0, "output": "{}"},
    )
    toolkit = PluginToolkit(
        sandbox,
        session_id="session-pipeline",
    )
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([CaptureInterceptor()])
    arguments = {"input_path": "/home/ubuntu/datasets/example.nc"}

    message = await toolkit.get_tool("scientific_inspect").ainvoke({
        "id": "call-pipeline",
        "args": arguments,
    })

    assert message.tool_call_id == "call-pipeline"
    assert message.artifact.success is True
    assert observed == [("scientific_inspect", "call-pipeline", arguments)]


def test_duplicate_plugin_tool_names_fail_startup(tmp_path):
    for plugin in ("one", "two"):
        directory = tmp_path / plugin
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "plugin": plugin,
            "tools": [{
                "name": "duplicate",
                "description": "Duplicate test tool",
                "parameters": {"type": "object", "properties": {}},
            }],
        }))

    with pytest.raises(ValueError, match="Duplicate plugin tool name"):
        PluginToolkit(AsyncMock(), session_id="session", plugins_dir=tmp_path)
