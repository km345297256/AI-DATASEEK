import logging
from copy import deepcopy
from datetime import datetime, UTC
from pymongo.errors import DuplicateKeyError
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPConfigurationConflict
from app.infrastructure.models.documents import MCPConfigDocument

logger = logging.getLogger(__name__)

_CONFIG_ID = "global"


class MongoMCPRepository(MCPRepository):
    async def get_mcp_config(self) -> MCPConfig:
        doc = await MCPConfigDocument.find_one(MCPConfigDocument.config_id == _CONFIG_ID)
        if not doc:
            return MCPConfig(mcpServers={})
        return MCPConfig.model_validate({"mcpServers": doc.servers})

    async def save_mcp_config(self, config: MCPConfig) -> None:
        servers = config.model_dump(mode="json")["mcpServers"]
        doc = await MCPConfigDocument.find_one(MCPConfigDocument.config_id == _CONFIG_ID)
        if doc:
            doc.servers = servers
            doc.updated_at = datetime.now(UTC)
            await doc.save()
        else:
            await MCPConfigDocument(config_id=_CONFIG_ID, servers=servers).insert()

    async def upsert_server(
        self, name: str, server_config: MCPServerConfig, *, expected: MCPServerConfig | None,
    ) -> MCPConfig:
        return await self._mutate_server(name, server_config, expected=expected)

    async def delete_server(self, name: str, *, expected: MCPServerConfig | None) -> MCPConfig:
        return await self._mutate_server(name, None, expected=expected)

    async def _mutate_server(
        self, name: str, replacement: MCPServerConfig | None, *, expected: MCPServerConfig | None,
    ) -> MCPConfig:
        """Compare-and-set the state whose ownership the caller authorized.

        Compare the raw aggregate, without interpolating names into Mongo paths.
        This also preserves legacy records with omitted default fields and avoids
        lost updates to unrelated servers. A conflict requires a fresh user action.
        """
        doc = await MCPConfigDocument.find_one({"config_id": _CONFIG_ID})
        original = deepcopy(doc.servers) if doc else {}
        config = MCPConfig.model_validate({"mcpServers": original})
        if config.mcpServers.get(name) != expected:
            raise MCPConfigurationConflict("MCP configuration changed")
        if replacement is None and name not in config.mcpServers:
            return config
        updated = deepcopy(original)
        if replacement is None:
            updated.pop(name, None)
            config.mcpServers.pop(name, None)
        else:
            updated[name] = replacement.model_dump(mode="json")
            config.mcpServers[name] = replacement
        if doc is None:
            try:
                await MCPConfigDocument(config_id=_CONFIG_ID, servers=updated).insert()
            except DuplicateKeyError:
                raise MCPConfigurationConflict("MCP configuration changed") from None
        else:
            result = await MCPConfigDocument.find_one({
                "config_id": _CONFIG_ID,
                "$expr": {"$eq": ["$servers", {"$literal": original}]},
            }).update({"$set": {"servers": updated, "updated_at": datetime.now(UTC)}})
            if not result or result.matched_count != 1:
                raise MCPConfigurationConflict("MCP configuration changed")
        return config
