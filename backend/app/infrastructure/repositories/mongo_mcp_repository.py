import logging
from datetime import datetime, UTC
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig
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

    async def upsert_server(self, name: str, server_config: MCPServerConfig) -> MCPConfig:
        config = await self.get_mcp_config()
        config.mcpServers[name] = server_config
        await self.save_mcp_config(config)
        return config

    async def delete_server(self, name: str) -> MCPConfig:
        config = await self.get_mcp_config()
        config.mcpServers.pop(name, None)
        await self.save_mcp_config(config)
        return config
