import logging
from typing import Optional

from app.domain.external.claw import ClawInstanceInfo
from app.core.config import get_settings
from app.infrastructure.external.claw.readiness import wait_for_http_health

logger = logging.getLogger(__name__)


class FixedClawRuntime:
    """Uses a pre-configured fixed address — no actual creation.

    Suitable for development or when the claw instance is managed
    externally (e.g., on a remote machine).
    """

    creates_immediately = True
    _HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0
    _HEALTH_RETRY_INTERVAL_SECONDS = 2.0

    def __init__(self, address: str):
        self._address = address
        self.settings = get_settings()

    @property
    def ready_timeout(self) -> int:
        return self.settings.claw_ready_timeout

    async def create(self, claw_id: str, api_key: str) -> ClawInstanceInfo:
        return ClawInstanceInfo(address=self._address)

    async def destroy(self, instance_name: Optional[str]) -> bool:
        # The fixed runtime is externally managed and never owned by a Claw
        # record (``create`` returns no instance_name), so there is no local
        # resource to destroy.
        return True

    async def wait_for_ready(self, base_url: str) -> bool:
        timeout = float(self.settings.claw_ready_timeout)
        ready = await wait_for_http_health(
            base_url,
            total_timeout_seconds=timeout,
            request_timeout_seconds=self._HEALTH_REQUEST_TIMEOUT_SECONDS,
            retry_interval_seconds=self._HEALTH_RETRY_INTERVAL_SECONDS,
        )
        if ready:
            logger.info("Fixed claw instance is ready")
            return True
        logger.warning(
            "Fixed claw instance not ready after %s seconds", timeout
        )
        return False
