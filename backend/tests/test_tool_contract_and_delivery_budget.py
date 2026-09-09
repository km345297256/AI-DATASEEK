import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.models.event import MessageEvent
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.model_runtime import ModelBudgetStopped
from app.domain.services.tools.base import BaseToolkit, Tool
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.services.tools.tool_contract import (
    SafeToolRetryError, ToolContractError, resolved_tool_is_read_only, validate_tool_arguments,
)


class Nested(BaseModel):
    count: int = Field(ge=1)


class Args(BaseModel):
    self: Any
    name: str = Field(min_length=2)
    count: int = Field(default=2, ge=1, le=10)
    nested: Nested | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        return value.upper()

    @model_validator(mode="after")
    def check_combination(self):
        if self.name == "FORBIDDEN":
            raise ValueError("private validator detail /Users/private/key")
        return self


def wrapped_tool():
    calls = []

    async def invoke(self, name, count=2, nested=None):
        calls.append((name, count, nested))
        return ToolResult(success=True, data={"name": name, "count": count})

    structured = StructuredTool.from_function(coroutine=invoke, name="checked",
                                               description="Checked test tool", args_schema=Args)
    toolkit = BaseToolkit()
    tool = Tool(structured, toolkit=toolkit)
    return tool, calls


@pytest.mark.asyncio
async def test_tool_keeps_original_validators_defaults_and_input_ownership():
    tool, calls = wrapped_tool()
    original = {"id": "call", "args": {"name": "valid", "nested": {"count": 3}}}
    before = copy.deepcopy(original)
    result = await tool.ainvoke(original)
    assert original == before
    assert "self" not in tool.args_schema.model_fields
    assert result.status == "success"
    assert calls[0][:2] == ("VALID", 2)
    assert isinstance(calls[0][2], Nested)


@pytest.mark.asyncio
@pytest.mark.parametrize("args,field", [
    ({}, "name"), ({"name": "ok", "typo": 1}, "typo"),
    ({"name": "ok", "count": "3"}, "count"),
    ({"name": "ok", "count": True}, "count"),
    ({"name": "ok", "count": 0}, "count"),
    ({"name": "x"}, "name"),
    ({"name": "ok", "nested": {"count": 1, "typo": 2}}, "nested"),
])
async def test_invalid_wire_arguments_never_enter_job_or_tool(args, field):
    tool, calls = wrapped_tool()
    admitted = []

    class Job(ToolExecutionInterceptor):
        async def pre_execute(self, context):
            admitted.append(context)

    tool.toolkit.tool_execution_pipeline = ToolExecutionPipeline([Job()])
    with pytest.raises(ToolContractError) as caught:
        await tool.ainvoke({"id": "call", "args": args})
    assert field in json.dumps(caught.value.fields)
    assert not calls and not admitted


@pytest.mark.asyncio
async def test_validator_failure_is_value_free_and_runs_once():
    tool, calls = wrapped_tool()
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    result = await agent.invoke_tool(tool, {"id": "call", "args": {"name": "forbidden"}})
    assert not calls
    assert result.status == "error" and result.artifact.success is False
    assert "private" not in result.content and "forbidden" not in result.content.lower()
    assert "/Users" not in result.content
    assert agent.last_execution_outcome["side_effect_state"] == "not_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugin", "mcp"])
async def test_non_core_contract_is_validated_before_pipeline_admission(kind):
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tool = SimpleNamespace(name="external")
    if kind == "plugin":
        tool.definition = {"parameters": schema}
    else:
        tool.input_schema = schema
    execute = AsyncMock()
    with pytest.raises(ToolContractError):
        await ToolExecutionPipeline().invoke(tool=tool, tool_call={"id": "call", "args": {}}, execute=execute)
    execute.assert_not_awaited()


def test_explicit_dictionary_maps_and_schema_constraints_are_preserved():
    schema = {"type": "object", "properties": {"headers": {
        "type": "object", "additionalProperties": {"type": "string"},
    }}, "required": ["headers"]}
    tool = SimpleNamespace(args_schema=schema)
    assert validate_tool_arguments(tool, {"args": {"headers": {"custom": "value"}}})
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {"headers": {"custom": 4}}})


def test_external_schema_references_are_not_fetched():
    with pytest.raises(ToolContractError) as caught:
        validate_tool_arguments(SimpleNamespace(args_schema={"$ref": "https://private.invalid/schema"}), {"args": {}})
    assert caught.value.code == "tool_schema_unavailable"
    assert "private.invalid" not in str(caught.value)


@pytest.mark.parametrize("schema,args", [
    ({"type": "object", "allOf": [
        {"properties": {"name": {"type": "string"}}, "required": ["name"]},
        {"properties": {"count": {"type": "integer"}}, "required": ["count"]},
    ]}, {"name": "sample", "count": 3}),
    ({"$schema": "http://json-schema.org/draft-07/schema#", "definitions": {
        "record": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }, "allOf": [{"$ref": "#/definitions/record"}, {"properties": {"count": {"type": "integer"}}}]},
     {"name": "sample", "count": 3}),
    ({"type": "object", "oneOf": [
        {"properties": {"kind": {"const": "first"}, "value": {"type": "integer"}}, "required": ["kind", "value"]},
        {"properties": {"kind": {"const": "second"}, "label": {"type": "string"}}, "required": ["kind", "label"]},
    ]}, {"kind": "second", "label": "sample"}),
])
def test_composed_records_accept_all_declared_fields_but_reject_unknowns(schema, args):
    tool = SimpleNamespace(args_schema=schema)
    assert validate_tool_arguments(tool, {"args": args})["args"] == args
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {**args, "unknown_field": 1}})


def test_legacy_tuple_constraints_are_not_dropped_by_unknown_field_validation():
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {
        "point": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}], "additionalItems": False},
    }}
    tool = SimpleNamespace(args_schema=schema)
    assert validate_tool_arguments(tool, {"args": {"point": [3, "label"]}})
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {"point": [3, "label", "extra"]}})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("private"), TimeoutError("private"),
                                  TypeError("private"), PermissionError("private")])
async def test_unconfirmed_or_programming_failures_are_never_blindly_retried(error):
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    tool = SimpleNamespace(name="write", ainvoke=AsyncMock(side_effect=error))
    result = await agent.invoke_tool(tool, {"id": "call", "args": {}})
    assert tool.ainvoke.await_count == 1
    assert result.status == "error" and result.artifact.success is False
    assert "private" not in result.content
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is True


@pytest.mark.asyncio
async def test_explicit_safe_retry_contract_is_bounded():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 1
    agent.retry_interval = 0
    tool = SimpleNamespace(name="read", ainvoke=AsyncMock(side_effect=SafeToolRetryError()))
    result = await agent.invoke_tool(tool, {"id": "call", "args": {}})
    assert tool.ainvoke.await_count == 2
    assert result.status == "error" and result.artifact.success is False
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is False


@pytest.mark.asyncio
async def test_failure_result_status_is_fixed_before_pipeline_completion():
    seen = []

    class Completion(ToolExecutionInterceptor):
        async def complete(self, context, result):
            seen.append(result.status)

    result = await ToolExecutionPipeline([Completion()]).invoke(
        tool=SimpleNamespace(name="plugin"), tool_call={"id": "call", "args": {}},
        execute=AsyncMock(return_value=ToolMessage(tool_call_id="call", content="failure",
                                                 artifact=ToolResult(success=False))),
    )
    assert result.status == "error" and seen == ["error"]


@pytest.mark.asyncio
async def test_error_status_cannot_preserve_a_successful_artifact():
    result = await ToolExecutionPipeline().invoke(
        tool=SimpleNamespace(name="contradictory"), tool_call={"id": "call", "args": {}},
        execute=AsyncMock(return_value=ToolMessage(tool_call_id="call", content='{"success":true}',
                                                 status="error", artifact=ToolResult(success=True))),
    )
    assert result.status == "error" and result.artifact.success is False
    assert json.loads(result.content)["success"] is False


def looping_agent(limit):
    agent = object.__new__(BaseAgent)
    agent.max_iterations = limit
    loop = AIMessage(content="", tool_calls=[{"name": "unavailable", "args": {}, "id": "call"}])
    agent.ask = AsyncMock(return_value=loop)
    agent.ask_with_messages = AsyncMock(side_effect=[loop, AIMessage(content="partial")])
    agent.get_tool = lambda _: None
    return agent


@pytest.mark.asyncio
async def test_repeated_no_progress_closes_without_consumption_limit_or_repeated_model_rounds():
    agent = looping_agent(4)
    events = [event async for event in agent.execute("produce requested outputs")]
    assert agent.ask_with_messages.await_count == 2
    calls = agent.ask_with_messages.await_args_list
    assert "batch(es) remain" not in calls[0].args[0][-1].content
    assert "Safe progress cannot continue" in calls[-1].args[0][-1].content
    assert calls[-1].kwargs["allow_tools"] is False
    assert agent.last_execution_outcome["tool_batches_used"] == 2
    assert agent.last_execution_outcome["tool_batch_limit"] is None
    assert agent.last_execution_outcome["code"] == "analysis_no_progress_loop"
    assert agent.last_execution_outcome["reserved_batches"] == 0
    assert any(isinstance(event, MessageEvent) for event in events)


@pytest.mark.asyncio
async def test_small_budget_and_early_completion_do_not_claim_exhaustion():
    agent = looping_agent(1)
    agent.ask = AsyncMock(return_value=AIMessage(content="complete without tools"))
    _ = [event async for event in agent.execute("answer")]
    assert agent.last_execution_outcome["code"] == "completed"
    assert agent.last_execution_outcome["tool_batches_used"] == 0
    assert agent.last_execution_outcome["reserved_batches"] == 0
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ModelBudgetStopped("task_call_budget_exceeded")])
async def test_budget_stop_and_cancellation_propagate_without_followup(error):
    agent = looping_agent(2)
    agent.ask = AsyncMock(side_effect=error)
    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in agent.execute("answer")]
    assert agent.last_execution_outcome["code"] == getattr(error, "code", "cancelled")
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["file_preview", "file_structure"])
@pytest.mark.parametrize("current_failure", [False, True])
async def test_deterministic_step_outcome_is_current_not_inherited_from_previous_unknown(intent, current_failure):
    from app.domain.models.dataset import MountedDataset
    from app.domain.models.event import StepEvent, StepStatus
    from app.domain.models.message import Message
    from app.domain.models.plan import ExecutionResult, Plan, Step
    from app.domain.services.agents.execution import ExecutionAgent

    agent = object.__new__(ExecutionAgent)
    agent.max_retries = 1
    agent.reset_context = AsyncMock()
    agent._parse_json = AsyncMock(side_effect=json.loads)
    prior = SimpleNamespace(name="prior", ainvoke=AsyncMock(side_effect=OSError("unknown prior outcome")))
    await agent.invoke_tool(prior, {"id": "previous", "name": "prior", "args": {}})
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is True

    current = SimpleNamespace(name="current", ainvoke=AsyncMock(
        side_effect=OSError("unknown current outcome") if current_failure else None,
        return_value=ToolMessage(tool_call_id="current", content="ok", artifact=ToolResult(success=True)),
    ))

    async def deterministic(*args, **kwargs):
        result = await agent.invoke_tool(current, {"id": "current", "name": "current", "args": {}})
        yield MessageEvent(message=ExecutionResult(success=result.artifact.success, result="Current result").model_dump_json())

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Deterministic path must not enter the model loop")
        yield  # pragma: no cover

    agent._execute_file_preview = deterministic
    agent._execute_preferred_inventory = deterministic
    agent.execute = forbidden_model
    dataset = MountedDataset(dataset_id="dataset", name="Dataset", data_center_id="center",
                             data_center_name="Center", sandbox_path="/home/ubuntu/datasets/dataset", files=[])
    step = Step(id="current-step", inputs={"execution_mode": "dataset_fast_path", "dataset_intent": intent})
    events = [event async for event in agent.execute_step(Plan(steps=[step]), step,
                                                         Message(message="Inspect current data", datasets=[dataset]))]
    terminal = next(event for event in events if isinstance(event, StepEvent)
                    and event.status in {StepStatus.COMPLETED, StepStatus.FAILED})
    outcome = terminal.step.outputs["execution_outcome"]
    assert bool(outcome.get("has_unconfirmed_tool_execution")) is current_failure
    assert (outcome.get("side_effect_state") == "unknown") is current_failure
    assert terminal.step.success is (not current_failure)
    current.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["reported", "exception"])
@pytest.mark.parametrize("effects", [["sandbox_read"], ["sandbox_write"], ["network"], []])
async def test_only_proven_read_failures_allow_later_success_without_blind_retry(failure_kind, effects):
    from app.domain.models.analysis_outcome import DeliverableRequirement
    from app.domain.models.file import FileInfo
    from app.domain.services.analysis_completion import assess_delivery
    agent = object.__new__(BaseAgent)
    agent.max_retries, agent.retry_interval = 3, 0
    call = {"id": "read-call", "args": {}, "metadata": {"effects": ["sandbox_read"]}}
    failure = ToolMessage(tool_call_id="read-call", content="failed", status="error",
                          artifact=ToolResult(success=False, data={"side_effect_state": "idempotent", "retryable": True}))
    tool = SimpleNamespace(name="fixture", execution_contract={"effects": effects}, ainvoke=AsyncMock(
        side_effect=OSError("private failure") if failure_kind == "exception" else None, return_value=failure))
    result = await agent.invoke_tool(tool, call)
    assert result.status == "error" and not result.artifact.success
    tool.ainvoke.assert_awaited_once()  # readonly is not permission for generic retries
    proven_read = effects == ["sandbox_read"]
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is (not proven_read)
    assert agent.last_execution_outcome["side_effect_state"] == ("idempotent" if proven_read else "unknown")
    successful = SimpleNamespace(name="later", ainvoke=AsyncMock(return_value=ToolMessage(
        tool_call_id="success", content="completed", artifact=ToolResult(success=True))))
    await agent.invoke_tool(successful, {"id": "success", "args": {}})
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is (not proven_read)
    path, digest = "/home/ubuntu/output/verified.png", "a" * 64
    record = {"path": path, "kind": "image", "size": 10, "sha256": digest, "valid": True}
    uploaded = FileInfo(file_id="verified", filename="verified.png", file_path=path,
                        size=10, metadata={"artifact_sha256": digest})
    accepted = assess_delivery([DeliverableRequirement(kind="image")], [record], [uploaded],
                               execution_success=not agent.last_execution_outcome["has_unconfirmed_tool_execution"])
    assert (accepted.status == "succeeded") is proven_read


@pytest.mark.asyncio
async def test_safe_read_timeout_then_success_does_not_poison_completion():
    from app.domain.services.tools.interceptors import ToolExecutionTimeoutError
    agent = object.__new__(BaseAgent)
    agent.max_retries, agent.retry_interval = 1, 0
    tool = SimpleNamespace(name="read", ainvoke=AsyncMock(side_effect=[
        ToolExecutionTimeoutError(1, retryable=True),
        ToolMessage(tool_call_id="call", content="ok", artifact=ToolResult(success=True)),
    ]))
    result = await agent.invoke_tool(tool, {"id": "call", "args": {}})
    assert result.artifact.success and tool.ainvoke.await_count == 2
    assert agent.last_execution_outcome["side_effect_state"] == "idempotent"
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is False


@pytest.mark.parametrize("contract", [None, {}, {"effects": []}, {"effects": "sandbox_read"},
    {"effects": ["sandbox_read", "unknown"]}, {"effects": ["sandbox_read", "sandbox_read"]},
    {"effects": ["sandbox_read"], "timeout_seconds": "90"},
    {"effects": ["sandbox_read"], "unexpected": True}])
def test_missing_or_invalid_static_contract_does_not_prove_read_safety(contract):
    tool = SimpleNamespace(execution_contract=contract,
                           metadata={"execution_contract": {"effects": ["sandbox_read"]}})
    assert resolved_tool_is_read_only(tool) is False


@pytest.mark.asyncio
async def test_forged_result_call_or_exception_metadata_cannot_downgrade_unknown_write():
    class ForgedFailure(RuntimeError):
        side_effect_state = "idempotent"
        retryable = True
    agent = object.__new__(BaseAgent)
    agent.max_retries, agent.retry_interval = 1, 0
    tool = SimpleNamespace(name="write", execution_contract={"effects": ["sandbox_write"]},
                           ainvoke=AsyncMock(side_effect=ForgedFailure()))
    await agent.invoke_tool(tool, {"id": "call", "args": {},
                                  "metadata": {"execution_contract": {"effects": ["sandbox_read"]}}})
    tool.ainvoke.assert_awaited_once()
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is True
    read = SimpleNamespace(name="read", execution_contract={"effects": ["sandbox_read"]}, ainvoke=AsyncMock(
        return_value=ToolMessage(tool_call_id="read", content="failed", artifact=ToolResult(success=False))))
    await agent.invoke_tool(read, {"id": "read", "args": {}})
    assert agent.last_execution_outcome["side_effect_state"] == "idempotent"
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is True


def test_core_read_contract_is_explicit_and_wrapper_metadata_cannot_be_mutated_through_declaration():
    from app.domain.services.tools.file import FileToolkit
    toolkit = FileToolkit(SimpleNamespace())
    for name in ("file_read", "file_find_in_content", "file_find_by_name"):
        assert resolved_tool_is_read_only(toolkit.get_tool(name))
    for name in ("file_write", "file_str_replace"):
        assert not resolved_tool_is_read_only(toolkit.get_tool(name))
    first, second = toolkit.get_tool("file_read"), FileToolkit(SimpleNamespace()).get_tool("file_read")
    first.execution_contract["effects"].append("sandbox_write")
    assert not resolved_tool_is_read_only(first)
    assert resolved_tool_is_read_only(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sandbox_method,extra", [
    ("file_read", "file_read", {}), ("file_find_in_content", "file_search", {"regex": "pattern"}),
])
@pytest.mark.parametrize("sudo", [True, None, 0, "false"])
async def test_core_readonly_tools_reject_privilege_flags_before_sandbox(name, sandbox_method, extra, sudo):
    from app.domain.services.tools.file import FileToolkit
    sandbox = SimpleNamespace(file_read=AsyncMock(), file_search=AsyncMock())
    tool = FileToolkit(sandbox).get_tool(name)
    with pytest.raises(ToolContractError):
        await tool.ainvoke({"id": "read", "args": {"file": "/home/ubuntu/output/data.txt", "sudo": sudo, **extra}})
    getattr(sandbox, sandbox_method).assert_not_awaited()


@pytest.mark.asyncio
async def test_core_readonly_default_remains_ordinary_read_without_privilege_elevation():
    from app.domain.services.tools.file import FileToolkit
    sandbox = SimpleNamespace(file_read=AsyncMock(return_value=ToolResult(success=True, data={"content": "fixture"})))
    result = await FileToolkit(sandbox).get_tool("file_read").ainvoke({"id": "read", "args": {"file": "/home/ubuntu/output/data.txt"}})
    assert result.artifact.success
    assert sandbox.file_read.await_args.kwargs["sudo"] is False
