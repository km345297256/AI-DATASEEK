from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Literal, Protocol, TypeAlias
import uuid


JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_REQUIRED_SCOPES = frozenset({"dataset_fast_path", "code_mode"})
_READ_ONLY_EFFECTS = frozenset({"sandbox_read"})
_SANDBOX_PATH_ROOTS = tuple(
    PurePosixPath(value)
    for value in (
        "/home/ubuntu/datasets",
        "/home/ubuntu/upload",
        "/home/ubuntu/output",
    )
)
_FORBIDDEN_TOOL_NAMES = frozenset({
    "code_mode",
    "code_mode_run",
    "catalog_load",
    "catalog_search",
    "shell_run",
    "program_run",
})
_FORBIDDEN_TOOL_PREFIXES = (
    "agent_",
    "browser_",
    "code_mode_",
    "delegate_",
    "discovery_",
    "file_",
    "handoff_",
    "mcp_",
    "message_",
    "plugin_",
    "shell_",
    "skill_",
    "spill_",
    "subagent_",
    "tool_discovery_",
)
_FORBIDDEN_PLUGINS = frozenset({
    "agent",
    "browser",
    "code_mode",
    "file",
    "mcp",
    "shell",
})


class CodeModeError(RuntimeError):
    """Public, non-retryable failure from the restricted interpreter."""

    code = "code_mode_error"
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


class CodeModeValidationError(CodeModeError):
    """The complete source program failed static validation."""

    code = "code_mode_validation_failed"


class CodeModeLimitExceeded(CodeModeError):
    """A configured code, time, call, depth, or result bound was exceeded."""

    code = "code_mode_limit_exceeded"


class CodeModeToolCallError(CodeModeError):
    """An inner governed tool call failed without exposing its exception."""

    code = "code_mode_tool_call_failed"

    def __init__(self, *, tool_name: str, call_id: str) -> None:
        self.tool_name = tool_name
        self.call_id = call_id
        super().__init__("governed_tool_failed")


class CodeModeDispatcher(Protocol):
    """Adapter to an already-active tool view.

    The adapter must resolve the named tool at call time, invoke its normal
    ``ainvoke`` wrapper, and return only its JSON-compatible public projection
    after policy, approval, AnalysisJob, audit, and Spill interceptors run. Raw
    ``ToolMessage.artifact`` values are intentionally outside this protocol.
    """

    async def __call__(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        *,
        call_id: str,
    ) -> JsonValue:
        ...


@dataclass(frozen=True, slots=True)
class CodeModeLimits:
    max_code_bytes: int = 12_000
    max_ast_nodes: int = 512
    max_statements: int = 32
    max_calls: int = 16
    timeout_seconds: float = 120.0
    max_value_bytes: int = 256_000
    max_result_bytes: int = 512_000
    max_depth: int = 16
    max_container_items: int = 10_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_code_bytes",
            "max_ast_nodes",
            "max_statements",
            "max_calls",
            "max_value_bytes",
            "max_result_bytes",
            "max_depth",
            "max_container_items",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")


@dataclass(frozen=True, slots=True)
class CodeModeToolSpec:
    """The minimal immutable manifest fields used for Code Mode admission."""

    name: str
    plugin: str
    contract_version: int
    scopes: frozenset[str]
    effects: frozenset[str]
    permissions: frozenset[str]
    credential_count: int

    @classmethod
    def from_manifest(
        cls,
        definition: Mapping[str, Any] | Any,
        *,
        plugin: str | None = None,
    ) -> "CodeModeToolSpec":
        if hasattr(definition, "model_dump"):
            definition = definition.model_dump(mode="python")
        if not isinstance(definition, Mapping):
            raise ValueError("code mode tool definition must be a mapping")
        execution = definition.get("execution")
        if hasattr(execution, "model_dump"):
            execution = execution.model_dump(mode="python")
        if not isinstance(execution, Mapping):
            execution = {}

        name = definition.get("name")
        plugin_name = definition.get("plugin", plugin)
        contract_version = definition.get("contract_version", 0)
        if not isinstance(name, str) or not _TOOL_NAME_PATTERN.fullmatch(name):
            raise ValueError("code mode tool name is invalid")
        if not isinstance(plugin_name, str) or not plugin_name.strip():
            raise ValueError("code mode plugin name is invalid")
        if isinstance(contract_version, bool) or not isinstance(contract_version, int):
            raise ValueError("code mode contract version is invalid")

        scopes = _manifest_string_set(definition.get("scopes", ()), "scopes")
        effects = _manifest_string_set(execution.get("effects", ()), "effects")
        permissions = _manifest_string_set(
            execution.get("permissions", ()),
            "permissions",
        )
        credentials = execution.get("credentials", ())
        if not isinstance(credentials, (list, tuple)):
            raise ValueError("code mode credentials must be a list")
        if any(not isinstance(value, Mapping) for value in credentials):
            raise ValueError("code mode credential declarations are invalid")
        return cls(
            name=name,
            plugin=plugin_name.strip(),
            contract_version=contract_version,
            scopes=scopes,
            effects=effects,
            permissions=permissions,
            credential_count=len(credentials),
        )

    @property
    def eligible(self) -> bool:
        """Require positive manifest opt-in and an effect-free read-only ceiling."""
        return (
            self.contract_version == 2
            and _REQUIRED_SCOPES <= self.scopes
            and self.effects == _READ_ONLY_EFFECTS
            and not self.permissions
            and self.credential_count == 0
            and self.plugin.casefold() not in _FORBIDDEN_PLUGINS
            and not _tool_name_is_forbidden(self.name)
        )


@dataclass(frozen=True, slots=True)
class CodeModeStep:
    sequence: int
    tool_name: str
    call_id: str
    status: Literal["succeeded", "failed"]

    def public_data(self) -> dict[str, JsonValue]:
        return {
            "sequence": self.sequence,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CodeModeExecutionResult:
    output: JsonValue
    steps: tuple[CodeModeStep, ...]

    @property
    def call_count(self) -> int:
        return len(self.steps)

    def public_data(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "status": "completed",
            "call_count": self.call_count,
            "steps": [step.public_data() for step in self.steps],
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedProgram:
    module: ast.Module
    call_count: int


class CodeModeInterpreter:
    """Interpret a prevalidated, expression-only Python-shaped tool program."""

    def __init__(self, limits: CodeModeLimits | None = None) -> None:
        self.limits = limits or CodeModeLimits()

    async def run(
        self,
        code: str,
        *,
        dispatcher: CodeModeDispatcher,
        tool_specs: Iterable[CodeModeToolSpec],
    ) -> CodeModeExecutionResult:
        allowed_tools = _eligible_tool_map(tool_specs)
        program = self._validate(code, allowed_tools)
        timeout = asyncio.timeout(float(self.limits.timeout_seconds))
        try:
            async with timeout:
                return await self._execute(program, dispatcher, allowed_tools)
        except asyncio.CancelledError:
            # Budget, AnalysisJob, authorization, and user cancellation are
            # control flow. Preserve the exact signal for the owning runtime.
            raise
        except TimeoutError:
            if timeout.expired():
                raise CodeModeLimitExceeded("duration") from None
            raise

    def _validate(
        self,
        code: str,
        allowed_tools: Mapping[str, CodeModeToolSpec],
    ) -> _ValidatedProgram:
        if not isinstance(code, str):
            raise CodeModeValidationError("source_must_be_text")
        try:
            encoded = code.encode("utf-8", errors="strict")
        except UnicodeError:
            raise CodeModeValidationError("source_encoding") from None
        if not encoded or len(encoded) > self.limits.max_code_bytes:
            raise CodeModeLimitExceeded("code_bytes")
        try:
            module = ast.parse(code, mode="exec")
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            raise CodeModeValidationError("syntax") from None

        nodes = 0
        try:
            for _node in ast.walk(module):
                nodes += 1
                if nodes > self.limits.max_ast_nodes:
                    raise CodeModeLimitExceeded("ast_nodes")
        except RecursionError:
            raise CodeModeLimitExceeded("ast_depth") from None
        if not module.body:
            raise CodeModeValidationError("empty_program")
        if len(module.body) > self.limits.max_statements:
            raise CodeModeLimitExceeded("statements")

        defined: set[str] = set()
        call_count = 0
        for index, statement in enumerate(module.body):
            if isinstance(statement, ast.Assign):
                if (
                    len(statement.targets) != 1
                    or not isinstance(statement.targets[0], ast.Name)
                    or not _VARIABLE_NAME_PATTERN.fullmatch(statement.targets[0].id)
                    or statement.targets[0].id == "tools"
                ):
                    raise CodeModeValidationError("assignment_target")
                if isinstance(statement.value, ast.Await):
                    self._validate_tool_call(statement.value, defined, allowed_tools)
                    call_count += 1
                    if call_count > self.limits.max_calls:
                        raise CodeModeLimitExceeded("calls")
                else:
                    self._validate_value_expression(statement.value, defined, depth=0)
                defined.add(statement.targets[0].id)
                continue
            if isinstance(statement, ast.Expr) and index == len(module.body) - 1:
                self._validate_value_expression(statement.value, defined, depth=0)
                continue
            raise CodeModeValidationError("unsupported_statement")
        if call_count == 0:
            raise CodeModeValidationError("tool_call_required")
        return _ValidatedProgram(module=module, call_count=call_count)

    def _validate_tool_call(
        self,
        expression: ast.Await,
        defined: set[str],
        allowed_tools: Mapping[str, CodeModeToolSpec],
    ) -> None:
        call = expression.value
        if (
            not isinstance(call, ast.Call)
            or call.keywords
            or len(call.args) != 2
            or not isinstance(call.func, ast.Attribute)
            or call.func.attr != "call"
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "tools"
        ):
            raise CodeModeValidationError("tool_call_shape")
        tool_name = call.args[0]
        if (
            not isinstance(tool_name, ast.Constant)
            or type(tool_name.value) is not str
            or tool_name.value not in allowed_tools
        ):
            raise CodeModeValidationError("tool_not_eligible")
        if not isinstance(call.args[1], ast.Dict):
            raise CodeModeValidationError("arguments_must_be_literal_dict")
        self._validate_value_expression(call.args[1], defined, depth=0)

    def _validate_value_expression(
        self,
        expression: ast.expr,
        defined: set[str],
        *,
        depth: int,
    ) -> None:
        if depth > self.limits.max_depth:
            raise CodeModeLimitExceeded("expression_depth")
        if isinstance(expression, ast.Constant):
            if type(expression.value) not in {type(None), bool, int, float, str}:
                raise CodeModeValidationError("literal_type")
            if isinstance(expression.value, float) and not math.isfinite(expression.value):
                raise CodeModeValidationError("literal_number")
            if isinstance(expression.value, str):
                _validate_public_path(expression.value)
            return
        if isinstance(expression, ast.UnaryOp) and isinstance(
            expression.op,
            (ast.UAdd, ast.USub),
        ):
            operand = expression.operand
            if not isinstance(operand, ast.Constant) or type(operand.value) not in {int, float}:
                raise CodeModeValidationError("unary_literal")
            value = +operand.value if isinstance(expression.op, ast.UAdd) else -operand.value
            if isinstance(value, float) and not math.isfinite(value):
                raise CodeModeValidationError("literal_number")
            return
        if isinstance(expression, ast.List):
            for item in expression.elts:
                self._validate_value_expression(item, defined, depth=depth + 1)
            return
        if isinstance(expression, ast.Dict):
            seen_keys: set[str] = set()
            for key, value in zip(expression.keys, expression.values, strict=True):
                if (
                    not isinstance(key, ast.Constant)
                    or type(key.value) is not str
                    or key.value in seen_keys
                ):
                    raise CodeModeValidationError("dictionary_key")
                seen_keys.add(key.value)
                self._validate_value_expression(value, defined, depth=depth + 1)
            return
        if isinstance(expression, ast.Name):
            if expression.id not in defined or expression.id == "tools":
                raise CodeModeValidationError("unknown_name")
            return
        if isinstance(expression, ast.Subscript):
            if not isinstance(expression.slice, ast.Constant) or type(expression.slice.value) is not str:
                raise CodeModeValidationError("dictionary_index")
            self._validate_subscript_base(expression.value, defined, depth=depth + 1)
            return
        # Attribute access, comprehensions, calls, operators, lambdas, and
        # f-strings all arrive here and are rejected before any dispatcher use.
        raise CodeModeValidationError("unsupported_expression")

    def _validate_subscript_base(
        self,
        expression: ast.expr,
        defined: set[str],
        *,
        depth: int,
    ) -> None:
        if depth > self.limits.max_depth:
            raise CodeModeLimitExceeded("expression_depth")
        if isinstance(expression, ast.Name):
            if expression.id not in defined or expression.id == "tools":
                raise CodeModeValidationError("unknown_name")
            return
        if isinstance(expression, ast.Subscript):
            if not isinstance(expression.slice, ast.Constant) or type(expression.slice.value) is not str:
                raise CodeModeValidationError("dictionary_index")
            self._validate_subscript_base(expression.value, defined, depth=depth + 1)
            return
        raise CodeModeValidationError("dictionary_index")

    async def _execute(
        self,
        program: _ValidatedProgram,
        dispatcher: CodeModeDispatcher,
        allowed_tools: Mapping[str, CodeModeToolSpec],
    ) -> CodeModeExecutionResult:
        variables: dict[str, JsonValue] = {}
        steps: list[CodeModeStep] = []
        output: JsonValue = None
        for statement in program.module.body:
            if isinstance(statement, ast.Assign):
                if isinstance(statement.value, ast.Await):
                    value = await self._execute_tool_call(
                        statement.value,
                        variables,
                        steps,
                        dispatcher,
                        allowed_tools,
                    )
                else:
                    value = self._evaluate_value(statement.value, variables)
                variables[statement.targets[0].id] = value
                output = value
            else:
                output = self._evaluate_value(statement.value, variables)

        output = _bounded_json_copy(
            output,
            limits=self.limits,
            byte_limit=self.limits.max_value_bytes,
            validate_paths=True,
        )
        result = CodeModeExecutionResult(output=output, steps=tuple(steps))
        if _json_size(result.public_data()) > self.limits.max_result_bytes:
            raise CodeModeLimitExceeded("result_bytes")
        return result

    async def _execute_tool_call(
        self,
        expression: ast.Await,
        variables: Mapping[str, JsonValue],
        steps: list[CodeModeStep],
        dispatcher: CodeModeDispatcher,
        allowed_tools: Mapping[str, CodeModeToolSpec],
    ) -> JsonValue:
        call = expression.value
        tool_name = call.args[0].value
        # Validation has already established the exact call shape and allowlist.
        if tool_name not in allowed_tools:
            raise CodeModeValidationError("tool_not_eligible")
        evaluated = self._evaluate_value(call.args[1], variables)
        arguments = _bounded_json_copy(
            evaluated,
            limits=self.limits,
            byte_limit=self.limits.max_value_bytes,
            validate_paths=True,
        )
        if not isinstance(arguments, dict):
            raise CodeModeValidationError("arguments_must_be_object")
        call_id = f"code-mode-{uuid.uuid4().hex}"
        try:
            raw_result = await dispatcher(tool_name, arguments, call_id=call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CodeModeToolCallError(
                tool_name=tool_name,
                call_id=call_id,
            ) from None
        try:
            value = _bounded_json_copy(
                raw_result,
                limits=self.limits,
                byte_limit=self.limits.max_value_bytes,
                validate_paths=True,
            )
        except CodeModeLimitExceeded:
            raise
        except CodeModeError:
            raise CodeModeToolCallError(
                tool_name=tool_name,
                call_id=call_id,
            ) from None
        status: Literal["succeeded", "failed"] = "succeeded"
        if isinstance(value, dict) and value.get("success") is False:
            status = "failed"
        steps.append(CodeModeStep(
            sequence=len(steps) + 1,
            tool_name=tool_name,
            call_id=call_id,
            status=status,
        ))
        return value

    def _evaluate_value(
        self,
        expression: ast.expr,
        variables: Mapping[str, JsonValue],
    ) -> JsonValue:
        if isinstance(expression, ast.Constant):
            return expression.value
        if isinstance(expression, ast.UnaryOp):
            operand = expression.operand.value
            return +operand if isinstance(expression.op, ast.UAdd) else -operand
        if isinstance(expression, ast.List):
            return [self._evaluate_value(item, variables) for item in expression.elts]
        if isinstance(expression, ast.Dict):
            return {
                key.value: self._evaluate_value(value, variables)
                for key, value in zip(expression.keys, expression.values, strict=True)
            }
        if isinstance(expression, ast.Name):
            return variables[expression.id]
        if isinstance(expression, ast.Subscript):
            base = self._evaluate_value(expression.value, variables)
            if not isinstance(base, dict) or expression.slice.value not in base:
                raise CodeModeValidationError("dictionary_lookup")
            return base[expression.slice.value]
        # Static validation makes this unreachable without an internal defect.
        raise CodeModeValidationError("unsupported_expression")


def eligible_code_mode_tool_specs(
    definitions: Iterable[Mapping[str, Any] | Any],
) -> tuple[CodeModeToolSpec, ...]:
    """Select eligible tools from one normalized, immutable catalog snapshot."""
    selected: dict[str, CodeModeToolSpec] = {}
    for definition in definitions:
        spec = CodeModeToolSpec.from_manifest(definition)
        if not spec.eligible:
            continue
        if spec.name in selected:
            raise ValueError("duplicate code mode tool name")
        selected[spec.name] = spec
    return tuple(selected[name] for name in sorted(selected))


def _eligible_tool_map(
    specs: Iterable[CodeModeToolSpec],
) -> dict[str, CodeModeToolSpec]:
    allowed: dict[str, CodeModeToolSpec] = {}
    for spec in specs:
        if not isinstance(spec, CodeModeToolSpec):
            raise TypeError("tool_specs must contain CodeModeToolSpec values")
        if not spec.eligible:
            continue
        if spec.name in allowed:
            raise ValueError("duplicate code mode tool name")
        allowed[spec.name] = spec
    return allowed


def _manifest_string_set(value: Any, label: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"code mode {label} must be a collection")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"code mode {label} contains an invalid value")
        normalized = item.strip()
        if normalized in values:
            raise ValueError(f"code mode {label} contains a duplicate")
        values.append(normalized)
    return frozenset(values)


def _tool_name_is_forbidden(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _FORBIDDEN_TOOL_NAMES or lowered.startswith(
        _FORBIDDEN_TOOL_PREFIXES
    )


def _validate_public_path(value: str) -> None:
    """Reject host-path spellings while retaining explicit sandbox paths."""
    if "\x00" in value:
        raise CodeModeValidationError("string_value")
    lowered = value.casefold()
    if (
        lowered.startswith("file:")
        or value.startswith("~/")
        or value.startswith("~\\")
        or value.startswith("\\\\")
        or _WINDOWS_ABSOLUTE_PATH.match(value)
    ):
        raise CodeModeValidationError("host_path")
    normalized_parts = value.replace("\\", "/").split("/")
    if ".." in normalized_parts:
        raise CodeModeValidationError("host_path")
    if not value.startswith("/"):
        return
    candidate = PurePosixPath(value)
    if not any(
        candidate == root or candidate.is_relative_to(root)
        for root in _SANDBOX_PATH_ROOTS
    ):
        raise CodeModeValidationError("host_path")


class _JsonCopyBudget:
    def __init__(self, *, limits: CodeModeLimits, byte_limit: int, validate_paths: bool):
        self._limits = limits
        self._byte_limit = byte_limit
        self._validate_paths = validate_paths
        self._items = 0
        self._minimum_bytes = 0
        self._active: set[int] = set()

    def copy(self, value: Any, *, depth: int = 0) -> JsonValue:
        if depth > self._limits.max_depth:
            raise CodeModeLimitExceeded("value_depth")
        if value is None or type(value) in {bool, int}:
            self._minimum_bytes += 4 if value is None else len(str(value))
            self._check_bytes()
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise CodeModeValidationError("non_finite_number")
            self._minimum_bytes += len(repr(value))
            self._check_bytes()
            return value
        if type(value) is str:
            if self._validate_paths:
                _validate_public_path(value)
            self._minimum_bytes += len(value.encode("utf-8"))
            self._check_bytes()
            return value
        if isinstance(value, list):
            return self._copy_list(value, depth)
        if isinstance(value, dict):
            return self._copy_dict(value, depth)
        raise CodeModeValidationError("non_json_value")

    def _copy_list(self, value: list[Any], depth: int) -> list[JsonValue]:
        identity = id(value)
        if identity in self._active:
            raise CodeModeValidationError("cyclic_value")
        self._active.add(identity)
        try:
            copied: list[JsonValue] = []
            for item in value:
                self._count_item()
                copied.append(self.copy(item, depth=depth + 1))
            return copied
        finally:
            self._active.remove(identity)

    def _copy_dict(self, value: dict[Any, Any], depth: int) -> dict[str, JsonValue]:
        identity = id(value)
        if identity in self._active:
            raise CodeModeValidationError("cyclic_value")
        self._active.add(identity)
        try:
            copied: dict[str, JsonValue] = {}
            for key, item in value.items():
                self._count_item()
                if type(key) is not str:
                    raise CodeModeValidationError("non_string_key")
                self._minimum_bytes += len(key.encode("utf-8"))
                self._check_bytes()
                copied[key] = self.copy(item, depth=depth + 1)
            return copied
        finally:
            self._active.remove(identity)

    def _count_item(self) -> None:
        self._items += 1
        if self._items > self._limits.max_container_items:
            raise CodeModeLimitExceeded("container_items")

    def _check_bytes(self) -> None:
        if self._minimum_bytes > self._byte_limit:
            raise CodeModeLimitExceeded("value_bytes")


def _bounded_json_copy(
    value: Any,
    *,
    limits: CodeModeLimits,
    byte_limit: int,
    validate_paths: bool,
) -> JsonValue:
    copied = _JsonCopyBudget(
        limits=limits,
        byte_limit=byte_limit,
        validate_paths=validate_paths,
    ).copy(value)
    if _json_size(copied) > byte_limit:
        raise CodeModeLimitExceeded("value_bytes")
    return copied


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise CodeModeValidationError("non_json_value") from None
