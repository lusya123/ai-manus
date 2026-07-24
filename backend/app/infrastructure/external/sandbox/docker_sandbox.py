from typing import Dict, Any, Optional, List, BinaryIO
import hashlib
import uuid
import httpx
import docker
import socket
import logging
import asyncio
import posixpath
import tempfile
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit
from async_lru import alru_cache
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from app.domain.models.tool_result import ToolResult
from app.domain.external.sandbox import (
    Sandbox,
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
from app.infrastructure.external.browser.browser_use_browser import BrowserUseBrowser
from app.infrastructure.external.docker_async import (
    DockerCallTimeoutError,
    run_bounded_docker_call,
    wait_for_retained_docker_call,
)
from app.infrastructure.external.runtime_network import (
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
from app.domain.external.browser import Browser

logger = logging.getLogger(__name__)

class DockerSandbox(Sandbox):
    _DOCKER_SDK_TIMEOUT_SECONDS = 15.0
    _DNS_TIMEOUT_SECONDS = 5.0
    _API_TIMEOUT_SECONDS = 30.0
    _WAIT_PROCESS_MARGIN_SECONDS = 5.0
    _READINESS_MAX_ATTEMPTS = 30
    _READINESS_RETRY_INTERVAL_SECONDS = 2.0
    _READINESS_REQUEST_TIMEOUT_SECONDS = 2.0
    _READINESS_TOTAL_TIMEOUT_SECONDS = 60.0
    _FILE_FIND_TIMEOUT_SECONDS = 15.0
    _FILE_DOWNLOAD_TIMEOUT_SECONDS = 30.0
    _FILE_DOWNLOAD_SPOOL_BYTES = 1024 * 1024
    _MAX_FILE_FIND_GLOB_PARTS = 64
    _VIRTUAL_FILESYSTEM_ROOTS = ("/dev", "/proc", "/run", "/sys")
    _LIFECYCLE_OPERATIONS: Dict[str, asyncio.Task[Any]] = {}
    _KNOWN_COMPLETED_CREATES: set[str] = set()
    # Backwards-compatible alias for focused tests and older embedded callers.
    _CREATE_OPERATIONS = _LIFECYCLE_OPERATIONS

    @staticmethod
    def _runtime_deployment_id(settings: Any | None = None) -> str:
        settings = settings or get_settings()
        return str(getattr(settings, "runtime_deployment_id", "ai-manus"))

    def __init__(
        self,
        ip: str = None,
        container_name: str = None,
        managed_container: bool = False,
        api_port: int = 8080,
        cdp_port: int = 9222,
        vnc_port: int = 5901,
        owner_id: Optional[str] = None,
    ):
        """Initialize Docker sandbox and API interaction client"""
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._API_TIMEOUT_SECONDS, connect=5.0)
        )
        self.ip = ip
        self.base_url = f"http://{self.ip}:{api_port}"
        self._vnc_url = f"ws://{self.ip}:{vnc_port}"
        self._cdp_url = f"http://{self.ip}:{cdp_port}"
        self._container_name = container_name
        self._managed_container = managed_container
        # New deterministic containers carry the exact session ID in a Docker
        # label.  Destructive cleanup checks this value before removing the
        # name so even a hash collision or manual name reuse fails closed.
        self._owner_id = owner_id
    
    @property
    def id(self) -> str:
        """Sandbox ID"""
        if not getattr(self, "_container_name", None):
            return "dev-sandbox"
        return self._container_name

    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def vnc_url(self) -> str:
        return self._vnc_url

    def _api_url(
        self,
        path: str,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Append an API path without moving signed query data into the path.

        Docker endpoints normally have no query string, while AgentBay gateway
        links often do. Plain string concatenation turns ``?signature=...``
        into the middle of the URL and makes every inherited API call invalid.
        """

        parsed = urlsplit(self.base_url)
        joined_path = f"{parsed.path.rstrip('/')}/{path.lstrip('/')}"
        query = parsed.query
        if query_params:
            encoded_params = urlencode(query_params, doseq=True)
            query = f"{query}&{encoded_params}" if query else encoded_params
        return urlunsplit(
            (parsed.scheme, parsed.netloc, joined_path, query, parsed.fragment)
        )

    @staticmethod
    def _get_container_ip(container) -> str:
        """Get container IP address from network settings
        
        Args:
            container: Docker container instance
            
        Returns:
            Container IP address
        """
        # Get container network settings
        network_settings = container.attrs['NetworkSettings']

        # Use .get() to avoid KeyError on newer Docker versions (e.g. Debian 13)
        # where the top-level IPAddress field may be absent when the container
        # is attached to a user-defined network instead of the default bridge.
        ip_address = network_settings.get('IPAddress', '')

        # Fall back to per-network IP when the top-level field is empty
        if not ip_address:
            networks = network_settings.get('Networks', {})
            for network_config in networks.values():
                candidate = network_config.get('IPAddress', '')
                if candidate:
                    ip_address = candidate
                    break

        return ip_address

    @staticmethod
    def planned_id(owner_id: str) -> str:
        """Return the legacy stable Docker name for compatibility checks."""

        settings = get_settings()
        if settings.sandbox_address:
            return "dev-sandbox"
        normalized = unicodedata.normalize("NFKC", str(owner_id)).strip()
        if not normalized:
            raise ValueError("sandbox owner ID is required")
        # 160 bits makes accidental collisions negligible while leaving ample
        # room for an operator-provided prefix under Docker's name limit.
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]
        return f"{settings.sandbox_name_prefix}-{digest}"

    @classmethod
    def plan_owned_id(cls, owner_id: str) -> str:
        """Allocate one fenced, deployment-scoped runtime incarnation."""

        base = cls.planned_id(owner_id)
        if get_settings().sandbox_address:
            # A configured sandbox host is one shared, externally managed
            # runtime.  It has no Docker generation to allocate and its only
            # authoritative persisted identity is ``dev-sandbox``.
            return base
        deployment = runtime_deployment_digest(
            cls._runtime_deployment_id()
        )[:10]
        return f"{base}-{deployment}-{uuid.uuid4().hex[:16]}"

    @classmethod
    def is_owned_id(cls, container_name: str, owner_id: str) -> bool:
        legacy = cls.planned_id(owner_id)
        if get_settings().sandbox_address:
            return container_name == legacy
        deployment = runtime_deployment_digest(
            cls._runtime_deployment_id()
        )[:10]
        if container_name == legacy:
            return True
        prefix = f"{legacy}-{deployment}-"
        generation = container_name[len(prefix):] if container_name.startswith(prefix) else ""
        return len(generation) == 16 and all(
            character in "0123456789abcdef" for character in generation
        )

    @staticmethod
    def _lifecycle_key(container_name: str) -> str:
        return f"sandbox:{container_name}"

    @staticmethod
    def _container_has_owner(container, owner_id: str) -> bool:
        labels = ((container.attrs or {}).get("Config") or {}).get("Labels") or {}
        if (
            labels.get("ai-manus.deployment_digest") is None
            and labels.get("ai-manus.generation_digest") is None
        ):
            return (
                labels.get("ai-manus.kind") == "sandbox"
                and labels.get("ai-manus.session_id") == owner_id
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
                runtime_kind="sandbox",
                container_name=container_name,
                owner_id=owner_id,
                deployment_id=DockerSandbox._runtime_deployment_id(),
                allow_legacy=True,
            )
        except PermissionError:
            return False
        return True

    @staticmethod
    def _prepare_network_intent_task(
        container_name: str,
        owner_id: Optional[str],
    ) -> bool:
        """Publish durable bridges before the possibly-late container call."""

        settings = get_settings()
        docker_client = docker.from_env(
            timeout=DockerSandbox._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            ensure_runtime_egress_network(
                docker_client,
                runtime_kind="sandbox",
                container_name=container_name,
                owner_id=owner_id,
                address_pool=settings.runtime_network_address_pool,
                subnet_prefix=settings.runtime_network_subnet_prefix,
                deployment_id=DockerSandbox._runtime_deployment_id(settings),
            )
            ensure_private_runtime_network(
                docker_client,
                runtime_kind="sandbox",
                container_name=container_name,
                owner_id=owner_id,
                address_pool=settings.runtime_network_address_pool,
                subnet_prefix=settings.runtime_network_subnet_prefix,
                deployment_id=DockerSandbox._runtime_deployment_id(settings),
            )
            return True
        finally:
            close = getattr(docker_client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _create_container_task(
        container_name: str,
        owner_id: Optional[str] = None,
    ) -> str:
        """Create and inspect one container inside a worker thread."""

        settings = get_settings()
        image = settings.sandbox_image

        docker_client = docker.from_env(
            timeout=DockerSandbox._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            container_config = {
                "image": image,
                "name": container_name,
                "detach": True,
                "remove": True,
                "labels": {
                    **runtime_container_identity_labels(
                        "sandbox",
                        container_name,
                        DockerSandbox._runtime_deployment_id(settings),
                    ),
                    **(
                        {"ai-manus.session_id": owner_id}
                        if owner_id is not None
                        else {}
                    ),
                },
                "environment": {
                    "SERVICE_TIMEOUT_MINUTES": settings.sandbox_ttl_minutes,
                    "CHROME_ARGS": settings.sandbox_chrome_args,
                    "HTTPS_PROXY": settings.sandbox_https_proxy,
                    "HTTP_PROXY": settings.sandbox_http_proxy,
                    "NO_PROXY": settings.sandbox_no_proxy,
                }
            }

            if settings.sandbox_memory_limit:
                container_config["mem_limit"] = settings.sandbox_memory_limit
            if settings.sandbox_cpu_limit:
                container_config["nano_cpus"] = int(
                    settings.sandbox_cpu_limit * 1_000_000_000
                )
            if settings.sandbox_pids_limit:
                container_config["pids_limit"] = settings.sandbox_pids_limit
            private_network_name = None
            egress_network_name = None
            if settings.runtime_network_isolation:
                require_containerized_runtime_gateway()
                egress_network_name = ensure_runtime_egress_network(
                    docker_client,
                    runtime_kind="sandbox",
                    container_name=container_name,
                    owner_id=owner_id,
                    address_pool=settings.runtime_network_address_pool,
                    subnet_prefix=settings.runtime_network_subnet_prefix,
                    deployment_id=DockerSandbox._runtime_deployment_id(settings),
                )
                private_network_name = ensure_private_runtime_network(
                    docker_client,
                    runtime_kind="sandbox",
                    container_name=container_name,
                    owner_id=owner_id,
                    address_pool=settings.runtime_network_address_pool,
                    subnet_prefix=settings.runtime_network_subnet_prefix,
                    deployment_id=DockerSandbox._runtime_deployment_id(settings),
                )
                # Start on the one-container external bridge so outbound web
                # access owns the only default route. The internal control
                # bridge is attached after creation and cannot replace it.
                container_config["network"] = egress_network_name
            elif settings.sandbox_network:
                container_config["network"] = settings.sandbox_network

            container = docker_client.containers.run(**container_config)
            container.reload()
            if private_network_name:
                return connect_runtime_to_private_network(
                    docker_client,
                    container,
                    runtime_kind="sandbox",
                    container_name=container_name,
                    owner_id=owner_id,
                    deployment_id=DockerSandbox._runtime_deployment_id(settings),
                )
            return DockerSandbox._get_container_ip(container)
        finally:
            close = getattr(docker_client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _destroy_container_task(
        container_name: str,
        owner_id: Optional[str],
        allow_absent_intent_cleanup: bool = False,
    ) -> bool:
        """Delete one exact container and both of its deterministic bridges."""

        settings = get_settings()
        docker_client = docker.from_env(
            timeout=DockerSandbox._DOCKER_SDK_TIMEOUT_SECONDS
        )
        try:
            try:
                container = docker_client.containers.get(container_name)
            except docker.errors.NotFound:
                container = None
            if container is not None:
                container.reload()
                if owner_id is not None and not DockerSandbox._container_has_owner(
                    container, owner_id
                ):
                    raise PermissionError(
                        "Docker sandbox owner label does not match the session"
                    )
                container.remove(force=True)
            elif (
                settings.runtime_network_isolation
                and not allow_absent_intent_cleanup
                and owned_runtime_network_intent_exists(
                    docker_client,
                    runtime_kind="sandbox",
                    container_name=container_name,
                    owner_id=owner_id,
                    deployment_id=DockerSandbox._runtime_deployment_id(settings),
                )
            ):
                return False
            # AutoRemove and failed ``containers.run`` paths can leave the
            # deterministic networks after the container itself is gone.
            if settings.runtime_network_isolation:
                remove_private_runtime_network(
                    docker_client,
                    runtime_kind="sandbox",
                    container_name=container_name,
                    owner_id=owner_id,
                    deployment_id=DockerSandbox._runtime_deployment_id(settings),
                )
            return True
        finally:
            close = getattr(docker_client, "close", None)
            if callable(close):
                close()

    @classmethod
    async def _destroy_by_id(
        cls,
        container_name: str,
        owner_id: Optional[str],
    ) -> bool:
        """Resolve a retained create then perform exact provider cleanup."""

        settings = get_settings()
        if settings.sandbox_address:
            return container_name == "dev-sandbox"
        try:
            pending_operation = cls._LIFECYCLE_OPERATIONS.get(container_name)
            allow_absent_intent_cleanup = (
                pending_operation is not None
                or container_name in cls._KNOWN_COMPLETED_CREATES
            )
            create_finished = await wait_for_retained_docker_call(
                pending_operation,
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"sandbox create {container_name}",
            )
            if not create_finished:
                logger.error(
                    "Sandbox creation is still indeterminate; retaining "
                    "ownership for %s",
                    container_name,
                )
                return False
            return await run_bounded_docker_call(
                lambda: cls._destroy_container_task(
                    container_name,
                    owner_id,
                    allow_absent_intent_cleanup,
                ),
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"sandbox destroy {container_name}",
                registry=cls._LIFECYCLE_OPERATIONS,
                registry_key=container_name,
                serialize_key=cls._lifecycle_key(container_name),
            )
        except DockerCallTimeoutError as exc:
            logger.error(
                "Timed out destroying Docker sandbox %s: %s",
                container_name,
                safe_exception_summary(exc),
            )
            return False
        except Exception as exc:
            logger.error(
                "Failed to destroy Docker sandbox %s: %s",
                container_name,
                safe_exception_summary(exc),
            )
            return False
        finally:
            cls._KNOWN_COMPLETED_CREATES.discard(container_name)

    @classmethod
    async def destroy_owned_by_id(
        cls,
        container_name: str,
        owner_id: str,
    ) -> bool:
        """Destroy an exact deterministic sandbox even after AutoRemove."""

        if not owner_id:
            return False
        if not cls.is_owned_id(container_name, owner_id):
            return False
        return await cls._destroy_by_id(container_name, owner_id)

    @classmethod
    async def destroy_by_id(cls, container_name: str) -> bool:
        """Compatibility cleanup for an unowned legacy dynamic sandbox."""

        return await cls._destroy_by_id(container_name, owner_id=None)

    async def ensure_sandbox(self) -> None:
        """Ensure sandbox is ready by checking that all services are RUNNING"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._READINESS_TOTAL_TIMEOUT_SECONDS
        last_state = "supervisor did not return a ready state"

        for attempt in range(self._READINESS_MAX_ATTEMPTS):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                request_budget = min(
                    self._READINESS_REQUEST_TIMEOUT_SECONDS,
                    remaining,
                )
                async with asyncio.timeout(request_budget):
                    response = await self.client.get(
                        self._api_url("/api/v1/supervisor/status"),
                        timeout=request_budget,
                    )
                response.raise_for_status()
                tool_result = ToolResult(**response.json())

                if not tool_result.success:
                    last_state = tool_result.message or "supervisor status failed"
                    logger.warning("Supervisor status check failed: %s", last_state)
                else:
                    services = tool_result.data or []
                    if not services:
                        last_state = "no supervisor services were reported"
                        logger.warning("No services found in supervisor status")
                    else:
                        non_running_services = [
                            f"{service.get('name', 'unknown')}"
                            f"({service.get('statename', '')})"
                            for service in services
                            if service.get("statename", "") != "RUNNING"
                        ]
                        if not non_running_services:
                            logger.info(
                                "All %d services are RUNNING - sandbox is ready",
                                len(services),
                            )
                            return
                        last_state = (
                            "non-running services: "
                            + ", ".join(non_running_services)
                        )
                        logger.info(
                            "Waiting for services to start: %s "
                            "(attempt %d/%d)",
                            ", ".join(non_running_services),
                            attempt + 1,
                            self._READINESS_MAX_ATTEMPTS,
                        )
            except Exception as e:
                last_state = safe_exception_summary(e)
                logger.warning(
                    "Failed to check supervisor status (attempt %d/%d): %s",
                    attempt + 1,
                    self._READINESS_MAX_ATTEMPTS,
                    last_state,
                )

            if attempt + 1 >= self._READINESS_MAX_ATTEMPTS:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(self._READINESS_RETRY_INTERVAL_SECONDS, remaining)
            )

        error_message = (
            "Sandbox services failed to become ready within "
            f"{self._READINESS_TOTAL_TIMEOUT_SECONDS:g} seconds: {last_state}"
        )
        logger.error(error_message)
        raise SandboxProvisioningError(self.id, error_message)

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        response = await self.client.post(
            self._api_url("/api/v1/shell/exec"),
            json={
                "id": session_id,
                "exec_dir": exec_dir,
                "command": command
            }
        )
        return ToolResult(**response.json())

    async def view_shell(self, session_id: str, console: bool = False) -> ToolResult:
        response = await self.client.post(
            self._api_url("/api/v1/shell/view"),
            json={
                "id": session_id,
                "console": console
            }
        )
        return ToolResult(**response.json())

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        server_wait_seconds = 60 if seconds is None else max(1, int(seconds))
        response = await self.client.post(
            self._api_url("/api/v1/shell/wait"),
            json={
                "id": session_id,
                "seconds": seconds
            },
            timeout=max(
                self._API_TIMEOUT_SECONDS,
                server_wait_seconds + self._WAIT_PROCESS_MARGIN_SECONDS,
            ),
        )
        return ToolResult(**response.json())

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool = True) -> ToolResult:
        response = await self.client.post(
            self._api_url("/api/v1/shell/write"),
            json={
                "id": session_id,
                "input": input_text,
                "press_enter": press_enter
            }
        )
        return ToolResult(**response.json())

    async def kill_process(self, session_id: str) -> ToolResult:
        response = await self.client.post(
            self._api_url("/api/v1/shell/kill"),
            json={"id": session_id}
        )
        return ToolResult(**response.json())

    async def file_write(self, file: str, content: str, append: bool = False, 
                        leading_newline: bool = False, trailing_newline: bool = False, 
                        sudo: bool = False) -> ToolResult:
        """Write content to file
        
        Args:
            file: File path
            content: Content to write
            append: Whether to append content
            leading_newline: Whether to add newline before content
            trailing_newline: Whether to add newline after content
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of write operation
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/write"),
            json={
                "file": file,
                "content": content,
                "append": append,
                "leading_newline": leading_newline,
                "trailing_newline": trailing_newline,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_read(self, file: str, start_line: int = None, 
                        end_line: int = None, sudo: bool = False) -> ToolResult:
        """Read file content
        
        Args:
            file: File path
            start_line: Start line number
            end_line: End line number
            sudo: Whether to use sudo privileges
            
        Returns:
            File content
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/read"),
            json={
                "file": file,
                "start_line": start_line,
                "end_line": end_line,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())
        
    async def file_exists(self, path: str) -> ToolResult:
        """Check if file exists
        
        Args:
            path: File path
            
        Returns:
            Whether file exists
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/exists"),
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_delete(self, path: str) -> ToolResult:
        """Delete file
        
        Args:
            path: File path
            
        Returns:
            Result of delete operation
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/delete"),
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_list(self, path: str) -> ToolResult:
        """List directory contents
        
        Args:
            path: Directory path
            
        Returns:
            List of directory contents
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/list"),
            json={"path": path}
        )
        return ToolResult(**response.json())

    async def file_replace(self, file: str, old_str: str, new_str: str, sudo: bool = False) -> ToolResult:
        """Replace string in file
        
        Args:
            file: File path
            old_str: String to replace
            new_str: String to replace with
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of replace operation
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/replace"),
            json={
                "file": file,
                "old_str": old_str,
                "new_str": new_str,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_search(self, file: str, regex: str, sudo: bool = False) -> ToolResult:
        """Search in file content
        
        Args:
            file: File path
            regex: Regular expression
            sudo: Whether to use sudo privileges
            
        Returns:
            Search results
        """
        response = await self.client.post(
            self._api_url("/api/v1/file/search"),
            json={
                "file": file,
                "regex": regex,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_find(self, path: str, glob_pattern: str) -> ToolResult:
        """Find files by name pattern
        
        Args:
            path: Search directory path
            glob_pattern: Glob match pattern
            
        Returns:
            List of found files
        """
        # Keep this guard in the backend as well as the sandbox service. Long-
        # lived per-user sandbox containers may still run an older image during
        # a rolling deployment; they must never receive the filesystem-wide
        # recursive glob that caused the original worker stall.
        raw_path = (path or ".").strip() or "."
        # Reject traversal before normalization can erase the evidence (for
        # example, ``foo/../../proc`` normalizes to ``../proc``).
        raw_path_parts = tuple(part for part in raw_path.split("/") if part)
        if ".." in raw_path_parts:
            raise ValueError("Search path must not contain parent traversal")
        # POSIX preserves exactly two leading slashes in ``normpath``. Collapse
        # every absolute-path prefix first so ``//`` and ``//proc`` cannot
        # bypass the root/virtual-filesystem guards below, and only send the
        # canonical path to older sandbox services.
        if raw_path.startswith("/"):
            raw_path = f"/{raw_path.lstrip('/')}"
        normalized_path = posixpath.normpath(raw_path)
        raw_glob = (glob_pattern or "").replace("\\", "/")
        raw_glob_parts = tuple(part for part in raw_glob.split("/") if part)
        if (
            not raw_glob
            or raw_glob.startswith("/")
            or ".." in raw_glob_parts
        ):
            raise ValueError(
                "Glob pattern must be a relative path without parent traversal"
            )
        glob_parts = tuple(part for part in raw_glob_parts if part != ".")
        if not glob_parts:
            raise ValueError("Glob pattern must select a file or directory")
        if len(glob_parts) > self._MAX_FILE_FIND_GLOB_PARTS:
            raise ValueError("Glob pattern contains too many path segments")
        recursive = "**" in glob_parts
        if normalized_path == "/" and (
            len(glob_parts) != 1 or glob_parts[0] == "**"
        ):
            raise ValueError(
                "Only root-level, non-recursive filesystem searches are allowed"
            )
        if recursive and any(
            normalized_path == root or normalized_path.startswith(f"{root}/")
            for root in self._VIRTUAL_FILESYSTEM_ROOTS
        ):
            raise ValueError(
                "Recursive searches of virtual filesystems are not allowed"
            )

        response = await self.client.post(
            self._api_url("/api/v1/file/find"),
            json={
                "path": normalized_path,
                "glob": glob_pattern
            },
            timeout=self._FILE_FIND_TIMEOUT_SECONDS,
        )
        return ToolResult(**response.json())

    async def file_upload(self, file_data: BinaryIO, path: str, filename: str = None) -> ToolResult:
        """Upload file to sandbox
        
        Args:
            file_data: File content as binary stream
            path: Target file path in sandbox
            filename: Original filename (optional)
            
        Returns:
            Upload operation result
        """
        # Prepare form data for upload
        files = {"file": (filename or "upload", file_data, "application/octet-stream")}
        data = {"path": path}
        
        response = await self.client.post(
            self._api_url("/api/v1/file/upload"),
            files=files,
            data=data
        )
        return ToolResult(**response.json())

    async def file_download(self, path: str) -> BinaryIO:
        """Download file from sandbox
        
        Args:
            path: File path in sandbox
            
        Returns:
            File content as binary stream
        """
        max_bytes = max(1, int(get_settings().file_upload_max_bytes))
        output = tempfile.SpooledTemporaryFile(
            max_size=self._FILE_DOWNLOAD_SPOOL_BYTES,
            mode="w+b",
        )
        total_bytes = 0
        try:
            async with self.client.stream(
                "GET",
                self._api_url(
                    "/api/v1/file/download",
                    query_params={"path": path},
                ),
                timeout=self._FILE_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_bytes = int(content_length)
                    except ValueError:
                        declared_bytes = None
                    if declared_bytes is not None and declared_bytes > max_bytes:
                        raise ValueError("Sandbox file exceeds the download limit")
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ValueError("Sandbox file exceeds the download limit")
                    output.write(chunk)
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise
    
    async def aclose(self) -> None:
        """Close only this handle's HTTP client.

        A sandbox ID is persisted independently of this Python object, so
        closing a request/runner handle must not remove its container.
        """
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    async def destroy(self) -> bool:
        """Destroy the managed Docker sandbox and close this handle."""
        destroyed = True
        try:
            if self._managed_container and self._container_name:
                destroyed = await type(self)._destroy_by_id(
                    self._container_name,
                    self._owner_id,
                )
        except Exception as e:
            logger.error(
                "Failed to destroy Docker sandbox: %s",
                safe_exception_summary(e),
            )
            destroyed = False
        finally:
            try:
                await self.aclose()
            except Exception as e:
                logger.error(
                    "Failed to close Docker sandbox handle: %s",
                    safe_exception_summary(e),
                )
        return destroyed
    
    async def get_browser(self) -> Browser:
        """Get browser instance

        Returns a browser implementation connected to the sandbox's Chrome via CDP.
        The concrete implementation is selected by the BROWSER_ENGINE setting:
          - "playwright"   → PlaywrightBrowser
          - "browser_use"  → BrowserUseBrowser  (default)
        """
        settings = get_settings()
        engine = (settings.browser_engine or "browser_use").lower().strip()
        if engine == "browser_use":
            # AgentBay CDP URLs contain signed gateway capabilities.
            logger.info("Using BrowserUseBrowser engine for sandbox %s", self.id)
            return BrowserUseBrowser(self.cdp_url)
        logger.info("Using PlaywrightBrowser engine for sandbox %s", self.id)
        return PlaywrightBrowser(self.cdp_url)

    @staticmethod
    @alru_cache(maxsize=128, typed=True)
    async def _resolve_hostname_to_ip(hostname: str) -> str:
        """Resolve hostname to IP address
        
        Args:
            hostname: Hostname to resolve
            
        Returns:
            Resolved IP address, or None if resolution fails
            
        Note:
            This method is cached using LRU cache with a maximum size of 128 entries.
            The cache helps reduce repeated DNS lookups for the same hostname.
        """
        try:
            # First check if hostname is already in IP address format
            try:
                socket.inet_pton(socket.AF_INET, hostname)
                # If successfully parsed, it's an IPv4 address format, return directly
                return hostname
            except OSError:
                # Not a valid IP address format, proceed with DNS resolution
                pass
                
            # libc name resolution is synchronous and may block on DNS. Keep it
            # out of the event loop and impose a small caller-side deadline.
            addr_info = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    hostname,
                    None,
                    socket.AF_INET,
                ),
                timeout=DockerSandbox._DNS_TIMEOUT_SECONDS,
            )
            # Return the first IPv4 address found
            if addr_info and len(addr_info) > 0:
                return addr_info[0][4][0]  # Return sockaddr[0] from (family, type, proto, canonname, sockaddr), which is the IP address
            return None
        except Exception as e:
            # Log a bounded summary and return None on failure.
            logger.error(
                "Failed to resolve sandbox hostname: %s",
                safe_exception_summary(e),
            )
            return None

    @classmethod
    async def _create_named(
        cls,
        container_name: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Sandbox:
        """Create exactly one named container and retain late SDK work."""

        try:
            prior_operation = cls._LIFECYCLE_OPERATIONS.get(container_name)
            if not await wait_for_retained_docker_call(
                prior_operation,
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"prior sandbox lifecycle {container_name}",
            ):
                raise DockerCallTimeoutError(
                    f"prior sandbox lifecycle {container_name}",
                    cls._DOCKER_SDK_TIMEOUT_SECONDS,
                )
            if get_settings().runtime_network_isolation:
                await run_bounded_docker_call(
                    lambda: cls._prepare_network_intent_task(
                        container_name,
                        owner_id,
                    ),
                    timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                    operation=f"sandbox network intent {container_name}",
                    registry=cls._LIFECYCLE_OPERATIONS,
                    registry_key=container_name,
                    serialize_key=cls._lifecycle_key(container_name),
                )
            ip_address = await run_bounded_docker_call(
                lambda: cls._create_container_task(container_name, owner_id),
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"sandbox create {container_name}",
                registry=cls._LIFECYCLE_OPERATIONS,
                registry_key=container_name,
                serialize_key=cls._lifecycle_key(container_name),
            )
        except asyncio.CancelledError as exc:
            # Cancellation cannot stop an already-running Docker SDK thread.
            # The provisioner pre-publishes this deterministic pointer before
            # calling create_owned(), so a later cleanup can recover it.
            exc.sandbox_id = container_name
            if cls._LIFECYCLE_OPERATIONS.get(container_name) is None:
                cls._KNOWN_COMPLETED_CREATES.add(container_name)
            raise
        except Exception as exc:
            if cls._LIFECYCLE_OPERATIONS.get(container_name) is None:
                cls._KNOWN_COMPLETED_CREATES.add(container_name)
            raise SandboxProvisioningError(
                container_name,
                "Docker sandbox creation failed or timed out; the deterministic "
                "container name was retained for cleanup",
            ) from exc
        cls._KNOWN_COMPLETED_CREATES.discard(container_name)
        return DockerSandbox(
            ip=ip_address,
            container_name=container_name,
            managed_container=True,
            owner_id=owner_id,
        )

    @classmethod
    async def create_owned(cls, owner_id: str) -> Sandbox:
        """Create the pre-publishable deterministic container for a session."""

        settings = get_settings()
        if settings.sandbox_address:
            return await cls.create()
        return await cls._create_named(
            cls.plan_owned_id(owner_id),
            owner_id=owner_id,
        )

    @classmethod
    async def create_owned_with_id(
        cls,
        owner_id: str,
        container_name: str,
    ) -> Sandbox:
        """Create exactly the generation already persisted by the caller."""

        if get_settings().sandbox_address:
            if container_name != "dev-sandbox":
                raise SandboxProvisioningError(
                    container_name,
                    "Fixed sandbox mode only accepts the dev-sandbox identity",
                )
            # Fixed-host mode is externally managed.  Delegating to create()
            # resolves that host and guarantees this path can never invoke
            # docker-py to create a dynamic container.
            return await cls.create()
        if not cls.is_owned_id(container_name, owner_id):
            raise SandboxProvisioningError(
                container_name,
                "Sandbox generation does not belong to this deployment/session",
            )
        return await cls._create_named(container_name, owner_id=owner_id)

    @classmethod
    async def create(cls) -> Sandbox:
        """Create a new sandbox instance
        
        Returns:
            New sandbox instance
        """
        settings = get_settings()

        if settings.sandbox_address:
            # Chrome CDP needs IP address
            ip = await cls._resolve_hostname_to_ip(settings.sandbox_address)
            return DockerSandbox(
                ip=ip,
                api_port=settings.sandbox_api_port,
                cdp_port=settings.sandbox_cdp_port,
                vnc_port=settings.sandbox_vnc_port,
            )
    
        container_name = (
            f"{settings.sandbox_name_prefix}-{str(uuid.uuid4())[:8]}"
        )
        return await cls._create_named(container_name)
    
    @classmethod
    async def get(cls, id: str) -> Sandbox:
        """Get sandbox by ID
        
        Args:
            id: Sandbox ID
            
        Returns:
            A fresh sandbox handle.  Live handles are intentionally not
            cached because each owns a closeable HTTP client.  In fixed-host
            mode, sharing a cached handle would let deleting one chat close
            the client used by every other chat.
        """
        settings = get_settings()
        if settings.sandbox_address:
            # Fixed-host mode has exactly one canonical runtime identity: the
            # handle returned by create() is always ``dev-sandbox``. Refusing
            # arbitrary persisted IDs is important for legacy provider-marker
            # adoption; otherwise an old AgentBay ID could be made to look
            # like an exact Docker match merely because both point at the same
            # configured development host.
            if id != "dev-sandbox":
                logger.warning("Fixed sandbox ID %s is not authoritative", id)
                return None
            ip = await cls._resolve_hostname_to_ip(settings.sandbox_address)
            return DockerSandbox(
                ip=ip,
                container_name=id,
                managed_container=False,
                api_port=settings.sandbox_api_port,
                cdp_port=settings.sandbox_cdp_port,
                vnc_port=settings.sandbox_vnc_port,
            )

        pending_create = cls._LIFECYCLE_OPERATIONS.get(id)
        create_finished = await wait_for_retained_docker_call(
            pending_create,
            timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
            operation=f"sandbox create {id}",
        )
        if not create_finished:
            raise SandboxUnavailableError(
                f"Docker sandbox {id} still has an indeterminate lifecycle operation"
            )

        def inspect_container() -> Optional[str]:
            docker_client = docker.from_env(
                timeout=cls._DOCKER_SDK_TIMEOUT_SECONDS
            )
            try:
                try:
                    container = docker_client.containers.get(id)
                except docker.errors.NotFound:
                    return None
                container.reload()
                if settings.runtime_network_isolation:
                    labels = dict(
                        ((container.attrs or {}).get("Config") or {}).get("Labels")
                        or {}
                    )
                    owner_id = labels.get("ai-manus.session_id")
                    if owner_id:
                        isolated_ip = resolve_owned_runtime_control_ip(
                            docker_client,
                            container,
                            runtime_kind="sandbox",
                            container_name=id,
                            owner_id=owner_id,
                            deployment_id=cls._runtime_deployment_id(settings),
                            address_pool=settings.runtime_network_address_pool,
                            subnet_prefix=settings.runtime_network_subnet_prefix,
                        )
                        if isolated_ip:
                            return isolated_ip
                    assert_legacy_network_has_at_most_one_runtime(
                        docker_client,
                        network_names=(
                            settings.sandbox_network or "bridge",
                            settings.claw_network or "bridge",
                        ),
                        sandbox_name_prefix=(
                            settings.sandbox_name_prefix or "None"
                        ),
                        claw_name_prefix=settings.claw_name_prefix,
                    )
                return cls._get_container_ip(container)
            finally:
                close = getattr(docker_client, "close", None)
                if callable(close):
                    close()

        try:
            ip_address = await run_bounded_docker_call(
                inspect_container,
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"sandbox inspect {id}",
                registry=cls._LIFECYCLE_OPERATIONS,
                registry_key=id,
                serialize_key=cls._lifecycle_key(id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SandboxUnavailableError(
                f"Docker could not confirm sandbox {id}; ownership was retained"
            ) from exc
        if ip_address is None:
            logger.warning("Sandbox container %s not found", id)
            return None

        logger.info(f"IP address: {ip_address}")
        return DockerSandbox(ip=ip_address, container_name=id, managed_container=True)

    @classmethod
    async def get_owned(cls, id: str, owner_id: str) -> Sandbox:
        """Inspect a deterministic sandbox and verify its exact owner label."""

        settings = get_settings()
        if settings.sandbox_address:
            if id != "dev-sandbox":
                return None
            return await cls.get(id)

        pending_create = cls._LIFECYCLE_OPERATIONS.get(id)
        create_finished = await wait_for_retained_docker_call(
            pending_create,
            timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
            operation=f"sandbox create {id}",
        )
        if not create_finished:
            raise SandboxUnavailableError(
                f"Docker sandbox {id} still has an indeterminate lifecycle operation"
            )

        def inspect_owned_container() -> Optional[str]:
            docker_client = docker.from_env(
                timeout=cls._DOCKER_SDK_TIMEOUT_SECONDS
            )
            try:
                try:
                    container = docker_client.containers.get(id)
                except docker.errors.NotFound:
                    return None
                container.reload()
                if not cls._container_has_owner(container, owner_id):
                    raise PermissionError(
                        "Docker sandbox owner label does not match the session"
                    )
                if settings.runtime_network_isolation:
                    isolated_ip = resolve_owned_runtime_control_ip(
                        docker_client,
                        container,
                        runtime_kind="sandbox",
                        container_name=id,
                        owner_id=owner_id,
                        deployment_id=cls._runtime_deployment_id(settings),
                        address_pool=settings.runtime_network_address_pool,
                        subnet_prefix=settings.runtime_network_subnet_prefix,
                    )
                    if isolated_ip:
                        return isolated_ip
                    assert_legacy_network_has_at_most_one_runtime(
                        docker_client,
                        network_names=(
                            settings.sandbox_network or "bridge",
                            settings.claw_network or "bridge",
                        ),
                        sandbox_name_prefix=(
                            settings.sandbox_name_prefix or "None"
                        ),
                        claw_name_prefix=settings.claw_name_prefix,
                    )
                return cls._get_container_ip(container)
            finally:
                close = getattr(docker_client, "close", None)
                if callable(close):
                    close()

        try:
            ip_address = await run_bounded_docker_call(
                inspect_owned_container,
                timeout_seconds=cls._DOCKER_SDK_TIMEOUT_SECONDS,
                operation=f"owned sandbox inspect {id}",
                registry=cls._LIFECYCLE_OPERATIONS,
                registry_key=id,
                serialize_key=cls._lifecycle_key(id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SandboxUnavailableError(
                f"Docker could not verify owner of sandbox {id}; ownership was retained"
            ) from exc
        if ip_address is None:
            return None
        return DockerSandbox(
            ip=ip_address,
            container_name=id,
            managed_container=True,
            owner_id=owner_id,
        )
