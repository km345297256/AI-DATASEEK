from __future__ import annotations

from typing import Protocol

from app.domain.external.plugin_runtime import PluginRuntimeUnavailableError
from app.domain.models.visualization import (
    VisualizationCatalog, VisualizationPlugin, VisualizationPluginState, VisualizationSnapshot,
)


class VisualizationNotFoundError(LookupError):
    pass


class VisualizationDisabledError(PermissionError):
    pass


class VisualizationRuntime(Protocol):
    async def visualization_snapshot(self) -> VisualizationSnapshot: ...


class VisualizationPreferences(Protocol):
    async def get_states(self, user_id: str) -> dict[str, bool]: ...
    async def set_state(self, user_id: str, plugin_id: str, enabled: bool) -> None: ...


class VisualizationCatalogService:
    def __init__(self, runtime: VisualizationRuntime | None, repository: VisualizationPreferences):
        self.runtime = runtime
        self.repository = repository

    async def _snapshot(self) -> VisualizationSnapshot:
        if self.runtime is None or not callable(getattr(self.runtime, "visualization_snapshot", None)):
            raise PluginRuntimeUnavailableError("Cordis visualization runtime is unavailable")
        # Deliberately no builtin-list fallback or stale process-wide cache.
        return await self.runtime.visualization_snapshot()

    async def list_for_user(self, user_id: str) -> VisualizationCatalog:
        snapshot = await self._snapshot()
        preferences = await self.repository.get_states(user_id)
        return VisualizationCatalog(revision=snapshot.revision, plugins=[
            VisualizationPluginState(**plugin.model_dump(), enabled=preferences.get(plugin.id, plugin.default_enabled))
            for plugin in snapshot.plugins
        ])

    async def require_enabled(self, user_id: str, plugin_id: str) -> VisualizationPlugin:
        snapshot = await self._snapshot()
        plugin = next((item for item in snapshot.plugins if item.id == plugin_id), None)
        if plugin is None:
            raise VisualizationNotFoundError("Visualization plugin not found")
        states = await self.repository.get_states(user_id)
        if not states.get(plugin_id, plugin.default_enabled):
            raise VisualizationDisabledError("Visualization plugin is disabled")
        return plugin.model_copy(deep=True)

    async def set_state(self, user_id: str, plugin_id: str, enabled: bool) -> VisualizationPluginState:
        snapshot = await self._snapshot()
        plugin = next((item for item in snapshot.plugins if item.id == plugin_id), None)
        if plugin is None:
            raise VisualizationNotFoundError("Visualization plugin not found")
        await self.repository.set_state(user_id, plugin_id, enabled)
        return VisualizationPluginState(**plugin.model_dump(), enabled=enabled)
