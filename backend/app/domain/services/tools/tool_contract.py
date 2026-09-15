"""Fail-closed tool input contracts and explicit, side-effect-aware failures."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import types
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from itertools import islice
from jsonschema.validators import Draft202012Validator, validator_for
from pydantic import BaseModel, ValidationError
from referencing import Registry
from langchain.messages import ToolMessage

from app.domain.models.tool_result import ToolResult


_CONTRACT_CACHE_ATTRIBUTE = "_dataseek_prepared_contracts"
_CONTRACT_CACHE_MAX_ENTRIES = 128
_CONTRACT_CACHE_MAX_BYTES = 1024 * 1024
_CONTRACT_CACHE_MAX_SCHEMA_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _PreparedContract:
    validator: Any
    closed_validator: Any
    source_bytes: int


class _ContractCache:
    """Bounded preparation-only cache owned by a task's execution pipeline.

    No arguments, validation errors or results are retained. Source-byte and
    entry ceilings bound retained schemas (validators also hold their copied
    original/closed schemas). Entries disappear with their owning pipeline.
    """

    def __init__(self) -> None:
        self.entries: OrderedDict[Any, _PreparedContract] = OrderedDict()
        self.source_bytes = 0

    def get(self, key: Any) -> _PreparedContract | None:
        prepared = self.entries.get(key)
        if prepared is not None:
            self.entries.move_to_end(key)
        return prepared

    def put(self, key: Any, prepared: _PreparedContract) -> None:
        if prepared.source_bytes > min(_CONTRACT_CACHE_MAX_SCHEMA_BYTES, _CONTRACT_CACHE_MAX_BYTES):
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.source_bytes -= previous.source_bytes
        self.entries[key] = prepared
        self.source_bytes += prepared.source_bytes
        while (len(self.entries) > _CONTRACT_CACHE_MAX_ENTRIES
               or self.source_bytes > _CONTRACT_CACHE_MAX_BYTES):
            _, removed = self.entries.popitem(last=False)
            self.source_bytes -= removed.source_bytes


def _contract_cache(tool: Any) -> _ContractCache | None:
    toolkit = getattr(tool, "toolkit", None)
    # Tool wrappers may be rebuilt for every lookup. The task-owned pipeline
    # outlives those wrappers but does not become a global model-class cache.
    owner = getattr(toolkit, "tool_execution_pipeline", None) if toolkit is not None else None
    if owner is None:
        owner = tool
    try:
        cache = getattr(owner, _CONTRACT_CACHE_ATTRIBUTE, None)
        if type(cache) is not _ContractCache:
            cache = _ContractCache()
            setattr(owner, _CONTRACT_CACHE_ATTRIBUTE, cache)
        return cache
    except (AttributeError, TypeError, ValueError):
        # Immutable/slot-only compatibility adapters still validate normally.
        return None


def _schema_fingerprint(value: Any, *, core_schema: bool = False) -> tuple[str, int] | None:
    """Bounded, type-sensitive key; unsupported/dynamic schemas bypass caching.

    Keep object order and distinguish tuples/bools from JSON arrays/integers.
    For Pydantic core schemas, ordinary validator identities are stable inputs,
    but dynamic JSON-schema hooks/serializers are deliberately not cached.
    """
    digest = hashlib.sha256()
    size = 0
    nodes = 0

    def feed(text: str) -> None:
        nonlocal size
        if len(text) > _CONTRACT_CACHE_MAX_SCHEMA_BYTES:
            raise ValueError("large schema")
        encoded = text.encode("utf-8")
        size += len(encoded)
        if size > _CONTRACT_CACHE_MAX_SCHEMA_BYTES:
            raise ValueError("large schema")
        digest.update(encoded)

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 10_000:
            raise ValueError("complex schema")
        if type(item) is dict:
            if core_schema:
                for key in ("pydantic_js_functions", "pydantic_js_annotation_functions"):
                    hooks = item.get(key, [])
                    if any(getattr(hook, "__func__", None) is not BaseModel.__get_pydantic_json_schema__.__func__
                           for hook in hooks):
                        raise ValueError("dynamic schema hook")
                if callable(item.get("pydantic_js_extra")) or callable(item.get("json_schema_extra")):
                    raise ValueError("dynamic schema extra")
                if "serialization" in item:
                    raise ValueError("custom serialization")
            feed("{")
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("non-JSON object key")
                if len(key) > _CONTRACT_CACHE_MAX_SCHEMA_BYTES:
                    raise ValueError("large schema key")
                feed(json.dumps(key, ensure_ascii=False))
                feed(":")
                visit(child, depth + 1)
                feed(",")
            feed("}")
        elif type(item) is list:
            feed("[")
            for child in item:
                visit(child, depth + 1)
                feed(",")
            feed("]")
        elif item is None or type(item) in (str, bool, int, float):
            if type(item) is str and len(item) > _CONTRACT_CACHE_MAX_SCHEMA_BYTES:
                raise ValueError("large schema string")
            feed(json.dumps(item, ensure_ascii=False, allow_nan=False))
        elif core_schema and isinstance(item, type) and issubclass(item, BaseModel):
            if (item.model_json_schema.__func__ is not BaseModel.model_json_schema.__func__
                    or item.__get_pydantic_json_schema__.__func__ is not BaseModel.__get_pydantic_json_schema__.__func__):
                raise ValueError("custom model schema")
            feed(f"model:{id(item)}:")
            visit(item.model_config, depth + 1)
        elif core_schema and isinstance(item, (types.FunctionType, types.BuiltinFunctionType)):
            feed(f"function:{id(item)}")
        elif core_schema and isinstance(item, types.MethodType):
            feed(f"method:{id(item.__func__)}:{id(item.__self__)}")
        else:
            raise ValueError("unsupported schema value")

    try:
        visit(value, 0)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return None
    return digest.hexdigest(), size


def _model_cache_key(model: type[BaseModel]) -> Any:
    if not getattr(model, "__pydantic_complete__", False):
        return None
    core = model.__pydantic_core_schema__
    fingerprint = _schema_fingerprint(core, core_schema=True)
    if fingerprint is None:
        return None
    # Weak identities cannot accidentally match a new same-named class after
    # GC. model_rebuild replaces the core schema/validator; in-place metadata
    # and nested model config mutations are detected by the content fingerprint.
    return ("model", weakref.ref(model), id(core), id(model.__pydantic_validator__), fingerprint[0])


class ToolContractError(ValueError):
    retryable = False
    code = "tool_arguments_invalid"
    side_effect_state = "not_started"

    def __init__(self, fields: list[dict[str, str]], *, code: str | None = None):
        self.fields = fields[:8]
        if code is not None:
            self.code = code
        super().__init__("Tool arguments do not satisfy the declared contract")


class SafeToolRetryError(RuntimeError):
    """An adapter explicitly guarantees repeating this invocation is safe.

    Raise only for a transient failure before execution, or after the adapter
    has established idempotency for the same invocation. A network error alone
    is not evidence that a write did not happen.
    """
    retryable = True
    safe_to_retry = True
    code = "tool_transient_failure"

    def __init__(self, *, side_effect_state: str = "not_started"):
        if side_effect_state not in {"not_started", "idempotent"}:
            raise ValueError("Safe retries require an execution-state guarantee")
        self.side_effect_state = side_effect_state
        super().__init__("The tool reported a safely retryable transient failure")


def _field_path(parts: Any) -> str:
    # Paths are schema locations, never error messages, values, or host paths.
    return ".".join(
        str(part) if isinstance(part, int) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(part))
        else "<field>"
        for part in parts
    )[:240] or "$"


def _closed_objects(schema: Any, *, shared_instance: bool = False, legacy_refs: bool = False) -> Any:
    """Secondary unknown-key check, respecting composed/ref record shapes.

    The original schema is independently validated first. This projection uses
    2020-12 evaluated-property accounting so allOf/oneOf/refs may contribute
    fields to one record without rejecting each other's legitimate fields.
    Explicit additionalProperties maps remain open with their own constraints.
    """
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    dialect = schema.get("$schema", "")
    legacy_refs = legacy_refs or bool(dialect and "2020-12" not in dialect and "2019-09" not in dialect)
    source = schema
    if legacy_refs and "$ref" in source:
        # Older drafts ignore $ref siblings. Do not start enforcing them in
        # the secondary projection; preserve definition containers for lookup.
        source = {key: value for key, value in source.items()
                  if key in {"$ref", "$defs", "definitions", "$id"}}
    result = {}
    for key, value in source.items():
        if key == "$schema":
            continue
        if key in {"$defs", "definitions"} and isinstance(value, dict):
            result[key] = {name: _closed_objects(item, shared_instance=True, legacy_refs=legacy_refs)
                           for name, item in value.items()}
        elif key in {"allOf", "anyOf", "oneOf"} and isinstance(value, list):
            result[key] = [_closed_objects(item, shared_instance=True, legacy_refs=legacy_refs) for item in value]
        elif key in {"if", "then", "else", "not"}:
            result[key] = _closed_objects(value, shared_instance=True, legacy_refs=legacy_refs)
        elif key in {"properties", "patternProperties", "dependentSchemas"} and isinstance(value, dict):
            result[key] = {name: _closed_objects(item, legacy_refs=legacy_refs) for name, item in value.items()}
        elif key in {"items", "additionalProperties", "additionalItems", "contains", "propertyNames"}:
            result[key] = ([_closed_objects(item, legacy_refs=legacy_refs) for item in value]
                           if isinstance(value, list) else _closed_objects(value, legacy_refs=legacy_refs))
        elif key == "prefixItems" and isinstance(value, list):
            result[key] = [_closed_objects(item, legacy_refs=legacy_refs) for item in value]
        else:
            result[key] = copy.deepcopy(value)
    # Translate legacy tuple/dependency syntax for the secondary evaluator.
    if isinstance(result.get("items"), list):
        result["prefixItems"] = result["items"]
        result["items"] = result.pop("additionalItems", True)
    if isinstance(result.get("dependencies"), dict):
        dependencies = result.pop("dependencies")
        result.setdefault("dependentRequired", {}).update({key: value for key, value in dependencies.items() if isinstance(value, list)})
        result.setdefault("dependentSchemas", {}).update({
            key: _closed_objects(value, shared_instance=True, legacy_refs=legacy_refs)
            for key, value in dependencies.items() if not isinstance(value, list)
        })
    for exclusive, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        if isinstance(result.get(exclusive), bool):
            enabled = result.pop(exclusive)
            if enabled and bound in result:
                result[exclusive] = result.pop(bound)
    if not shared_instance and any(key in result for key in ("properties", "$ref", "allOf", "anyOf", "oneOf", "then", "else")):
        result.setdefault("unevaluatedProperties", False)
    return result


def _prepared_tool_contract(tool: Any, model: Any, definition: Any) -> _PreparedContract | None:
    cache = _contract_cache(tool)
    model_key = None
    if isinstance(model, type) and issubclass(model, BaseModel):
        model_key = _model_cache_key(model) if cache is not None else None
        if model_key is not None:
            prepared = cache.get(model_key)
            if prepared is not None:
                return prepared
        schema = model.model_json_schema()
    elif isinstance(model, dict):
        schema = model
    elif isinstance(getattr(tool, "input_schema", None), dict):
        schema = tool.input_schema
    elif isinstance(definition, dict):
        schema = definition.get("parameters")
    else:
        schema = None
    if schema is None:
        return None

    fingerprint = _schema_fingerprint(schema) if cache is not None else None
    schema_key = ("json-schema", fingerprint[0]) if fingerprint is not None else None
    prepared = cache.get(schema_key) if schema_key is not None else None
    if prepared is None:
        # The cached validators must never retain a caller-owned mutable schema.
        snapshot = copy.deepcopy(schema) if schema_key is not None else schema
        validator_class = validator_for(snapshot)
        validator_class.check_schema(snapshot)
        validator = validator_class(snapshot, registry=Registry())
        closed = _closed_objects(snapshot)
        prepared = _PreparedContract(
            validator=validator,
            closed_validator=Draft202012Validator(closed, registry=Registry()),
            source_bytes=fingerprint[1] if fingerprint is not None else 0,
        )
        if schema_key is not None:
            cache.put(schema_key, prepared)
    # Never cache a failed preparation or oversized/dynamic model schema key.
    # Dynamic model hooks may still share a prepared *content*-keyed schema.
    if model_key is not None and schema_key is not None:
        cache.put(model_key, prepared)
    return prepared


def validate_tool_arguments(tool: Any, tool_call: Any) -> Any:
    """Validate before policy admission/jobs; preserve caller-owned arguments.

    JSON schema validates the uncoerced wire shape. The original Pydantic
    model then applies its validators and defaults; neither constraints nor
    custom validators are reconstructed or silently discarded.
    """
    if not isinstance(tool_call, dict) or not isinstance(tool_call.get("args", {}), dict):
        raise ToolContractError([{"field": "args", "type": "object_required"}])
    args = tool_call.get("args", {})
    model = getattr(tool, "args_schema", None)
    definition = getattr(tool, "definition", None)
    try:
        prepared = _prepared_tool_contract(tool, model, definition)
        # Compatibility for trusted in-process adapters with no declared schema.
        if prepared is None:
            return tool_call
        errors = list(islice(prepared.validator.iter_errors(args), 8))
        if not errors:
            errors = list(islice(prepared.closed_validator.iter_errors(args), 8))
    except Exception:
        raise ToolContractError([], code="tool_schema_unavailable") from None
    if errors:
        fields = []
        for error in errors:
            if error.validator == "required" and isinstance(error.instance, dict):
                for key in error.validator_value:
                    if key not in error.instance:
                        fields.append({"field": _field_path([*error.absolute_path, key]), "type": "required"})
            elif error.validator in {"additionalProperties", "unevaluatedProperties"} and isinstance(error.instance, dict):
                properties = error.schema.get("properties", {})
                fields.extend({"field": _field_path([*error.absolute_path, key]), "type": "unknown_field"}
                              for key in error.instance if key not in properties)
            else:
                fields.append({"field": _field_path(error.absolute_path), "type": str(error.validator)})
        raise ToolContractError(fields)
    validated_args = copy.deepcopy(args)
    if isinstance(model, type) and issubclass(model, BaseModel):
        try:
            validated = model.model_validate_json(json.dumps(args), strict=True)
            validated_args = {name: getattr(validated, name) for name in model.model_fields}
        except ValidationError as error:
            raise ToolContractError([
                {"field": _field_path(item["loc"]), "type": item["type"]}
                for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]
            ]) from None
        except Exception:
            raise ToolContractError([], code="tool_validator_failed") from None
    return {**tool_call, "args": validated_args}


def resolved_tool_is_read_only(tool: Any) -> bool:
    """Use only a valid static contract from the resolved executable identity.

    Reuse the pipeline's contract resolution, but intentionally supply no
    request/result metadata: neither model input nor returned tool data can
    claim that an arbitrary write was merely a read. Missing/empty/invalid
    contracts fail closed, including malformed fields unrelated to effects.
    """
    from app.domain.external.plugin_runtime import ToolExecutionDescriptor
    from app.domain.services.tools.interceptors import _tool_execution_contract
    from app.domain.services.tools.pipeline import ToolExecutionContext
    try:
        contract = _tool_execution_contract(ToolExecutionContext(tool=tool, tool_call={}))
        if contract is None:
            return False
        descriptor = ToolExecutionDescriptor.model_validate(dict(contract))
        return bool(descriptor.effects) and set(descriptor.effects) == {"sandbox_read"}
    except (TypeError, ValueError, AttributeError):
        return False


def resolved_tool_can_observe_pending(tool: Any, tool_call: dict, ledger: Any) -> bool:
    """Only audited core observers may wait on an already registered launch.

    This is not a read-only or replay contract: shell_wait can cancel its own
    original process on timeout. Exact host declaration and adapter identities
    exclude plugin/model names and self-reported metadata from this decision.
    """
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.shell import ShellToolkit
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    from app.domain.services.execution_evidence import ToolExecutionLedger
    if (type(tool) is not Tool or type(getattr(tool, "toolkit", None)) is not ShellToolkit
            or type(ledger) is not ToolExecutionLedger):
        return False
    toolkit = tool.toolkit
    if (not any(registered is tool for registered in toolkit.tools)
            or type(toolkit.sandbox) is not DockerSandbox
            or toolkit.sandbox.supports_execution_receipts is not True
            or not any(tool._tool is declaration for declaration in
                       (ShellToolkit.shell_wait, ShellToolkit.shell_view))):
        return False
    if not isinstance(tool_call, dict) or tool_call.get("name") != tool.name:
        return False
    args = tool_call.get("args")
    session_id = args.get("id") if isinstance(args, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return False
    return any(attempt.sandbox_id == str(toolkit.sandbox.id) and attempt.shell_id == session_id
               for attempt in ledger._attempts.values())


def tool_reported_failure_data(data: Any, *, read_only: bool) -> dict[str, Any]:
    # Returned metadata may describe the error, but cannot establish whether
    # execution started or whether repeating a write would be safe.
    code = data.get("error_code") if isinstance(data, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
        code = "tool_reported_failure"
    return {"error_code": code, "side_effect_state": "idempotent" if read_only else "unknown", "retryable": False}


def tool_failure_result(error: Exception, *, read_only: bool = False) -> ToolResult:
    """Never put provider/validator exception text or argument values on wire."""
    if isinstance(error, ToolContractError):
        return ToolResult(success=False, message=str(error), data={
            "error_code": error.code, "fields": error.fields,
            "side_effect_state": "not_started", "retryable": False,
        })
    if isinstance(error, PermissionError):
        code = "tool_permission_denied"
    elif isinstance(error, (TypeError, ValueError, AttributeError, NotImplementedError)):
        code = "tool_execution_error"
    else:
        code = getattr(error, "code", "tool_execution_unconfirmed")
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "tool_execution_unconfirmed"
    if isinstance(error, SafeToolRetryError):
        state = error.side_effect_state
    elif read_only or safe_tool_retry(error):
        # A retryable timeout is issued only by the explicit read-only timeout
        # contract. Recording it as unknown before retry would permanently
        # poison even a successful safe retry.
        state = "idempotent"
    else:
        state = "unknown"
    return ToolResult(success=False, message=(
        "Tool execution failed. Inspect its confirmed state before retrying; do not assume it did not run."
        if state == "unknown" else "Tool execution did not complete successfully."
    ), data={"error_code": code, "side_effect_state": state, "retryable": False})


def safe_tool_retry(error: Exception) -> bool:
    if isinstance(error, SafeToolRetryError):
        return True
    # The existing timeout interceptor issues this flag only after verifying
    # that the tool's declared effects are exclusively sandbox reads.
    from app.domain.services.tools.interceptors import ToolExecutionTimeoutError
    return isinstance(error, ToolExecutionTimeoutError) and error.retryable is True


def result_failed(result: Any) -> bool:
    if getattr(result, "status", None) == "error":
        return True
    artifact = getattr(result, "artifact", result)
    if isinstance(artifact, ToolResult):
        return not artifact.success
    if isinstance(artifact, dict):
        return artifact.get("success") is False
    return getattr(result, "status", None) == "error"


def normalize_failed_tool_result(result: Any) -> Any:
    """An explicit error cannot retain a contradictory successful artifact."""
    if not isinstance(result, ToolMessage) or not result_failed(result):
        return result
    result.status = "error"
    artifact = result.artifact
    if isinstance(artifact, ToolResult):
        if artifact.success:
            result.artifact = artifact.model_copy(update={"success": False})
            result.content = result.artifact.model_dump_json()
    elif isinstance(artifact, dict):
        if artifact.get("success") is not False:
            result.artifact = {**artifact, "success": False}
            result.content = json.dumps(result.artifact, ensure_ascii=False)
    else:
        result.artifact = ToolResult(success=False, message="Tool execution failed", data={
            "error_code": "tool_reported_failure", "side_effect_state": "unknown",
        })
    return result
