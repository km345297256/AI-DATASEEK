import json
import os
import logging
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig
from app.core.config import get_settings

logger = logging.getLogger(__name__)

class FileMCPRepository(MCPRepository):
    """Repository for MCP config stored in a file"""

    def _get_file_path(self) -> str:
        return get_settings().mcp_config_path
    
    async def get_mcp_config(self) -> MCPConfig:
        """Get the MCP config from the file"""
        file_path = self._get_file_path()
        if not os.path.exists(file_path):
            return MCPConfig(mcpServers={})
        try:
            with open(file_path, "r") as file:
                return MCPConfig.model_validate_json(file.read())
        except Exception as e:
            logger.exception(f"Error reading MCP config file: {e}")
        
        return MCPConfig(mcpServers={})

    async def save_mcp_config(self, config: MCPConfig) -> None:
        """Persist the MCP config to the file."""
        file_path = self._get_file_path()
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(config.model_dump(mode="json"), file, ensure_ascii=False, indent=2)

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
