"""Renewable Redis lease for cross-replica Agent session mutations."""

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.domain.external.coordination import (
    SessionLifecycleLeaseBusyError,
    SessionLifecycleLeaseUnavailableError,
    mark_lifecycle_task_lease_lost,
)
from app.infrastructure.storage.redis import get_redis
from app.domain.utils.error_reporting import safe_exception_summary


logger = logging.getLogger(__name__)
T = TypeVar("T")


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisSessionLifecycleLease:
    """Serialize one session's enqueue/delete lifecycle across API replicas.

    The value stored in Redis is an unguessable owner token. Renewal and
    release are compare-and-act Lua scripts, so an expired owner can never
    extend or delete a newer owner's lease.
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        ttl_seconds: int = 30,
        acquire_timeout_seconds: float = 30.0,
        retry_interval_seconds: float = 0.05,
        command_timeout_seconds: float | None = None,
        renew_interval_seconds: float | None = None,
    ) -> None:
        if ttl_seconds < 3:
            raise ValueError("Session lifecycle lease TTL must be at least 3 seconds")
        if acquire_timeout_seconds < 0:
            raise ValueError("Session lifecycle lease wait must not be negative")
        self._redis_client = redis_client
        self._ttl_seconds = int(ttl_seconds)
        self._acquire_timeout_seconds = float(acquire_timeout_seconds)
        self._retry_interval_seconds = max(0.001, float(retry_interval_seconds))
        default_command_timeout = max(
            0.25, min(5.0, self._ttl_seconds / 4)
        )
        self._command_timeout_seconds = float(
            default_command_timeout
            if command_timeout_seconds is None
            else command_timeout_seconds
        )
        default_renew_interval = max(1.0, self._ttl_seconds / 3)
        self._renew_interval_seconds = float(
            default_renew_interval
            if renew_interval_seconds is None
            else renew_interval_seconds
        )
        if not 0 < self._command_timeout_seconds < self._ttl_seconds:
            raise ValueError(
                "Session lifecycle Redis command timeout must be positive "
                "and shorter than the lease TTL"
            )
        if not 0 < self._renew_interval_seconds < self._ttl_seconds:
            raise ValueError(
                "Session lifecycle renewal interval must be positive and "
                "shorter than the lease TTL"
            )
        if (
            self._renew_interval_seconds + self._command_timeout_seconds
            >= self._ttl_seconds
        ):
            raise ValueError(
                "Session lifecycle renewal interval plus Redis command "
                "timeout must be shorter than the lease TTL"
            )

    @property
    def _client(self) -> Any:
        # Resolve lazily: the composition root may be built only after startup
        # has initialized the shared Redis client.
        if self._redis_client is not None:
            return self._redis_client
        try:
            return get_redis().client
        except Exception as exc:
            raise SessionLifecycleLeaseUnavailableError(
                "Agent session coordination is unavailable; please retry"
            ) from exc

    @staticmethod
    def _lease_key(session_id: str) -> str:
        # Session IDs are caller-controlled path values. Hashing keeps Redis
        # keys bounded and prevents separator-based key namespace confusion.
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return f"agent:session-lifecycle:{digest}"

    async def _redis_call(self, awaitable: Awaitable[T]) -> T:
        try:
            return await asyncio.wait_for(
                awaitable, timeout=self._command_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SessionLifecycleLeaseUnavailableError(
                "Agent session coordination is unavailable; please retry"
            ) from exc

    async def _acquire(self, key: str, owner_token: str) -> None:
        deadline = time.monotonic() + self._acquire_timeout_seconds
        while True:
            acquired = bool(
                await self._redis_call(
                    self._client.set(
                        key,
                        owner_token,
                        nx=True,
                        ex=self._ttl_seconds,
                    )
                )
            )
            if acquired:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionLifecycleLeaseBusyError(
                    "Agent session is busy; please retry"
                )
            await asyncio.sleep(min(self._retry_interval_seconds, remaining))

    async def _renew_once(self, key: str, owner_token: str) -> None:
        renewed = bool(
            await self._redis_call(
                self._client.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    owner_token,
                    self._ttl_seconds,
                )
            )
        )
        if not renewed:
            raise SessionLifecycleLeaseUnavailableError(
                "Agent session coordination lease ownership was lost"
            )

    async def _renew_loop(self, key: str, owner_token: str) -> None:
        while True:
            await asyncio.sleep(self._renew_interval_seconds)
            await self._renew_once(key, owner_token)

    async def _release(self, key: str, owner_token: str) -> bool:
        try:
            return bool(
                await self._redis_call(
                    self._client.eval(
                        _RELEASE_SCRIPT,
                        1,
                        key,
                        owner_token,
                    )
                )
            )
        except SessionLifecycleLeaseUnavailableError as exc:
            # Never issue an unconditional DEL. The lease has a bounded TTL,
            # and its key may already belong to a newer replica.
            logger.warning(
                "Failed to release Agent session lease %s; awaiting TTL: %s",
                key,
                safe_exception_summary(exc),
            )
            return False

    async def run_exclusive(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        key = self._lease_key(session_id)
        owner_token = uuid.uuid4().hex
        await self._acquire(key, owner_token)

        renewal_task: asyncio.Task[None] | None = None
        operation_task: asyncio.Future[T] | None = None
        try:
            # Verify ownership once before any domain mutation. A Redis failure
            # immediately after SET NX therefore fails closed.
            await self._renew_once(key, owner_token)
            renewal_task = asyncio.create_task(
                self._renew_loop(key, owner_token)
            )
            operation_task = asyncio.ensure_future(operation())
            done, _ = await asyncio.wait(
                {renewal_task, operation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task
            if renewal_task in done:
                coordination_error = renewal_task.exception()
                if not operation_task.done():
                    mark_lifecycle_task_lease_lost(operation_task)
                    operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                raise coordination_error or SessionLifecycleLeaseUnavailableError(
                    "Agent session lease renewal stopped unexpectedly"
                )
            return await operation_task
        except BaseException:
            if operation_task is not None and not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            raise
        finally:
            if renewal_task is not None and not renewal_task.done():
                renewal_task.cancel()
            if renewal_task is not None:
                await asyncio.gather(renewal_task, return_exceptions=True)
            await self._release(key, owner_token)
