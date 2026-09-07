from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, Field

from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginDescriptor,
    PluginToolDefinition,
    ToolCredentialRequirement,
)


PluginRuntimeStatus = Literal["healthy", "unavailable", "error"]
_REDACTED_METADATA = "[redacted sensitive metadata]"
_WEB_URL = re.compile(r"\b(?:https?|wss?)://[^\s<>'\"]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (?:
        ["']?authorization["']?\s*[:=]\s*(?:(?:bearer|basic)\s+)?["']?\S
        |
        ["']?
        (?:x[-_\s]?api[-_\s]?key|api[-_\s]?key|access[-_\s]?token|token|password|passwd|secret)
        ["']?\s*[:=]\s*["']?\S
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"
)
_UNC_ABSOLUTE_PATH = re.compile(r"(?<![\\])\\\\[^\\/\s]+[\\/]")
_POSIX_ABSOLUTE_PATH = re.compile(
    # A slash after a delimiter (including `file:` and `path:`) starts an
    # absolute filesystem path. `#/$defs/x` and ordinary `namespace/name`
    # remain valid public metadata.
    r"(?<![A-Za-z0-9_#])/(?!/)(?=[^\s/])"
)
_COMMON_POSIX_ROOT = re.compile(
    r"/(?:Users|Volumes|home|root|private|tmp|var|srv|mnt|data|opt|etc|run|proc|dev)(?:/|\b)",
    re.IGNORECASE,
)
_CREDENTIAL_KEY_SUFFIXES = frozenset({
    "authorization",
    "apikey",
    "accesskey",
    "xapikey",
    "accesstoken",
    "token",
    "password",
    "passwd",
    "secret",
    "secretkey",
    "clientsecret",
    "credential",
    "cookie",
    "privatekey",
    "signature",
})
_ASSIGNMENT_KEY = re.compile(
    r"(?<![A-Za-z0-9])[\"']?([A-Za-z][A-Za-z0-9_-]{0,80})[\"']?\s*[:=]"
)


def _is_credential_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _decoded(value).casefold())
    return any(normalized.endswith(suffix) for suffix in _CREDENTIAL_KEY_SUFFIXES)


def _decoded(value: str) -> str:
    decoded = value
    for _ in range(2):
        candidate = unquote(decoded)
        if candidate == decoded:
            break
        decoded = candidate
    return decoded


def _unsafe_web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return True
    except ValueError:
        return True
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = re.sub(r"[-_\s]", "", _decoded(key)).casefold()
        decoded_item = _decoded(item)
        if _is_credential_key(normalized_key):
            return True
        if (
            _WINDOWS_ABSOLUTE_PATH.search(decoded_item)
            or _UNC_ABSOLUTE_PATH.search(decoded_item)
            or _POSIX_ABSOLUTE_PATH.search(decoded_item)
            or _COMMON_POSIX_ROOT.search(decoded_item)
        ):
            return True
    return False


def _public_text(value: object) -> str:
    text = str(value)
    if not text.strip():
        return ""
    inspected_text = _decoded(text)
    if (
        _CREDENTIAL_ASSIGNMENT.search(inspected_text)
        or any(
            _is_credential_key(match.group(1))
            for match in _ASSIGNMENT_KEY.finditer(inspected_text)
        )
        or _WINDOWS_ABSOLUTE_PATH.search(inspected_text)
        or _UNC_ABSOLUTE_PATH.search(inspected_text)
        or _COMMON_POSIX_ROOT.search(inspected_text)
    ):
        return _REDACTED_METADATA

    # Inspect only non-web-URL spans for filesystem paths. This preserves
    # useful documentation links without a replace-and-restore sentinel that
    # attacker-controlled input could imitate.
    cursor = 0
    non_url_spans: list[str] = []
    for match in _WEB_URL.finditer(text):
        if _unsafe_web_url(match.group(0)):
            return _REDACTED_METADATA
        non_url_spans.append(text[cursor:match.start()])
        cursor = match.end()
    non_url_spans.append(text[cursor:])
    inspected = " ".join(non_url_spans)
    if (
        _WINDOWS_ABSOLUTE_PATH.search(inspected)
        or _UNC_ABSOLUTE_PATH.search(inspected)
        or _POSIX_ABSOLUTE_PATH.search(inspected)
        or _COMMON_POSIX_ROOT.search(inspected)
    ):
        return _REDACTED_METADATA
    return text


class PublicToolExecution(BaseModel):
    credentials: list[ToolCredentialRequirement] = Field(default_factory=list)


class PluginRuntimeToolResponse(BaseModel):
    name: str
    description: str
    scopes: list[str] = Field(default_factory=list)
    timeout_seconds: int
    plugin: str
    version: str
    execution: PublicToolExecution = Field(default_factory=PublicToolExecution)

    @classmethod
    def from_tool(cls, tool: PluginToolDefinition) -> "PluginRuntimeToolResponse":
        return cls(
            name=_public_text(tool.name),
            description=_public_text(tool.description),
            scopes=[_public_text(scope) for scope in tool.scopes],
            timeout_seconds=tool.timeout_seconds,
            plugin=_public_text(tool.plugin),
            version=_public_text(tool.version),
            execution=PublicToolExecution(credentials=list(tool.execution.credentials)),
        )


class PluginRuntimePluginResponse(BaseModel):
    plugin: str
    version: str
    manifest_digest: str
    tool_count: int
    tools: list[PluginRuntimeToolResponse] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def from_plugin(
        cls,
        plugin: PluginDescriptor,
        tools: list[PluginRuntimeToolResponse],
    ) -> "PluginRuntimePluginResponse":
        return cls(
            plugin=_public_text(plugin.plugin),
            version=_public_text(plugin.version),
            manifest_digest=_public_text(plugin.manifest_digest),
            tool_count=plugin.tool_count,
            tools=tools,
            # Reload is all-or-nothing, so every plugin in the active snapshot
            # is valid. Keep the field explicit for a stable WebUI contract.
            errors=[],
        )


class PluginRuntimeSnapshotResponse(BaseModel):
    engine: str
    version: str
    status: PluginRuntimeStatus
    healthy: bool
    revision: str
    manifest_digest: str
    execution_bundle_digest: str
    plugin_count: int
    tool_count: int
    plugins: list[PluginRuntimePluginResponse] = Field(default_factory=list)
    tools: list[PluginRuntimeToolResponse] = Field(default_factory=list)
    last_error: str | None = None

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PluginCatalogSnapshot,
        *,
        healthy: bool,
        status: PluginRuntimeStatus | None = None,
        last_error: object | None = None,
    ) -> "PluginRuntimeSnapshotResponse":
        tools = [PluginRuntimeToolResponse.from_tool(tool) for tool in snapshot.tools]
        tools_by_plugin: dict[str, list[PluginRuntimeToolResponse]] = {}
        for tool in tools:
            tools_by_plugin.setdefault(tool.plugin, []).append(tool)

        public_status: PluginRuntimeStatus = status or ("healthy" if healthy else "unavailable")
        return cls(
            engine=_public_text(snapshot.engine),
            version=_public_text(snapshot.version),
            status=public_status,
            healthy=healthy,
            revision=_public_text(snapshot.revision),
            manifest_digest=_public_text(snapshot.manifest_digest),
            execution_bundle_digest=_public_text(snapshot.execution_bundle_digest),
            plugin_count=snapshot.plugin_count,
            tool_count=snapshot.tool_count,
            plugins=[
                PluginRuntimePluginResponse.from_plugin(
                    plugin,
                    tools_by_plugin.get(_public_text(plugin.plugin), []),
                )
                for plugin in snapshot.plugins
            ],
            tools=tools,
            last_error=_public_text(last_error) if last_error is not None else None,
        )
