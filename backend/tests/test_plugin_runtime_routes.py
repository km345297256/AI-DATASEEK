import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginDescriptor,
    PluginRuntimeRPCError,
    PluginToolDefinition,
    ToolExecutionDescriptor,
    ToolPresentationDescriptor,
)
from app.interfaces.dependencies import get_plugin_runtime
from app.interfaces.api.plugin_runtime_routes import (
    get_runtime_snapshot,
    reload_runtime_snapshot,
    router as plugin_runtime_router,
)
from app.interfaces.api.routes import router


def _snapshot(
    *,
    revision: str = "revision-one",
    description: str = "Calculate a climate average",
    parameters: dict | None = None,
) -> PluginCatalogSnapshot:
    return PluginCatalogSnapshot(
        engine="cordis",
        version="4.0.2",
        revision=revision,
        manifest_digest="catalog-digest",
        execution_bundle_digest="execution-digest",
        plugin_count=1,
        tool_count=1,
        plugins=(
            PluginDescriptor(
                plugin="climate",
                version="1.0.0",
                manifest_digest="d" * 64,
                tool_count=1,
            ),
        ),
        tools=(
            PluginToolDefinition(
                contract_version=2,
                name="climate_average",
                description=description,
                parameters=parameters or {
                    "type": "object",
                    "$ref": "#/$defs/request",
                    "$defs": {
                        "request": {
                            "type": "object",
                            "properties": {
                                "api_key": {"default": "top-secret"},
                                "X-API-Key": {"default": "another-secret"},
                            },
                        },
                    },
                    "properties": {
                        "source": {
                            "type": "string",
                            "default": "file:/Users/alice/My Data/private.nc",
                        }
                    },
                },
                output_schema=None,
                execution=ToolExecutionDescriptor(),
                presentation=ToolPresentationDescriptor(),
                scopes=("dataset:read",),
                plugin="climate",
                version="1.0.0",
            ),
        ),
    )


class FakeRuntime:
    def __init__(self, snapshot: PluginCatalogSnapshot):
        self._snapshot = snapshot
        self.reload_result = snapshot
        self.reload_error: Exception | None = None
        self.snapshot_error: Exception | None = None
        self.reload_calls = 0
        self.healthy = True
        self._last_error: str | None = None

    @property
    def current_snapshot(self) -> PluginCatalogSnapshot:
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def snapshot(self) -> PluginCatalogSnapshot:
        if self.snapshot_error:
            raise self.snapshot_error
        return self._snapshot

    async def reload(self) -> PluginCatalogSnapshot:
        self.reload_calls += 1
        if self.reload_error:
            self._last_error = str(self.reload_error)
            raise self.reload_error
        self._snapshot = self.reload_result
        self._last_error = None
        return self._snapshot


def test_get_runtime_snapshot_is_public_and_groups_tools_by_plugin():
    response = asyncio.run(get_runtime_snapshot(FakeRuntime(_snapshot())))
    payload = response.model_dump(mode="json")["data"]
    serialized = json.dumps(payload)

    assert payload["engine"] == "cordis"
    assert payload["status"] == "healthy"
    assert payload["plugins"][0]["version"] == "1.0.0"
    assert payload["plugins"][0]["tools"][0]["name"] == "climate_average"
    assert payload["plugins"][0]["errors"] == []
    assert "parameters" not in payload["tools"][0]
    assert "$ref" not in serialized
    assert "top-secret" not in serialized
    assert "another-secret" not in serialized
    assert "/Users/alice" not in serialized


def test_public_tool_descriptions_fail_closed_for_path_and_credential_bypasses():
    unsafe_descriptions = (
        "file:/Users/alice/private/input.nc",
        "path:/srv/private/input.nc",
        "Open /Users/alice/My Data/input.nc",
        ":__cordis_public_url__/Users/alice/private/input.nc",
        "api_key=top-secret",
        "openaiApiKey=camel-secret",
        "dbPassword: database-secret",
        "apiSecret=provider-secret",
        "accessToken=provider-token",
        "X-API-Key: another-secret",
        "https://example.test/view?path=/Users/alice/private.nc",
        "https://user:password@example.test/",
        "https://example.test/view?api_key=url-secret",
    )

    for description in unsafe_descriptions:
        response = asyncio.run(
            get_runtime_snapshot(FakeRuntime(_snapshot(description=description)))
        )
        public_description = response.model_dump(mode="json")["data"]["tools"][0][
            "description"
        ]
        assert public_description == "[redacted sensitive metadata]"

    benign = "Documentation: https://example.test/docs?q=climate"
    response = asyncio.run(
        get_runtime_snapshot(FakeRuntime(_snapshot(description=benign)))
    )
    assert response.model_dump(mode="json")["data"]["tools"][0]["description"] == benign


def test_get_runtime_snapshot_reports_disabled_runtime_without_failing():
    response = asyncio.run(get_runtime_snapshot(None))
    payload = response.model_dump(mode="json")["data"]

    assert payload["status"] == "unavailable"
    assert payload["healthy"] is False
    assert payload["plugin_count"] == 0
    assert payload["tool_count"] == 0


def test_get_runtime_snapshot_keeps_last_complete_catalog_on_host_error():
    runtime = FakeRuntime(_snapshot())
    runtime.snapshot_error = PluginRuntimeRPCError(
        -32001,
        "Host failed beside /private/tmp/cordis.sock",
    )

    response = asyncio.run(get_runtime_snapshot(runtime))
    payload = response.model_dump(mode="json")["data"]

    assert payload["status"] == "error"
    assert payload["healthy"] is True
    assert payload["revision"] == "revision-one"
    assert "/private/tmp" not in payload["last_error"]
    assert payload["last_error"] == "[redacted sensitive metadata]"


def test_reload_returns_only_the_completed_candidate_generation():
    runtime = FakeRuntime(_snapshot())
    runtime.reload_result = _snapshot(revision="revision-two")

    response = asyncio.run(reload_runtime_snapshot(runtime))
    payload = response.model_dump(mode="json")["data"]

    assert runtime.reload_calls == 1
    assert payload["revision"] == "revision-two"
    assert runtime.current_snapshot.revision == "revision-two"


def test_failed_reload_keeps_previous_generation_and_redacts_error_paths():
    original = _snapshot()
    runtime = FakeRuntime(original)
    runtime.reload_error = PluginRuntimeRPCError(
        -32000,
        "Invalid manifest at file:/Users/alice/My Plugins/manifest.json; api_key=top-secret",
    )

    failed = asyncio.run(reload_runtime_snapshot(runtime)).model_dump(mode="json")["data"]

    assert failed["status"] == "error"
    assert failed["healthy"] is True
    assert failed["revision"] == "revision-one"
    assert failed["last_error"] == "[redacted sensitive metadata]"
    assert runtime.current_snapshot is original

    persisted = asyncio.run(get_runtime_snapshot(runtime)).model_dump(mode="json")["data"]
    assert persisted["status"] == "error"
    assert persisted["revision"] == "revision-one"
    assert persisted["last_error"] == "[redacted sensitive metadata]"

    runtime.reload_error = None
    runtime.reload_result = _snapshot(revision="revision-two")
    recovered = asyncio.run(reload_runtime_snapshot(runtime)).model_dump(mode="json")["data"]
    assert recovered["status"] == "healthy"
    assert recovered["revision"] == "revision-two"
    assert recovered["last_error"] is None


def test_concurrent_reload_responses_are_serialized_by_generation():
    class ConcurrentRuntime(FakeRuntime):
        def __init__(self):
            super().__init__(_snapshot())
            self.in_flight = 0
            self.max_in_flight = 0

        async def reload(self) -> PluginCatalogSnapshot:
            self.reload_calls += 1
            call_number = self.reload_calls
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.01)
                if call_number == 1:
                    self._snapshot = _snapshot(revision="revision-two")
                    self._last_error = None
                    return self._snapshot
                self._last_error = "second reload rejected"
                raise PluginRuntimeRPCError(-32000, self._last_error)
            finally:
                self.in_flight -= 1

    async def run():
        runtime = ConcurrentRuntime()
        return runtime, await asyncio.gather(
            reload_runtime_snapshot(runtime),
            reload_runtime_snapshot(runtime),
        )

    runtime, responses = asyncio.run(run())
    payloads = [response.model_dump(mode="json")["data"] for response in responses]

    assert runtime.max_in_flight == 1
    assert payloads[0]["revision"] == "revision-two"
    assert payloads[0]["status"] == "healthy"
    assert payloads[1]["revision"] == "revision-two"
    assert payloads[1]["status"] == "error"


def _reload_client(runtime: FakeRuntime) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_runtime_router)
    app.dependency_overrides[get_plugin_runtime] = lambda: runtime
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        server_host="http://localhost:7001"
    )
    return TestClient(app, base_url="http://localhost:7001")


def test_reload_requires_custom_action_header_and_trusted_same_origin():
    runtime = FakeRuntime(_snapshot())
    client = _reload_client(runtime)
    valid_headers = {
        "Origin": "http://localhost:7001",
        "X-AI-DataSeek-Action": "plugin-runtime-reload",
    }

    assert client.post("/plugins/runtime/reload", headers=valid_headers).status_code == 200
    assert runtime.reload_calls == 1

    rejected_headers = (
        {"Origin": "http://localhost:7001"},
        {**valid_headers, "X-AI-DataSeek-Action": "wrong-action"},
        {
            "Origin": "http://evil.example",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
        {"X-AI-DataSeek-Action": "plugin-runtime-reload"},
        {
            "Referer": "http://evil.example/plugins",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
        {
            "Origin": "http://localhost:7001",
            "Host": "evil.example",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
        {
            "Origin": "http://localhost:7001",
            "Host": "localhost:8000",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
    )
    for headers in rejected_headers:
        assert client.post("/plugins/runtime/reload", headers=headers).status_code == 403
    assert runtime.reload_calls == 1


def test_reload_accepts_a_trusted_referer_through_the_frontend_proxy_host():
    runtime = FakeRuntime(_snapshot())
    client = _reload_client(runtime)
    response = client.post(
        "/plugins/runtime/reload",
        headers={
            "Referer": "http://localhost:7001/plugins?tab=runtime",
            # nginx forwards `$host` without the public port.
            "Host": "localhost",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
    )

    assert response.status_code == 200
    assert runtime.reload_calls == 1


def test_reload_fails_closed_when_public_server_origin_is_not_configured():
    runtime = FakeRuntime(_snapshot())
    app = FastAPI()
    app.include_router(plugin_runtime_router)
    app.dependency_overrides[get_plugin_runtime] = lambda: runtime
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(server_host=None)
    client = TestClient(app, base_url="http://localhost:7001")

    response = client.post(
        "/plugins/runtime/reload",
        headers={
            "Origin": "http://localhost:7001",
            "X-AI-DataSeek-Action": "plugin-runtime-reload",
        },
    )

    assert response.status_code == 503
    assert runtime.reload_calls == 0


def test_plugin_runtime_routes_are_registered_without_replacing_legacy_plugin_apis():
    paths = {route.path for route in router.routes}

    assert "/plugins/runtime" in paths
    assert "/plugins/runtime/reload" in paths
    assert "/skills" in paths
    assert "/mcp/servers" in paths
    assert "/renderers" in paths
