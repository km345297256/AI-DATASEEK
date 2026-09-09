import asyncio
import json
import logging
import re
from types import SimpleNamespace

import pytest

from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.services.tools.interceptors import (
    AuditServiceToolTraceSink,
    StructuredToolTraceInterceptor,
    ToolConcurrencyInterceptor,
    ToolExecutionContractError,
    ToolExecutionDeniedError,
    ToolExecutionTimeoutError,
    ToolPolicyDefault,
    ToolPolicyGuardInterceptor,
    ToolPolicySnapshot,
    ToolTimeoutInterceptor,
    ToolTracePhase,
    create_production_tool_interceptors,
)
from app.domain.services.tools.pipeline import (
    ToolExecutionInterceptor,
    ToolExecutionPipeline,
)
from app.domain.services.tools.registry import ToolRegistry


class _CaptureSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _tool(
    name: str = "demo_tool",
    *,
    execution: dict | None = None,
    plugin: str = "demo-plugin",
    session_id: str = "session-default",
):
    definition = {
        "plugin": plugin,
        "parameters": {
            "type": "object",
            "properties": {
                "api_key": {},
                "query": {},
                "password": {},
            },
        },
    }
    if execution is not None:
        definition["execution"] = execution
    return SimpleNamespace(
        name=name,
        definition=definition,
        execution_contract=definition,
        toolkit=SimpleNamespace(name="plugin", session_id=session_id),
    )


@pytest.mark.asyncio
async def test_pipeline_complete_observes_transformed_value_and_error_hook_preserves_error(
    caplog,
):
    events = []

    class LifecycleInterceptor(ToolExecutionInterceptor):
        async def result(self, context, result):
            return f"{result}-transformed"

        async def complete(self, context, result):
            events.append(("complete", result))

        async def on_error(self, context, error):
            events.append(("error", error))
            raise RuntimeError("observer must not replace the primary error")

    pipeline = ToolExecutionPipeline([LifecycleInterceptor()])
    result = await pipeline.invoke(
        tool=_tool(),
        tool_call={"name": "demo_tool", "id": "ok", "args": {}},
        execute=lambda _context: asyncio.sleep(0, result="raw"),
    )
    assert result == "raw-transformed"
    assert events == [("complete", "raw-transformed")]

    primary = ValueError("private-primary-value")

    async def fail(_context):
        raise primary

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError) as raised:
            await pipeline.invoke(
                tool=_tool(),
                tool_call={"name": "demo_tool", "id": "failed", "args": {}},
                execute=fail,
            )
    assert raised.value is primary
    assert events[-1] == ("error", primary)
    assert "private-primary-value" not in caplog.text
    assert "observer must not replace" not in caplog.text


@pytest.mark.asyncio
async def test_policy_guard_defaults_to_deny_all_before_handler_runs():
    called = False

    async def execute(_context):
        nonlocal called
        called = True

    pipeline = ToolExecutionPipeline([ToolPolicyGuardInterceptor()])
    with pytest.raises(ToolExecutionDeniedError) as raised:
        await pipeline.invoke(
            tool=_tool(),
            tool_call={"name": "demo_tool", "id": "call-1", "args": {}},
            execute=execute,
        )

    assert called is False
    assert raised.value.reason == "tool_not_allowed"


@pytest.mark.asyncio
async def test_policy_binds_requested_name_to_actual_executable_identity():
    called = False

    async def execute(_context):
        nonlocal called
        called = True

    snapshot = ToolPolicySnapshot.for_registered_tools(["allowed_alias"])
    pipeline = ToolExecutionPipeline([ToolPolicyGuardInterceptor(snapshot)])
    with pytest.raises(ToolExecutionDeniedError) as raised:
        await pipeline.invoke(
            tool=_tool(name="different_executable"),
            tool_call={"name": "allowed_alias", "id": "mismatch", "args": {}},
            execute=execute,
        )

    assert called is False
    assert raised.value.reason == "tool_identity_mismatch"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_registered_tool_compatibility_policy_allows_unclassified_core_tools():
    snapshot = ToolPolicySnapshot.for_registered_tools(["core_tool"])
    pipeline = ToolExecutionPipeline([ToolPolicyGuardInterceptor(snapshot)])

    result = await pipeline.invoke(
        tool=SimpleNamespace(name="core_tool"),
        tool_call={"name": "core_tool", "id": "call-2", "args": {}},
        execute=lambda _context: asyncio.sleep(0, result="ok"),
    )

    assert result == "ok"


@pytest.mark.asyncio
async def test_policy_rejects_unknown_effects_and_ungranted_permissions():
    snapshot = ToolPolicySnapshot(
        version="policy-test",
        allowed_tools=frozenset({"demo_tool"}),
        allowed_effects=frozenset({"sandbox_read"}),
        granted_permissions=frozenset({"dataset.read"}),
    )

    for execution, reason in (
        (
            {
                "effects": ["future_unknown_effect"],
                "permissions": [],
            },
            "effect_not_allowed",
        ),
        (
            {
                "effects": ["sandbox_read"],
                "permissions": ["dataset.delete"],
            },
            "permission_not_granted",
        ),
    ):
        pipeline = ToolExecutionPipeline([ToolPolicyGuardInterceptor(snapshot)])
        with pytest.raises(ToolExecutionDeniedError) as raised:
            await pipeline.invoke(
                tool=_tool(execution=execution),
                tool_call={"name": "demo_tool", "id": "denied", "args": {}},
                execute=lambda _context: asyncio.sleep(0, result="must-not-run"),
            )
        assert raised.value.reason == reason


@pytest.mark.asyncio
async def test_policy_provider_failure_is_fail_closed_without_logging_secret(caplog):
    async def broken_provider(_context):
        raise RuntimeError("provider-secret-value")

    pipeline = ToolExecutionPipeline([ToolPolicyGuardInterceptor(broken_provider)])
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ToolExecutionDeniedError) as raised:
            await pipeline.invoke(
                tool=_tool(),
                tool_call={"name": "demo_tool", "id": "denied", "args": {}},
                execute=lambda _context: asyncio.sleep(0),
            )

    assert raised.value.reason == "policy_evaluation_failed"
    assert "RuntimeError" in caplog.text
    assert "provider-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_hanging_policy_provider_is_bounded_and_fails_closed():
    async def hanging_provider(_context):
        await asyncio.Event().wait()

    pipeline = ToolExecutionPipeline([
        ToolPolicyGuardInterceptor(
            hanging_provider,
            evaluation_timeout_seconds=0.01,
        ),
    ])
    with pytest.raises(ToolExecutionDeniedError) as raised:
        await pipeline.invoke(
            tool=_tool(),
            tool_call={"name": "demo_tool", "id": "denied", "args": {}},
            execute=lambda _context: asyncio.sleep(0),
        )

    assert raised.value.reason == "policy_evaluation_failed"


@pytest.mark.asyncio
async def test_timeout_notifies_cleanup_and_emits_value_free_terminal_trace():
    sink = _CaptureSink()
    cleanup_reasons = []
    pipeline = ToolExecutionPipeline([
        StructuredToolTraceInterceptor(sink, attributes={"session_id": "session-1"}),
        ToolTimeoutInterceptor(
            0.01,
            maximum_timeout_seconds=1,
            cancellation_cleanup_timeout_seconds=0.5,
        ),
    ])

    async def execute(context):
        context.register_cancellation_callback(cleanup_reasons.append)
        await asyncio.Event().wait()

    with pytest.raises(ToolExecutionTimeoutError) as raised:
        await pipeline.invoke(
            tool=_tool(execution={"timeout_seconds": 0.01, "cancellable": True}),
            tool_call={
                "name": "demo_tool",
                "id": "timeout-call",
                "args": {"api_key": "secret-value", "query": "private-query"},
            },
            execute=execute,
        )

    assert raised.value.timeout_seconds == 0.01
    assert raised.value.retryable is False
    assert cleanup_reasons == ["timeout"]
    assert [event.phase for event in sink.events] == [
        ToolTracePhase.STARTED,
        ToolTracePhase.TIMED_OUT,
    ]
    terminal = sink.events[-1]
    assert len(terminal.argument_keys) == 2
    assert all(
        re.fullmatch(r"arg:sha256:[0-9a-f]{12}", key)
        for key in terminal.argument_keys
    )
    assert terminal.error_code == "tool_execution_timeout"
    assert terminal.cancellable is True
    serialized = json.dumps([event.as_dict() for event in sink.events])
    assert "secret-value" not in serialized
    assert "private-query" not in serialized


@pytest.mark.asyncio
async def test_trace_records_only_declared_keys_and_counts_hostile_unknown_keys():
    sink = _CaptureSink()
    pipeline = ToolExecutionPipeline([StructuredToolTraceInterceptor(sink)])
    traced_tool = _tool()
    # This trace-only case deliberately declares an extensible argument map;
    # ordinary record schemas reject unknown keys before admission.
    traced_tool.definition["parameters"]["additionalProperties"] = True
    traced_tool.definition["parameters"]["properties"].update({
        "Authorization: Bearer key-in-key": {},
        "/Users/alice/private/key": {},
    })

    await pipeline.invoke(
        tool=traced_tool,
        tool_call={
            "name": "demo_tool",
            "id": "Bearer call-id-secret /Users/alice/call",
            "args": {
                "query": "safe value",
                "Authorization: Bearer key-in-key": "ignored",
                "/Users/alice/private/key": "ignored",
                "undeclared-secret-key": "ignored",
            },
        },
        execute=lambda _context: asyncio.sleep(0, result="ok"),
    )

    assert len(sink.events[0].argument_keys) == 4
    assert sink.events[0].argument_keys[-1] == "[undeclared-keys:1]"
    assert all(
        re.fullmatch(r"arg:sha256:[0-9a-f]{12}", key)
        for key in sink.events[0].argument_keys[:-1]
    )
    rendered = json.dumps([event.as_dict() for event in sink.events])
    assert "key-in-key" not in rendered
    assert "/Users/alice" not in rendered
    assert "call-id-secret" not in rendered


@pytest.mark.asyncio
async def test_trace_hashes_identity_attributes_and_untrusted_tool_labels():
    sink = _CaptureSink()
    raw_values = {
        "agent_id": "agent-private-value",
        "session_id": "session-private-value",
        "task_id": "task-private-value",
        "user_id": "user-private-value",
        "workspace_id": "workspace-private-value",
        "catalog_revision": "/Users/alice/private/catalog",
    }
    pipeline = ToolExecutionPipeline([
        StructuredToolTraceInterceptor(sink, attributes=raw_values),
    ])
    hostile_tool = _tool(name="/Users/alice/private/tool")
    hostile_tool.toolkit.name = "Bearer private-toolkit"

    await pipeline.invoke(
        tool=hostile_tool,
        tool_call={
            "name": "/Users/alice/private/tool",
            "id": "call-private-value",
            "args": {},
        },
        execute=lambda _context: asyncio.sleep(0, result="ok"),
    )

    event = sink.events[0]
    assert event.tool_name.startswith("tool:sha256:")
    assert event.toolkit.startswith("toolkit:sha256:")
    for key, namespace in (
        ("agent_id", "agent"),
        ("session_id", "session"),
        ("task_id", "task"),
        ("user_id", "user"),
        ("workspace_id", "workspace"),
    ):
        assert event.attributes[key].startswith(f"{namespace}:sha256:")
    assert event.attributes["catalog_revision"].startswith(
        "catalog-revision:sha256:"
    )
    rendered = json.dumps([item.as_dict() for item in sink.events])
    assert all(value not in rendered for value in raw_values.values())
    assert "/Users/alice" not in rendered
    assert "private-toolkit" not in rendered


@pytest.mark.asyncio
async def test_external_cancellation_remains_cancelled_error_and_runs_cleanup_once():
    sink = _CaptureSink()
    started = asyncio.Event()
    cleanup_reasons = []
    pipeline = ToolExecutionPipeline([
        StructuredToolTraceInterceptor(sink),
        ToolTimeoutInterceptor(5, maximum_timeout_seconds=5),
    ])

    async def execute(context):
        context.register_cancellation_callback(cleanup_reasons.append)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pipeline.invoke(
        tool=_tool(),
        tool_call={"name": "demo_tool", "id": "cancel-call", "args": {}},
        execute=execute,
    ))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_reasons == ["cancelled"]
    assert [event.phase for event in sink.events] == [
        ToolTracePhase.STARTED,
        ToolTracePhase.CANCELLED,
    ]


@pytest.mark.asyncio
async def test_hanging_best_effort_trace_sink_does_not_block_tool_completion(caplog):
    class HangingSink:
        async def emit(self, _event):
            await asyncio.Event().wait()

    pipeline = ToolExecutionPipeline([
        StructuredToolTraceInterceptor(
            HangingSink(),
            sink_timeout_seconds=0.01,
        ),
    ])
    with caplog.at_level(logging.WARNING):
        result = await pipeline.invoke(
            tool=_tool(),
            tool_call={"name": "demo_tool", "id": "trace-timeout", "args": {}},
            execute=lambda _context: asyncio.sleep(0, result="ok"),
        )

    assert result == "ok"
    assert "Tool trace sink failed" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_during_async_trace_sink_is_never_suppressed():
    entered = asyncio.Event()
    release = asyncio.Event()
    executed = False

    class BlockingSink:
        async def emit(self, _event):
            entered.set()
            await release.wait()

    async def execute(_context):
        nonlocal executed
        executed = True
        return "should-not-run"

    pipeline = ToolExecutionPipeline([
        StructuredToolTraceInterceptor(BlockingSink(), sink_timeout_seconds=10),
    ])
    task = asyncio.create_task(pipeline.invoke(
        tool=_tool(),
        tool_call={"name": "demo_tool", "id": "trace-cancel", "args": {}},
        execute=execute,
    ))
    await entered.wait()
    task.cancel()

    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executed is False
    assert asyncio.get_running_loop().time() - started < 0.3


@pytest.mark.asyncio
async def test_structured_trace_treats_explicit_tool_result_failure_as_failed():
    sink = _CaptureSink()
    pipeline = ToolExecutionPipeline([StructuredToolTraceInterceptor(sink)])
    failed_message = SimpleNamespace(
        artifact=SimpleNamespace(success=False, message="private failure detail"),
    )

    returned = await pipeline.invoke(
        tool=_tool(),
        tool_call={"name": "demo_tool", "id": "failed-result", "args": {}},
        execute=lambda _context: asyncio.sleep(0, result=failed_message),
    )

    assert returned is failed_message
    assert sink.events[-1].phase is ToolTracePhase.FAILED
    assert sink.events[-1].error_code == "tool_result_failed"
    assert "private failure detail" not in json.dumps(sink.events[-1].as_dict())


@pytest.mark.asyncio
async def test_tool_raised_timeout_error_is_not_misclassified_as_pipeline_deadline():
    original = TimeoutError("tool-owned timeout")

    async def execute(_context):
        raise original

    pipeline = ToolExecutionPipeline([
        ToolTimeoutInterceptor(1, maximum_timeout_seconds=1),
    ])
    with pytest.raises(TimeoutError) as raised:
        await pipeline.invoke(
            tool=_tool(),
            tool_call={"name": "demo_tool", "id": "inner-timeout", "args": {}},
            execute=execute,
        )

    assert raised.value is original
    assert not isinstance(raised.value, ToolExecutionTimeoutError)


@pytest.mark.asyncio
async def test_timeout_retryability_requires_explicit_read_only_effects():
    async def invoke(effects):
        pipeline = ToolExecutionPipeline([
            ToolTimeoutInterceptor(0.01, maximum_timeout_seconds=1),
        ])
        with pytest.raises(ToolExecutionTimeoutError) as raised:
            await pipeline.invoke(
                tool=_tool(execution={
                    "timeout_seconds": 0.01,
                    "effects": effects,
                }),
                tool_call={"name": "demo_tool", "id": "timeout", "args": {}},
                execute=lambda _context: asyncio.Event().wait(),
            )
        return raised.value.retryable

    assert await invoke(["sandbox_read"]) is True
    assert await invoke(["sandbox_read", "sandbox_write"]) is False


@pytest.mark.asyncio
async def test_exclusive_concurrency_serializes_same_plugin_tool_and_parallel_does_not():
    interceptor = ToolConcurrencyInterceptor()

    async def maximum_active(concurrency):
        active = 0
        maximum = 0
        gate = asyncio.Event()

        async def execute(_context):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            gate.set()
            await asyncio.sleep(0.02)
            active -= 1
            return "ok"

        pipeline = ToolExecutionPipeline([interceptor])
        tool = _tool(execution={"concurrency": concurrency})
        tasks = [
            asyncio.create_task(pipeline.invoke(
                tool=tool,
                tool_call={"name": "demo_tool", "id": f"call-{index}", "args": {}},
                execute=execute,
            ))
            for index in range(2)
        ]
        await gate.wait()
        await asyncio.gather(*tasks)
        return maximum

    assert await maximum_active("exclusive") == 1
    assert await maximum_active("parallel") == 2


@pytest.mark.asyncio
async def test_exclusive_concurrency_does_not_serialize_different_sessions():
    interceptor = ToolConcurrencyInterceptor()
    pipeline = ToolExecutionPipeline([interceptor])
    active = 0
    maximum = 0

    async def execute(_context):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    await asyncio.gather(*(
        pipeline.invoke(
            tool=_tool(
                execution={"concurrency": "exclusive"},
                session_id=session_id,
            ),
            tool_call={"name": "demo_tool", "id": session_id, "args": {}},
            execute=execute,
        )
        for session_id in ("session-a", "session-b")
    ))

    assert maximum == 2


@pytest.mark.asyncio
async def test_invalid_concurrency_contract_fails_before_execution():
    called = False

    async def execute(_context):
        nonlocal called
        called = True

    pipeline = ToolExecutionPipeline([ToolConcurrencyInterceptor()])
    with pytest.raises(ToolExecutionContractError):
        await pipeline.invoke(
            tool=_tool(execution={"concurrency": "global-lock"}),
            tool_call={"name": "demo_tool", "id": "invalid", "args": {}},
            execute=execute,
        )
    assert called is False


@pytest.mark.asyncio
async def test_audit_sink_records_only_terminal_sanitized_contract_metadata():
    class FakeAuditService:
        def __init__(self):
            self.records = []

        async def record(self, **kwargs):
            self.records.append(kwargs)

    audit_service = FakeAuditService()
    sink = AuditServiceToolTraceSink(
        audit_service,
        actor_user_id="user-1",
        session_id="session-1",
    )
    snapshot = ToolPolicySnapshot(
        version="session-policy-7",
        allowed_tools=frozenset({"demo_tool"}),
        allowed_effects=frozenset({"external_side_effect"}),
    )
    pipeline = ToolExecutionPipeline(create_production_tool_interceptors(
        policy_snapshot=snapshot,
        trace_sink=sink,
        default_timeout_seconds=1,
        maximum_timeout_seconds=1,
    ))

    await pipeline.invoke(
        tool=_tool(execution={
            "timeout_seconds": 1,
            "cancellable": False,
            "concurrency": "exclusive",
            "effects": ["external_side_effect"],
            "permissions": [],
        }),
        tool_call={
            "name": "demo_tool",
            "id": "audit-call",
            "args": {"password": "plain-password"},
        },
        execute=lambda _context: asyncio.sleep(0, result="secret-result"),
    )

    assert len(audit_service.records) == 1
    record = audit_service.records[0]
    assert record["status"] is AuditStatus.SUCCESS
    assert record["risk_level"] is AuditRiskLevel.HIGH
    assert len(record["metadata"]["argument_keys"]) == 1
    assert re.fullmatch(
        r"arg:sha256:[0-9a-f]{12}",
        record["metadata"]["argument_keys"][0],
    )
    assert record["metadata"]["policy_version"] == "session-policy-7"
    assert "plain-password" not in repr(record)
    assert "secret-result" not in repr(record)


@pytest.mark.asyncio
async def test_registry_registers_production_bundle_as_one_reversible_unit():
    class Toolkit:
        def __init__(self):
            self.tool_execution_pipeline = ToolExecutionPipeline()

        def get_tools(self):
            return []

        def get_tool(self, _name):
            return None

    toolkit = Toolkit()
    registry = ToolRegistry([toolkit])
    snapshot = ToolPolicySnapshot.for_registered_tools(["demo_tool"])
    dispose = registry.register_interceptors(create_production_tool_interceptors(
        policy_snapshot=snapshot,
        trace_sink=_CaptureSink(),
        default_timeout_seconds=1,
        maximum_timeout_seconds=1,
    ))

    assert len(toolkit.tool_execution_pipeline.interceptors) == 4
    dispose()
    dispose()
    assert toolkit.tool_execution_pipeline.interceptors == ()


def test_policy_snapshot_rejects_ambiguous_or_permissive_invalid_state():
    with pytest.raises(ValueError, match="both explicitly allowed and denied"):
        ToolPolicySnapshot(
            allowed_tools=frozenset({"same"}),
            denied_tools=frozenset({"same"}),
        )
    with pytest.raises(ValueError, match="invalid default"):
        ToolPolicySnapshot(default_decision="anything")
    with pytest.raises(ValueError, match="collection of strings"):
        ToolPolicySnapshot(allowed_tools="looks-like-one-tool")
    assert ToolPolicySnapshot().default_decision is ToolPolicyDefault.DENY
