"""Pinned HTTPS transport for user-managed remote MCP servers."""
from urllib.parse import urlsplit
import httpx
from app.infrastructure.external.llm.security import PinnedModelEndpointTransport

class ConnectorEndpointTransport(PinnedModelEndpointTransport):
    async def handle_async_request(self, request):
        requested_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        configured_port = self._port or (443 if self._scheme == "https" else 80)
        if request.url.scheme != self._scheme or requested_port != configured_port:
            raise httpx.UnsupportedProtocol("MCP client refused a request to another origin")
        return await super().handle_async_request(request)


def pinned_mcp_client_factory(url, pinned_ip):
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=ConnectorEndpointTransport(url, pinned_ip), headers=headers, timeout=timeout or 30, auth=auth, follow_redirects=False, trust_env=False)
    return factory
