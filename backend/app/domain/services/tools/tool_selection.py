"""Task-local views over one immutable, fully governed Cordis generation."""

import copy
import re
from typing import Any, Annotated

from langchain.tools import tool
from pydantic import Field

from app.domain.models.domain_preset import ToolSelectionMode
from app.domain.models.tool_result import ToolResult
from app.domain.services.domain_presets import get_domain_preset, list_domain_presets
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.plugin import PluginToolkit
from app.domain.services.tools.spill_projection import sanitize_spill_public_text


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_PLUGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_MAX_LOAD_BATCH = 8
_MAX_SEARCH_RESULTS = 12


class _SelectedTool:
    """Recheck selection at execution so cached handles cannot survive reset."""

    def __init__(self, view: "PluginToolView", resolved: Any):
        self._view = view
        self._resolved = resolved

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolved, name)

    async def ainvoke(self, tool_call: dict[str, Any]):
        if not self._view._is_loaded(self._resolved.name):
            raise ValueError("Tool is not loaded in this task view")
        # The existing PluginToolkit wrapper owns the entire execution pipeline
        # (permission, credential, timeout, spill, snapshot and cancellation).
        return await self._resolved.ainvoke(tool_call)


class PluginToolView:
    name = "plugin"

    def __init__(
        self,
        plugin_toolkit: PluginToolkit,
        *,
        preset_id: str = "general",
        selection_mode: ToolSelectionMode = "on_demand",
        max_loaded_tools: int = 48,
    ):
        if selection_mode not in {"all", "on_demand"}:
            raise ValueError("Invalid tool selection mode")
        if type(max_loaded_tools) is not int or not 8 <= max_loaded_tools <= 96:
            raise ValueError("Tool selection capacity must be between 8 and 96")
        self.plugin_toolkit = plugin_toolkit
        self.preset = get_domain_preset(preset_id)
        self.selection_mode = selection_mode
        self.max_loaded_tools = max_loaded_tools
        self.enabled = True
        self._schemas = {
            schema["function"]["name"]: copy.deepcopy(schema)
            for schema in plugin_toolkit.get_tools()
        }
        self._metadata = {}
        for name, schema in self._schemas.items():
            definition = plugin_toolkit.get_tool(name).definition
            if self.preset.id != "general" and definition["plugin"] not in self.preset.plugin_ids:
                continue
            self._metadata[name] = {
                "name": name,
                "plugin": definition["plugin"],
                "version": definition["version"],
                "description": sanitize_spill_public_text(schema["function"]["description"][:4000])[:600],
            }
        self._available = frozenset(self._metadata)
        # This scope is a fast-path *eligibility* marker, not an instruction to
        # eagerly load all plugins (most existing tools carry this marker).
        self.dataset_fast_path_tool_names = (
            plugin_toolkit.dataset_fast_path_tool_names & self._available
        )
        self._loaded: set[str] = set()
        self._selection_revision = 0
        self.reset()

    @property
    def tool_execution_pipeline(self):
        return self.plugin_toolkit.tool_execution_pipeline

    @property
    def loaded_tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._loaded))

    def _is_loaded(self, name: str) -> bool:
        return self.enabled and self.plugin_toolkit.enabled and name in self._loaded

    def set_enabled(self, enabled: bool) -> None:
        # This affects only the owning Agent's view, never the Cordis runtime.
        self.enabled = enabled

    def reset(self) -> None:
        self._loaded = set(self._available if self.selection_mode == "all" else (
            self._available.intersection(self.preset.initial_tools)
        ))
        self._selection_revision += 1

    def get_tools(self) -> list[Any]:
        if not self.enabled or not self.plugin_toolkit.enabled:
            return []
        return [copy.deepcopy(self._schemas[name]) for name in sorted(self._loaded)]

    def get_tool(self, tool_name: str) -> Any | None:
        if not self._is_loaded(tool_name):
            return None
        resolved = self.plugin_toolkit.get_tool(tool_name)
        return _SelectedTool(self, resolved) if resolved is not None else None

    def selection_snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "preset_id": self.preset.id,
            "selection_mode": self.selection_mode,
            "selection_revision": self._selection_revision,
            "catalog_revision": self.plugin_toolkit.catalog_revision,
            "available_tool_count": len(self._available),
            "loaded_tool_count": len(self._loaded),
            "loaded_tool_names": list(self.loaded_tool_names),
            "max_loaded_tools": self.max_loaded_tools,
        }

    def _public_tool(self, name: str) -> dict[str, Any]:
        return {**self._metadata[name], "loaded": name in self._loaded}

    def search(self, query: str = "", plugin_id: str | None = None, limit: int = 8) -> ToolResult:
        if not self.enabled or not self.plugin_toolkit.enabled:
            return ToolResult(success=False, message="Tool catalog is unavailable")
        if not isinstance(query, str) or len(query) > 160 or type(limit) is not int or not 1 <= limit <= _MAX_SEARCH_RESULTS:
            return ToolResult(success=False, message="Invalid bounded catalog search")
        if plugin_id is not None and (not isinstance(plugin_id, str) or not _PLUGIN.fullmatch(plugin_id)):
            return ToolResult(success=False, message="Invalid plugin identifier")
        tokens = query.casefold().split()
        domain_terms: dict[str, str] = {}
        for preset in list_domain_presets():
            for plugin in preset.plugin_ids:
                domain_terms[plugin] = domain_terms.get(plugin, "") + f" {preset.name} {preset.description} {preset.id}"
        matches = []
        for name, item in self._metadata.items():
            if plugin_id is not None and item["plugin"] != plugin_id:
                continue
            haystack = f"{name} {item['plugin']} {item['description']} {domain_terms.get(item['plugin'], '')}".casefold()
            score = sum(3 if token in name.casefold() else 1 for token in tokens if token in haystack)
            if tokens and not score:
                continue
            matches.append((-score, name not in self.preset.initial_tools, name))
        matches.sort()
        return ToolResult(success=True, data={
            "preset_id": self.preset.id,
            "catalog_revision": self.plugin_toolkit.catalog_revision,
            "matches": [self._public_tool(name) for _, _, name in matches[:limit]],
            "total_matches": len(matches),
            "has_more": len(matches) > limit,
            "loaded_tool_count": len(self._loaded),
        })

    def load(self, tool_names: list[str]) -> ToolResult:
        if not self.enabled or not self.plugin_toolkit.enabled:
            return ToolResult(success=False, message="Tool catalog is unavailable")
        if not isinstance(tool_names, list) or not 1 <= len(tool_names) <= _MAX_LOAD_BATCH or any(
            not isinstance(name, str) or not _NAME.fullmatch(name) for name in tool_names
        ):
            return ToolResult(success=False, message="Load between 1 and 8 valid tool names")
        requested = set(tool_names)
        if not requested.issubset(self._available):
            return ToolResult(success=False, message="Requested tools are unavailable in this preset")
        updated = self._loaded | requested
        if self.selection_mode == "on_demand" and len(updated) > self.max_loaded_tools:
            return ToolResult(success=False, message="Task tool-selection capacity reached", data={
                "max_loaded_tools": self.max_loaded_tools, "loaded_tool_count": len(self._loaded),
            })
        if updated != self._loaded:
            self._loaded = updated
            self._selection_revision += 1
        return ToolResult(success=True, message="Tools loaded; their schemas are available on the next model turn", data={
            "tools": [self._public_tool(name) for name in sorted(requested)],
            "loaded_tool_count": len(self._loaded),
            "selection_revision": self._selection_revision,
        })


class ToolDiscoveryToolkit(BaseToolkit):
    name: str = "tool_catalog"

    def __init__(self, view: PluginToolView):
        super().__init__()
        self.view = view
        self.dataset_fast_path_tool_names = {"tool_catalog_search", "tool_catalog_load"}

    @tool
    async def tool_catalog_search(
        self,
        query: Annotated[str, Field(max_length=160)] = "",
        plugin_id: Annotated[str | None, Field(max_length=80)] = None,
        limit: Annotated[int, Field(ge=1, le=12)] = 8,
    ) -> ToolResult:
        """Search trusted plugin descriptions in the current domain preset. Results are metadata, not executable code or permission grants. Use precise tool names or keywords; load selected tools before calling them."""
        return self.view.search(query=query, plugin_id=plugin_id, limit=limit)

    @tool
    async def tool_catalog_load(
        self,
        tool_names: Annotated[list[str], Field(min_length=1, max_length=8)],
    ) -> ToolResult:
        """Load up to 8 named plugin tools for this task only. Their parameter schemas become available on the next model turn. Loading never invokes tools, enables plugins globally, or grants permissions."""
        return self.view.load(tool_names)
