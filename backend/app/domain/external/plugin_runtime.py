from __future__ import annotations

import re
from typing import Any, Literal, Protocol, runtime_checkable

from jsonschema import Draft7Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


_JSON_SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
_PLUGIN_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_PLUGIN_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$"
_TOOL_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_REGEX_ESCAPES = frozenset(r"\.^$*+?()[]{}|/")
_PORTABLE_REGEX_QUANTIFIER = re.compile(
    r"\{(?P<minimum>[0-9]{1,6})(?:,(?P<maximum>[0-9]{0,6}))?\}"
)
_MAX_PORTABLE_REGEX_LENGTH = 2048


def _validate_portable_regex(pattern: str) -> None:
    """Restrict schemas to the Python/ECMAScript regex intersection.

    JSON Schema draft-07 defines patterns in terms of ECMAScript. Python's
    validator uses ``re`` instead, so shorthand character classes, property
    escapes and implementation-specific groups could otherwise disagree.
    Non-capturing groups are retained because both runtimes implement them.
    """
    if len(pattern) > _MAX_PORTABLE_REGEX_LENGTH:
        raise ValueError("JSON Schema pattern exceeds the portable length limit")

    index = 0
    in_character_class = False
    group_stack: list[dict[str, bool]] = []
    last_closed_group: dict[str, bool] | None = None
    while index < len(pattern):
        character = pattern[index]
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("JSON Schema pattern contains an invalid surrogate")

        if character == "\\":
            if index + 1 >= len(pattern):
                raise ValueError("JSON Schema pattern ends with an escape")
            escaped = pattern[index + 1]
            if escaped not in _PORTABLE_REGEX_ESCAPES:
                raise ValueError("JSON Schema pattern uses a non-portable escape")
            last_closed_group = None
            index += 2
            continue

        if in_character_class:
            if character == "[":
                raise ValueError("JSON Schema pattern uses a nested character class")
            if character == "]":
                in_character_class = False
                last_closed_group = None
            index += 1
            continue

        if character == "[":
            in_character_class = True
            first = index + 1
            if first < len(pattern) and pattern[first] == "^":
                first += 1
            if first >= len(pattern) or pattern[first] == "]":
                raise ValueError("JSON Schema pattern uses an empty character class")
            index += 1
            continue
        if character == "]":
            raise ValueError("JSON Schema pattern has an unmatched character class")
        if character == "(":
            if pattern[index : index + 3] == "(?:":
                group_stack.append({"quantified": False, "alternation": False})
                last_closed_group = None
                index += 3
                continue
            if index + 1 < len(pattern) and pattern[index + 1] == "?":
                raise ValueError("JSON Schema pattern uses a non-portable group")
            group_stack.append({"quantified": False, "alternation": False})
            last_closed_group = None
            index += 1
            continue
        if character == ")":
            if group_stack:
                last_closed_group = group_stack.pop()
                if group_stack:
                    group_stack[-1]["quantified"] |= last_closed_group["quantified"]
                    group_stack[-1]["alternation"] |= last_closed_group["alternation"]
            index += 1
            continue
        if character == "|":
            if group_stack:
                group_stack[-1]["alternation"] = True
            last_closed_group = None
            index += 1
            continue
        if character == "{":
            quantifier = _PORTABLE_REGEX_QUANTIFIER.match(pattern, index)
            if quantifier is None:
                raise ValueError("JSON Schema pattern uses a non-portable brace")
            minimum = int(quantifier.group("minimum"))
            maximum_text = quantifier.group("maximum")
            if maximum_text and minimum > int(maximum_text):
                raise ValueError("JSON Schema pattern has a reversed quantifier")
            repeats = (
                minimum > 1
                if maximum_text is None
                else maximum_text == "" or int(maximum_text) > 1
            )
            if (
                repeats
                and last_closed_group is not None
                and any(last_closed_group.values())
            ):
                raise ValueError("JSON Schema pattern uses an unsafe nested quantifier")
            if group_stack:
                group_stack[-1]["quantified"] = True
            last_closed_group = None
            index = quantifier.end()
            if index < len(pattern) and pattern[index] == "+":
                raise ValueError("JSON Schema pattern uses a possessive quantifier")
            continue
        if character == "}":
            raise ValueError("JSON Schema pattern uses a non-portable brace")
        if character in "*+?" and index + 1 < len(pattern) and pattern[index + 1] == "+":
            raise ValueError("JSON Schema pattern uses a possessive quantifier")
        if character in "*+?":
            if (
                character in "*+"
                and last_closed_group is not None
                and any(last_closed_group.values())
            ):
                raise ValueError("JSON Schema pattern uses an unsafe nested quantifier")
            if group_stack:
                group_stack[-1]["quantified"] = True
            last_closed_group = None
            index += 1
            continue
        last_closed_group = None
        index += 1

    re.compile(pattern)
    if _has_unseparated_unbounded_repetitions(pattern):
        raise ValueError("JSON Schema pattern uses ambiguous adjacent repetitions")


def _has_unseparated_unbounded_repetitions(pattern: str) -> bool:
    """Conservatively reject repeat chains that can cause polynomial backtracking."""
    index = 0
    unbounded_since_separator = False
    while index < len(pattern):
        character = pattern[index]
        if character in "^$()":
            index += 1
            continue
        if character == "|":
            unbounded_since_separator = False
            index += 1
            continue
        if character == "\\":
            atom_end = index + 2
        elif character == "[":
            atom_end = index + 1
            while atom_end < len(pattern):
                if pattern[atom_end] == "\\":
                    atom_end += 2
                    continue
                atom_end += 1
                if pattern[atom_end - 1] == "]":
                    break
        elif character in "*+?{}":
            index += 1
            continue
        else:
            atom_end = index + 1

        quantifier_end = atom_end
        minimum = 1
        maximum: int | None = 1
        if quantifier_end < len(pattern):
            quantifier = pattern[quantifier_end]
            if quantifier == "*":
                minimum, maximum = 0, None
                quantifier_end += 1
            elif quantifier == "+":
                minimum, maximum = 1, None
                quantifier_end += 1
            elif quantifier == "?":
                minimum, maximum = 0, 1
                quantifier_end += 1
            elif quantifier == "{":
                match = _PORTABLE_REGEX_QUANTIFIER.match(pattern, quantifier_end)
                if match is not None:
                    minimum = int(match.group("minimum"))
                    maximum_text = match.group("maximum")
                    maximum = (
                        minimum
                        if maximum_text is None
                        else None
                        if maximum_text == ""
                        else int(maximum_text)
                    )
                    quantifier_end = match.end()
        if quantifier_end < len(pattern) and pattern[quantifier_end] == "?":
            quantifier_end += 1

        if maximum is None:
            if unbounded_since_separator:
                return True
            unbounded_since_separator = True
        elif minimum >= 1:
            unbounded_since_separator = False
        index = quantifier_end
    return False


def _resolve_local_json_pointer(root: Any, reference: str) -> None:
    """Require a deterministic, self-contained draft-07 reference.

    The Cordis host does not load remote schemas. Mirroring that boundary in
    Python prevents a forged or stale snapshot from deferring reference errors
    until a live tool invocation.
    """
    if reference == "#":
        return
    if not reference.startswith("#/") or "%" in reference:
        raise ValueError("JSON Schema references must be local JSON pointers")
    current = root
    for raw_token in reference[2:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise ValueError("JSON Schema reference contains an invalid escape")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        if isinstance(current, list) and re.fullmatch(r"(?:0|[1-9][0-9]*)", token):
            index = int(token)
            if index < len(current):
                current = current[index]
                continue
        raise ValueError("JSON Schema reference cannot be resolved")


def _validate_compilable_draft7_schema(schema: dict[str, Any]) -> None:
    """Mirror the relevant fail-closed parts of the host's AJV compilation."""
    Draft7Validator.check_schema(schema)
    pending: list[Any] = [schema]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            reference = value.get("$ref")
            if reference is not None:
                if not isinstance(reference, str):
                    raise ValueError("JSON Schema reference must be a string")
                _resolve_local_json_pointer(schema, reference)
            pattern = value.get("pattern")
            if pattern is not None:
                if not isinstance(pattern, str):
                    raise ValueError("JSON Schema pattern must be a string")
                _validate_portable_regex(pattern)
            pattern_properties = value.get("patternProperties")
            if isinstance(pattern_properties, dict):
                for property_pattern in pattern_properties:
                    _validate_portable_regex(property_pattern)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


class PluginRuntimeError(RuntimeError):
    """Base error raised by the external plugin catalog runtime."""


class PluginRuntimeUnavailableError(PluginRuntimeError):
    """The plugin host is not running or cannot answer requests safely."""


class PluginRuntimeProtocolError(PluginRuntimeError):
    """The plugin host returned a malformed or incompatible response."""


class PluginRuntimeRPCError(PluginRuntimeError):
    """The plugin host returned a JSON-RPC error response."""

    def __init__(self, code: int | str | None, message: str):
        self.code = code
        super().__init__(message)


class PluginDescriptor(BaseModel):
    """One plugin package reported by the Cordis host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin: str = Field(strict=True, min_length=1, pattern=_PLUGIN_NAME_PATTERN)
    version: str = Field(strict=True, min_length=1, pattern=_PLUGIN_VERSION_PATTERN)
    manifest_digest: str = Field(
        strict=True,
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    tool_count: int = Field(strict=True, ge=0)


ToolEffect = Literal[
    "sandbox_read",
    "sandbox_write",
    "network",
    "credential_use",
    "external_side_effect",
]


class ToolCredentialRequirement(BaseModel):
    """A named secret slot, never a secret or an environment variable name."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ToolExecutionDescriptor(BaseModel):
    """Immutable execution policy input published by the Cordis catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int = Field(default=90, strict=True, ge=1, le=120)
    cancellable: bool = Field(default=False, strict=True)
    concurrency: Literal["exclusive", "parallel"] = "exclusive"
    effects: tuple[ToolEffect, ...] = ("sandbox_read", "sandbox_write")
    permissions: tuple[str, ...] = ()
    credentials: tuple[ToolCredentialRequirement, ...] = ()

    @model_validator(mode="after")
    def validate_sets(self) -> "ToolExecutionDescriptor":
        if len(self.effects) != len(set(self.effects)):
            raise ValueError("tool execution effects must be unique")
        if len(self.permissions) > 32 or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value) for value in self.permissions):
            raise ValueError("tool execution permissions must be bounded identifiers")
        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("tool execution permissions must be unique")
        if len(self.credentials) > 8 or len({item.slot for item in self.credentials}) != len(self.credentials):
            raise ValueError("tool credential slots must be unique and bounded")
        if self.credentials and "credential_use" not in self.effects:
            raise ValueError("tool credential slots require the credential_use effect")
        return self


class ToolPresentationDescriptor(BaseModel):
    """Static, data-free hint for the browser's declarative tool card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[
        "auto",
        "generic",
        "table",
        "chart",
        "map",
        "image",
        "artifact",
        "log",
    ] = "auto"
    title: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_labels(self) -> "ToolPresentationDescriptor":
        if self.title is not None and not self.title.strip():
            raise ValueError("tool presentation title must be non-empty")
        if self.description is not None and not self.description.strip():
            raise ValueError("tool presentation description must be non-empty")
        return self


class PluginToolDefinition(BaseModel):
    """Provider-neutral tool metadata exposed by the Cordis catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[2]
    name: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=_TOOL_NAME_PATTERN,
    )
    description: str = Field(strict=True, min_length=1)
    parameters: dict[str, Any]
    # ``None`` is intentionally different from an unrestricted schema: it
    # means this legacy handler has not declared a verifiable output contract.
    output_schema: dict[str, Any] | None
    execution: ToolExecutionDescriptor
    presentation: ToolPresentationDescriptor
    scopes: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=90, strict=True, ge=1, le=120)
    plugin: str = Field(strict=True, min_length=1, pattern=_PLUGIN_NAME_PATTERN)
    version: str = Field(strict=True, min_length=1, pattern=_PLUGIN_VERSION_PATTERN)

    @model_validator(mode="after")
    def validate_parameter_schema(self) -> "PluginToolDefinition":
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object JSON schema")
        if not self.description.strip():
            raise ValueError("tool description must be non-empty")
        if any(not scope.strip() for scope in self.scopes):
            raise ValueError("tool scopes must be non-empty")
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("tool scopes must be unique")
        for label, schema in (
            ("parameters", self.parameters),
            ("output_schema", self.output_schema),
        ):
            if (
                schema is not None
                and schema.get("$schema") not in {None, _JSON_SCHEMA_DIALECT}
            ):
                raise ValueError(f"{label} must use JSON Schema draft-07")
            if schema is not None:
                try:
                    _validate_compilable_draft7_schema(schema)
                except Exception as error:
                    raise ValueError(
                        f"{label} must be a valid JSON Schema draft-07 schema"
                    ) from error
        if self.execution.timeout_seconds != self.timeout_seconds:
            raise ValueError(
                "tool timeout_seconds must match execution.timeout_seconds"
            )
        return self

    def as_legacy_definition(self) -> dict[str, Any]:
        """Return the manifest shape consumed by the existing Agent adapter."""
        return self.model_dump(mode="python")


class PluginCatalogSnapshot(BaseModel):
    """An immutable, internally consistent Cordis catalog generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(strict=True)
    version: str = Field(strict=True)
    revision: str = Field(strict=True)
    manifest_digest: str = Field(strict=True)
    execution_bundle_digest: str = Field(strict=True)
    plugin_count: int = Field(strict=True, ge=0)
    tool_count: int = Field(strict=True, ge=0)
    plugins: tuple[PluginDescriptor, ...] = ()
    tools: tuple[PluginToolDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_counts_and_names(self) -> "PluginCatalogSnapshot":
        if self.engine != "cordis":
            raise ValueError("plugin catalog engine must be cordis")
        if self.plugin_count != len(self.plugins):
            raise ValueError("plugin_count does not match plugins")
        if self.tool_count != len(self.tools):
            raise ValueError("tool_count does not match tools")
        plugin_names = [plugin.plugin for plugin in self.plugins]
        if len(plugin_names) != len(set(plugin_names)):
            raise ValueError("Cordis catalog contains duplicate plugin names")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Cordis catalog contains duplicate tool names")
        plugins = {plugin.plugin: plugin for plugin in self.plugins}
        actual_tool_counts = {plugin_name: 0 for plugin_name in plugins}
        for tool in self.tools:
            descriptor = plugins.get(tool.plugin)
            if descriptor is None:
                raise ValueError("Cordis tool references an unknown plugin")
            if descriptor.version != tool.version:
                raise ValueError("Cordis tool and plugin versions do not match")
            actual_tool_counts[tool.plugin] += 1
        for plugin_name, descriptor in plugins.items():
            if actual_tool_counts[plugin_name] != descriptor.tool_count:
                raise ValueError("plugin tool_count does not match catalog tools")
        return self

    @classmethod
    def unavailable(cls) -> "PluginCatalogSnapshot":
        return cls(
            engine="cordis",
            version="unavailable",
            revision="",
            manifest_digest="",
            execution_bundle_digest="",
            plugin_count=0,
            tool_count=0,
            plugins=(),
            tools=(),
        )


@runtime_checkable
class PluginRuntime(Protocol):
    """Lifecycle and atomic-snapshot port implemented by the Node supervisor."""

    @property
    def healthy(self) -> bool:
        ...

    @property
    def current_snapshot(self) -> PluginCatalogSnapshot:
        """Return the cached generation, or an empty snapshot when unhealthy."""
        ...

    @property
    def last_error(self) -> str | None:
        """Return the retained safe reload or runtime failure message, if any."""
        ...

    async def start(self) -> PluginCatalogSnapshot:
        ...

    async def reload(self) -> PluginCatalogSnapshot:
        ...

    async def snapshot(self) -> PluginCatalogSnapshot:
        ...

    async def shutdown(self) -> None:
        ...
