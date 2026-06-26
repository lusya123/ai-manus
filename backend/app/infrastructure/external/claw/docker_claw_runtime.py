import asyncio
import logging
from typing import Optional

import httpx

from app.domain.external.claw import ClawInstanceInfo
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class DockerClawRuntime:
    """Creates claw instances as local Docker containers."""

    creates_immediately = False

    def __init__(self):
        self.settings = get_settings()

    @property
    def ready_timeout(self) -> int:
        return self.settings.claw_ready_timeout

    @staticmethod
    def _strip_openai_path(base_url: Optional[str]) -> Optional[str]:
        if not base_url:
            return None
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return base_url[:-3]
        return base_url

    def _container_reachable_backend_url(self) -> str:
        default_compose_url = "http://backend:8000"
        configured = self._strip_openai_path(self.settings.manus_api_base_url)
        if configured and configured != default_compose_url:
            return configured

        for candidate in (
            self.settings.backend_sandbox_url,
            self.settings.backend_internal_url,
            self.settings.backend_public_url,
            configured,
        ):
            candidate = self._strip_openai_path(candidate)
            if candidate:
                return candidate
        return default_compose_url

    def _published_http_address(self, container) -> Optional[str]:
        ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{self.settings.claw_http_container_port}/tcp") or []
        if not bindings:
            return None
        host_port = bindings[0].get("HostPort")
        if not host_port:
            return None
        host = bindings[0].get("HostIp") or self.settings.claw_host_bind_address
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"{host}:{host_port}"

    async def create(self, claw_id: str, api_key: str) -> ClawInstanceInfo:
        import docker
        docker_client = docker.from_env()

        claw_network = self.settings.claw_network
        manus_api_base_url = self._container_reachable_backend_url()
        container_name = f"{self.settings.claw_name_prefix}-{claw_id[:8]}"

        container_config = {
            "image": self.settings.claw_image,
            "name": container_name,
            "detach": True,
            "remove": True,
            "labels": {
                "ai-manus.kind": "claw",
                "ai-manus.claw_id": claw_id,
            },
            "environment": {
                "CLAW_TTL_SECONDS": str(self.settings.claw_ttl_seconds),
                "MANUS_API_KEY": api_key,
                "MANUS_API_BASE_URL": manus_api_base_url,
            },
        }
        if self.settings.claw_memory_limit:
            container_config["mem_limit"] = self.settings.claw_memory_limit
        if self.settings.claw_nano_cpus:
            container_config["nano_cpus"] = self.settings.claw_nano_cpus
        if self.settings.claw_pids_limit:
            container_config["pids_limit"] = self.settings.claw_pids_limit
        if self.settings.claw_publish_host_ports:
            container_config["ports"] = {
                f"{self.settings.claw_http_container_port}/tcp": (
                    self.settings.claw_host_bind_address,
                    None,
                ),
                f"{self.settings.claw_gateway_container_port}/tcp": (
                    self.settings.claw_host_bind_address,
                    None,
                ),
            }
        if claw_network:
            container_config["network"] = claw_network
        if "host.docker.internal" in manus_api_base_url:
            container_config["extra_hosts"] = {"host.docker.internal": "host-gateway"}

        container = docker_client.containers.run(**container_config)
        published_address = None
        for _ in range(20):
            container.reload()
            published_address = self._published_http_address(container)
            if published_address:
                break
            await asyncio.sleep(0.25)

        network_settings = container.attrs["NetworkSettings"]
        ip_address = network_settings.get("IPAddress", "")
        if not ip_address and "Networks" in network_settings:
            for _, nc in network_settings["Networks"].items():
                if nc.get("IPAddress"):
                    ip_address = nc["IPAddress"]
                    break

        address = published_address or ip_address
        logger.info(
            "Claw container started: %s address=%s container_ip=%s manus_api_base_url=%s",
            container_name,
            address,
            ip_address,
            manus_api_base_url,
        )
        return ClawInstanceInfo(address=address, instance_name=container_name)

    async def destroy(self, instance_name: Optional[str]) -> None:
        if not instance_name:
            return
        try:
            import docker
            docker_client = docker.from_env()
            container = docker_client.containers.get(instance_name)
            container.remove(force=True)
        except Exception as e:
            logger.warning(f"Failed to remove container {instance_name}: {e}")

    async def wait_for_ready(self, base_url: str) -> bool:
        timeout = self.settings.claw_ready_timeout
        interval = 2.0
        max_retries = int(timeout / interval)
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(max_retries):
                try:
                    resp = await client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        logger.info(f"Claw instance ready: {base_url}")
                        return True
                except Exception:
                    pass
                await asyncio.sleep(interval)
        logger.warning(
            f"Claw instance did not become ready after {timeout}s: {base_url}"
        )
        return False
