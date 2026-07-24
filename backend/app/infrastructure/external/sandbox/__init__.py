"""Sandbox provider selection shared by API and worker composition roots."""
import logging
from typing import Type

from app.core.config import get_settings
from app.domain.external.sandbox import Sandbox

logger = logging.getLogger(__name__)


def get_sandbox_provider() -> Type[Sandbox]:
    """Return the configured sandbox class and fail fast on invalid values."""
    provider = (get_settings().sandbox_provider or "docker").lower().strip()
    if provider == "docker":
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

        return DockerSandbox
    if provider == "agentbay":
        from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox

        logger.info("Using AgentBay sandbox provider")
        return AgentBaySandbox
    raise ValueError(
        f"Unknown SANDBOX_PROVIDER '{provider}' (expected 'docker' or 'agentbay')"
    )


__all__ = ["get_sandbox_provider"]
