import logging
import asyncio
import json
import uuid
from collections import defaultdict
from typing import Optional, List

from app.domain.models.claw import Claw, ClawMessage, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
from app.core.config import get_settings
from app.infrastructure.storage.redis import get_redis

logger = logging.getLogger(__name__)


class ClawEventBus:
    """Per-user event bus backed by Redis pub/sub with local in-process fanout."""

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
            logger.warning("[claw-bus] unable to start redis subscriber; using local events only")
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
                json.dumps({"origin": self._origin, "event": event}, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning("[claw-bus] redis publish failed: %s", e)

    async def _redis_subscribe(self, user_id: str, queue: asyncio.Queue) -> None:
        pubsub = get_redis().client.pubsub()
        try:
            await pubsub.subscribe(self._channel(user_id))
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
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[claw-bus] redis subscribe failed: %s", e)
        finally:
            try:
                await pubsub.unsubscribe(self._channel(user_id))
                await pubsub.close()
            except Exception:
                pass


class _ChatState:
    """Tracks an in-progress response so new SSE clients can catch up."""
    __slots__ = ("pending_text",)

    def __init__(self):
        self.pending_text = ""


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
        self._chat_states: dict[str, _ChatState] = {}
        self._maintenance_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Delegates to domain service
    # ------------------------------------------------------------------

    async def get_or_create_api_key(self, user_id: str) -> str:
        return await self.domain.get_or_create_api_key(user_id)

    async def get_claw(self, user_id: str) -> Optional[Claw]:
        return await self.domain.get_claw(user_id)

    async def get_claw_by_api_key(self, api_key: str) -> Optional[Claw]:
        return await self.domain.get_claw_by_api_key(api_key)

    async def get_history(self, user_id: str) -> List[ClawMessage]:
        return await self.domain.get_history(user_id)

    async def delete_claw(self, user_id: str) -> bool:
        claw = await self.claw_repository.get_by_user_id(user_id)
        deleted = await self.domain.delete_claw(user_id)
        if claw:
            await self._release_provision_lock(f"claw:provision:{claw.id}")
        return deleted

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
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _maintenance_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.claw_cleanup_interval_seconds)
                try:
                    result = await self.domain.cleanup_instances()
                    if result.get("removed") or result.get("errored"):
                        logger.info("[claw-cleanup] result=%s", result)
                except Exception as e:
                    logger.warning("[claw-cleanup] failed: %s", e)
        except asyncio.CancelledError:
            pass

    async def get_file(self, user_id: str, filename: str) -> tuple[bytes, str]:
        return await self.domain.get_file(user_id, filename)

    async def verify_api_key(self, api_key: str) -> Optional[str]:
        return await self.domain.verify_api_key(api_key, self.settings.claw_api_key)

    # ------------------------------------------------------------------
    # Claw creation – background provisioning
    # ------------------------------------------------------------------

    async def create_claw(self, user_id: str) -> Claw:
        claw = await self.domain.prepare_claw_for_creation(user_id)
        if claw.status == ClawStatus.RUNNING:
            return claw
        lock_key = f"claw:provision:{claw.id}"
        if not await self._acquire_provision_lock(lock_key):
            return claw
        task = asyncio.create_task(self._provision_in_background(claw, lock_key))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return claw

    async def _acquire_provision_lock(self, lock_key: str) -> bool:
        try:
            return bool(await get_redis().client.set(
                lock_key,
                "1",
                nx=True,
                ex=self.settings.claw_ready_timeout + 60,
            ))
        except Exception as e:
            logger.warning("[claw] provisioning lock unavailable, proceeding locally: %s", e)
            return True

    async def _release_provision_lock(self, lock_key: str) -> None:
        try:
            await get_redis().client.delete(lock_key)
        except Exception:
            pass

    async def _provision_in_background(self, claw: Claw, lock_key: str) -> None:
        try:
            await self.domain.provision_claw_instance(claw, self.settings.claw_ttl_seconds)
        finally:
            await self._release_provision_lock(lock_key)

    # ------------------------------------------------------------------
    # Chat  – fire-and-forget + event bus
    # ------------------------------------------------------------------

    async def send_message(self, user_id: str, message: str, session_id: str = "default") -> None:
        """Accept a user message and kick off background processing."""
        self._active_user_id = user_id

        claw = await self.domain.validate_claw_for_chat(user_id)

        await self.claw_repository.append_message(user_id, "user", message)

        task = asyncio.create_task(
            self._process_chat(user_id, claw.http_base_url, message, session_id)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_chat(
        self, user_id: str, base_url: str, message: str, session_id: str,
    ) -> None:
        """Background task: stream from claw, broadcast events, persist."""
        state = _ChatState()
        self._chat_states[user_id] = state

        try:
            async for chunk in self.domain.process_chat_stream(
                user_id, base_url, message, session_id,
            ):
                if chunk.get("type") == "text" and chunk.get("content"):
                    state.pending_text += chunk["content"]

                if chunk.get("type") != "done":
                    await self.event_bus.publish(user_id, chunk)

        except Exception as e:
            logger.error(f"[claw-chat] background processing error for user={user_id}: {e}")
            await self.event_bus.publish(user_id, {"type": "error", "error": str(e)})
        finally:
            await self.event_bus.publish(user_id, {"type": "done", "stop_reason": "end_turn"})
            self._chat_states.pop(user_id, None)

    def get_pending_content(self, user_id: str) -> Optional[str]:
        """Return accumulated text for an in-progress response (for SSE catch-up)."""
        state = self._chat_states.get(user_id)
        if state and state.pending_text:
            return state.pending_text
        return None

    def is_processing(self, user_id: str) -> bool:
        return user_id in self._chat_states

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_active_user_id(self) -> Optional[str]:
        return self._active_user_id
