from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .client import MCPClient
from .config import MCPServerConfig
from .types import MCPToolCallResult


ClientFactory = Callable[[MCPServerConfig], MCPClient]
CLIENT_INVALIDATION_ERROR_CODES = {
    "mcp_transport_closed",
    "mcp_transport_connect_failed",
    "mcp_transport_timeout",
    "mcp_transport_read_timeout",
    "mcp_protocol_error",
}


@dataclass(slots=True)
class MCPManager:
    configs: dict[str, MCPServerConfig]
    client_factory: ClientFactory
    _clients: dict[str, MCPClient] = field(default_factory=dict)
    logger: object | None = None

    def get_client(self, server_name: str) -> MCPClient:
        client = self._clients.get(server_name)
        if client is not None:
            return client

        config = self.configs[server_name]
        if self.logger is not None:
            self.logger.record("mcp.server.start", {"server_name": server_name, "transport": config.transport})
        client = self.client_factory(config)
        try:
            client.initialize()
        except Exception as exc:
            if self.logger is not None:
                self.logger.record("mcp.server.error", {
                    "server_name": server_name,
                    "transport": config.transport,
                    "error_code": getattr(exc, "error_code", "mcp_initialize_failed"),
                    "error": str(exc),
                })
            try:
                client.close()
            except Exception:
                pass
            raise
        self._clients[server_name] = client
        if self.logger is not None:
            self.logger.record("mcp.server.ready", {"server_name": server_name, "transport": config.transport})
        return client

    def invalidate(self, server_name: str) -> None:
        client = self._clients.pop(server_name, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> MCPToolCallResult:
        result = self.get_client(server_name).call_tool(tool_name, arguments)
        if result.is_error and result.error_code in CLIENT_INVALIDATION_ERROR_CODES:
            # Once tools/call has started, a transport failure cannot tell us
            # whether a remote side effect already happened. Drop the broken
            # connection, but never replay the call implicitly.
            self.invalidate(server_name)
            if self.logger is not None:
                self.logger.record("mcp.server.reconnect_deferred", {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error_code": result.error_code,
                    "reason": "tool_call_outcome_unknown",
                })
        return result

    def read_resource(self, server_name: str, resource_id: str) -> str:
        try:
            return self.get_client(server_name).read_resource(resource_id)
        except Exception as exc:
            error_code = getattr(exc, "error_code", "")
            if error_code in CLIENT_INVALIDATION_ERROR_CODES:
                self.invalidate(server_name)
                if self.logger is not None:
                    self.logger.record("mcp.server.reconnect_deferred", {
                        "server_name": server_name,
                        "resource_id": resource_id,
                        "error_code": error_code,
                        "reason": "resource_read_failed",
                    })
            raise

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
