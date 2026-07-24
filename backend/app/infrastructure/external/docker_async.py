"""Bounded async bridge for the synchronous Docker SDK.

``asyncio.to_thread`` keeps the event loop responsive, but cancelling its
awaiter cannot stop a function that is already running in a worker thread.
The optional keyed registry below deliberately retains those detached calls so
resource owners can wait for a late lifecycle mutation before deciding whether
creation or deletion was confirmed.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable, MutableMapping
from typing import Any, TypeVar


T = TypeVar("T")


class DockerCallTimeoutError(TimeoutError):
    """A synchronous Docker SDK call did not finish within its wait budget."""

    def __init__(self, operation: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Docker {operation} did not finish within {timeout_seconds:g} seconds"
        )
        self.operation = operation
        self.timeout_seconds = timeout_seconds


_inflight_calls: set[asyncio.Task[Any]] = set()
_MAX_INFLIGHT_DOCKER_CALLS = 16
_SERIALIZATION_STRIPES = 256
_loop_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.BoundedSemaphore
] = weakref.WeakKeyDictionary()
_loop_serialization_stripes: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[asyncio.BoundedSemaphore, ...]
] = weakref.WeakKeyDictionary()


def _semaphore_for_running_loop() -> asyncio.BoundedSemaphore:
    loop = asyncio.get_running_loop()
    semaphore = _loop_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.BoundedSemaphore(_MAX_INFLIGHT_DOCKER_CALLS)
        _loop_semaphores[loop] = semaphore
    return semaphore


def _serialization_semaphore_for_running_loop(
    key: str,
) -> asyncio.BoundedSemaphore:
    """Return a bounded-memory, per-loop single-flight gate for one key."""

    loop = asyncio.get_running_loop()
    stripes = _loop_serialization_stripes.get(loop)
    if stripes is None:
        stripes = tuple(
            asyncio.BoundedSemaphore(1) for _ in range(_SERIALIZATION_STRIPES)
        )
        _loop_serialization_stripes[loop] = stripes
    return stripes[hash(key) % len(stripes)]


def _retain_call(
    task: asyncio.Task[Any],
    *,
    registry: MutableMapping[str, asyncio.Task[Any]] | None,
    registry_key: str | None,
    semaphore: asyncio.BoundedSemaphore,
    serialization_semaphore: asyncio.BoundedSemaphore | None,
) -> None:
    """Keep a detached ``to_thread`` task alive and consume late failures."""

    _inflight_calls.add(task)
    if registry is not None and registry_key is not None:
        registry[registry_key] = task

    def done(completed: asyncio.Task[Any]) -> None:
        semaphore.release()
        if serialization_semaphore is not None:
            serialization_semaphore.release()
        _inflight_calls.discard(completed)
        if (
            registry is not None
            and registry_key is not None
            and registry.get(registry_key) is completed
        ):
            registry.pop(registry_key, None)
        if not completed.cancelled():
            # Retrieve a late exception so detached operations never produce
            # "Task exception was never retrieved" warnings.
            completed.exception()

    task.add_done_callback(done)


async def run_bounded_docker_call(
    function: Callable[[], T],
    *,
    timeout_seconds: float,
    operation: str,
    registry: MutableMapping[str, asyncio.Task[Any]] | None = None,
    registry_key: str | None = None,
    serialize_key: str | None = None,
) -> T:
    """Run one synchronous Docker call without blocking the event loop.

    Timeout and caller cancellation detach from the worker instead of
    cancelling its task, because cancelling an ``asyncio.to_thread`` task does
    not stop the underlying thread.  Callers that mutate provider resources can
    supply a keyed registry and retain the corresponding ownership pointer.
    """

    timeout_seconds = float(timeout_seconds)
    if timeout_seconds <= 0:
        raise ValueError("Docker call timeout must be positive")

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    semaphore = _semaphore_for_running_loop()
    serialization_semaphore = (
        _serialization_semaphore_for_running_loop(serialize_key)
        if serialize_key is not None
        else None
    )
    serialization_acquired = False
    capacity_acquired = False
    try:
        if serialization_semaphore is not None:
            await asyncio.wait_for(
                serialization_semaphore.acquire(), timeout=timeout_seconds
            )
            serialization_acquired = True
        remaining = timeout_seconds - (loop.time() - started_at)
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(semaphore.acquire(), timeout=remaining)
        capacity_acquired = True
    except TimeoutError as exc:
        if serialization_acquired:
            serialization_semaphore.release()
        raise DockerCallTimeoutError(operation, timeout_seconds) from exc
    except BaseException:
        if serialization_acquired:
            serialization_semaphore.release()
        raise

    remaining = timeout_seconds - (loop.time() - started_at)
    if remaining <= 0:
        semaphore.release()
        if serialization_acquired:
            serialization_semaphore.release()
        raise DockerCallTimeoutError(operation, timeout_seconds)
    try:
        task = asyncio.create_task(asyncio.to_thread(function))
    except BaseException:
        if capacity_acquired:
            semaphore.release()
        if serialization_acquired:
            serialization_semaphore.release()
        raise
    _retain_call(
        task,
        registry=registry,
        registry_key=registry_key,
        semaphore=semaphore,
        serialization_semaphore=serialization_semaphore,
    )
    done, _ = await asyncio.wait((task,), timeout=remaining)
    if task not in done:
        raise DockerCallTimeoutError(operation, timeout_seconds)
    return task.result()


async def wait_for_retained_docker_call(
    task: asyncio.Task[Any] | None,
    *,
    timeout_seconds: float,
    operation: str,
) -> bool:
    """Wait boundedly for a prior mutation before checking provider state.

    A failed late mutation still has a known outcome, so ``True`` means it is
    now safe to query the deterministic resource name. ``False`` means the
    mutation remains indeterminate and ownership must be retained.
    """

    if task is None or task.done():
        return True
    if task.get_loop() is not asyncio.get_running_loop():
        # Never treat an indeterminate mutation owned by another loop as
        # complete. Production uses one loop; this also fails closed in tests
        # and unusual embedded runtimes.
        return False
    done, _ = await asyncio.wait((task,), timeout=float(timeout_seconds))
    if task not in done:
        return False
    # Consume success/failure alike.  A partial create may exist after either,
    # and the subsequent provider lookup is the deletion authority.
    if not task.cancelled():
        task.exception()
    return True
