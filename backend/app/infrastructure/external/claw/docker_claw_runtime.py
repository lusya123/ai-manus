import asyncio
import logging
import os
from typing import Optional

import httpx

from app.domain.external.claw import ClawInstanceInfo
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary

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
        return base_url[:-3] if base_url.endswith("/v1") else base_url

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
        bindings = ports.get(
            f"{self.settings.claw_http_container_port}/tcp"
        ) or []
        if not bindings:
            return None
        host_port = bindings[0].get("HostPort")
        if not host_port:
            return None
        host = bindings[0].get("HostIp") or self.settings.claw_host_bind_address
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"{host}:{host_port}"

    @staticmethod
    def _is_running_in_container() -> bool:
        return os.path.exists("/.dockerenv") or bool(
            os.environ.get("KUBERNETES_SERVICE_HOST")
        )

    def _select_runtime_address(
        self,
        container_ip: str,
        published_address: Optional[str],
    ) -> str:
        """Choose an address reachable from the backend process.

        A published ``127.0.0.1`` port is valid only when the backend itself
        runs on the Docker host.  Inside Compose/Kubernetes, or when an
        explicit claw network is configured, the backend must use the claw
        container's network address instead.
        """
        container_topology = bool(self.settings.claw_network) or self._is_running_in_container()
        if container_topology:
            if not container_ip:
                raise RuntimeError(
                    "Claw container has no network IP reachable from backend"
                )
            return container_ip
        if published_address:
            return published_address
        if container_ip:
            return container_ip
        raise RuntimeError("Claw container has no reachable address")

    async def create(self, claw_id: str, api_key: str) -> ClawInstanceInfo:
        import docker
        docker_client = docker.from_env()

        claw_network = self.settings.claw_network
        manus_api_base_url = self._container_reachable_backend_url()
        container_name = f"{self.settings.claw_name_prefix}-{claw_id[:8]}"

        # Remove any stale container left over from a previous provisioning
        # attempt with the same claw id, otherwise `run` fails with a name
        # conflict and the old container lingers forever.
        try:
            stale = docker_client.containers.get(container_name)
            logger.warning(f"Removing stale claw container: {container_name}")
            stale.remove(force=True)
        except docker.errors.NotFound:
            pass
        except Exception as e:
            error = RuntimeError(
                f"Failed to remove stale Claw container {container_name}: {e}"
            )
            # The predictable instance name is durable ownership information.
            # The domain layer persists it when cleanup fails so a later delete
            # can retry instead of orphaning the container.
            error.claw_instance_name = container_name
            raise error from e

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
            container_config["extra_hosts"] = {
                "host.docker.internal": "host-gateway"
            }

        try:
            container = docker_client.containers.run(**container_config)
        except BaseException as e:
            # A name-conflict can mean a stale container still exists even if
            # the SDK failed before returning a Container object.  Preserve the
            # deterministic name for domain-level rollback/retry.
            e.claw_instance_name = container_name
            raise
        try:
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

            address = self._select_runtime_address(
                ip_address, published_address
            )
            logger.info("Claw container started: %s", container_name)
            return ClawInstanceInfo(
                address=address, instance_name=container_name
            )
        except BaseException as e:
            # Inspection can fail or be cancelled after the container exists.
            # Let the domain layer perform the rollback so destroy failures are
            # persisted with ownership intact and remain retryable.
            e.claw_instance_name = container_name
            raise

    async def destroy(self, instance_name: Optional[str]) -> bool:
        if not instance_name:
            return True
        try:
            import docker
            docker_client = docker.from_env()
            try:
                container = docker_client.containers.get(instance_name)
            except docker.errors.NotFound:
                # Already gone (e.g. the container's TTL expired and it
                # removed itself) — nothing to do.
                return True
            logger.info(f"Removing claw container: {instance_name}")
            container.remove(force=True)
            return True
        except Exception as e:
            logger.error(
                "Failed to remove container %s: %s",
                instance_name,
                safe_exception_summary(e),
            )
            return False

    async def wait_for_ready(self, base_url: str) -> bool:
        timeout = self.settings.claw_ready_timeout
        interval = 2.0
        max_retries = int(timeout / interval)
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(max_retries):
                try:
                    resp = await client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        logger.info("Claw instance is ready")
                        return True
                except Exception:
                    pass
                await asyncio.sleep(interval)
        logger.warning(
            "Claw instance did not become ready after %s seconds", timeout
        )
        return False
