"""Alibaba Cloud Wuying AgentBay sandbox implementation.

This adapter runs the ai-manus sandbox image as an AgentBay cloud session
instead of a local Docker container. It reuses the entire /api/v1 HTTP
protocol from DockerSandbox — only the sandbox lifecycle (create/get/destroy)
and URL acquisition differ:

- Sandbox creation:   agent_bay.create(CreateSessionParams(image_id=...))
- Endpoint discovery: session.get_link(protocol, port) via the AgentBay
                      gateway. Only ports in [30100, 30199] are open by
                      default, so the sandbox image forwards:
                        30150 -> 8080  (FastAPI /api/v1)
                        30151 -> 8222  (Chrome CDP)
                        30152 -> 5901  (VNC websocket)
                      (see sandbox/supervisord.conf socat entries)
- Destruction:        session.delete()

Requires the optional dependency `wuying-agentbay-sdk` and the following
settings: SANDBOX_PROVIDER=agentbay, AGENTBAY_API_KEY, AGENTBAY_IMAGE_ID.
"""
from typing import Any, Mapping, Optional
import asyncio
import logging
import httpx

from app.core.config import get_settings
from app.domain.external.sandbox import (
    Sandbox,
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

logger = logging.getLogger(__name__)

_agent_bay_client = None


def _get_agent_bay():
    """Lazily create a singleton AsyncAgentBay client.

    The import happens here so that deployments using the default Docker
    provider do not need the AgentBay SDK installed.
    """
    global _agent_bay_client
    if _agent_bay_client is None:
        try:
            from agentbay import AsyncAgentBay, Config
            from agentbay._common.logger import AgentBayLogger
        except ImportError as e:
            raise RuntimeError(
                "SANDBOX_PROVIDER=agentbay requires the 'wuying-agentbay-sdk' "
                "package. Install it with: uv add wuying-agentbay-sdk"
            ) from e

        # AgentBay's SDK otherwise logs complete signed gateway links at INFO
        # and creates ``agentbay.log`` in the process working directory. Those
        # links are bearer capabilities, so disable both SDK sinks before any
        # client/session method can emit them.
        AgentBayLogger.setup(
            level="WARNING",
            enable_console=False,
            enable_file=False,
        )

        settings = get_settings()
        if not settings.agentbay_api_key:
            raise RuntimeError("AGENTBAY_API_KEY is required when SANDBOX_PROVIDER=agentbay")

        cfg = None
        if settings.agentbay_region_id:
            cfg = Config(region_id=settings.agentbay_region_id)
        _agent_bay_client = AsyncAgentBay(api_key=settings.agentbay_api_key, cfg=cfg)
    return _agent_bay_client


class AgentBaySandbox(DockerSandbox):
    """Sandbox running as an AgentBay cloud session.

    Inherits every /api/v1 HTTP method (shell, file, supervisor) and
    get_browser() from DockerSandbox; overrides lifecycle and URLs.
    """

    _PROVIDER_DELETE_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        session,
        base_url: str,
        cdp_url: str,
        vnc_url: str,
    ):
        # Intentionally NOT calling super().__init__: URLs come from the
        # AgentBay gateway, not from a container IP.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._API_TIMEOUT_SECONDS, connect=5.0)
        )
        self.ip = None
        self.base_url = base_url.rstrip("/")
        self._cdp_url = cdp_url
        self._vnc_url = vnc_url
        self._session = session
        # Fields referenced by inherited methods; no Docker container involved.
        self._container_name = None
        self._managed_container = False

    @property
    def id(self) -> str:
        """Sandbox ID is the AgentBay session ID (persisted per chat session)."""
        return self._session.session_id

    @staticmethod
    async def _resolve_links(session) -> tuple[str, str, str]:
        """Fetch gateway URLs for the three forwarded service ports concurrently."""
        settings = get_settings()
        api_link, cdp_link, vnc_link = await asyncio.gather(
            session.get_link("https", settings.agentbay_api_port),
            session.get_link("wss", settings.agentbay_cdp_port),
            session.get_link("wss", settings.agentbay_vnc_port),
        )
        for name, link in (("api", api_link), ("cdp", cdp_link), ("vnc", vnc_link)):
            if not link.success or not link.data:
                raise SandboxUnavailableError(
                    f"AgentBay {name} gateway link is unavailable"
                )
        return api_link.data, cdp_link.data, vnc_link.data

    @classmethod
    async def _from_session(cls, session) -> "AgentBaySandbox":
        base_url, cdp_url, vnc_url = await cls._resolve_links(session)
        # Gateway URLs are bearer capabilities and commonly contain signed
        # query parameters. Never write them to logs.
        logger.info("AgentBay sandbox %s gateway links resolved", session.session_id)
        return cls(session=session, base_url=base_url, cdp_url=cdp_url, vnc_url=vnc_url)

    @classmethod
    async def allocate(
        cls, *, labels: Optional[Mapping[str, str]] = None
    ) -> Any:
        """Allocate a provider session without resolving bearer gateway links.

        The lifecycle provisioner uses this split operation so the billable
        provider ID can be persisted before link resolution, cancellation, or
        any task construction can fail.
        """
        from agentbay import CreateSessionParams

        settings = get_settings()
        if not settings.agentbay_image_id:
            raise RuntimeError("AGENTBAY_IMAGE_ID is required when SANDBOX_PROVIDER=agentbay")

        agent_bay = _get_agent_bay()
        params = CreateSessionParams(
            image_id=settings.agentbay_image_id,
            labels=dict(labels or {"app": "ai-manus"}),
        )
        if settings.sandbox_ttl_minutes:
            # AgentBay expects seconds; auto-release idle sessions like the
            # Docker sandbox TTL does.
            params.idle_release_timeout = settings.sandbox_ttl_minutes * 60

        result = await agent_bay.create(params)
        # SDK result objects always carry success/error_message (default False/"")
        if not result.success or not result.session:
            raise SandboxUnavailableError("AgentBay session creation failed")
        session = result.session
        logger.info("Created AgentBay session %s", session.session_id)
        return session

    @classmethod
    async def connect(cls, session: Any) -> "AgentBaySandbox":
        """Resolve service links for an already-owned provider session."""
        return await cls._from_session(session)

    @classmethod
    async def lookup_provider_session(cls, id: str) -> Optional[Any]:
        """Return the raw SDK session, or ``None`` only for exact NotFound."""
        agent_bay = _get_agent_bay()
        try:
            result = await agent_bay.get(id)
        except Exception as exc:
            logger.warning(
                "AgentBay session %s lookup failed (%s)", id, type(exc).__name__
            )
            raise SandboxUnavailableError(
                f"AgentBay could not confirm sandbox {id} state"
            ) from exc
        if not result.success or not result.session:
            # The SDK preserves the provider's machine-readable code on
            # ``SessionResult``. Free-form text and partial code matches are
            # not authoritative.
            code = str(getattr(result, "code", "") or "")
            if code == "InvalidMcpSession.NotFound":
                logger.info("AgentBay session %s no longer exists", id)
                return None
            logger.warning("AgentBay session %s lookup was inconclusive", id)
            raise SandboxUnavailableError(
                f"AgentBay could not confirm sandbox {id} state"
            )
        return result.session

    @classmethod
    async def list_provider_session_ids(
        cls, labels: Mapping[str, str]
    ) -> list[str]:
        """List every provider session matching stable recovery labels."""
        agent_bay = _get_agent_bay()
        page = 1
        found: list[str] = []
        while page <= 100:
            try:
                result = await agent_bay.list(
                    labels=dict(labels), page=page, limit=100
                )
            except Exception as exc:
                raise SandboxUnavailableError(
                    "AgentBay session inventory is unavailable"
                ) from exc
            if not result.success:
                raise SandboxUnavailableError(
                    "AgentBay session inventory is unavailable"
                )
            for value in result.session_ids or []:
                if isinstance(value, dict):
                    session_id = value.get("sessionId") or value.get("session_id")
                else:
                    session_id = value
                if isinstance(session_id, str) and session_id and session_id not in found:
                    found.append(session_id)
            if not getattr(result, "next_token", ""):
                return found
            page += 1
        raise SandboxUnavailableError("AgentBay session inventory pagination overflow")

    @classmethod
    async def create(cls) -> Sandbox:
        """Legacy convenience wrapper; production creation uses a provisioner."""
        session = await cls.allocate()
        try:
            return await cls.connect(session)
        except Exception as provisioning_error:
            # Don't leak (and keep billing for) a session we can't reach.
            try:
                async with asyncio.timeout(
                    cls._PROVIDER_DELETE_TIMEOUT_SECONDS
                ):
                    delete_result = await session.delete()
                if not getattr(delete_result, "success", False):
                    raise SandboxProvisioningError(
                        session.session_id,
                        "AgentBay provisioning failed and rollback was not confirmed",
                    )
                logger.info("Rolled back unreachable AgentBay session %s", session.session_id)
            except SandboxProvisioningError:
                raise
            except Exception as cleanup_error:
                logger.error(
                    "Failed to roll back AgentBay session %s (%s)",
                    session.session_id,
                    type(cleanup_error).__name__,
                )
                raise SandboxProvisioningError(
                    session.session_id,
                    "AgentBay provisioning failed and rollback was not confirmed",
                ) from provisioning_error
            raise provisioning_error

    @classmethod
    async def get(cls, id: str) -> Optional[Sandbox]:
        """Reconnect to an existing AgentBay session by session ID.

        Live sandbox handles deliberately are not cached.  Their HTTP client
        and provider session can be closed/deleted, so returning a cached
        handle after another request destroys it would resurrect stale state.
        """
        session = await cls.lookup_provider_session(id)
        return await cls.connect(session) if session is not None else None

    async def aclose(self) -> None:
        """Close only this handle's HTTP client, preserving the cloud session."""
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    async def destroy(self) -> bool:
        """Delete the AgentBay session (stops billing) and close the handle."""
        destroyed = False
        try:
            async with asyncio.timeout(
                self._PROVIDER_DELETE_TIMEOUT_SECONDS
            ):
                result = await self._session.delete()
            if result.success:
                logger.info("Deleted AgentBay session %s", self._session.session_id)
            else:
                logger.error(
                    "AgentBay did not confirm deletion of session %s",
                    self._session.session_id,
                )
            destroyed = bool(result.success)
        except Exception as e:
            logger.error(
                "Failed to destroy AgentBay sandbox (%s)", type(e).__name__
            )
        finally:
            try:
                await self.aclose()
            except Exception as e:
                logger.error(
                    "Failed to close AgentBay sandbox handle (%s)",
                    type(e).__name__,
                )
        return destroyed
