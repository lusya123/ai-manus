import asyncio
import io
import time
from types import MethodType

from app.domain.models.file import FileInfo
from app.domain.services import agent_task_runner as runner_module
from app.domain.services.agent_task_runner import AgentTaskRunner


def _bare_runner(agent_id: str) -> AgentTaskRunner:
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = agent_id
    runner._session_id = f"session-{agent_id}"
    runner._user_id = "user"
    runner._closing = False
    runner._artifact_cleanup_tasks = set()
    return runner


def test_artifact_url_filter_is_linear_for_large_untrusted_input():
    runner = _bare_runner("linear-url-filter")
    text = " ".join(
        f"https://x.test/?file=/report-{index}.md"
        for index in range(6_000)
    )
    text += " /home/ubuntu/upload/final.md"

    started = time.perf_counter()
    paths = runner._extract_artifact_paths(text)
    elapsed = time.perf_counter() - started

    assert paths == ["/home/ubuntu/upload/final.md"]
    # The former nested span scan took seconds for this shape. Keep ample CI
    # headroom while still catching a return to quadratic event-loop work.
    assert elapsed < 1.0


def test_artifact_lock_path_is_canonical_and_cannot_escape_relative_root():
    canonicalize = runner_module._canonical_artifact_lock_path

    assert canonicalize(
        "/home//ubuntu/upload/./draft/../report.md"
    ) == "/home/ubuntu/upload/report.md"
    assert canonicalize(
        "~/upload/./draft/../report.md"
    ) == "/home/ubuntu/upload/report.md"
    assert canonicalize("/../../etc/config.json") == "/etc/config.json"
    assert canonicalize("../../etc/config.json") == (
        "/home/ubuntu/upload/etc/config.json"
    )


async def test_canonical_path_aliases_share_one_artifact_lock():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    active = 0
    max_active = 0

    async def use_lock(file_path: str, *, hold: bool) -> None:
        nonlocal active, max_active
        if not hold:
            second_attempted.set()
        async with runner_module._serialize_artifact_path_sync(
            "shared-session",
            file_path,
        ):
            active += 1
            max_active = max(max_active, active)
            try:
                if hold:
                    first_entered.set()
                    await release_first.wait()
            finally:
                active -= 1

    first = asyncio.create_task(
        use_lock("/home/ubuntu/upload/report.md", hold=True)
    )
    await first_entered.wait()
    second = asyncio.create_task(
        use_lock(
            "/home//ubuntu/upload/draft/.././report.md",
            hold=False,
        )
    )
    await second_attempted.wait()

    # The second task has reached lock acquisition. With a non-canonical key
    # it completes the critical section synchronously and raises this to 2;
    # with the shared key it is deterministically suspended behind ``first``.
    assert max_active == 1

    release_first.set()
    await asyncio.gather(first, second)
    assert max_active == 1
    assert asyncio.get_running_loop() not in (
        runner_module._ARTIFACT_PATH_SYNCS_BY_LOOP
    )


async def test_shutdown_gate_rejects_new_enrichment_until_explicit_startup_reset():
    runner = _bare_runner("gate")
    called = 0

    async def sync(self, *args, **kwargs):
        nonlocal called
        called += 1
        return None

    runner._sync_file_to_storage = MethodType(sync, runner)
    loop = asyncio.get_running_loop()
    runner_module.begin_artifact_enrichment_shutdown()
    try:
        refused = await runner._sync_auto_artifact_before_deadline(
            "/home/ubuntu/upload/report.md",
            loop.time() + 0.1,
            source="test",
        )
        assert refused == (None, True)
        assert called == 0
    finally:
        runner_module.end_artifact_enrichment_shutdown()

    accepted = await runner._sync_auto_artifact_before_deadline(
        "/home/ubuntu/upload/report.md",
        loop.time() + 0.1,
        source="test",
    )
    assert accepted == (None, False)
    assert called == 1


def test_shutdown_gate_is_isolated_by_event_loop():
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    try:
        runner_module.begin_artifact_enrichment_shutdown(first_loop)
        assert runner_module._artifact_enrichment_is_shutting_down(first_loop)
        assert not runner_module._artifact_enrichment_is_shutting_down(second_loop)

        runner_module.end_artifact_enrichment_shutdown(first_loop)
        assert not runner_module._artifact_enrichment_is_shutting_down(first_loop)
    finally:
        runner_module.end_artifact_enrichment_shutdown(first_loop)
        runner_module.end_artifact_enrichment_shutdown(second_loop)
        first_loop.close()
        second_loop.close()


async def test_cancelled_path_waiter_releases_its_lock_reference():
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder():
        async with runner_module._serialize_artifact_path_sync(
            "session",
            "/home/ubuntu/upload/report.md",
        ):
            holder_entered.set()
            await release_holder.wait()

    async def waiter():
        async with runner_module._serialize_artifact_path_sync(
            "session",
            "/home/ubuntu/upload/report.md",
        ):
            raise AssertionError("cancelled waiter must not acquire the lock")

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    await asyncio.gather(waiter_task, return_exceptions=True)

    loop = asyncio.get_running_loop()
    entries = runner_module._ARTIFACT_PATH_SYNCS_BY_LOOP[loop]
    entry = entries[("session", "/home/ubuntu/upload/report.md")]
    assert entry.users == 1

    release_holder.set()
    await holder_task
    assert loop not in runner_module._ARTIFACT_PATH_SYNCS_BY_LOOP


async def test_done_task_registry_pruning_uses_a_stable_snapshot():
    class DoneTask:
        @staticmethod
        def done():
            return True

    loop = asyncio.get_running_loop()
    registries_and_accessors = (
        (
            runner_module._ARTIFACT_TASKS_BY_LOOP,
            runner_module._artifact_tasks_for_loop,
        ),
        (
            runner_module._DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP,
            runner_module._deferred_close_tasks_for_loop,
        ),
    )

    for registry, accessor in registries_and_accessors:
        registry[loop] = {DoneTask(), DoneTask()}
        try:
            assert accessor(loop) == set()
            assert loop not in registry
        finally:
            registry.pop(loop, None)


async def test_late_artifact_callback_preserves_replacement_loop_registry():
    runner = _bare_runner("late-artifact")
    loop = asyncio.get_running_loop()

    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    runner._track_artifact_cleanup_task(completed)
    assert runner_module._artifact_tasks_for_loop(loop) == set()

    release_replacement = asyncio.Event()
    replacement = asyncio.create_task(release_replacement.wait())
    runner._track_artifact_cleanup_task(replacement)
    replacement_registry = runner_module._ARTIFACT_TASKS_BY_LOOP[loop]
    try:
        # The callback registered on the already-completed task runs here,
        # after pruning replaced its old empty set with a new live registry.
        await asyncio.sleep(0)

        assert runner_module._ARTIFACT_TASKS_BY_LOOP.get(loop) is replacement_registry
        assert replacement in replacement_registry
    finally:
        release_replacement.set()
        await asyncio.gather(replacement, return_exceptions=True)
        await asyncio.sleep(0)
        runner_module._ARTIFACT_TASKS_BY_LOOP.pop(loop, None)


async def test_late_deferred_close_callback_preserves_replacement_loop_registry():
    loop = asyncio.get_running_loop()

    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    runner_module._retain_deferred_close_task(completed, agent_id="old")
    assert runner_module._deferred_close_tasks_for_loop(loop) == set()

    release_replacement = asyncio.Event()
    replacement = asyncio.create_task(release_replacement.wait())
    runner_module._retain_deferred_close_task(replacement, agent_id="new")
    replacement_registry = (
        runner_module._DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP[loop]
    )
    try:
        # The stale callback may clean only the set it captured; it must not
        # remove a newer registry installed under the same event-loop key.
        await asyncio.sleep(0)

        assert (
            runner_module._DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.get(loop)
            is replacement_registry
        )
        assert replacement in replacement_registry
    finally:
        release_replacement.set()
        await asyncio.gather(replacement, return_exceptions=True)
        await asyncio.sleep(0)
        runner_module._DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.pop(loop, None)


async def test_shutdown_drain_rechecks_cleanup_generations_until_stable():
    runner = _bare_runner("drain")
    successor_started = asyncio.Event()
    successor_cancelled = asyncio.Event()
    release_successor = asyncio.Event()

    async def successor():
        successor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            successor_cancelled.set()
            await release_successor.wait()

    async def first_generation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            next_task = asyncio.create_task(successor())
            runner._track_artifact_cleanup_task(next_task)

    first_task = asyncio.create_task(first_generation())
    runner._track_artifact_cleanup_task(first_task)
    await asyncio.sleep(0)

    runner_module.begin_artifact_enrichment_shutdown()
    try:
        drain_task = asyncio.create_task(
            runner_module.drain_artifact_enrichment_tasks(0.5)
        )
        await asyncio.wait_for(successor_started.wait(), timeout=0.1)
        await asyncio.wait_for(successor_cancelled.wait(), timeout=0.1)
        assert not drain_task.done()

        release_successor.set()
        assert await asyncio.wait_for(drain_task, timeout=0.2) == 0
    finally:
        runner_module.end_artifact_enrichment_shutdown()

    await asyncio.sleep(0)
    assert runner_module._artifact_tasks_for_loop() == set()


async def test_same_canonical_session_path_stays_serial_after_deadline_detaches():
    publish_started = asyncio.Event()
    release_first_publish = asyncio.Event()

    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        def __init__(self):
            self.current = None
            self.calls = 0
            self.active = 0
            self.max_active = 0

        async def get_file_by_path(self, session_id, file_path):
            return self.current

        async def upsert_file_by_path(self, session_id, file_info):
            previous = self.current
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if call == 1:
                    publish_started.set()
                    await release_first_publish.wait()
                self.current = file_info
                return previous
            finally:
                self.active -= 1

    class Storage:
        def __init__(self):
            self.uploads = 0

        async def upload_file(self, stream, filename, user_id, metadata=None):
            self.uploads += 1
            return FileInfo(
                file_id=f"file-{self.uploads}",
                filename=filename,
                metadata=metadata,
            )

    repository = Repository()
    storage = Storage()
    runners = [_bare_runner(f"serial-{index}") for index in range(2)]
    for runner in runners:
        runner._session_id = "shared-session"
        runner._sandbox = Sandbox()
        runner._session_repository = repository
        runner._file_storage = storage
        runner._generated_artifacts = {}
        runner._synced_artifacts = {}

    loop = asyncio.get_running_loop()
    first_result = await runners[0]._sync_auto_artifact_before_deadline(
        "/home/ubuntu/upload/report.md",
        loop.time() + 0.02,
        source="first",
        generated=True,
    )
    assert first_result == (None, True)
    await asyncio.wait_for(publish_started.wait(), timeout=0.1)

    second_task = asyncio.create_task(
        runners[1]._sync_auto_artifact_before_deadline(
            "/home//ubuntu/upload/draft/.././report.md",
            loop.time() + 0.5,
            source="second",
            generated=True,
        )
    )
    await asyncio.sleep(0.02)
    assert storage.uploads == 1
    assert repository.calls == 1
    assert repository.max_active == 1

    release_first_publish.set()
    second_result = await asyncio.wait_for(second_task, timeout=0.2)
    assert second_result[1] is False
    assert second_result[0].file_id == "file-2"
    assert repository.current.file_id == "file-2"
    assert repository.max_active == 1

    await asyncio.sleep(0)
    assert runner_module._artifact_tasks_for_loop() == set()
    assert asyncio.get_running_loop() not in runner_module._ARTIFACT_PATH_SYNCS_BY_LOOP


async def test_relative_resolution_and_absolute_alias_share_actual_path_lock():
    both_resolved = asyncio.Event()
    first_publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    class Sandbox:
        def __init__(self):
            self.actual_resolutions = 0

        async def file_find(self, path, glob_pattern):
            files = []
            if path == "/home/ubuntu" and glob_pattern == "report.md":
                files = ["/home/ubuntu/report.md"]
                self.actual_resolutions += 1
                if self.actual_resolutions == 2:
                    both_resolved.set()
            return type("Result", (), {"data": {"files": files}})()

        async def file_download(self, path):
            assert path == "/home/ubuntu/report.md"
            return io.BytesIO(b"report")

    class Repository:
        def __init__(self):
            self.current = None
            self.calls = 0
            self.active = 0
            self.max_active = 0

        async def get_file_by_path(self, session_id, file_path):
            return self.current

        async def upsert_file_by_path(self, session_id, file_info):
            previous = self.current
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if self.calls == 1:
                    first_publish_started.set()
                await release_publish.wait()
                self.current = file_info
                return previous
            finally:
                self.active -= 1

    class Storage:
        def __init__(self):
            self.uploads = 0

        async def upload_file(self, stream, filename, user_id, metadata=None):
            self.uploads += 1
            return FileInfo(
                file_id=f"file-{self.uploads}",
                filename=filename,
                metadata=metadata,
            )

    sandbox = Sandbox()
    repository = Repository()
    storage = Storage()
    runners = [_bare_runner(f"resolved-{index}") for index in range(2)]
    for runner in runners:
        runner._session_id = "shared-session"
        runner._sandbox = sandbox
        runner._session_repository = repository
        runner._file_storage = storage
        runner._generated_artifacts = {}
        runner._synced_artifacts = {}

    loop = asyncio.get_running_loop()
    first = asyncio.create_task(
        runners[0]._sync_auto_artifact_before_deadline(
            "report.md",
            loop.time() + 1,
            source="relative",
            generated=True,
        )
    )
    await first_publish_started.wait()
    second = asyncio.create_task(
        runners[1]._sync_auto_artifact_before_deadline(
            "/home//ubuntu/./report.md",
            loop.time() + 1,
            source="absolute",
            generated=True,
        )
    )
    await both_resolved.wait()

    # Resolution has completed in both tasks. The second task must now be
    # waiting on the first task's actual-path lock, before upload/upsert.
    assert storage.uploads == 1
    assert repository.calls == 1
    assert repository.max_active == 1

    release_publish.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result[0].file_path == "/home/ubuntu/report.md"
    assert second_result[0].file_path == "/home/ubuntu/report.md"
    assert repository.max_active == 1
    await asyncio.sleep(0)
    assert runner_module._artifact_tasks_for_loop() == set()
    assert loop not in runner_module._ARTIFACT_PATH_SYNCS_BY_LOOP


def test_celery_shutdown_closes_gate_without_reopening(monkeypatch):
    from app.infrastructure.external.task import celery_worker

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(celery_worker, "_loop", loop)
    runner_module.end_artifact_enrichment_shutdown(loop)
    try:
        celery_worker._drain_artifact_tasks_before_worker_exit()
        assert runner_module._artifact_enrichment_is_shutting_down(loop)
    finally:
        # Celery itself intentionally never performs this reset. The explicit
        # hook exists so a process-isolated unit test can release its loop.
        runner_module.end_artifact_enrichment_shutdown(loop)
        loop.close()


async def test_process_artifact_limit_is_shared_across_runners():
    started = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    runners = [_bare_runner(f"agent-{index}") for index in range(3)]
    for runner in runners:
        runner._MAX_BACKGROUND_ARTIFACT_CLEANUPS = 2

    for index, runner in enumerate(runners):
        async def blocked_sync(
            self,
            path,
            fallback_content=None,
            generated=False,
            *,
            _index=index,
        ):
            started[_index].set()
            await release.wait()
            return None

        runner._sync_file_to_storage = MethodType(blocked_sync, runner)

    loop = asyncio.get_running_loop()
    first = asyncio.create_task(
        runners[0]._sync_auto_artifact_before_deadline(
            "/home/ubuntu/upload/one.md",
            loop.time() + 1,
            source="test",
        )
    )
    await started[0].wait()
    second = asyncio.create_task(
        runners[1]._sync_auto_artifact_before_deadline(
            "/home/ubuntu/upload/two.md",
            loop.time() + 1,
            source="test",
        )
    )
    await started[1].wait()

    refused = await runners[2]._sync_auto_artifact_before_deadline(
        "/home/ubuntu/upload/three.md",
        loop.time() + 0.1,
        source="test",
    )

    assert refused == (None, True)
    assert not started[2].is_set()
    assert len(runner_module._artifact_tasks_for_loop()) == 2

    release.set()
    await asyncio.gather(first, second)
    await asyncio.sleep(0)
    assert runner_module._artifact_tasks_for_loop() == set()
    assert loop not in runner_module._ARTIFACT_TASKS_BY_LOOP


async def test_double_cancel_during_artifact_publish_keeps_known_outcome():
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        published = None
        cancelled = False

        async def get_file_by_path(self, session_id, file_path):
            return None

        async def upsert_file_by_path(self, session_id, file_info):
            publish_started.set()
            try:
                await release_publish.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.published = file_info
            return None

    class Storage:
        async def upload_file(self, stream, filename, user_id, metadata=None):
            return FileInfo(
                file_id="new-file",
                filename=filename,
                metadata=metadata,
            )

    runner = _bare_runner("agent")
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = Sandbox()
    runner._session_repository = Repository()
    runner._file_storage = Storage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    task = asyncio.create_task(
        runner._sync_file_to_storage(
            "/home/ubuntu/upload/report.md",
            generated=True,
        )
    )
    runner._track_artifact_cleanup_task(task)
    await publish_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert runner._session_repository.cancelled is False
    assert task not in runner._artifact_cleanup_tasks

    release_publish.set()
    result = await asyncio.wait_for(asyncio.shield(task), timeout=0.2)

    assert result.file_id == "new-file"
    assert runner._session_repository.published.file_id == "new-file"
    assert runner._session_repository.cancelled is False
    await asyncio.sleep(0)
    assert runner_module._artifact_tasks_for_loop() == set()


async def test_cancelled_failure_verification_completes_unpublished_rollback():
    reference_query_started = asyncio.Event()
    release_reference_query = asyncio.Event()
    reference_query_cancelled = False
    detached = asyncio.Event()
    delete_completed = asyncio.Event()

    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        async def get_file_by_path(self, session_id, file_path):
            return None

        async def upsert_file_by_path(self, session_id, file_info):
            raise ValueError("session was deleted before publish")

        async def count_file_references(self, file_id):
            nonlocal reference_query_cancelled
            reference_query_started.set()
            try:
                await release_reference_query.wait()
            except asyncio.CancelledError:
                reference_query_cancelled = True
                raise
            return 0

    class Storage:
        def __init__(self):
            self.uploaded = []
            self.deleted = []

        async def upload_file(self, stream, filename, user_id, metadata=None):
            self.uploaded.append("f1")
            return FileInfo(
                file_id="f1",
                filename=filename,
                metadata=metadata,
            )

        async def delete_file(self, file_id, user_id):
            self.deleted.append(file_id)
            delete_completed.set()
            return True

    runner = _bare_runner("cancelled-verification")
    runner._session_id = "session"
    runner._sandbox = Sandbox()
    runner._session_repository = Repository()
    runner._file_storage = Storage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}
    original_detach = runner._detach_artifact_cleanup_task

    def observe_detach(self, task):
        original_detach(task)
        detached.set()

    runner._detach_artifact_cleanup_task = MethodType(
        observe_detach,
        runner,
    )
    loop = asyncio.get_running_loop()
    caller = asyncio.create_task(
        runner._sync_auto_artifact_before_deadline(
            "/home/ubuntu/upload/report.md",
            loop.time() + 10,
            source="test",
            generated=True,
        )
    )

    await reference_query_started.wait()
    assert runner._file_storage.uploaded == ["f1"]
    assert len(runner_module._artifact_tasks_for_loop()) == 1

    caller.cancel()
    cancelled_result = await asyncio.gather(
        caller,
        return_exceptions=True,
    )
    assert isinstance(cancelled_result[0], asyncio.CancelledError)
    await detached.wait()
    assert reference_query_cancelled is False
    assert runner._artifact_cleanup_tasks == set()
    # Handle ownership is detached, but the process-wide capacity slot is
    # retained until verification and deterministic deletion have completed.
    assert len(runner_module._artifact_tasks_for_loop()) == 1
    assert runner._file_storage.deleted == []

    release_reference_query.set()
    await delete_completed.wait()
    for _ in range(100):
        if not runner_module._artifact_tasks_for_loop():
            break
        await asyncio.sleep(0)

    assert runner._file_storage.deleted == ["f1"]
    assert reference_query_cancelled is False
    assert runner_module._artifact_tasks_for_loop() == set()
    assert loop not in runner_module._ARTIFACT_TASKS_BY_LOOP


async def test_runner_close_defers_handles_until_owned_enrichment_stops():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    close_calls = []

    async def stubborn_prepare():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    class Resource:
        def __init__(self, name):
            self.name = name

        async def cleanup(self):
            close_calls.append(self.name)

        async def aclose(self):
            close_calls.append(self.name)

    runner = _bare_runner("agent")
    runner._close_lock = asyncio.Lock()
    runner._closed = False
    runner._close_task = None
    runner._ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS = 0.01
    runner._browser = Resource("browser")
    runner._mcp_tool = Resource("mcp")
    runner._llm = Resource("llm")
    runner._sandbox = Resource("sandbox")

    enrichment = asyncio.create_task(stubborn_prepare())
    runner._track_artifact_cleanup_task(enrichment)
    await started.wait()

    await asyncio.wait_for(runner.aclose(), timeout=0.1)

    assert cancelled.is_set()
    assert close_calls == []
    assert runner._closed is False

    release.set()
    await asyncio.wait_for(asyncio.shield(runner._close_task), timeout=0.2)

    assert close_calls == ["browser", "mcp", "llm", "sandbox"]
    assert runner._closed is True
    await runner.aclose()
    assert close_calls == ["browser", "mcp", "llm", "sandbox"]


async def test_publish_ownership_transfer_unblocks_runner_handle_close():
    publishing = asyncio.Event()
    release_publish = asyncio.Event()
    close_calls = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def cleanup(self):
            close_calls.append(self.name)

        async def aclose(self):
            close_calls.append(self.name)

    runner = _bare_runner("agent")
    runner._close_lock = asyncio.Lock()
    runner._closed = False
    runner._close_task = None
    runner._ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS = 0.05
    runner._browser = Resource("browser")
    runner._mcp_tool = Resource("mcp")
    runner._llm = Resource("llm")
    runner._sandbox = Resource("sandbox")

    async def transfer_after_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            runner._detach_artifact_cleanup_task(asyncio.current_task())
            publishing.set()
            while not release_publish.is_set():
                try:
                    await release_publish.wait()
                except asyncio.CancelledError:
                    continue

    enrichment = asyncio.create_task(transfer_after_cancel())
    runner._track_artifact_cleanup_task(enrichment)

    await asyncio.wait_for(runner.aclose(), timeout=0.2)

    assert publishing.is_set()
    assert runner._closed is True
    assert close_calls == ["browser", "mcp", "llm", "sandbox"]
    assert not enrichment.done()

    release_publish.set()
    await asyncio.wait_for(asyncio.shield(enrichment), timeout=0.2)


async def test_artifact_commit_then_network_error_never_deletes_new_blob():
    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        current = None

        async def get_file_by_path(self, session_id, file_path):
            return self.current

        async def upsert_file_by_path(self, session_id, file_info):
            self.current = file_info
            raise ConnectionError("reply lost after commit")

    class Storage:
        deleted = []

        async def upload_file(self, stream, filename, user_id, metadata=None):
            return FileInfo(
                file_id="new-file",
                filename=filename,
                metadata=metadata,
            )

        async def delete_file(self, file_id, user_id):
            self.deleted.append(file_id)
            return True

    runner = _bare_runner("agent")
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = Sandbox()
    runner._session_repository = Repository()
    runner._file_storage = Storage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    result = await runner._sync_file_to_storage(
        "/home/ubuntu/upload/report.md",
        generated=True,
    )

    assert result.file_id == "new-file"
    assert runner._session_repository.current.file_id == "new-file"
    assert runner._file_storage.deleted == []


async def test_screenshot_commit_then_network_error_never_deletes_new_blob():
    class Browser:
        async def screenshot(self):
            return b"png"

    class Repository:
        current = None

        async def add_file(self, session_id, file_info):
            self.current = file_info
            raise ConnectionError("reply lost after commit")

        async def count_file_references(self, file_id):
            return int(
                self.current is not None
                and self.current.file_id == file_id
            )

    class Storage:
        deleted = []

        async def upload_file(self, *args, **kwargs):
            return FileInfo(file_id="screenshot-file", filename="shot.png")

        async def delete_file(self, file_id, user_id):
            self.deleted.append(file_id)
            return True

    runner = _bare_runner("agent")
    runner._session_id = "session"
    runner._user_id = "user"
    runner._browser = Browser()
    runner._session_repository = Repository()
    runner._file_storage = Storage()

    result = await runner._get_browser_screenshot()

    assert result == "screenshot-file"
    assert runner._session_repository.current.file_id == "screenshot-file"
    assert runner._file_storage.deleted == []


async def test_session_deleted_between_upload_and_path_upsert_is_compensated():
    calls = []

    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        deleted = False

        async def get_file_by_path(self, session_id, file_path):
            calls.append(("get-path", session_id, file_path))
            if self.deleted:
                raise ValueError(f"Session {session_id} not found")
            return None

        async def upsert_file_by_path(self, session_id, file_info):
            calls.append(("upsert", session_id, file_info.file_id))
            self.deleted = True
            raise ValueError(f"Session {session_id} not found")

        async def count_file_references(self, file_id):
            calls.append(("count", file_id))
            return 0

        async def find_by_id(self, session_id):
            calls.append(("find-session", session_id))
            return None

    class Storage:
        deleted = []

        async def upload_file(self, stream, filename, user_id, metadata=None):
            return FileInfo(
                file_id="orphaned-upload",
                filename=filename,
                metadata=metadata,
            )

        async def delete_file(self, file_id, user_id):
            self.deleted.append((file_id, user_id))
            return True

    runner = _bare_runner("deleted-session")
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = Sandbox()
    runner._session_repository = Repository()
    runner._file_storage = Storage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    result = await runner._sync_file_to_storage(
        "/home/ubuntu/upload/report.md",
        generated=True,
    )

    assert result is None
    assert runner._file_storage.deleted == [("orphaned-upload", "user")]
    assert calls == [
        ("get-path", "session", "/home/ubuntu/upload/report.md"),
        ("upsert", "session", "orphaned-upload"),
        ("count", "orphaned-upload"),
        ("get-path", "session", "/home/ubuntu/upload/report.md"),
        ("find-session", "session"),
    ]


async def test_missing_session_identity_db_failure_retains_ambiguous_upload():
    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class Repository:
        deleted = False

        async def get_file_by_path(self, session_id, file_path):
            if self.deleted:
                raise ValueError(f"Session {session_id} not found")
            return None

        async def upsert_file_by_path(self, session_id, file_info):
            self.deleted = True
            raise ValueError(f"Session {session_id} not found")

        async def count_file_references(self, file_id):
            return 0

        async def find_by_id(self, session_id):
            raise ConnectionError("identity read unavailable")

    class Storage:
        deleted = []

        async def upload_file(self, stream, filename, user_id, metadata=None):
            return FileInfo(
                file_id="ambiguous-upload",
                filename=filename,
                metadata=metadata,
            )

        async def delete_file(self, file_id, user_id):
            self.deleted.append(file_id)
            return True

    runner = _bare_runner("ambiguous-session")
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = Sandbox()
    runner._session_repository = Repository()
    runner._file_storage = Storage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    result = await runner._sync_file_to_storage(
        "/home/ubuntu/upload/report.md",
        generated=True,
    )

    assert result is None
    assert runner._file_storage.deleted == []


async def test_runner_bundle_starts_only_one_stubborn_resource_close_at_a_time(
    monkeypatch,
):
    monkeypatch.setattr(
        runner_module,
        "_MAX_RUNNER_CLEANUP_BUNDLES",
        1,
    )
    browser_started = asyncio.Event()
    browser_release = asyncio.Event()
    mcp_started = asyncio.Event()
    mcp_release = asyncio.Event()

    class StubbornResource:
        def __init__(self, started, release):
            self.started = started
            self.release = release

        async def cleanup(self):
            self.started.set()
            await self.release.wait()

    runner = _bare_runner("agent")
    runner._close_lock = asyncio.Lock()
    runner._closed = False
    runner._close_task = None
    runner._cleanup_lease = None
    runner._ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS = 0.01
    runner._browser = StubbornResource(browser_started, browser_release)
    runner._mcp_tool = StubbornResource(mcp_started, mcp_release)
    runner._llm = None
    runner._sandbox = None

    await asyncio.wait_for(runner.aclose(), timeout=0.1)

    assert browser_started.is_set()
    assert not mcp_started.is_set()
    assert len(runner_module._runner_cleanup_leases_for_loop()) == 1
    assert len(runner_module._deferred_close_tasks_for_loop()) == 1

    browser_release.set()
    await asyncio.wait_for(mcp_started.wait(), timeout=0.2)
    assert not runner._close_task.done()

    mcp_release.set()
    await asyncio.wait_for(asyncio.shield(runner._close_task), timeout=0.2)

    await asyncio.sleep(0)
    assert runner_module._deferred_close_tasks_for_loop() == set()
    assert runner_module._runner_cleanup_leases_for_loop() == set()
    assert runner._closed is True
