import asyncio
import hashlib
import logging
import os
import time
import uuid
from typing import Any, Optional

from app.domain.external.claw import ClawInstanceInfo
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from app.infrastructure.external.claw.readiness import wait_for_http_health
from app.infrastructure.external.docker_async import (
    DockerCallTimeoutError,
    run_bounded_docker_call,
    wait_for_retained_docker_call,
)
from app.infrastructure.external.runtime_network import (
    RUNTIME_GATEWAY_ALIAS,
    assert_legacy_network_has_at_most_one_runtime,
    connect_runtime_to_private_network,
    container_ip_on_network,
    ensure_private_runtime_network,
    ensure_runtime_egress_network,
    owned_runtime_network_intent_exists,
    remove_private_runtime_network,
    require_containerized_runtime_gateway,
    resolve_owned_runtime_control_ip,
    runtime_container_identity_labels,
    runtime_deployment_digest,
    verify_owned_runtime_container,
)

logger = logging.getLogger(__name__)


class DockerClawRuntime:
    """Creates claw instances as local Docker containers."""

    creates_immediately = False
    _DOCKER_SDK_TIMEOUT_SECONDS = 15.0
    _CONTAINER_INSPECTION_ATTEMPTS = 20
    _CONTAINER_INSPECTION_INTERVAL_SECONDS = 0.25
    _HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0
    _HEALTH_RETRY_INTERVAL_SECONDS = 2.0
    _LIFECYCLE_OPERATIONS: dict[str, asyncio.Task[Any]] = {}
    _KNOWN_COMPLETED_CREATES: set[str] = set()
    # Backwards-compatible alias for focused tests and embedded callers.
    _CREATE_OPERATIONS = _LIFECYCLE_OPERATIONS

    def __init__(self):
        self.settings = get_settings()

    @property
    def ready_timeout(self) -> int:
        return self.settings.claw_ready_timeout

    @property
    def _runtime_network_isolation(self) -> bool:
        # A few embedders/tests provide a deliberately small settings object;
        # the real Settings model always supplies this secure production flag.
        return bool(getattr(self.settings, "runtime_network_isolation", False))

    @property
    def _runtime_deployment_id(self) -> str:
        return str(getattr(self.settings, "runtime_deployment_id", "ai-manus"))

    @property
    def _publish_host_ports(self) -> bool:
        return bool(
            self.settings.claw_publish_host_ports
            and not self._runtime_network_isolation
        )

    @staticmethod
    def _strip_openai_path(base_url: Optional[str]) -> Optional[str]:
        if not base_url:
            return None
        base_url = base_url.rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url

    def _container_reachable_backend_url(self) -> str:
        default_compose_url = "http://backend:8000"
        configured = self._strip_openai_path(self.settings.manus_api_base_url)
        if (
            self._runtime_network_isolation
            and (not configured or configured == default_compose_url)
        ):
            return f"http://{RUNTIME_GATEWAY_ALIAS}:8000"
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

    def planned_id(self, claw_id: str) -> str:
        """Return the legacy deterministic name for compatibility checks."""

        return f"{self.settings.claw_name_prefix}-{claw_id[:8]}"

    def plan_owned_id(self, claw_id: str) -> str:
        """Allocate one fenced, deployment-scoped runtime incarnation."""

        deployment = runtime_deployment_digest(
            self._runtime_deployment_id
        )[:10]
        owner = hashlib.sha256(claw_id.encode("utf-8")).hexdigest()[:24]
        return (
            f"{self.settings.claw_name_prefix}-{deployment}-{owner}-"
            f"{uuid.uuid4().hex[:16]}"
        )

    def is_owned_id(self, instance_name: str, claw_id: str) -> bool:
        if instance_name == self.planned_id(claw_id):
            return True
        deployment = runtime_deployment_digest(
            self._runtime_deployment_id
        )[:10]
        owner = hashlib.sha256(claw_id.encode("utf-8")).hexdigest()[:24]
        prefix = f"{self.settings.claw_name_prefix}-{deployment}-{owner}-"
        generation = (
            instance_name[len(prefix):]
            if instance_name.startswith(prefix)
            else ""
        )
        return len(generation) == 16 and all(
            character in "0123456789abcdef" for character in generation
        )

    @staticmethod
    def _lifecycle_key(container_name: str) -> str:
        return f"claw:{container_name}"

    @staticmethod
    def _container_has_owner(container, claw_id: str) -> bool:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if (
            labels.get("ai-manus.deployment_digest") is None
            and labels.get("ai-manus.generation_digest") is None
        ):
            return (
                labels.get("ai-manus.kind") == "claw"
                and labels.get("ai-manus.claw_id") == claw_id
            )
        container_name = str(
            getattr(container, "name", None)
            or (container.attrs or {}).get("Name", "").lstrip("/")
        )
        if not container_name:
            return False
        try:
            verify_owned_runtime_container(
                container,
                runtime_kind="claw",
                container_name=container_name,
                owner_id=claw_id,
                deployment_id=str(
                    getattr(get_settings(), "runtime_deployment_id", "ai-manus")
                ),
                allow_legacy=True,
            )
        except PermissionError:
            return False
        return True

    def _prepare_network_intent_task(
        self,
        container_name: str,
        claw_id: str,
    ) -> bool:
        """Publish both durable intent bridges before ``containers.run``."""

        import docker

        docker_client = docker.from_env(
            timeout=self._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            ensure_runtime_egress_network(
                docker_client,
                runtime_kind="claw",
                container_name=container_name,
                owner_id=claw_id,
                address_pool=self.settings.runtime_network_address_pool,
                subnet_prefix=self.settings.runtime_network_subnet_prefix,
                deployment_id=self._runtime_deployment_id,
            )
            ensure_private_runtime_network(
                docker_client,
                runtime_kind="claw",
                container_name=container_name,
                owner_id=claw_id,
                address_pool=self.settings.runtime_network_address_pool,
                subnet_prefix=self.settings.runtime_network_subnet_prefix,
                deployment_id=self._runtime_deployment_id,
            )
            return True
        finally:
            self._close_docker_client(docker_client)

    @staticmethod
    def _close_docker_client(docker_client) -> None:
        close = getattr(docker_client, "close", None)
        if callable(close):
            close()

    def _create_container_task(
        self,
        container_name: str,
        claw_id: str,
        container_config: dict[str, Any],
    ) -> tuple[str, Optional[str]]:
        """Create or adopt one exact fenced Claw generation."""

        import docker

        docker_client = docker.from_env(
            timeout=self._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            private_network_name = None
            egress_network_name = None
            if self._runtime_network_isolation:
                require_containerized_runtime_gateway()
                egress_network_name = ensure_runtime_egress_network(
                    docker_client,
                    runtime_kind="claw",
                    container_name=container_name,
                    owner_id=claw_id,
                    address_pool=self.settings.runtime_network_address_pool,
                    subnet_prefix=self.settings.runtime_network_subnet_prefix,
                    deployment_id=self._runtime_deployment_id,
                )
                private_network_name = ensure_private_runtime_network(
                    docker_client,
                    runtime_kind="claw",
                    container_name=container_name,
                    owner_id=claw_id,
                    address_pool=self.settings.runtime_network_address_pool,
                    subnet_prefix=self.settings.runtime_network_subnet_prefix,
                    deployment_id=self._runtime_deployment_id,
                )
                container_config["network"] = egress_network_name
            try:
                stale = docker_client.containers.get(container_name)
            except docker.errors.NotFound:
                stale = None
            if stale is not None:
                if not self._container_has_owner(stale, claw_id):
                    raise PermissionError(
                        "Refusing to replace a Claw container owned by another record"
                    )
                container = stale
                if private_network_name:
                    ip_address = resolve_owned_runtime_control_ip(
                        docker_client,
                        container,
                        runtime_kind="claw",
                        container_name=container_name,
                        owner_id=claw_id,
                        deployment_id=self._runtime_deployment_id,
                        address_pool=self.settings.runtime_network_address_pool,
                        subnet_prefix=self.settings.runtime_network_subnet_prefix,
                    )
                else:
                    ip_address = None
            else:
                container = docker_client.containers.run(**container_config)
                if private_network_name:
                    ip_address = connect_runtime_to_private_network(
                        docker_client,
                        container,
                        runtime_kind="claw",
                        container_name=container_name,
                        owner_id=claw_id,
                        deployment_id=self._runtime_deployment_id,
                    )
                else:
                    ip_address = None
            published_address = None
            for attempt in range(self._CONTAINER_INSPECTION_ATTEMPTS):
                container.reload()
                published_address = self._published_http_address(container)
                if published_address or not self._publish_host_ports:
                    break
                if attempt + 1 < self._CONTAINER_INSPECTION_ATTEMPTS:
                    time.sleep(self._CONTAINER_INSPECTION_INTERVAL_SECONDS)

            if private_network_name:
                return (ip_address, published_address)
            network_settings = container.attrs["NetworkSettings"]
            ip_address = network_settings.get("IPAddress", "")
            if not ip_address and "Networks" in network_settings:
                for network_config in network_settings["Networks"].values():
                    if network_config.get("IPAddress"):
                        ip_address = network_config["IPAddress"]
                        break
            return ip_address, published_address
        finally:
            self._close_docker_client(docker_client)

    @staticmethod
    def _destroy_container_task(
        instance_name: str,
        claw_id: Optional[str] = None,
        runtime_network_isolation: bool = False,
        deployment_id: str = "ai-manus",
        allow_absent_intent_cleanup: bool = False,
    ) -> bool:
        import docker

        docker_client = docker.from_env(
            timeout=DockerClawRuntime._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            try:
                container = docker_client.containers.get(instance_name)
            except docker.errors.NotFound:
                container = None
            if container is not None:
                if claw_id and not DockerClawRuntime._container_has_owner(
                    container, claw_id
                ):
                    raise PermissionError(
                        "Refusing to remove a Claw container owned by another record"
                    )
                logger.info("Removing claw container: %s", instance_name)
                container.remove(force=True)
            elif (
                runtime_network_isolation
                and not allow_absent_intent_cleanup
                and owned_runtime_network_intent_exists(
                    docker_client,
                    runtime_kind="claw",
                    container_name=instance_name,
                    owner_id=claw_id,
                    deployment_id=deployment_id,
                )
            ):
                return False
            if runtime_network_isolation:
                remove_private_runtime_network(
                    docker_client,
                    runtime_kind="claw",
                    container_name=instance_name,
                    owner_id=claw_id,
                    deployment_id=deployment_id,
                )
            return True
        finally:
            DockerClawRuntime._close_docker_client(docker_client)

    async def create(self, claw_id: str, api_key: str) -> ClawInstanceInfo:
        """Compatibility path for callers without durable prepublication."""

        return await self.create_owned(
            claw_id,
            api_key,
            self.planned_id(claw_id),
        )

    async def create_owned(
        self,
        claw_id: str,
        api_key: str,
        container_name: str,
    ) -> ClawInstanceInfo:
        """Create the exact runtime generation already persisted in Mongo."""

        claw_network = self.settings.claw_network
        manus_api_base_url = self._container_reachable_backend_url()
        if not self.is_owned_id(container_name, claw_id):
            raise PermissionError(
                "Claw runtime generation does not belong to this deployment/record"
            )

        try:
            prior_create = self._LIFECYCLE_OPERATIONS.get(container_name)
            if not await wait_for_retained_docker_call(
                prior_create,
                timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"prior Claw create {container_name}",
            ):
                raise DockerCallTimeoutError(
                    f"prior Claw create {container_name}",
                    self._DOCKER_SDK_TIMEOUT_SECONDS,
                )
            if self._runtime_network_isolation:
                await run_bounded_docker_call(
                    lambda: self._prepare_network_intent_task(
                        container_name,
                        claw_id,
                    ),
                    timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
                    operation=f"Claw network intent {container_name}",
                    registry=self._LIFECYCLE_OPERATIONS,
                    registry_key=container_name,
                    serialize_key=self._lifecycle_key(container_name),
                )

            container_config = {
                "image": self.settings.claw_image,
                "name": container_name,
                "detach": True,
                "remove": True,
                "labels": {
                    **runtime_container_identity_labels(
                        "claw",
                        container_name,
                        self._runtime_deployment_id,
                    ),
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
            if self._publish_host_ports:
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
            if claw_network and not self._runtime_network_isolation:
                container_config["network"] = claw_network
            if "host.docker.internal" in manus_api_base_url:
                container_config["extra_hosts"] = {
                    "host.docker.internal": "host-gateway"
                }

            ip_address, published_address = await run_bounded_docker_call(
                lambda: self._create_container_task(
                    container_name, claw_id, container_config
                ),
                timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"Claw create {container_name}",
                registry=self._LIFECYCLE_OPERATIONS,
                registry_key=container_name,
                serialize_key=self._lifecycle_key(container_name),
            )

            address = self._select_runtime_address(
                ip_address, published_address
            )
            logger.info("Claw container started: %s", container_name)
            self._KNOWN_COMPLETED_CREATES.discard(container_name)
            return ClawInstanceInfo(
                address=address, instance_name=container_name
            )
        except BaseException as e:
            # A worker-thread timeout/cancellation cannot prove that creation
            # stopped. The domain layer persists this deterministic pointer and
            # invokes ``destroy``; destroy refuses success while a late create
            # remains indeterminate.
            try:
                e.claw_instance_name = container_name
            except Exception:
                pass
            if self._LIFECYCLE_OPERATIONS.get(container_name) is None:
                self._KNOWN_COMPLETED_CREATES.add(container_name)
            raise

    async def destroy(self, instance_name: Optional[str]) -> bool:
        return await self._destroy(instance_name, claw_id=None)

    async def resolve_owned(
        self,
        instance_name: str,
        claw_id: str,
    ) -> Optional[str]:
        """Verify ownership, attach this API replica, and refresh its IP."""

        if not self._runtime_network_isolation:
            return None
        require_containerized_runtime_gateway()
        prior_create = self._LIFECYCLE_OPERATIONS.get(instance_name)
        if not await wait_for_retained_docker_call(
            prior_create,
            timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
            operation=f"prior Claw create {instance_name}",
        ):
            raise DockerCallTimeoutError(
                f"prior Claw create {instance_name}",
                self._DOCKER_SDK_TIMEOUT_SECONDS,
            )

        def inspect_owned() -> Optional[str]:
            import docker

            docker_client = docker.from_env(
                timeout=self._DOCKER_SDK_TIMEOUT_SECONDS
            )
            try:
                try:
                    container = docker_client.containers.get(instance_name)
                except docker.errors.NotFound:
                    raise RuntimeError(
                        "Owned Claw runtime container no longer exists"
                    )
                container.reload()
                if not self._container_has_owner(container, claw_id):
                    raise PermissionError(
                        "Claw container owner label does not match the record"
                    )
                isolated_ip = resolve_owned_runtime_control_ip(
                    docker_client,
                    container,
                    runtime_kind="claw",
                    container_name=instance_name,
                    owner_id=claw_id,
                    deployment_id=self._runtime_deployment_id,
                    address_pool=self.settings.runtime_network_address_pool,
                    subnet_prefix=self.settings.runtime_network_subnet_prefix,
                )
                if isolated_ip:
                    return isolated_ip
                assert_legacy_network_has_at_most_one_runtime(
                    docker_client,
                    network_names=(self.settings.claw_network or "bridge",),
                    sandbox_name_prefix=(
                        self.settings.sandbox_name_prefix or "None"
                    ),
                    claw_name_prefix=self.settings.claw_name_prefix,
                )
                network_settings = container.attrs["NetworkSettings"]
                ip_address = network_settings.get("IPAddress", "")
                if not ip_address:
                    for network in (
                        network_settings.get("Networks") or {}
                    ).values():
                        if network.get("IPAddress"):
                            ip_address = network["IPAddress"]
                            break
                return ip_address or None
            finally:
                self._close_docker_client(docker_client)

        return await run_bounded_docker_call(
            inspect_owned,
            timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
            operation=f"owned Claw inspect {instance_name}",
            registry=self._LIFECYCLE_OPERATIONS,
            registry_key=instance_name,
            serialize_key=self._lifecycle_key(instance_name),
        )

    async def destroy_owned(
        self,
        instance_name: Optional[str],
        claw_id: str,
    ) -> bool:
        """Destroy only when the full durable owner label matches."""

        if instance_name and not self.is_owned_id(instance_name, claw_id):
            return False
        return await self._destroy(instance_name, claw_id=claw_id)

    async def _destroy(
        self,
        instance_name: Optional[str],
        *,
        claw_id: Optional[str],
    ) -> bool:
        if not instance_name:
            return True
        try:
            pending_operation = self._LIFECYCLE_OPERATIONS.get(instance_name)
            allow_absent_intent_cleanup = (
                pending_operation is not None
                or instance_name in self._KNOWN_COMPLETED_CREATES
            )
            create_finished = await wait_for_retained_docker_call(
                pending_operation,
                timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"Claw create {instance_name}",
            )
            if not create_finished:
                logger.error(
                    "Claw creation is still indeterminate; retaining ownership "
                    "for %s",
                    instance_name,
                )
                return False
            return await run_bounded_docker_call(
                lambda: self._destroy_container_task(
                    instance_name,
                    claw_id,
                    self._runtime_network_isolation,
                    self._runtime_deployment_id,
                    allow_absent_intent_cleanup,
                ),
                timeout_seconds=self._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"Claw destroy {instance_name}",
                registry=self._LIFECYCLE_OPERATIONS,
                registry_key=instance_name,
                serialize_key=self._lifecycle_key(instance_name),
            )
        except DockerCallTimeoutError as e:
            logger.error(
                "Timed out removing container %s: %s",
                instance_name,
                safe_exception_summary(e),
            )
            return False
        except Exception as e:
            logger.error(
                "Failed to remove container %s: %s",
                instance_name,
                safe_exception_summary(e),
            )
            return False
        finally:
            self._KNOWN_COMPLETED_CREATES.discard(instance_name)

    async def wait_for_ready(self, base_url: str) -> bool:
        timeout = float(self.settings.claw_ready_timeout)
        ready = await wait_for_http_health(
            base_url,
            total_timeout_seconds=timeout,
            request_timeout_seconds=self._HEALTH_REQUEST_TIMEOUT_SECONDS,
            retry_interval_seconds=self._HEALTH_RETRY_INTERVAL_SECONDS,
        )
        if ready:
            logger.info("Claw instance is ready")
            return True
        logger.warning(
            "Claw instance did not become ready after %s seconds", timeout
        )
        return False
