import copy
import gc
import weakref
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, create_model, field_validator

from app.domain.services.tools import tool_contract as contracts
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.tool_contract import ToolContractError, validate_tool_arguments


class UncachedTool:
    __slots__ = ("args_schema", "definition")

    def __init__(self, schema):
        self.args_schema = schema if isinstance(schema, dict) else None
        self.definition = {"parameters": schema}


def make_tool(schema, *, pipeline=None):
    return SimpleNamespace(
        name="same_tool_name",
        args_schema=schema if isinstance(schema, dict) or isinstance(schema, type) else None,
        definition={"parameters": schema} if not isinstance(schema, type) else None,
        toolkit=SimpleNamespace(tool_execution_pipeline=pipeline) if pipeline is not None else None,
    )


def count_preparation(monkeypatch):
    counts = {"validator_for": 0, "closed": 0}
    original_validator_for = contracts.validator_for
    original_closed = contracts._closed_objects

    def validator_for(schema):
        counts["validator_for"] += 1
        return original_validator_for(schema)

    def closed(schema, **kwargs):
        # Recursive calls also use the patched function; count roots only.
        if not kwargs:
            counts["closed"] += 1
        return original_closed(schema, **kwargs)

    monkeypatch.setattr(contracts, "validator_for", validator_for)
    monkeypatch.setattr(contracts, "_closed_objects", closed)
    return counts


def schema_for(kind="integer"):
    return {"type": "object", "properties": {"value": {"type": kind}}, "required": ["value"]}


def outcome(tool, args):
    try:
        return "ok", validate_tool_arguments(tool, {"args": copy.deepcopy(args)})
    except ToolContractError as error:
        return "error", error.code, error.fields


def test_preparation_is_cached_but_every_argument_is_validated_and_not_retained(monkeypatch):
    counts = count_preparation(monkeypatch)
    tool = make_tool(schema_for("string"))
    request = {"args": {"value": "private-user-argument"}, "id": "private-call-id"}
    assert validate_tool_arguments(tool, request) == request
    assert validate_tool_arguments(tool, {"args": {"value": "different"}})["args"]["value"] == "different"
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {"value": 42}})
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {"value": "allowed", "unexpected": True}})

    assert counts == {"validator_for": 1, "closed": 1}
    cache = getattr(tool, contracts._CONTRACT_CACHE_ATTRIBUTE)
    assert "private-user-argument" not in repr(cache.entries)
    assert "private-call-id" not in repr(cache.entries)
    assert request == {"args": {"value": "private-user-argument"}, "id": "private-call-id"}


def test_new_wrappers_share_only_their_task_pipeline_cache(monkeypatch):
    counts = count_preparation(monkeypatch)
    first_pipeline = ToolExecutionPipeline()
    second_pipeline = ToolExecutionPipeline()
    for pipeline in (first_pipeline, first_pipeline, second_pipeline):
        validate_tool_arguments(make_tool(schema_for(), pipeline=pipeline), {"args": {"value": 1}})
    assert counts["validator_for"] == 2
    assert getattr(first_pipeline, contracts._CONTRACT_CACHE_ATTRIBUTE) is not getattr(
        second_pipeline, contracts._CONTRACT_CACHE_ATTRIBUTE,
    )


def test_mutable_nested_schema_and_same_name_changes_never_use_a_stale_contract():
    pipeline = ToolExecutionPipeline()
    schema = schema_for()
    tool = make_tool(schema, pipeline=pipeline)
    assert outcome(tool, {"value": 1})[0] == "ok"
    schema["properties"]["value"]["type"] = "string"
    assert outcome(tool, {"value": 1})[0] == "error"
    assert outcome(tool, {"value": "new"})[0] == "ok"
    other = make_tool(schema_for("boolean"), pipeline=pipeline)
    assert outcome(other, {"value": "new"})[0] == "error"
    assert outcome(other, {"value": True})[0] == "ok"


def test_pydantic_schema_generation_is_cached_but_validators_and_default_factories_run_each_time(monkeypatch):
    calls = {"schema": 0, "validator": 0, "default": 0}

    def next_default():
        calls["default"] += 1
        return calls["default"]

    class Args(BaseModel):
        value: int
        generated: int = Field(default_factory=next_default)

        @field_validator("value")
        @classmethod
        def positive(cls, value):
            calls["validator"] += 1
            if value < 0:
                raise ValueError("positive required")
            return value + 1

    original = BaseModel.model_json_schema.__func__

    def schema(cls, *args, **kwargs):
        calls["schema"] += 1
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(BaseModel, "model_json_schema", classmethod(schema))
    tool = make_tool(Args)
    assert validate_tool_arguments(tool, {"args": {"value": 2}})["args"] == {"value": 3, "generated": 1}
    assert validate_tool_arguments(tool, {"args": {"value": 4}})["args"] == {"value": 5, "generated": 2}
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, {"args": {"value": -1}})
    assert calls == {"schema": 1, "validator": 3, "default": 3}


def test_model_rebuild_schema_metadata_and_same_named_model_are_invalidated(monkeypatch):
    calls = []
    original = BaseModel.model_json_schema.__func__

    def schema(cls, *args, **kwargs):
        calls.append(cls)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(BaseModel, "model_json_schema", classmethod(schema))
    Args = create_model("Args", value=(int, Field(json_schema_extra={"maximum": 10})))
    tool = make_tool(Args)
    assert outcome(tool, {"value": 9})[0] == "ok"
    Args.model_fields["value"].json_schema_extra["maximum"] = 5
    assert outcome(tool, {"value": 9})[0] == "error"
    Args.model_fields["value"].annotation = str
    Args.model_fields["value"].json_schema_extra.clear()
    Args.model_rebuild(force=True)
    assert outcome(tool, {"value": 9})[0] == "error"
    assert outcome(tool, {"value": "text"})[0] == "ok"
    OtherArgs = create_model("Args", value=(bool, ...))
    tool.args_schema = OtherArgs
    assert outcome(tool, {"value": "text"})[0] == "error"
    assert outcome(tool, {"value": True})[0] == "ok"
    assert calls == [Args, Args, Args, OtherArgs]


@pytest.mark.parametrize("hook", ["override", "extra", "field_extra"])
def test_dynamic_schema_hooks_are_re_evaluated(hook):
    state = {"maximum": 10, "calls": 0}

    def extra(schema):
        state["calls"] += 1
        schema["maximum"] = state["maximum"]

    if hook == "override":
        class Args(BaseModel):
            value: int

            @classmethod
            def model_json_schema(cls, *args, **kwargs):
                schema = super().model_json_schema(*args, **kwargs)
                extra(schema["properties"]["value"])
                return schema
    elif hook == "extra":
        class Args(BaseModel):
            value: int
            model_config = {"json_schema_extra": lambda schema: extra(schema["properties"]["value"])}
    else:
        class Args(BaseModel):
            value: int = Field(json_schema_extra=extra)

    tool = make_tool(Args)
    assert outcome(tool, {"value": 9})[0] == "ok"
    state["maximum"] = 5
    assert outcome(tool, {"value": 9})[0] == "error"
    assert state["calls"] == 2


def test_failed_preparation_is_not_cached_and_can_recover(monkeypatch):
    tool = make_tool({"type": "not-a-json-schema-type"})
    assert outcome(tool, {})[1] == "tool_schema_unavailable"
    assert not getattr(tool, contracts._CONTRACT_CACHE_ATTRIBUTE).entries
    tool.args_schema["type"] = "object"
    assert outcome(tool, {})[0] == "ok"


def test_large_schemas_and_unattachable_tools_bypass_cache(monkeypatch):
    counts = count_preparation(monkeypatch)
    monkeypatch.setattr(contracts, "_CONTRACT_CACHE_MAX_SCHEMA_BYTES", 128)
    schema = {**schema_for(), "description": "long" * 1024}
    tool = make_tool(schema)
    for candidate in (tool, tool, UncachedTool(schema_for()), UncachedTool(schema_for())):
        assert outcome(candidate, {"value": 1})[0] == "ok"
    assert counts["validator_for"] == 4
    assert not getattr(tool, contracts._CONTRACT_CACHE_ATTRIBUTE).entries


def test_cache_entries_and_source_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(contracts, "_CONTRACT_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(contracts, "_CONTRACT_CACHE_MAX_BYTES", 300)
    pipeline = ToolExecutionPipeline()
    for index in range(20):
        validate_tool_arguments(make_tool({**schema_for(), "title": str(index)}, pipeline=pipeline),
                                {"args": {"value": index}})
    cache = getattr(pipeline, contracts._CONTRACT_CACHE_ATTRIBUTE)
    assert len(cache.entries) <= 3
    assert cache.source_bytes <= 300
    assert cache.source_bytes == sum(entry.source_bytes for entry in cache.entries.values())


def test_task_cache_does_not_keep_dynamic_pydantic_classes_alive():
    pipeline = ToolExecutionPipeline()
    Args = create_model("DisposableArgs", value=(int, ...))
    reference = weakref.ref(Args)
    tool = make_tool(Args, pipeline=pipeline)
    validate_tool_arguments(tool, {"args": {"value": 1}})
    del tool, Args
    gc.collect()
    assert reference() is None
    assert getattr(pipeline, contracts._CONTRACT_CACHE_ATTRIBUTE).entries


@pytest.mark.parametrize("schema,args_cases", [
    (schema_for(), [{"value": 1}, {"value": True}, {"value": "1"}, {}, {"value": 1, "extra": 2}]),
    ({"type": "object", "properties": {"pair": {
        "type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}], "items": False,
    }}}, [{"pair": [1, "x"]}, {"pair": (1, "x")}, {"pair": [1, "x", 2]}]),
    ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {
        "pair": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}], "additionalItems": False},
    }}, [{"pair": [1, "x"]}, {"pair": [1, "x", 2]}, {"pair": [True, "x"]}]),
    ({"type": "object", "allOf": [
        {"properties": {"left": {"type": "integer"}}}, {"properties": {"right": {"type": "string"}}},
    ]}, [{"left": 1, "right": "x"}, {"left": 1, "right": "x", "unknown": True}]),
    ({"type": "object", "properties": {"mapping": {"type": "object", "additionalProperties": {"type": "integer"}}}},
     [{"mapping": {"custom": 1}}, {"mapping": {"custom": "invalid"}}]),
    ({"$ref": "https://never-fetched.invalid/schema"}, [{}, {"value": "secret"}]),
    (True, [{}, {"arbitrary": "allowed"}]),
    (False, [{}, {"arbitrary": "denied"}]),
])
def test_cached_and_uncached_contracts_have_identical_outcomes(schema, args_cases):
    cached = make_tool(copy.deepcopy(schema))
    uncached = UncachedTool(copy.deepcopy(schema))
    for _ in range(2):
        for args in args_cases:
            assert outcome(cached, args) == outcome(uncached, args)
