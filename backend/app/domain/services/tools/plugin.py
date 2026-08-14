import base64
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Optional

from langchain.messages import ToolMessage

from app.domain.external.sandbox import Sandbox
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit


logger = logging.getLogger(__name__)


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
        self.toolkit = toolkit

    async def ainvoke(self, tool_call: dict[str, Any]) -> ToolMessage:
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}
        tool_call_id = tool_call.get("id", "") if isinstance(tool_call, dict) else ""
        result = await self.toolkit.call_tool(self.name, args)
        return ToolMessage(
            tool_call_id=tool_call_id,
            name=self.name,
            content=result.model_dump_json(),
            artifact=result,
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
    ):
        super().__init__()
        self.sandbox = sandbox
        self.session_id = session_id
        self.plugins_dir = (plugins_dir or default_plugin_directory()).resolve()
        self._definitions = self._load_definitions()
        self._schemas = [self._openai_schema(item) for item in self._definitions.values()]
        self.dataset_fast_path_tool_names = {
            name
            for name, item in self._definitions.items()
            if "dataset_fast_path" in item.get("scopes", [])
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
            tools = manifest.get("tools")
            if not isinstance(plugin_name, str) or not plugin_name.strip():
                raise ValueError(f"Tool plugin manifest has no plugin name: {manifest_path}")
            if not isinstance(tools, list) or not tools:
                raise ValueError(f"Tool plugin manifest has no tools: {manifest_path}")
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
                definition = dict(item)
                definition["plugin"] = plugin_name
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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        definition = self._definitions.get(tool_name)
        if definition is None:
            return ToolResult(success=False, message=f"Unknown plugin tool: {tool_name}")
        payload = base64.urlsafe_b64encode(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        timeout = max(1, min(int(definition.get("timeout_seconds", 90)), 120))
        command = (
            f"ai-dataseek-tool run {shlex.quote(tool_name)} "
            f"--arguments-base64 {shlex.quote(payload)}"
        )
        result = await self.sandbox.exec_command(self.session_id, "/home/ubuntu", command)
        data = self._result_data(result)
        if data.get("status") != "running":
            return result
        waited = await self.sandbox.wait_for_process(self.session_id, timeout)
        wait_data = self._result_data(waited)
        if wait_data.get("status") != "completed":
            return ToolResult(
                success=waited.success,
                message=f"Plugin tool is still running after {timeout} seconds",
                data={"status": "running", "tool": tool_name, "returncode": None},
            )
        viewed = await self.sandbox.view_shell(self.session_id)
        view_data = self._result_data(viewed)
        returncode = wait_data.get("returncode")
        return ToolResult(
            success=returncode == 0 and viewed.success,
            message=(
                f"Plugin tool {tool_name} completed"
                if returncode == 0
                else f"Plugin tool {tool_name} failed with exit code {returncode}"
            ),
            data={
                "session_id": self.session_id,
                "command": command,
                "status": "completed",
                "returncode": returncode,
                "output": view_data.get("output", ""),
            },
        )

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return result.data if isinstance(result.data, dict) else {}
