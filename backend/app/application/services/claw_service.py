import logging
import asyncio
import hashlib
import json
import time
import uuid
from collections import defaultdict
from typing import Optional, List

from app.domain.models.claw import Claw, ClawMessage, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
from app.domain.utils.model_output import extract_model_thinking_text, sanitize_model_text
from app.domain.utils.error_reporting import safe_exception_summary
from app.core.config import get_settings
from app.infrastructure.storage.redis import get_redis
from app.application.errors.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)


_RELEASE_PROVISION_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_PROVISION_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_ACQUIRE_PROXY_QUOTA_SCRIPT = """
local request_count = redis.call('incr', KEYS[1])
if request_count == 1 then
    redis.call('expire', KEYS[1], ARGV[1])
end
if request_count > tonumber(ARGV[2]) then
    return 0
end

redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[3])
if redis.call('zcard', KEYS[2]) >= tonumber(ARGV[4]) then
    return 0
end
redis.call('zadd', KEYS[2], ARGV[5], ARGV[6])
redis.call('expire', KEYS[2], ARGV[7])
return 1
"""

_RELEASE_PROXY_QUOTA_SCRIPT = """
return redis.call('zrem', KEYS[1], ARGV[1])
"""


class _ProvisionLockUnavailable(RuntimeError):
    """Raised when Redis cannot safely coordinate Claw provisioning."""


class ClawEventBus:
    """Per-user event bus backed by Redis with local in-process fanout."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._subscriber_tasks: dict[asyncio.Queue, asyncio.Task] = {}
        self._origin = str(uuid.uuid4())

    @staticmethod
    def _channel(user_id: str) -> str:
        return f"claw:events:{user_id}"

    def subscribe(self, user_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers[user_id].append(queue)
        try:
            task = asyncio.create_task(self._redis_subscribe(user_id, queue))
            self._subscriber_tasks[queue] = task
        except RuntimeError:
            logger.warning(
                "[claw-bus] unable to start redis subscriber; using local events only"
            )
        return queue

    def unsubscribe(self, user_id: str, queue: asyncio.Queue):
        subs = self._subscribers.get(user_id)
        if subs:
            self._subscribers[user_id] = [q for q in subs if q is not queue]
        task = self._subscriber_tasks.pop(queue, None)
        if task:
            task.cancel()

    async def publish(self, user_id: str, event: dict):
        for queue in self._subscribers.get(user_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        try:
            await get_redis().client.publish(
                self._channel(user_id),
                json.dumps(
                    {"origin": self._origin, "event": event}, ensure_ascii=False
                ),
            )
        except Exception as e:
            logger.warning(
                "[claw-bus] redis publish failed: %s",
                safe_exception_summary(e),
            )

    async def _redis_subscribe(
        self, user_id: str, queue: asyncio.Queue
    ) -> None:
        channel = self._channel(user_id)
        while True:
            pubsub = None
            try:
                pubsub = get_redis().client.pubsub()
                await pubsub.subscribe(channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message.get("data") or "{}")
                    except Exception:
                        continue
                    if payload.get("origin") == self._origin:
                        continue
                    event = payload.get("event")
                    if not isinstance(event, dict):
                        continue
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                logger.warning("[claw-bus] redis subscribe ended; retrying")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "[claw-bus] redis subscribe failed; retrying: %s",
                    safe_exception_summary(e),
                )
                await asyncio.sleep(1)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(channel)
                        close = getattr(pubsub, "aclose", None) or pubsub.close
                        await close()
                    except Exception:
                        pass


class _ChatState:
    """Tracks an in-progress response so new SSE clients can catch up."""
    __slots__ = ("pending_text", "pending_thinking", "raw_text")

    def __init__(self):
        self.pending_text = ""
        self.pending_thinking = ""
        self.raw_text = ""


class ClawService:
    """Application service for managing OpenClaw instances.

    Thin orchestration layer: delegates core business logic to
    ``ClawDomainService`` and adds application-level concerns such as
    the SSE event bus, background task scheduling, and chat state tracking.
    """

    def __init__(self, claw_domain_service: ClawDomainService):
        self.domain = claw_domain_service
        self.claw_repository = claw_domain_service.claw_repository
        self.settings = get_settings()
        self._active_user_id: Optional[str] = None
        self.event_bus = ClawEventBus()
        self._bg_tasks: set[asyncio.Task] = set()
        self._chat_states: dict[tuple[str, str], _ChatState] = {}
        self._maintenance_task: Optional[asyncio.Task] = None
        self._local_provision_locks: dict[str, str] = {}
        self._local_creation_locks: dict[str, str] = {}
        self._provision_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Delegates to domain service
    # ------------------------------------------------------------------

    async def get_claw(self, user_id: str) -> Optional[Claw]:
        return await self.domain.get_claw(user_id)

    async def get_claw_by_api_key(self, api_key: str) -> Optional[Claw]:
        return await self.domain.get_claw_by_api_key(api_key)

    async def get_history(self, user_id: str) -> List[ClawMessage]:
        return await self.domain.get_history(user_id)

    async def delete_claw(self, user_id: str) -> bool:
        claw = await self.claw_repository.get_by_user_id(user_id)
        if not claw:
            raise NotFoundError("No claw instance found")

        task = self._provision_tasks.get(claw.id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif claw.status == ClawStatus.CREATING:
            # A different application replica may own the provisioning lease.
            # Never delete the record underneath that replica: it would lose
            # the only durable ownership pointer to a partially-created
            # container.
            lock_key = f"claw:provision:{claw.id}"
            try:
                provisioning_owned = bool(
                    await get_redis().client.exists(lock_key)
                )
            except Exception as e:
                # Redis is the cross-replica source of truth.  During an
                # outage, failing closed is safer than racing a provisioner.
                logger.error(
                    "[claw] cannot verify provisioning ownership; refusing "
                    "delete for id=%s: %s",
                    claw.id,
                    safe_exception_summary(e),
                )
                raise ServiceUnavailableError(
                    "Cannot verify Claw provisioning ownership; please retry"
                )
            if provisioning_owned:
                logger.warning(
                    "[claw] refusing delete while provisioning is owned "
                    "by another replica: id=%s",
                    claw.id,
                )
                raise ConflictError(
                    "Claw provisioning is still in progress"
                )

            # Even without a Redis key, a lease may have just expired while a
            # remote process is still unwinding a blocked runtime call.  Only
            # the domain timeout path is allowed to classify and clean a stale
            # CREATING record; a recent one remains fail-closed.
            claw = await self.domain.get_claw(user_id)
            if claw and claw.status == ClawStatus.CREATING:
                logger.warning(
                    "[claw] refusing delete for in-flight provisioning "
                    "without a visible lease: id=%s",
                    claw.id,
                )
                raise ConflictError(
                    "Claw provisioning is still unwinding; please retry"
                )

        deleted = await self.domain.delete_claw(user_id)
        if not deleted:
            raise ServiceUnavailableError(
                "Claw runtime cleanup failed; ownership was retained for retry"
            )
        return True

    def start_maintenance(self) -> None:
        if self._maintenance_task and not self._maintenance_task.done():
            return
        if self.settings.claw_cleanup_interval_seconds <= 0:
            return
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._bg_tasks.add(self._maintenance_task)
        self._maintenance_task.add_done_callback(self._bg_tasks.discard)

    async def shutdown(self) -> None:
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        for task in list(self.event_bus._subscriber_tasks.values()):
            task.cancel()
        tasks.extend(self.event_bus._subscriber_tasks.values())
        if tasks:
            await asyncio.gather(*set(tasks), return_exceptions=True)
        self.event_bus._subscriber_tasks.clear()

    async def _maintenance_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.claw_cleanup_interval_seconds)
                try:
                    result = await self.domain.cleanup_instances()
                    if result.get("removed") or result.get("errored"):
                        logger.info("[claw-cleanup] result=%s", result)
                except Exception as e:
                    logger.warning(
                        "[claw-cleanup] failed: %s",
                        safe_exception_summary(e),
                    )
        except asyncio.CancelledError:
            pass

    async def get_file(self, user_id: str, filename: str) -> tuple[bytes, str]:
        return await self.domain.get_file(user_id, filename)

    async def verify_api_key(self, api_key: str) -> Optional[str]:
        return await self.domain.verify_api_key(api_key)

    async def acquire_proxy_quota(self, user_id: str) -> Optional[str]:
        """Acquire a distributed rate/concurrency lease for one LLM request.

        Redis is the cross-replica authority.  Any Redis failure denies the
        request so a cache outage cannot silently disable spend controls.
        """
        now = int(time.time())
        rate_limit = max(1, int(self.settings.claw_proxy_requests_per_minute))
        concurrency_limit = max(
            1, int(self.settings.claw_proxy_max_concurrent_requests)
        )
        lease_seconds = max(
            30, int(self.settings.claw_proxy_request_lease_seconds)
        )
        lease_token = uuid.uuid4().hex
        # Hash tags keep both keys on the same Redis Cluster slot.
        key_prefix = f"claw:proxy:{{{user_id}}}"
        rate_key = f"{key_prefix}:rate:{now // 60}"
        active_key = f"{key_prefix}:active"
        try:
            acquired = bool(
                await get_redis().client.eval(
                    _ACQUIRE_PROXY_QUOTA_SCRIPT,
                    2,
                    rate_key,
                    active_key,
                    120,
                    rate_limit,
                    now,
                    concurrency_limit,
                    now + lease_seconds,
                    lease_token,
                    lease_seconds * 2,
                )
            )
        except Exception as exc:
            logger.error(
                "[claw-proxy] quota authority unavailable; denying user=%s: %s",
                user_id,
                safe_exception_summary(exc),
            )
            return None
        return lease_token if acquired else None

    async def release_proxy_quota(self, user_id: str, lease_token: str) -> None:
        active_key = f"claw:proxy:{{{user_id}}}:active"
        try:
            await get_redis().client.eval(
                _RELEASE_PROXY_QUOTA_SCRIPT,
                1,
                active_key,
                lease_token,
            )
        except Exception as exc:
            # The member has a bounded score/TTL and is pruned by the next
            # acquisition, so a failed release cannot permanently deadlock.
            logger.warning(
                "[claw-proxy] failed to release quota lease for user=%s: %s",
                user_id,
                safe_exception_summary(exc),
            )

    # ------------------------------------------------------------------
    # Claw creation – background provisioning
    # ------------------------------------------------------------------

    async def create_claw(self, user_id: str) -> Claw:
        user_lock_key = f"claw:create:user:{user_id}"
        capacity_lock_key = "claw:create:capacity"
        user_owner = None
        capacity_owner = None
        try:
            user_owner = await self._acquire_creation_lock(user_lock_key)
            if user_owner is None:
                existing = await self._wait_for_active_claw(user_id)
                if existing:
                    return existing
                raise RuntimeError(
                    "Claw creation is already in progress. Please retry."
                )

            # This short Redis critical section makes the domain's
            # count-by-status + insert/update capacity decision atomic across
            # application replicas.  It is released before slow runtime
            # provisioning begins.
            capacity_owner = await self._acquire_creation_lock(
                capacity_lock_key
            )
            if capacity_owner is None:
                raise RuntimeError(
                    "Claw capacity reservation is busy. Please retry."
                )
            claw = await self.domain.prepare_claw_for_creation(user_id)
        except _ProvisionLockUnavailable as exc:
            existing = await self.claw_repository.get_by_user_id(user_id)
            if existing and existing.status in {
                ClawStatus.CREATING,
                ClawStatus.RUNNING,
            }:
                return existing
            raise RuntimeError(
                "Claw creation coordination is unavailable. Please retry."
            ) from exc
        finally:
            if capacity_owner:
                await self._release_creation_lock(
                    capacity_lock_key, capacity_owner
                )
            if user_owner:
                await self._release_creation_lock(user_lock_key, user_owner)

        if claw.status == ClawStatus.RUNNING:
            return claw
        lock_key = f"claw:provision:{claw.id}"
        try:
            owner_token = await self._acquire_provision_lock(lock_key)
        except _ProvisionLockUnavailable as e:
            # Do not leave a record looking indefinitely in-flight when no
            # distributed coordination was established.
            claw.status = ClawStatus.ERROR
            claw.error_message = (
                "Claw provisioning coordination is unavailable. Please retry."
            )
            logger.error(
                "[claw] provisioning coordination failed for id=%s: %s",
                claw.id,
                safe_exception_summary(e),
            )
            await self.claw_repository.update(claw)
            return claw
        if owner_token is None:
            return claw
        task = asyncio.create_task(
            self._provision_in_background(claw, lock_key, owner_token)
        )
        self._bg_tasks.add(task)
        self._provision_tasks[claw.id] = task

        def _discard_provision_task(done_task: asyncio.Task) -> None:
            self._bg_tasks.discard(done_task)
            if self._provision_tasks.get(claw.id) is done_task:
                self._provision_tasks.pop(claw.id, None)

        task.add_done_callback(_discard_provision_task)
        return claw

    async def _wait_for_active_claw(
        self, user_id: str, attempts: int = 20
    ) -> Optional[Claw]:
        for _ in range(attempts):
            claw = await self.claw_repository.get_by_user_id(user_id)
            if claw and claw.status in {
                ClawStatus.CREATING,
                ClawStatus.RUNNING,
            }:
                return claw
            await asyncio.sleep(0.05)
        return None

    async def _acquire_creation_lock(self, lock_key: str) -> Optional[str]:
        if lock_key in self._local_creation_locks:
            return None
        owner_token = uuid.uuid4().hex
        self._local_creation_locks[lock_key] = owner_token
        try:
            acquired = bool(
                await get_redis().client.set(
                    lock_key,
                    owner_token,
                    nx=True,
                    ex=30,
                )
            )
            if not acquired:
                self._local_creation_locks.pop(lock_key, None)
                return None
            return owner_token
        except Exception as exc:
            self._local_creation_locks.pop(lock_key, None)
            logger.error(
                "[claw] creation coordination unavailable for %s: %s",
                lock_key,
                safe_exception_summary(exc),
            )
            raise _ProvisionLockUnavailable(
                "Claw creation coordination is unavailable"
            ) from exc

    async def _release_creation_lock(
        self, lock_key: str, owner_token: str
    ) -> bool:
        released = False
        try:
            released = bool(
                await get_redis().client.eval(
                    _RELEASE_PROVISION_LOCK_SCRIPT,
                    1,
                    lock_key,
                    owner_token,
                )
            )
        except Exception as exc:
            logger.warning(
                "[claw] failed to release creation lease for %s; "
                "waiting for TTL expiry: %s",
                lock_key,
                safe_exception_summary(exc),
            )
        finally:
            if self._local_creation_locks.get(lock_key) == owner_token:
                self._local_creation_locks.pop(lock_key, None)
        return released

    def _provision_lock_ttl_seconds(self) -> int:
        # The lease comfortably covers runtime creation plus readiness checks.
        # A renewal loop below keeps it alive for unexpectedly slow starts.
        return max(60, int(self.settings.claw_ready_timeout) + 60)

    def _provision_coordination_timeout_seconds(self) -> float:
        return max(
            1.0,
            min(10.0, self._provision_lock_ttl_seconds() / 4),
        )

    async def _acquire_provision_lock(self, lock_key: str) -> Optional[str]:
        if lock_key in self._local_provision_locks:
            return None
        owner_token = uuid.uuid4().hex
        self._local_provision_locks[lock_key] = owner_token
        try:
            acquired = bool(
                await asyncio.wait_for(
                    get_redis().client.set(
                        lock_key,
                        owner_token,
                        nx=True,
                        ex=self._provision_lock_ttl_seconds(),
                    ),
                    timeout=self._provision_coordination_timeout_seconds(),
                )
            )
            if not acquired:
                if self._local_provision_locks.get(lock_key) == owner_token:
                    self._local_provision_locks.pop(lock_key, None)
                return None
            return owner_token
        except Exception as e:
            if self._local_provision_locks.get(lock_key) == owner_token:
                self._local_provision_locks.pop(lock_key, None)
            logger.error(
                "[claw] provisioning lock unavailable; failing closed: %s",
                safe_exception_summary(e),
            )
            raise _ProvisionLockUnavailable(
                "Claw provisioning coordination is unavailable. Please retry."
            ) from e

    async def _release_provision_lock(
        self, lock_key: str, owner_token: str
    ) -> bool:
        released = False
        try:
            released = bool(
                await asyncio.wait_for(
                    get_redis().client.eval(
                        _RELEASE_PROVISION_LOCK_SCRIPT,
                        1,
                        lock_key,
                        owner_token,
                    ),
                    timeout=self._provision_coordination_timeout_seconds(),
                )
            )
        except Exception as e:
            # The key has a bounded TTL, so a failed release cannot become a
            # permanent lock.  Crucially, we never fall back to an unconditional
            # DEL because that could delete a newer owner's lease.
            logger.warning(
                "[claw] failed to release provisioning lease for %s; "
                "waiting for TTL expiry: %s",
                lock_key,
                safe_exception_summary(e),
            )
        finally:
            if self._local_provision_locks.get(lock_key) == owner_token:
                self._local_provision_locks.pop(lock_key, None)
        return released

    async def _renew_provision_lock(
        self, lock_key: str, owner_token: str
    ) -> None:
        ttl_seconds = self._provision_lock_ttl_seconds()
        renew_interval = max(1, min(30, ttl_seconds // 3))
        while True:
            try:
                renewed = bool(
                    await asyncio.wait_for(
                        get_redis().client.eval(
                            _RENEW_PROVISION_LOCK_SCRIPT,
                            1,
                            lock_key,
                            owner_token,
                            ttl_seconds,
                        ),
                        timeout=self._provision_coordination_timeout_seconds(),
                    )
                )
            except Exception as e:
                raise _ProvisionLockUnavailable(
                    "Redis failed while renewing the provisioning lease"
                ) from e
            if not renewed:
                raise _ProvisionLockUnavailable(
                    "Claw provisioning lease ownership was lost"
                )
            await asyncio.sleep(renew_interval)

    async def _mark_provision_coordination_failure(
        self, claw: Claw, message: str
    ) -> None:
        try:
            current = await self.claw_repository.get_by_user_id(claw.user_id)
            if not current:
                return
            current.status = ClawStatus.ERROR
            if current.error_message:
                current.error_message = f"{current.error_message}; {message}"
            else:
                current.error_message = message
            await self.claw_repository.update(current)
        except Exception as exc:
            logger.error(
                "[claw] failed to persist provisioning coordination error: "
                "id=%s error=%s",
                claw.id,
                safe_exception_summary(exc),
            )

    async def _provision_in_background(
        self, claw: Claw, lock_key: str, owner_token: str
    ) -> None:
        # Schedule the ownership check first so a Redis fault immediately
        # after SET NX fails closed before runtime creation begins.
        renewal_task = asyncio.create_task(
            self._renew_provision_lock(lock_key, owner_token)
        )
        provision_task = asyncio.create_task(
            self.domain.provision_claw_instance(
                claw, self.settings.claw_ttl_seconds
            )
        )
        try:
            done, _ = await asyncio.wait(
                {provision_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                coordination_error = renewal_task.exception()
                provision_task.cancel()
                await asyncio.gather(provision_task, return_exceptions=True)
                message = str(
                    coordination_error
                    or "Claw provisioning lease renewal stopped unexpectedly"
                )
                logger.error(
                    "[claw] provisioning lease renewal failed: id=%s error=%s",
                    claw.id,
                    safe_exception_summary(coordination_error)
                    if isinstance(coordination_error, BaseException)
                    else "renewal stopped",
                )
                await self._mark_provision_coordination_failure(claw, message)
                return

            # Propagate cancellation or an unexpected domain exception to the
            # outer lifecycle handler.  Normal provisioning failures are
            # persisted by the domain service and return normally.
            await provision_task
        except asyncio.CancelledError:
            provision_task.cancel()
            renewal_task.cancel()
            await asyncio.gather(
                provision_task, renewal_task, return_exceptions=True
            )
            raise
        finally:
            if not provision_task.done():
                provision_task.cancel()
            if not renewal_task.done():
                renewal_task.cancel()
            await asyncio.gather(
                provision_task, renewal_task, return_exceptions=True
            )
            await self._release_provision_lock(lock_key, owner_token)

    # ------------------------------------------------------------------
    # Chat  – fire-and-forget + event bus
    # ------------------------------------------------------------------

    @staticmethod
    def _chat_turn_lock_key(user_id: str, session_id: str) -> str:
        # User/session identifiers are untrusted and may contain Redis key
        # separators.  Hash the length-delimited pair to keep the key bounded
        # and collision-safe without leaking either identifier.
        identity = json.dumps(
            [user_id, session_id], ensure_ascii=False, separators=(",", ":")
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"claw:chat-turn:{digest}"

    def _chat_turn_lock_ttl_seconds(self) -> int:
        return max(3, int(self.settings.claw_chat_turn_lease_seconds))

    def _chat_coordination_timeout_seconds(self) -> float:
        # redis-py has a connect timeout in this project, but an established
        # TCP connection can still black-hole a command.  Keep every lock
        # operation well below the lease TTL so the old stream is cancelled
        # before another replica could acquire an expired key.
        return max(
            0.25,
            min(5.0, self._chat_turn_lock_ttl_seconds() / 4),
        )

    async def _acquire_chat_turn_lock(
        self, user_id: str, session_id: str
    ) -> tuple[str, str]:
        lock_key = self._chat_turn_lock_key(user_id, session_id)
        owner_token = uuid.uuid4().hex
        try:
            acquired = bool(
                await asyncio.wait_for(
                    get_redis().client.set(
                        lock_key,
                        owner_token,
                        nx=True,
                        ex=self._chat_turn_lock_ttl_seconds(),
                    ),
                    timeout=self._chat_coordination_timeout_seconds(),
                )
            )
        except Exception as exc:
            logger.error(
                "[claw-chat] turn coordination unavailable for user=%s: %s",
                user_id,
                safe_exception_summary(exc),
            )
            raise ServiceUnavailableError(
                "Claw chat coordination is unavailable; please retry"
            ) from exc
        if not acquired:
            raise ConflictError(
                "A Claw response is already in progress for this conversation"
            )
        return lock_key, owner_token

    async def _release_chat_turn_lock(
        self, lock_key: str, owner_token: str
    ) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    get_redis().client.eval(
                        _RELEASE_PROVISION_LOCK_SCRIPT,
                        1,
                        lock_key,
                        owner_token,
                    ),
                    timeout=self._chat_coordination_timeout_seconds(),
                )
            )
        except Exception as exc:
            # Never fall back to DEL: the lease might have expired and been
            # acquired by a newer turn.  Its TTL bounds recovery after Redis
            # becomes healthy again.
            logger.warning(
                "[claw-chat] failed to release turn lease %s: %s",
                lock_key,
                safe_exception_summary(exc),
            )
            return False

    async def _renew_chat_turn_lock(
        self, lock_key: str, owner_token: str
    ) -> None:
        ttl_seconds = self._chat_turn_lock_ttl_seconds()
        renew_interval = max(1, min(30, ttl_seconds // 3))
        while True:
            await asyncio.sleep(renew_interval)
            try:
                renewed = bool(
                    await asyncio.wait_for(
                        get_redis().client.eval(
                            _RENEW_PROVISION_LOCK_SCRIPT,
                            1,
                            lock_key,
                            owner_token,
                            ttl_seconds,
                        ),
                        timeout=self._chat_coordination_timeout_seconds(),
                    )
                )
            except Exception as exc:
                raise ServiceUnavailableError(
                    "Redis failed while renewing the Claw chat turn lease"
                ) from exc
            if not renewed:
                raise ConflictError("Claw chat turn lease ownership was lost")

    async def send_message(
        self, user_id: str, message: str, session_id: str = "default"
    ) -> None:
        """Accept one exclusively-owned chat turn and process it in background."""
        self._active_user_id = user_id

        if not isinstance(session_id, str):
            raise ValueError("Invalid Claw session id")
        session_id = session_id.strip() or "default"
        # The current Claw product has one conversation per user.  The event
        # bus and persisted history are therefore user-scoped; accepting a
        # caller-chosen second session would bypass the turn lease and mix its
        # events into the default conversation.
        if session_id != "default":
            raise ValueError("Only the default Claw conversation is supported")

        claw = await self.domain.validate_claw_for_chat(user_id)
        lock_key, owner_token = await self._acquire_chat_turn_lock(
            user_id, session_id
        )

        try:
            await self.claw_repository.append_message(user_id, "user", message)

            task = asyncio.create_task(
                self._process_chat(
                    user_id,
                    claw.http_base_url,
                    message,
                    session_id,
                    lock_key,
                    owner_token,
                )
            )
        except BaseException:
            await self._release_chat_turn_lock(lock_key, owner_token)
            raise
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_chat(
        self, user_id: str, base_url: str, message: str, session_id: str,
        lock_key: Optional[str] = None, owner_token: Optional[str] = None,
    ) -> None:
        """Background task: stream from claw, broadcast events, persist."""
        state = _ChatState()
        state_key = (user_id, session_id)
        self._chat_states[state_key] = state
        renewal_task: Optional[asyncio.Task] = None
        stream_task: Optional[asyncio.Task] = None

        async def _stream() -> None:
            async for chunk in self.domain.process_chat_stream(
                user_id, base_url, message, session_id,
            ):
                outbound = chunk
                if chunk.get("type") == "text" and chunk.get("content"):
                    state.raw_text += chunk["content"]
                    thinking_text = extract_model_thinking_text(state.raw_text)
                    visible_text = sanitize_model_text(state.raw_text)

                    if (
                        not visible_text
                        and thinking_text.startswith(state.pending_thinking)
                    ):
                        thinking_delta = thinking_text[len(state.pending_thinking):]
                        state.pending_thinking = thinking_text
                        if thinking_delta:
                            await self.event_bus.publish(
                                user_id,
                                {"type": "thinking", "content": thinking_delta},
                            )
                    elif thinking_text:
                        state.pending_thinking = thinking_text

                    if visible_text.startswith(state.pending_text):
                        visible_delta = visible_text[len(state.pending_text):]
                    else:
                        visible_delta = visible_text
                    state.pending_text = visible_text
                    if not visible_delta:
                        continue
                    outbound = {**chunk, "content": visible_delta}

                if chunk.get("type") != "done":
                    await self.event_bus.publish(user_id, outbound)

        try:
            if lock_key and owner_token:
                renewal_task = asyncio.create_task(
                    self._renew_chat_turn_lock(lock_key, owner_token)
                )
                stream_task = asyncio.create_task(_stream())
                done, _ = await asyncio.wait(
                    {renewal_task, stream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if renewal_task in done:
                    coordination_error = renewal_task.exception()
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                    raise coordination_error or ServiceUnavailableError(
                        "Claw chat turn lease renewal stopped unexpectedly"
                    )
                await stream_task
            else:
                # Kept for direct domain-stream unit tests.  Production calls
                # always provide a distributed lease via send_message().
                await _stream()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[claw-chat] background processing error for user=%s: %s",
                user_id,
                safe_exception_summary(e),
            )
            await self.event_bus.publish(
                user_id,
                {
                    "type": "error",
                    "error": "Claw response failed; please retry",
                },
            )
        finally:
            for task in (stream_task, renewal_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (stream_task, renewal_task) if task),
                return_exceptions=True,
            )
            await self.event_bus.publish(user_id, {"type": "done", "stop_reason": "end_turn"})
            if self._chat_states.get(state_key) is state:
                self._chat_states.pop(state_key, None)
            if lock_key and owner_token:
                await self._release_chat_turn_lock(lock_key, owner_token)

    def get_pending_content(
        self, user_id: str, session_id: str = "default"
    ) -> Optional[str]:
        """Return accumulated text for an in-progress response (for SSE catch-up)."""
        state = self._chat_states.get((user_id, session_id))
        if state and state.pending_text:
            return state.pending_text
        return None

    def get_pending_thinking_content(
        self, user_id: str, session_id: str = "default"
    ) -> Optional[str]:
        """Return thinking catch-up until visible answer text begins."""
        state = self._chat_states.get((user_id, session_id))
        if state and not state.pending_text and state.pending_thinking:
            return state.pending_thinking
        return None

    def is_processing(self, user_id: str, session_id: str = "default") -> bool:
        return (user_id, session_id) in self._chat_states

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_active_user_id(self) -> Optional[str]:
        return self._active_user_id
