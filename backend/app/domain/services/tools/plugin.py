import asyncio
import base64
import json
import logging
import os
import shlex
import uuid
from urllib.parse import quote
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft7Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from langchain.messages import ToolMessage

from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginRuntime,
    PluginToolDefinition,
)
from app.domain.external.sandbox import Sandbox
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit


logger = logging.getLogger(__name__)

_MAX_NORMALIZED_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_PATTERN_INSTANCE_STRING_CHARS = 4096
_JSON_SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
_PUBLIC_SCHEMA_PATH_KEYWORDS = frozenset({
    "$defs",
    "$ref",
    "$schema",
    "additionalItems",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "contains",
    "dependencies",
    "definitions",
    "else",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "if",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "patternProperties",
    "properties",
    "propertyNames",
    "required",
    "then",
    "type",
    "uniqueItems",
})


def _tool_name(value: Any) -> str | None:
    if isinstance(value, dict):
        function = value.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        return None
    name = getattr(value, "name", None)
    return name if isinstance(name, str) and name else None


def default_plugin_directory() -> Path:
    configured = os.getenv("TOOL_PLUGINS_DIR")
    if configured:
        return Path(configured)
    packaged = Path("/opt/ai-dataseek/tools")
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[5] / "tools"


class _PluginToolWrapper:
    def __init__(self, definition: dict[str, Any], toolkit: "PluginToolkit"):
        self.name = definition["name"]
        # The toolkit captures one immutable Cordis catalog generation. Keep
        # the complete normalized v2 descriptor on the resolved tool so the
        # execution policy and event projection consume that same generation.
        self.definition = definition
        self.execution_contract = definition.get("execution") or {}
        self.output_schema = definition.get("output_schema")
        self.presentation = definition.get("presentation") or {"kind": "auto"}
        self.toolkit = toolkit

    async def ainvoke(self, tool_call: dict[str, Any]) -> ToolMessage:
        async def execute(context):
            execution_session_id = self.toolkit.create_execution_session_id()
            if any(
                callable(getattr(self.toolkit.sandbox, method, None))
                for method in ("kill_process", "release_shell")
            ):
                async def cancel_sandbox_process(_reason: str) -> None:
                    await self.toolkit._cancel_execution_session(
                        execution_session_id
                    )

                # Keep the callback registered until the invocation context is
                # released. On an asyncio timeout/cancellation, the outer
                # interceptor runs it after the cancelled call stack unwinds.
                context.register_cancellation_callback(cancel_sandbox_process)
            private = ({"credential_values": context.metadata["credential_values"]}
                       if context.metadata.get("credential_values") else {})
            result = await self.toolkit.call_tool(
                self.name,
                context.arguments,
                execution_session_id=execution_session_id,
                **private,
            )
            return ToolMessage(
                tool_call_id=context.tool_call_id,
                name=self.name,
                content=result.model_dump_json(),
                artifact=result,
            )

        return await self.toolkit.tool_execution_pipeline.invoke(
            tool=self,
            tool_call=tool_call,
            execute=execute,
            metadata={
                "contract_version": self.definition.get("contract_version", 2),
                "plugin": self.definition.get("plugin", ""),
                "plugin_version": self.definition.get("version", ""),
                "execution_contract": self.execution_contract,
                "output_schema": self.output_schema,
                "presentation": self.presentation,
                "session_id": self.toolkit.session_id,
            },
        )


class PluginToolkit(BaseToolkit):
    """Discover trusted sandbox tools from declarative plugin manifests."""

    name: str = "plugin"

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        session_id: str,
        plugins_dir: Optional[Path] = None,
        plugin_runtime: Optional[PluginRuntime] = None,
    ):
        super().__init__()
        self.sandbox = sandbox
        self.session_id = session_id
        self.plugin_runtime = plugin_runtime
        if plugin_runtime is None:
            # Explicit compatibility path for isolated tests and controlled
            # rollbacks. Production injects the Cordis runtime.
            self.plugins_dir = (plugins_dir or default_plugin_directory()).resolve()
            self._definitions = self._load_definitions()
            self.catalog_revision = "legacy-filesystem"
            self.catalog_manifest_digest: str | None = None
            self.execution_bundle_digest: str | None = None
            self.catalog_snapshot: PluginCatalogSnapshot | None = None
        else:
            # Capture exactly one immutable generation. Existing Agent/tool
            # calls never observe a half-reloaded catalog; the next task gets
            # the next generation. An unhealthy host returns an empty snapshot.
            self.plugins_dir = None
            snapshot = plugin_runtime.current_snapshot
            self.catalog_snapshot = snapshot
            self._definitions = self._definitions_from_snapshot(snapshot)
            self.catalog_revision = snapshot.revision
            self.catalog_manifest_digest = snapshot.manifest_digest or None
            self.execution_bundle_digest = snapshot.execution_bundle_digest or None
        self._contract_validators: dict[tuple[str, str], Any] = {}
        self._schemas = [self._openai_schema(item) for item in self._definitions.values()]
        self.dataset_fast_path_tool_names = {
            name
            for name, item in self._definitions.items()
            if "dataset_fast_path" in item.get("scopes", [])
        }

    @staticmethod
    def _definitions_from_snapshot(
        snapshot: PluginCatalogSnapshot,
    ) -> dict[str, dict[str, Any]]:
        return {
            tool.name: tool.as_legacy_definition()
            for tool in snapshot.tools
        }

    def _load_definitions(self) -> dict[str, dict[str, Any]]:
        definitions: dict[str, dict[str, Any]] = {}
        if not self.plugins_dir.is_dir():
            logger.info("Tool plugin directory is unavailable: %s", self.plugins_dir)
            return definitions
        for manifest_path in sorted(self.plugins_dir.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid tool plugin manifest {manifest_path}: {exc}") from exc
            plugin_name = manifest.get("plugin")
            plugin_version = manifest.get("version")
            tools = manifest.get("tools")
            if not isinstance(plugin_name, str) or not plugin_name.strip():
                raise ValueError(f"Tool plugin manifest has no plugin name: {manifest_path}")
            if not isinstance(tools, list) or not tools:
                raise ValueError(f"Tool plugin manifest has no tools: {manifest_path}")
            if not isinstance(plugin_version, str) or not plugin_version.strip():
                # Pre-Cordis local test fixtures did not require a version.
                # This path is an explicit rollback/testing adapter only.
                plugin_version = "legacy"
            for item in tools:
                if not isinstance(item, dict):
                    raise ValueError(f"Invalid tool definition in {manifest_path}")
                name = item.get("name")
                description = item.get("description")
                parameters = item.get("parameters")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"Tool definition has no name in {manifest_path}")
                if name in definitions:
                    raise ValueError(f"Duplicate plugin tool name: {name}")
                if not isinstance(description, str) or not description.strip():
                    raise ValueError(f"Plugin tool {name} has no description")
                if not isinstance(parameters, dict) or parameters.get("type") != "object":
                    raise ValueError(f"Plugin tool {name} has an invalid parameter schema")
                legacy_timeout = item.get("timeout_seconds", 90)
                execution = item.get("execution")
                if execution is None:
                    execution = {}
                if not isinstance(execution, dict):
                    raise ValueError(f"Plugin tool {name} has an invalid execution contract")
                normalized_execution = {
                    "timeout_seconds": execution.get("timeout_seconds", legacy_timeout),
                    "cancellable": execution.get("cancellable", False),
                    "concurrency": execution.get("concurrency", "exclusive"),
                    "effects": execution.get(
                        "effects", ["sandbox_read", "sandbox_write"]
                    ),
                    "permissions": execution.get("permissions", []),
                    "credentials": execution.get("credentials", []),
                }
                try:
                    definition = PluginToolDefinition.model_validate({
                        **item,
                        "contract_version": item.get("contract_version", 2),
                        "output_schema": item.get("output_schema"),
                        "execution": normalized_execution,
                        "presentation": item.get("presentation") or {"kind": "auto"},
                        "timeout_seconds": normalized_execution["timeout_seconds"],
                        "plugin": plugin_name,
                        "version": plugin_version,
                    }).as_legacy_definition()
                except ValueError as exc:
                    raise ValueError(
                        f"Plugin tool {name} has an invalid v2 contract: {exc}"
                    ) from exc
                definitions[name] = definition
        return definitions

    @staticmethod
    def _openai_schema(definition: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": definition["name"],
                "description": definition["description"],
                "parameters": definition["parameters"],
            },
        }

    def get_tools(self) -> list[Any]:
        return self._schemas if self.enabled else []

    def get_tool(self, tool_name: str) -> Optional[_PluginToolWrapper]:
        definition = self._definitions.get(tool_name)
        if not self.enabled or definition is None:
            return None
        return _PluginToolWrapper(definition, self)

    def assert_no_tool_name_collisions(self, other_toolkits: list[Any]) -> None:
        """Fail before an Agent turn if a plugin shadows another toolkit.

        Cordis rejects stable core names during catalog validation. This live
        check additionally covers dynamic providers such as MCP without
        changing the existing first-match lookup contract.
        """
        plugin_names = set(self._definitions)
        collisions = sorted({
            name
            for toolkit in other_toolkits
            for item in toolkit.get_tools()
            if (name := _tool_name(item)) in plugin_names
        })
        if collisions:
            rendered = ", ".join(collisions[:8])
            if len(collisions) > 8:
                rendered += ", ..."
            raise ValueError(
                f"Cordis plugin tool names collide with Agent tools: {rendered}"
            )

    def create_execution_session_id(self) -> str:
        """Allocate one isolated shell channel for one plugin invocation."""
        return str(uuid.uuid4())

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_session_id: str | None = None,
        credential_values: dict[str, str] | None = None,
    ) -> ToolResult:
        definition = self._definitions.get(tool_name)
        if definition is None:
            return ToolResult(success=False, message=f"Unknown plugin tool: {tool_name}")
        requirements = (definition.get("execution") or {}).get("credentials", [])
        expected_slots = {item["slot"] for item in requirements}
        if expected_slots != set(credential_values or {}):
            return ToolResult(success=False, message="Required credential bindings are unavailable",
                              data={"status": "credential_unavailable"})
        input_error = self._contract_error(
            tool_name,
            "input",
            definition.get("parameters"),
            arguments,
        )
        if input_error is not None:
            return ToolResult(
                success=False,
                message="Plugin tool arguments do not satisfy the declared contract",
                data=input_error,
            )
        payload = base64.urlsafe_b64encode(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        execution = definition.get("execution")
        declared_timeout = (
            execution.get("timeout_seconds", definition.get("timeout_seconds", 90))
            if isinstance(execution, dict)
            else definition.get("timeout_seconds", 90)
        )
        timeout = max(1, min(int(declared_timeout), 120))
        catalog_guard = (
            "--catalog-manifest-digest "
            f"{shlex.quote(self.catalog_manifest_digest)} "
            if self.catalog_manifest_digest
            else ""
        )
        execution_guard = (
            "--execution-bundle-digest "
            f"{shlex.quote(self.execution_bundle_digest)} "
            if self.execution_bundle_digest
            else ""
        )
        command = (
            f"ai-dataseek-tool run {shlex.quote(tool_name)} "
            f"{catalog_guard}"
            f"{execution_guard}"
            f"--arguments-base64 {shlex.quote(payload)}"
        )
        execution_session_id = (
            execution_session_id or self.create_execution_session_id()
        )
        try:
            if credential_values:
                execute_private = getattr(self.sandbox, "exec_command_with_credentials", None)
                if not callable(execute_private):
                    raise RuntimeError("Sandbox does not support private credential transport")
                result = await execute_private(execution_session_id, "/home/ubuntu", command, credential_values)
            else:
                result = await self.sandbox.exec_command(execution_session_id, "/home/ubuntu", command)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Plugin tool transport failed tool=%s error_type=%s",
                tool_name,
                type(error).__name__,
            )
            return await self._released_result(
                execution_session_id,
                self._transport_error(tool_name),
            )
        data = self._result_data(result)
        status = data.get("status")
        if status not in {"running", "completed"}:
            return await self._released_result(
                execution_session_id,
                self._transport_error(tool_name),
            )
        returncode = data.get("returncode")
        output = data.get("output", "")
        transport_success = result.success
        if status == "running":
            # The production timeout interceptor owns the exact deadline. Give
            # the sandbox wait one extra second so that interceptor wins
            # deterministically and runs its kill callback. This fallback also
            # stops the process if the toolkit is invoked outside production.
            try:
                waited = await self.sandbox.wait_for_process(
                    execution_session_id,
                    timeout + 1,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Plugin tool wait failed tool=%s error_type=%s",
                    tool_name,
                    type(error).__name__,
                )
                return await self._released_result(
                    execution_session_id,
                    self._transport_error(tool_name),
                )
            wait_data = self._result_data(waited)
            if wait_data.get("status") != "completed":
                return await self._released_result(
                    execution_session_id,
                    ToolResult(
                        success=False,
                        message=(
                            f"Plugin tool exceeded its {timeout} second execution limit"
                        ),
                        data={"status": "timed_out", "tool": tool_name},
                    ),
                )
            try:
                viewed = await self.sandbox.view_shell(execution_session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Plugin tool output retrieval failed tool=%s error_type=%s",
                    tool_name,
                    type(error).__name__,
                )
                return await self._released_result(
                    execution_session_id,
                    self._transport_error(tool_name),
                )
            view_data = self._result_data(viewed)
            returncode = wait_data.get("returncode")
            output = view_data.get("output", "")
            transport_success = waited.success and viewed.success

        if self._output_exceeds_budget(output):
            return await self._released_result(
                execution_session_id,
                ToolResult(
                    success=False,
                    message="Plugin tool output exceeded the in-memory contract limit",
                    data={
                        "status": "contract_rejected",
                        "returncode": returncode,
                        "contract_error": {
                            "error": "tool_output_size_limit",
                            "path": [],
                        },
                    },
                ),
            )

        # Quarantine echoed secrets before schema errors, model context, SSE or
        # Spill sees the output. The shell channel is private and is released
        # before returning. Trusted handlers must not write secrets to files.
        output = self._redact_credentials(output, credential_values or {})
        normalized_output = self._normalize_output(output)
        output_error = self._contract_error(
            tool_name,
            "output",
            definition.get("output_schema"),
            normalized_output,
        )
        success = returncode == 0 and transport_success and output_error is None
        if output_error is not None:
            # A rejected payload is quarantined at the contract boundary. It
            # must not enter model context, SSE, or a declarative card.
            return await self._released_result(
                execution_session_id,
                ToolResult(
                    success=False,
                    message="Plugin tool output violated its declared contract",
                    data={
                        "status": "contract_rejected",
                        "returncode": returncode,
                        "contract_error": output_error,
                    },
                ),
            )
        final_result = ToolResult(
            success=success,
            message=(
                f"Plugin tool {tool_name} completed"
                if success
                else f"Plugin tool {tool_name} failed with exit code {returncode}"
            ),
            # Keep the legacy status/returncode/output envelope for existing
            # deterministic scientific consumers while adding the v2 parsed
            # result. Command text and session identifiers are intentionally
            # omitted so base64-encoded arguments never enter model context.
            data={
                "status": "completed",
                "returncode": returncode,
                "output": output,
                "result": normalized_output,
            },
        )
        return await self._released_result(execution_session_id, final_result)

    @staticmethod
    def _redact_credentials(value: Any, credentials: dict[str, str]) -> Any:
        if not credentials:
            return value
        variants: set[str] = set()
        for secret in credentials.values():
            variants.update({secret, quote(secret, safe=""), json.dumps(secret, ensure_ascii=True)[1:-1],
                             json.dumps(secret, ensure_ascii=False)[1:-1],
                             base64.b64encode(secret.encode()).decode(),
                             base64.urlsafe_b64encode(secret.encode()).decode()})
        def redact(item):
            if isinstance(item, str):
                for secret in sorted(variants, key=len, reverse=True):
                    item = item.replace(secret, "[redacted credential]")
                return item
            if isinstance(item, dict):
                return {redact(key): redact(child) for key, child in item.items()}
            if isinstance(item, list):
                return [redact(child) for child in item]
            return item
        return redact(value)

    async def _released_result(
        self,
        execution_session_id: str,
        result: ToolResult,
    ) -> ToolResult:
        await self._release_execution_session(execution_session_id)
        return result

    async def _release_execution_session(
        self,
        execution_session_id: str,
    ) -> None:
        release_shell = getattr(self.sandbox, "release_shell", None)
        if callable(release_shell):
            try:
                await release_shell(execution_session_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Failed to release plugin shell error_type=%s",
                    type(error).__name__,
                )
        kill_process = getattr(self.sandbox, "kill_process", None)
        if not callable(kill_process):
            return
        try:
            await kill_process(execution_session_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Failed to stop plugin shell error_type=%s",
                type(error).__name__,
            )

    async def _cancel_execution_session(
        self,
        execution_session_id: str,
    ) -> None:
        """Stop execution first, then best-effort discard its private shell."""
        kill_process = getattr(self.sandbox, "kill_process", None)
        if callable(kill_process):
            try:
                await kill_process(execution_session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Failed to cancel plugin shell error_type=%s",
                    type(error).__name__,
                )

        release_shell = getattr(self.sandbox, "release_shell", None)
        if not callable(release_shell):
            return
        try:
            await release_shell(execution_session_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Failed to discard cancelled plugin shell error_type=%s",
                type(error).__name__,
            )

    @staticmethod
    def _normalize_output(output: Any) -> Any:
        if not isinstance(output, str):
            return output
        stripped = output.strip()
        if not stripped:
            return ""
        try:
            return json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return output

    @staticmethod
    def _output_exceeds_budget(output: Any) -> bool:
        return (
            isinstance(output, str)
            and len(output.encode("utf-8")) > _MAX_NORMALIZED_OUTPUT_BYTES
        )

    @staticmethod
    def _transport_error(tool_name: str) -> ToolResult:
        return ToolResult(
            success=False,
            message=f"Plugin tool {tool_name} transport failed",
            data={"status": "transport_error", "tool": tool_name},
        )

    def _contract_error(
        self,
        tool_name: str,
        direction: str,
        schema: Any,
        value: Any,
    ) -> dict[str, Any] | None:
        if schema is None:
            return None
        if not isinstance(schema, dict):
            return {
                "error": f"tool_{direction}_contract_invalid",
                "path": [],
            }
        dialect = schema.get("$schema")
        if dialect is not None and dialect != _JSON_SCHEMA_DIALECT:
            return {
                "error": f"tool_{direction}_contract_invalid",
                "path": ["$schema"],
            }
        if (
            self._schema_uses_patterns(schema)
            and self._pattern_candidate_exceeds_budget(value)
        ):
            return {
                "error": f"tool_{direction}_contract_violation",
                "path": ["pattern"],
            }
        key = (tool_name, direction)
        validator = self._contract_validators.get(key)
        if validator is None:
            try:
                Draft7Validator.check_schema(schema)
                validator = Draft7Validator(schema)
            except Exception as error:
                logger.warning(
                    "Plugin tool contract is invalid tool=%s direction=%s error_type=%s",
                    tool_name,
                    direction,
                    type(error).__name__,
                )
                return {
                    "error": f"tool_{direction}_contract_invalid",
                    "path": [],
                }
            self._contract_validators[key] = validator
        try:
            validator.validate(value)
        except JsonSchemaValidationError as error:
            # Report only the schema path, never the rejected value or message;
            # arguments and outputs can contain paths or credentials.
            path = self._public_schema_path(error.absolute_schema_path)
            logger.info(
                "Plugin tool contract rejected tool=%s direction=%s path=%s",
                tool_name,
                direction,
                ".".join(path),
            )
            return {
                "error": f"tool_{direction}_contract_violation",
                "path": path,
            }
        except Exception as error:
            # Reference lookup and regex evaluation can still fail at runtime
            # if a non-Cordis adapter or stale object mutates the cached
            # descriptor. Treat this as a non-retryable contract defect and do
            # not expose the validator's message (which may include values).
            logger.warning(
                "Plugin tool contract evaluation failed tool=%s direction=%s error_type=%s",
                tool_name,
                direction,
                type(error).__name__,
            )
            return {
                "error": f"tool_{direction}_contract_invalid",
                "path": [],
            }
        return None

    @staticmethod
    def _schema_uses_patterns(schema: dict[str, Any]) -> bool:
        pending: list[Any] = [schema]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                identity = id(current)
                if identity in seen:
                    continue
                seen.add(identity)
                if "pattern" in current or current.get("patternProperties"):
                    return True
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return False

    @staticmethod
    def _pattern_candidate_exceeds_budget(value: Any) -> bool:
        """Bound every possible regex candidate before stdlib ``re`` runs."""
        pending: list[Any] = [value]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if isinstance(current, str):
                if len(current) > _MAX_PATTERN_INSTANCE_STRING_CHARS:
                    return True
                continue
            if isinstance(current, dict):
                identity = id(current)
                if identity in seen:
                    return True
                seen.add(identity)
                for key, nested in current.items():
                    if (
                        isinstance(key, str)
                        and len(key) > _MAX_PATTERN_INSTANCE_STRING_CHARS
                    ):
                        return True
                    pending.append(nested)
                continue
            if isinstance(current, (list, tuple)):
                identity = id(current)
                if identity in seen:
                    return True
                seen.add(identity)
                pending.extend(current)
        return False

    @staticmethod
    def _public_schema_path(path: Any) -> list[str]:
        """Expose validator structure, never extension-controlled field text."""
        return [
            str(item)
            if isinstance(item, int)
            else item
            if isinstance(item, str) and item in _PUBLIC_SCHEMA_PATH_KEYWORDS
            else "[field]"
            for item in list(path)[:16]
        ]

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return result.data if isinstance(result.data, dict) else {}
