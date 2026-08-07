import logging
import asyncio
import hashlib
import inspect
import io
import json
import time
import uuid
from collections import defaultdict
from typing import Optional, List

from app.domain.external.claw import ClawResponseTooLargeError
from app.domain.external.coordination import mark_lifecycle_task_lease_lost
from app.domain.models.claw import Claw, ClawMessage, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
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


class _ClawResponseDeadlineError(RuntimeError):
    """Raised when one Claw turn exceeds its monotonic total deadline."""


_TERMINAL_CLAW_EVENT_TYPES = frozenset({"done", "error"})
_COALESCIBLE_CLAW_EVENT_TYPES = frozenset({"text"})


class _QueuedClawEvent:
    """Queue entry that can accumulate adjacent text without repeated joins."""

    __slots__ = ("event", "content_buffer", "is_terminal", "byte_size")

    def __init__(self, event: dict):
        self.event = dict(event)
        content = self.event.get("content")
        self.content_buffer: Optional[io.StringIO] = None
        if (
            self.event.get("type") in _COALESCIBLE_CLAW_EVENT_TYPES
            and isinstance(content, str)
        ):
            self.content_buffer = io.StringIO()
            self.content_buffer.write(content)
            self.event.pop("content", None)
        self.is_terminal = self.event.get("type") in _TERMINAL_CLAW_EVENT_TYPES
        self.byte_size = len(
            json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
        )

    def try_merge(self, event: dict, max_byte_size: int) -> int:
        if self.content_buffer is None:
            return 0
        content = event.get("content")
        if not isinstance(content, str):
            return 0
        metadata = dict(event)
        metadata.pop("content", None)
        if metadata != self.event:
            return 0
        # Match the actual serialized JSON footprint, including expansion of
        # quotes, backslashes and control characters.  Raw UTF-8 length alone
        # can undercount a slow subscriber's queue by up to 6x.
        added_bytes = max(
            0,
            len(
                json.dumps(content, ensure_ascii=False).encode("utf-8")
            ) - 2,
        )
        if self.byte_size + added_bytes > max_byte_size:
            return 0
        self.content_buffer.write(content)
        self.byte_size += added_bytes
        return added_bytes

    def materialize(self) -> dict:
        if self.content_buffer is None:
            return dict(self.event)
        return {**self.event, "content": self.content_buffer.getvalue()}


class _ClawSubscriberQueue(asyncio.Queue):
    """Bound ordinary events while reserving lossless terminal delivery.

    ``asyncio.Queue(maxsize=N)`` drops the very event that lets a WebSocket
    stop its spinner when a slow client fills the queue.  This queue instead
    coalesces adjacent text chunks, evicts only old non-terminal
    events at the ordinary-event cap, and always retains ``error``/``done``.
    """

    def __init__(
        self,
        max_non_terminal_events: int = 200,
        max_buffer_bytes: int = 512 * 1024,
        max_terminal_events: int = 2,
    ):
        super().__init__(maxsize=0)
        self._max_non_terminal_events = max(1, max_non_terminal_events)
        self._max_buffer_bytes = max(1024, max_buffer_bytes)
        self._max_terminal_events = max(1, max_terminal_events)
        self._non_terminal_events = 0
        self._terminal_events = 0
        self._buffered_bytes = 0

    def put_nowait(self, event: dict) -> None:
        event_type = event.get("type")
        if (
            event_type == "done"
            and self._queue
            and self._queue[-1].event.get("type") == "done"
        ):
            return
        # Once a done is queued, the next non-done event belongs to a newer
        # turn.  A subscriber that missed an entire turn needs the latest turn,
        # not an unbounded history of completed turns.
        if event_type != "done" and any(
            queued.event.get("type") == "done" for queued in self._queue
        ):
            self._clear_buffer()

        if event_type in _TERMINAL_CLAW_EVENT_TYPES:
            event = self._bounded_terminal_event(event)
        queued = _QueuedClawEvent(event)
        if not queued.is_terminal:
            if self._queue:
                last = self._queue[-1]
                max_last_size = (
                    self._max_buffer_bytes
                    - self._buffered_bytes
                    + last.byte_size
                )
                merged_bytes = last.try_merge(event, max_last_size)
                if merged_bytes:
                    self._buffered_bytes += merged_bytes
                    return
            if self._non_terminal_events >= self._max_non_terminal_events:
                self._evict_oldest_non_terminal()
        else:
            while self._terminal_events >= self._max_terminal_events:
                if not self._evict_oldest_terminal():
                    break

        while self._buffered_bytes + queued.byte_size > self._max_buffer_bytes:
            if self._evict_oldest_non_terminal():
                continue
            if queued.is_terminal and self._evict_oldest_terminal():
                continue
            # An oversized non-terminal event is disposable; retaining it
            # would defeat the subscriber memory bound.
            return

        if queued.is_terminal:
            self._terminal_events += 1
        else:
            self._non_terminal_events += 1
        self._buffered_bytes += queued.byte_size
        super().put_nowait(queued)

    def get_nowait(self) -> dict:
        queued = super().get_nowait()
        self._buffered_bytes -= queued.byte_size
        if queued.is_terminal:
            self._terminal_events -= 1
        else:
            self._non_terminal_events -= 1
        return queued.materialize()

    @property
    def buffered_bytes(self) -> int:
        return self._buffered_bytes

    def _bounded_terminal_event(self, event: dict) -> dict:
        if _QueuedClawEvent(event).byte_size <= self._max_buffer_bytes:
            return event
        if event.get("type") == "error":
            return {
                "type": "error",
                "error": "Claw response failed; please retry",
            }
        return {"type": "done", "stop_reason": "end_turn"}

    def _evict_oldest_non_terminal(self) -> bool:
        for index, queued in enumerate(self._queue):
            if queued.is_terminal:
                continue
            self._remove_at(index)
            return True
        return False

    def _evict_oldest_terminal(self) -> bool:
        for index, queued in enumerate(self._queue):
            if queued.is_terminal:
                self._remove_at(index)
                return True
        return False

    def _clear_buffer(self) -> None:
        while self._queue:
            self._remove_at(0)

    def _remove_at(self, index: int) -> None:
        queued = self._queue[index]
        del self._queue[index]
        self._buffered_bytes -= queued.byte_size
        if queued.is_terminal:
            self._terminal_events -= 1
        else:
            self._non_terminal_events -= 1
        # Keep Queue.join()/task_done() accounting correct even though the
        # WebSocket consumer itself does not currently use those methods.
        self._unfinished_tasks -= 1
        if self._unfinished_tasks <= 0:
            self._finished.set()


class ClawEventBus:
    """Per-user event bus backed by Redis with local in-process fanout."""

    def __init__(self):
        self._subscribers: dict[str, list[_ClawSubscriberQueue]] = defaultdict(list)
        self._subscriber_tasks: dict[_ClawSubscriberQueue, asyncio.Task] = {}
        self._origin = str(uuid.uuid4())
        self._redis_publish_disabled_until = 0.0

    @staticmethod
    def _channel(user_id: str) -> str:
        return f"claw:events:{user_id}"

    def subscribe(self, user_id: str) -> _ClawSubscriberQueue:
        queue = _ClawSubscriberQueue(
            max_non_terminal_events=200,
            max_buffer_bytes=max(
                1024, int(get_settings().claw_event_queue_max_bytes)
            ),
        )
        self._subscribers[user_id].append(queue)
        try:
            task = asyncio.create_task(self._redis_subscribe(user_id, queue))
            self._subscriber_tasks[queue] = task
        except RuntimeError:
            logger.warning(
                "[claw-bus] unable to start redis subscriber; using local events only"
            )
        return queue

    def unsubscribe(self, user_id: str, queue: _ClawSubscriberQueue):
        subs = self._subscribers.get(user_id)
        if subs:
            self._subscribers[user_id] = [q for q in subs if q is not queue]
        task = self._subscriber_tasks.pop(queue, None)
        if task:
            task.cancel()

    async def publish(self, user_id: str, event: dict):
        for queue in self._subscribers.get(user_id, []):
            queue.put_nowait(event)
        loop = asyncio.get_running_loop()
        if loop.time() < self._redis_publish_disabled_until:
            return
        try:
            async with asyncio.timeout(self._redis_publish_timeout_seconds()):
                await get_redis().client.publish(
                    self._channel(user_id),
                    json.dumps(
                        {"origin": self._origin, "event": event}, ensure_ascii=False
                    ),
                )
        except Exception as e:
            self._redis_publish_disabled_until = loop.time() + 5.0
            logger.warning(
                "[claw-bus] redis publish failed: %s",
                safe_exception_summary(e),
            )

    @staticmethod
    def _redis_publish_timeout_seconds() -> float:
        return 2.0

    async def _redis_subscribe(
        self, user_id: str, queue: _ClawSubscriberQueue
    ) -> None:
        channel = self._channel(user_id)
        while True:
            pubsub = None
            cancelled = False
            try:
                pubsub = get_redis().client.pubsub()
                await self._await_redis_operation(pubsub.subscribe(channel))
                while True:
                    # redis-py's ``listen()`` performs an unbounded socket
                    # read.  A half-open connection would therefore leave the
                    # subscriber task stuck forever and silently miss every
                    # later terminal event.  ``get_message`` has its own poll
                    # timeout and the outer monotonic timeout remains the hard
                    # bound even if a client implementation ignores it.
                    message = await self._await_redis_operation(
                        pubsub.get_message(
                            ignore_subscribe_messages=False,
                            timeout=self._redis_read_poll_seconds(),
                        )
                    )
                    if not message:
                        continue
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
                    queue.put_nowait(event)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as e:
                logger.warning(
                    "[claw-bus] redis subscribe failed; retrying: %s",
                    safe_exception_summary(e),
                )
            finally:
                if pubsub is not None:
                    try:
                        await self._await_redis_operation(
                            pubsub.unsubscribe(channel)
                        )
                    except Exception as e:
                        logger.warning(
                            "[claw-bus] redis unsubscribe cleanup failed: %s",
                            safe_exception_summary(e),
                        )
                    try:
                        close = getattr(pubsub, "aclose", None) or pubsub.close
                        close_result = close()
                        if inspect.isawaitable(close_result):
                            await self._await_redis_operation(close_result)
                    except Exception as e:
                        logger.warning(
                            "[claw-bus] redis pubsub close failed: %s",
                            safe_exception_summary(e),
                        )
            if cancelled:
                break
            # Reconnect only after the failed connection has been bounded and
            # closed; delaying cleanup first leaves a known one-second event
            # loss window on every half-open read.
            await asyncio.sleep(1)

    async def _await_redis_operation(self, awaitable):
        """Hard-bound pubsub setup/cleanup, even during service shutdown."""
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=self._redis_operation_timeout_seconds()
            )
        except asyncio.CancelledError:
            task.cancel()
            self._detach_task(task)
            raise
        if not done:
            task.cancel()
            self._detach_task(task)
            raise TimeoutError("Redis pubsub operation timed out")
        return task.result()

    @staticmethod
    def _detach_task(task: asyncio.Future) -> None:
        def _consume_result(completed: asyncio.Future) -> None:
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_consume_result)

    @staticmethod
    def _redis_operation_timeout_seconds() -> float:
        return 2.0

    @staticmethod
    def _redis_read_poll_seconds() -> float:
        return 1.0


_HIDDEN_TAG_OPENERS = (
    ("<think", "think"),
    ("<thinking", "thinking"),
    ("<reasoning", "reasoning"),
)
_HIDDEN_FENCE_OPENERS = tuple(
    (f"```{name}", name)
    for name in ("think", "thinking", "thought", "reasoning")
)
_HIDDEN_TOKEN_OPENERS = tuple(
    f"<|{prefix}_of_{name}|>"
    for prefix in ("begin", "start")
    for name in ("thinking", "thought", "reasoning")
)
_HIDDEN_TOKEN_CLOSERS = tuple(
    f"<|{prefix}_of_{name}|>"
    for prefix in ("end", "stop")
    for name in ("thinking", "thought", "reasoning")
)
_FIXED_HIDDEN_OPENERS = (
    _HIDDEN_TOKEN_OPENERS
    + tuple(opener for opener, _ in _HIDDEN_TAG_OPENERS)
    + tuple(opener for opener, _ in _HIDDEN_FENCE_OPENERS)
)


class _IncrementalClawOutputFilter:
    """Split visible and private output in one pass across arbitrary chunks.

    The previous implementation repeatedly joined and regex-scanned the whole
    answer for every chunk, making many small chunks quadratic.  This finite
    state parser retains only delimiter prefixes and whitespace tails while
    supporting the same tag, token, and fenced reasoning wrappers.
    """

    def __init__(self):
        self._mode = "visible"
        self._open_probe = ""
        self._close_probe = ""
        self._tag_name: Optional[str] = None
        self._fence_preamble: list[str] = []
        self._closers: tuple[str, ...] = ()
        self._visible_started = False
        self._visible_trailing: list[str] = []
        self._hidden_started = False
        self._hidden_trailing: list[str] = []

    def feed(self, content: str) -> tuple[str, str]:
        visible: list[str] = []
        thinking: list[str] = []
        for char in content:
            self._process_char(char, visible, thinking)
        return "".join(visible), "".join(thinking)

    def finish(self) -> tuple[str, str]:
        """Finish a normally-ended stream without exposing partial markers."""
        visible: list[str] = []
        thinking: list[str] = []
        if self._mode == "hidden" and self._close_probe:
            # An incomplete closing delimiter is still private model output.
            probe = self._close_probe
            self._close_probe = ""
            for char in probe:
                self._emit_hidden(char, thinking)
        # A partial opener is withheld: treating an unfinished private marker
        # as visible can briefly expose the reasoning it was meant to guard.
        self._open_probe = ""
        self._fence_preamble.clear()
        self._visible_trailing.clear()
        self._hidden_trailing.clear()
        return "".join(visible), "".join(thinking)

    def _process_char(
        self, char: str, visible: list[str], thinking: list[str]
    ) -> None:
        if self._mode == "visible":
            self._process_visible_char(char, visible, thinking)
            return
        if self._mode == "tag_attributes":
            if char == ">":
                tag_name = self._tag_name or "think"
                self._start_hidden((f"</{tag_name}>",))
            return
        if self._mode == "fence_preamble":
            if char == "\n":
                self._start_hidden(("```",))
            elif char.isspace():
                self._fence_preamble.append(char)
            else:
                literal = "".join(self._fence_preamble)
                self._fence_preamble.clear()
                self._mode = "visible"
                for literal_char in literal:
                    self._emit_visible(literal_char, visible)
                self._process_visible_char(char, visible, thinking)
            return
        self._process_hidden_char(char, visible, thinking)

    def _process_visible_char(
        self, char: str, visible: list[str], thinking: list[str]
    ) -> None:
        if not self._open_probe:
            if char not in {"<", "`"}:
                self._emit_visible(char, visible)
                return
            self._open_probe = char
        else:
            self._open_probe += char
        self._evaluate_open_probe(visible, thinking)

    def _evaluate_open_probe(
        self, visible: list[str], thinking: list[str]
    ) -> None:
        probe = self._open_probe.casefold()

        if probe in _HIDDEN_TOKEN_OPENERS:
            self._open_probe = ""
            self._start_hidden(_HIDDEN_TOKEN_CLOSERS)
            return

        for opener, tag_name in _HIDDEN_TAG_OPENERS:
            if not probe.startswith(opener):
                continue
            remainder = probe[len(opener):]
            if remainder == ">":
                self._open_probe = ""
                self._start_hidden((f"</{tag_name}>",))
                return
            if len(remainder) == 1 and remainder.isspace():
                self._tag_name = tag_name
                self._open_probe = ""
                self._mode = "tag_attributes"
                return

        for opener, _ in _HIDDEN_FENCE_OPENERS:
            if not probe.startswith(opener):
                continue
            remainder = probe[len(opener):]
            if remainder == "\n":
                self._open_probe = ""
                self._start_hidden(("```",))
                return
            if len(remainder) == 1 and remainder.isspace():
                self._fence_preamble = [self._open_probe]
                self._open_probe = ""
                self._mode = "fence_preamble"
                return

        if any(opener.startswith(probe) for opener in _FIXED_HIDDEN_OPENERS):
            return

        literal = self._open_probe
        self._open_probe = ""
        self._emit_visible(literal[0], visible)
        for remaining in literal[1:]:
            self._process_visible_char(remaining, visible, thinking)

    def _process_hidden_char(
        self, char: str, visible: list[str], thinking: list[str]
    ) -> None:
        if not self._close_probe:
            if any(closer.startswith(char.casefold()) for closer in self._closers):
                self._close_probe = char
            else:
                self._emit_hidden(char, thinking)
            return

        self._close_probe += char
        probe = self._close_probe.casefold()
        if probe in self._closers:
            self._close_probe = ""
            self._hidden_trailing.clear()
            self._mode = "visible"
            self._closers = ()
            return
        if any(closer.startswith(probe) for closer in self._closers):
            return

        literal = self._close_probe
        self._close_probe = ""
        self._emit_hidden(literal[0], thinking)
        for remaining in literal[1:]:
            self._process_hidden_char(remaining, visible, thinking)

    def _start_hidden(self, closers: tuple[str, ...]) -> None:
        self._mode = "hidden"
        self._closers = tuple(closer.casefold() for closer in closers)
        self._tag_name = None
        self._fence_preamble.clear()
        self._hidden_started = False
        self._hidden_trailing.clear()

    def _emit_visible(self, char: str, output: list[str]) -> None:
        if char.isspace():
            if self._visible_started:
                self._visible_trailing.append(char)
            return
        if self._visible_trailing:
            output.extend(self._visible_trailing)
            self._visible_trailing.clear()
        self._visible_started = True
        output.append(char)

    def _emit_hidden(self, char: str, output: list[str]) -> None:
        if char.isspace():
            if self._hidden_started:
                self._hidden_trailing.append(char)
            return
        if self._hidden_trailing:
            output.extend(self._hidden_trailing)
            self._hidden_trailing.clear()
        self._hidden_started = True
        output.append(char)


class _ChatState:
    """Tracks an in-progress response so new WebSocket clients can catch up."""
    __slots__ = ("_pending_text", "_terminal_event")

    def __init__(self):
        self._pending_text: list[str] = []
        self._terminal_event: Optional[dict] = None

    @property
    def pending_text(self) -> str:
        return "".join(self._pending_text)

    @pending_text.setter
    def pending_text(self, value: str) -> None:
        self._pending_text = [value] if value else []

    def append_text(self, value: str) -> None:
        if value:
            self._pending_text.append(value)

    @property
    def terminal_event(self) -> Optional[dict]:
        if self._terminal_event is None:
            return None
        return dict(self._terminal_event)

    def latch_terminal(self, event: dict) -> None:
        """Expose terminal state before local fanout can yield to Redis I/O."""
        self._terminal_event = dict(event)


class ClawService:
    """Application service for managing OpenClaw instances.

    Thin orchestration layer: delegates core business logic to
    ``ClawDomainService`` and adds application-level concerns such as
    the WebSocket event bus, background task scheduling, and chat state tracking.
    """

    def __init__(self, claw_domain_service: ClawDomainService):
        self.domain = claw_domain_service
        self.claw_repository = claw_domain_service.claw_repository
        self.settings = get_settings()
        self._active_user_id: Optional[str] = None
        self.event_bus = ClawEventBus()
        self._bg_tasks: set[asyncio.Task] = set()
        self._detached_chat_tasks: set[asyncio.Task] = set()
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

    async def validate_claw_for_chat(self, user_id: str) -> Claw:
        """Resolve this replica's exact owned runtime before direct HTTP I/O."""

        return await self.domain.validate_claw_for_chat(user_id)

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
        # A broken upstream may deliberately swallow cancellation.  Such a
        # task is retained and exception-consumed while the process is alive,
        # but shutdown must remain bounded as well.
        for task in list(self._detached_chat_tasks):
            await self._cancel_chat_task_bounded(task)

    @staticmethod
    def _chat_task_cleanup_timeout_seconds() -> float:
        return 1.0

    def _retain_detached_chat_task(self, task: asyncio.Task) -> None:
        """Keep cancellation-resistant upstream tasks owned and observed."""
        if task in self._detached_chat_tasks:
            return
        self._detached_chat_tasks.add(task)

        def _consume(completed: asyncio.Task) -> None:
            self._detached_chat_tasks.discard(completed)
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "[claw-chat] failed to inspect detached stream: %s",
                    safe_exception_summary(exc),
                )
                return
            if error is not None:
                logger.warning(
                    "[claw-chat] detached stream ended with error: %s",
                    safe_exception_summary(error),
                )

        task.add_done_callback(_consume)

    async def _cancel_chat_task_bounded(self, task: asyncio.Task) -> bool:
        """Try cancellation twice, then retain instead of awaiting forever."""
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return True
        timeout = max(0.001, self._chat_task_cleanup_timeout_seconds())
        for _ in range(2):
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if done:
                await asyncio.gather(task, return_exceptions=True)
                return True
        self._retain_detached_chat_task(task)
        logger.warning(
            "[claw-chat] upstream ignored two cancellation deadlines; "
            "retaining task until it exits"
        )
        return False

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
            if provision_task in done:
                # Provisioning may complete in the same event-loop turn as a
                # renewal fault. A completed lifecycle result is authoritative
                # and must not be cancelled retroactively.
                await provision_task
                return
            if renewal_task in done:
                coordination_error = renewal_task.exception()
                mark_lifecycle_task_lease_lost(provision_task)
                provision_task.cancel()
                await asyncio.gather(provision_task, return_exceptions=True)
                logger.error(
                    "[claw] provisioning lease renewal failed: id=%s error=%s",
                    claw.id,
                    safe_exception_summary(coordination_error)
                    if isinstance(coordination_error, BaseException)
                    else "renewal stopped",
                )
                # Do not mutate Mongo here. A newer lease owner may already
                # have adopted the same durable generation. The fenced task
                # deliberately leaves its pointer untouched for that owner.
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
        state_key = (user_id, session_id)
        state: Optional[_ChatState] = None

        try:
            await self.claw_repository.append_message(user_id, "user", message)

            # Install the in-flight marker synchronously with task creation so
            # a reconnect cannot observe an accepted/leased turn as idle before
            # the new background task receives its first event-loop timeslice.
            state = _ChatState()
            self._chat_states[state_key] = state
            task = asyncio.create_task(
                self._process_chat(
                    user_id,
                    claw.http_base_url,
                    message,
                    session_id,
                    lock_key,
                    owner_token,
                    state=state,
                )
            )
        except BaseException:
            if state is not None and self._chat_states.get(state_key) is state:
                self._chat_states.pop(state_key, None)
            await self._release_chat_turn_lock(lock_key, owner_token)
            raise
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_chat(
        self, user_id: str, base_url: str, message: str, session_id: str,
        lock_key: Optional[str] = None, owner_token: Optional[str] = None,
        *, state: Optional[_ChatState] = None,
    ) -> None:
        """Background task: stream from claw, broadcast events, persist."""
        state = state or _ChatState()
        state_key = (user_id, session_id)
        self._chat_states[state_key] = state
        renewal_task: Optional[asyncio.Task] = None
        stream_task: Optional[asyncio.Task] = None
        loop = asyncio.get_running_loop()
        max_duration = max(
            0.001, float(self.settings.claw_chat_max_duration_seconds)
        )
        stream_deadline = loop.time() + max_duration
        stop_stream = asyncio.Event()

        async def _stream() -> None:
            output_filter = _IncrementalClawOutputFilter()
            response_bytes = 0

            async def _publish_filtered(
                visible_delta: str,
                _thinking_delta: str,
                source_chunk: Optional[dict] = None,
            ) -> None:
                # Private model reasoning is deliberately discarded. The
                # integrated upstream Claw UX exposes only visible answer text.
                if visible_delta:
                    state.append_text(visible_delta)
                    outbound = {
                        **(source_chunk or {"type": "text"}),
                        "type": "text",
                        "content": visible_delta,
                    }
                    await self.event_bus.publish(user_id, outbound)

            async for chunk in self.domain.process_chat_stream(
                user_id, base_url, message, session_id,
            ):
                # The outer asyncio.wait timeout cannot run if an async
                # generator repeatedly yields without yielding control to the
                # event loop.  Check the same monotonic deadline in-band so
                # even that hostile/coincident path terminates.
                if stop_stream.is_set() or loop.time() >= stream_deadline:
                    raise _ClawResponseDeadlineError(
                        "Claw response exceeded the total turn deadline"
                    )
                outbound = chunk
                if chunk.get("type") == "text" and chunk.get("content"):
                    content = chunk["content"]
                    if not isinstance(content, str):
                        raise TypeError("Claw text chunks must contain strings")
                    response_bytes += len(content.encode("utf-8"))
                    if response_bytes > max(
                        1, int(self.settings.claw_chat_max_response_bytes)
                    ):
                        raise ClawResponseTooLargeError(
                            "Claw response exceeded the configured size limit"
                        )
                    visible_delta, thinking_delta = output_filter.feed(content)
                    await _publish_filtered(
                        visible_delta, thinking_delta, source_chunk=chunk
                    )
                    if not visible_delta:
                        continue
                    # The filtered text was published by _publish_filtered.
                    continue

                if chunk.get("type") != "done":
                    await self.event_bus.publish(user_id, outbound)

            if stop_stream.is_set() or loop.time() >= stream_deadline:
                raise _ClawResponseDeadlineError(
                    "Claw response exceeded the total turn deadline"
                )
            visible_delta, thinking_delta = output_filter.finish()
            await _publish_filtered(visible_delta, thinking_delta)

        try:
            stream_task = asyncio.create_task(_stream())
            if lock_key and owner_token:
                renewal_task = asyncio.create_task(
                    self._renew_chat_turn_lock(lock_key, owner_token)
                )
                done, _ = await asyncio.wait(
                    {renewal_task, stream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=max_duration,
                )
                if not done:
                    stop_stream.set()
                    raise _ClawResponseDeadlineError(
                        "Claw response exceeded the total turn deadline"
                    )
                if renewal_task in done:
                    coordination_error = renewal_task.exception()
                    stop_stream.set()
                    raise coordination_error or ServiceUnavailableError(
                        "Claw chat turn lease renewal stopped unexpectedly"
                    )
                await stream_task
            else:
                # Kept for direct domain-stream unit tests.  Production calls
                # always provide a distributed lease via send_message().
                done, _ = await asyncio.wait(
                    {stream_task}, timeout=max_duration
                )
                if not done:
                    stop_stream.set()
                    raise _ClawResponseDeadlineError(
                        "Claw response exceeded the total turn deadline"
                    )
                await stream_task
        except asyncio.CancelledError:
            stop_stream.set()
            raise
        except _ClawResponseDeadlineError:
            logger.warning(
                "[claw-chat] total response deadline exceeded for user=%s",
                user_id,
            )
            await self.event_bus.publish(
                user_id,
                {
                    "type": "error",
                    "error": "Claw response timed out; please retry",
                },
            )
        except ClawResponseTooLargeError:
            logger.warning(
                "[claw-chat] response size limit exceeded for user=%s",
                user_id,
            )
            await self.event_bus.publish(
                user_id,
                {
                    "type": "error",
                    "error": "Claw response exceeded the configured size limit",
                },
            )
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
            stop_stream.set()
            for task in (stream_task, renewal_task):
                if task:
                    await self._cancel_chat_task_bounded(task)
            terminal_event = {"type": "done", "stop_reason": "end_turn"}
            # Establish the terminal latch synchronously before publish().
            # ClawEventBus performs local queue fanout before its first await,
            # so a reconnect either sees this latch or receives the queued
            # event; there is no state=true/event-missed spinner window.
            state.latch_terminal(terminal_event)
            try:
                await self.event_bus.publish(user_id, terminal_event)
            finally:
                if self._chat_states.get(state_key) is state:
                    self._chat_states.pop(state_key, None)
                if lock_key and owner_token:
                    await self._release_chat_turn_lock(lock_key, owner_token)

    def get_pending_content(
        self, user_id: str, session_id: str = "default"
    ) -> Optional[str]:
        """Return accumulated text for an in-progress response (for WS catch-up)."""
        state = self._chat_states.get((user_id, session_id))
        if state:
            pending_text = state.pending_text
            if pending_text:
                return pending_text
        return None

    def is_processing(self, user_id: str, session_id: str = "default") -> bool:
        return (user_id, session_id) in self._chat_states

    def get_terminal_event(
        self, user_id: str, session_id: str = "default"
    ) -> Optional[dict]:
        state = self._chat_states.get((user_id, session_id))
        return state.terminal_event if state else None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_active_user_id(self) -> Optional[str]:
        return self._active_user_id
