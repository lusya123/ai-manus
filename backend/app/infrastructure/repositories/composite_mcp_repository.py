from typing import Optional

from app.application.services.connector_service import ConnectorService
from app.domain.models.mcp_config import MCPConfig


class ConnectorMCPRepository:
    """MCP servers for one user: enabled Mongo connectors only."""

    def __init__(self, connector_service: ConnectorService, legacy_repository=None):
        self._connector_service = connector_service
        self._legacy_repository = legacy_repository

    async def get_mcp_config(self, user_id: Optional[str] = None) -> MCPConfig:
        legacy = await self._legacy_repository.get_mcp_config() if self._legacy_repository else MCPConfig()
        if not user_id:
            return legacy
        personal = await self._connector_service.mcp_config_for_user(user_id)
        servers = dict(legacy.mcpServers)
        for key, config in personal.mcpServers.items():
            candidate = key
            while candidate in servers:
                candidate = "user_" + candidate
            servers[candidate] = config
        return MCPConfig(mcpServers=servers)
