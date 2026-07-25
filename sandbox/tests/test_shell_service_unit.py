import asyncio
import os
import shlex
import time
from types import SimpleNamespace

import pytest

from app.core.exceptions import AppException, BadRequestException
from app.models.shell import ConsoleRecord
from app.services import shell as shell_module
from app.services.shell import ShellService


@pytest.mark.parametrize(
    ("uvicorn_args", "web_concurrency"),
    [
        ("--workers 2", None),
        ("--workers=3", None),
        ("--reload", "4"),
    ],
)
def test_shell_isolation_rejects_multiple_app_workers(
    monkeypatch,
    uvicorn_args,
    web_concurrency,
):
    monkeypatch.setenv("UVI_ARGS", uvicorn_args)
    if web_concurrency is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", web_concurrency)

    with pytest.raises(RuntimeError, match="exactly one app worker"):
        ShellService._validate_single_worker_configuration()


@pytest.mark.asyncio
async def test_shell_isolation_startup_reaps_all_uids_once(monkeypatch):
    service = ShellService()
    reap_calls = []
    service.active_shells["stale"] = object()
    service.shell_tasks["stale"] = object()
    loop = asyncio.get_running_loop()
    shell_module._SHELL_UID_LEASES_BY_LOOP[loop] = {20_000: object()}

    # Existing long-lived server containers do not gain a new Dockerfile ENV
    # during an in-place hotpatch. Installed helpers must still activate the
    # startup sweep without depending on that image metadata.
    monkeypatch.delenv("MANUS_SHELL_ISOLATION_REQUIRED", raising=False)
    monkeypatch.setenv("UVI_ARGS", "--workers=1")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(
        ShellService,
        "_isolation_helpers_available",
        classmethod(lambda cls: True),
    )

    async def reap_all(cls):
        reap_calls.append("all")

    monkeypatch.setattr(
        ShellService,
        "_privileged_reap_all_shell_uids",
        classmethod(reap_all),
    )

    try:
        await service.initialize_process_isolation()
        assert reap_calls == ["all"]
        assert service.active_shells == {}
        assert service.shell_tasks == {}
        assert shell_module._SHELL_UID_LEASES_BY_LOOP == {}
    finally:
        shell_module._SHELL_UID_LEASES_BY_LOOP.clear()


@pytest.mark.asyncio
async def test_shell_isolation_required_fails_when_helpers_are_missing(
    monkeypatch,
):
    service = ShellService()
    monkeypatch.setenv("MANUS_SHELL_ISOLATION_REQUIRED", "1")
    monkeypatch.setattr(
        ShellService,
        "_isolation_helpers_available",
        classmethod(lambda cls: False),
    )

    with pytest.raises(RuntimeError, match="helper is unavailable"):
        await service.initialize_process_isolation()


def test_shell_output_keeps_only_bounded_recent_tail():
    service = ShellService()
    service._MAX_CURRENT_OUTPUT_CHARS = 8
    service._MAX_CONSOLE_OUTPUT_CHARS = 5
    shell = {
        "output": "",
        "console": [ConsoleRecord(ps1="$", command="cmd", output="")],
    }

    service._append_output(shell, "1234")
    service._append_output(shell, "567890")

    assert shell["output"] == "34567890"
    assert shell["console"][-1].output == "67890"


def test_shell_console_history_is_a_bounded_ring():
    service = ShellService()
    service._MAX_CONSOLE_RECORDS = 2
    shell = {"output": "", "console": []}

    for index in range(4):
        service._append_console_record(
            shell,
            ConsoleRecord(ps1="$", command=f"cmd-{index}", output=""),
        )

    assert [record.command for record in shell["console"]] == [
        "cmd-2",
        "cmd-3",
    ]


@pytest.mark.asyncio
async def test_shell_output_decodes_utf8_split_across_read_chunks():
    service = ShellService()
    session_id = "split-utf8"
    encoded = "你好🙂".encode("utf-8")
    chunks = [encoded[:1], encoded[1:4], encoded[4:7], encoded[7:], b""]

    class ChunkedStdout:
        async def read(self, size):
            return chunks.pop(0)

    service.active_shells[session_id] = {
        "output": "",
        "console": [ConsoleRecord(ps1="$", command="cmd", output="")],
    }
    try:
        await service._start_output_reader(
            session_id,
            SimpleNamespace(stdout=ChunkedStdout()),
        )

        assert service.active_shells[session_id]["output"] == "你好🙂"
        assert service.active_shells[session_id]["console"][-1].output == "你好🙂"
        assert chunks == []
    finally:
        service.active_shells.pop(session_id, None)


@pytest.mark.asyncio
async def test_stop_process_kills_background_process_group(tmp_path):
    service = ShellService()
    process = await service._create_process(
        "trap '' TERM; sleep 30 & wait",
        str(tmp_path),
    )
    await asyncio.sleep(0.05)

    await service._stop_process(process)

    assert process.returncode is not None
    for _ in range(100):
        if not service._process_group_has_members(process.pid):
            break
        await asyncio.sleep(0.01)
    assert service._process_group_has_members(process.pid) is False


@pytest.mark.asyncio
async def test_stuck_process_stop_is_bounded_and_does_not_hold_session_lock(
    monkeypatch,
):
    service = ShellService()
    monkeypatch.setattr(ShellService, "_PROCESS_STOP_WAIT_SECONDS", 0.02)

    class StuckProcess:
        pid = 12345
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            await asyncio.Event().wait()

    process = StuckProcess()
    service.active_shells["stuck"] = {
        "process": process,
        "exec_dir": os.getcwd(),
        "output": "",
        "console": [],
        "last_access": time.monotonic(),
    }
    monkeypatch.setattr(
        ShellService,
        "_process_groups_for_session",
        classmethod(lambda cls, session_id, expected_start_time=None: set()),
    )

    with pytest.raises(Exception, match="did not report exit"):
        await asyncio.wait_for(
            service.exec_command("stuck", os.getcwd(), "echo next"),
            timeout=0.2,
        )

    assert process.killed is True
    # A failed cleanup remains owned so a later request can retry it instead
    # of losing the only handle to a potentially expensive descendant.
    assert service.active_shells["stuck"]["process"] is process
    async def acquire_lock_once():
        async with service._session_lock:
            pass
    await asyncio.wait_for(acquire_lock_once(), timeout=0.05)


@pytest.mark.asyncio
async def test_stop_process_uses_privileged_kill_for_root_group(monkeypatch):
    service = ShellService()
    groups_by_scan = [{321}, {321}, {321}, {321}, set()]
    privileged_calls = []

    class Process:
        pid = 123
        returncode = None
        _manus_session_start_time = 456

    def scan(cls, session_id, expected_start_time=None):
        assert session_id == 123
        assert expected_start_time == 456
        if groups_by_scan:
            return groups_by_scan.pop(0)
        Process.returncode = -9
        return set()

    async def privileged(cls, session_id, expected_start_time, groups):
        privileged_calls.append(
            (session_id, expected_start_time, groups.copy())
        )
        Process.returncode = -9

    monkeypatch.setattr(
        ShellService,
        "_process_groups_for_session",
        classmethod(scan),
    )
    monkeypatch.setattr(
        ShellService,
        "_privileged_kill_process_groups",
        classmethod(privileged),
    )
    monkeypatch.setattr(os, "killpg", lambda *_: None)
    monkeypatch.setattr(os.path, "isdir", lambda path: path == "/proc")

    await service._stop_process(Process())

    assert privileged_calls
    assert all(call == (123, 456, {321}) for call in privileged_calls)


def test_session_fingerprint_rejects_reused_process_id(monkeypatch):
    monkeypatch.setattr(
        ShellService,
        "_read_process_identity",
        staticmethod(lambda _pid: ("S", 1, 777, 777, 999)),
    )

    assert ShellService._session_fingerprint_matches(777, 123) is False


@pytest.mark.asyncio
async def test_stop_process_cleans_group_after_wrapper_exits_early(tmp_path):
    service = ShellService()
    process = await service._create_process(
        "set -e; sleep 30 & false",
        str(tmp_path),
    )
    await asyncio.wait_for(process.wait(), timeout=1)

    assert process.returncode != 0
    assert service._process_group_has_members(process.pid) is True

    await service._stop_process(process)

    for _ in range(100):
        if not service._process_group_has_members(process.pid):
            break
        await asyncio.sleep(0.01)
    assert service._process_group_has_members(process.pid) is False


@pytest.mark.asyncio
async def test_kill_reaps_completed_uid_once_before_it_can_be_reused(
    monkeypatch,
):
    service = ShellService()
    loop = asyncio.get_running_loop()
    reaped = []

    class Process:
        pid = 123
        returncode = 0
        _manus_shell_uid = 20_000
        _manus_shell_loop = loop

    process = Process()
    service.active_shells["completed"] = {
        "process": process,
        "exec_dir": os.getcwd(),
        "output": "",
        "console": [],
        "last_access": time.monotonic(),
    }
    shell_module._SHELL_UID_LEASES_BY_LOOP[loop] = {20_000: process}

    async def reap(cls, user_id):
        reaped.append(user_id)

    monkeypatch.setattr(
        ShellService,
        "_privileged_reap_shell_uid",
        classmethod(reap),
    )
    monkeypatch.setattr(
        ShellService,
        "_process_groups_for_session",
        classmethod(lambda cls, session_id, expected_start_time=None: set()),
    )

    try:
        first = await service.kill_process("completed")
        assert first.status == "terminated"
        assert reaped == [20_000]
        assert process._manus_shell_uid_cleaned is True

        replacement_owner = object()
        shell_module._SHELL_UID_LEASES_BY_LOOP[loop] = {
            20_000: replacement_owner
        }
        second = await service.kill_process("completed")
        assert second.status == "already_terminated"
        assert reaped == [20_000]
        assert shell_module._SHELL_UID_LEASES_BY_LOOP[loop][20_000] is replacement_owner
    finally:
        shell_module._SHELL_UID_LEASES_BY_LOOP.pop(loop, None)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc semantics")
async def test_stop_process_kills_job_control_subgroups(tmp_path):
    service = ShellService()
    process = await service._create_process(
        "set -m; set -e; sleep 30 & false",
        str(tmp_path),
    )
    await asyncio.wait_for(process.wait(), timeout=1)

    assert service._process_groups_for_session(process.pid)

    await service._stop_process(process)

    assert service._process_groups_for_session(process.pid) == set()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("MANUS_RUN_ROOT_SHELL_TEST") != "1",
    reason="requires the sandbox image's passwordless root helper",
)
async def test_stop_process_reaps_double_forked_daemon_after_wrapper_exit(
    tmp_path,
):
    service = ShellService()
    daemon_pid_file = os.path.join(
        "/home/ubuntu",
        f"daemon-{service.create_session_id()}.pid",
    )
    process = await service._create_process(
        "setsid /bin/sh -c 'sleep 30 & echo $! > "
        f'"{daemon_pid_file}"' + "'",
        "/home/ubuntu",
    )
    await asyncio.wait_for(process.wait(), timeout=2)
    for _ in range(100):
        if os.path.exists(daemon_pid_file):
            break
        await asyncio.sleep(0.01)
    with open(daemon_pid_file, "r", encoding="utf-8") as stream:
        daemon_pid = int(stream.read().strip())

    assert process.returncode == 0
    assert os.path.exists(f"/proc/{daemon_pid}")

    await service._stop_process(process)

    identity = service._read_process_identity(daemon_pid)
    assert identity is None or identity[0] == "Z"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("MANUS_RUN_ROOT_SHELL_TEST") != "1",
    reason="requires the sandbox image's isolated shell account",
)
async def test_model_shell_user_has_no_sudo_rule(tmp_path):
    service = ShellService()
    process = await service._create_process("sudo -n true", "/home/ubuntu")

    await asyncio.wait_for(process.wait(), timeout=2)

    assert process.returncode != 0


@pytest.mark.asyncio
async def test_session_cap_prefers_oldest_completed_session():
    service = ShellService()
    service._MAX_SHELL_SESSIONS = 2

    class CompletedProcess:
        returncode = 0

        async def wait(self):
            return 0

    now = time.monotonic()
    service.active_shells = {
        "old": {
            "process": CompletedProcess(),
            "last_access": now - 2,
        },
        "new": {
            "process": CompletedProcess(),
            "last_access": now - 1,
        },
    }

    await service._prune_sessions_for_creation()

    assert set(service.active_shells) == {"new"}


@pytest.mark.asyncio
async def test_shell_input_has_service_level_size_limit():
    service = ShellService()
    service.active_shells["input"] = {
        "process": SimpleNamespace(returncode=None),
        "last_access": time.monotonic(),
    }

    with pytest.raises(BadRequestException, match="size limit"):
        await service.write_to_process(
            "input",
            "x" * (service._MAX_STDIN_BYTES + 1),
            False,
        )


@pytest.mark.asyncio
async def test_kill_all_prevents_late_side_effect_and_shell_can_be_reused(
    tmp_path,
):
    service = ShellService()
    marker = tmp_path / "late-marker"
    command = (
        "sleep 0.4; printf late > "
        f"{shlex.quote(str(marker))}"
    )
    execution = asyncio.create_task(
        service.exec_command("reusable", str(tmp_path), command)
    )
    for _ in range(100):
        if "reusable" in service.active_shells:
            break
        await asyncio.sleep(0.01)
    assert "reusable" in service.active_shells

    first = await service.kill_all_processes()
    await asyncio.wait_for(execution, timeout=1)
    await asyncio.sleep(0.5)

    assert first.sessions_seen == 1
    assert first.sessions_terminated == 1
    assert marker.exists() is False

    second = await service.kill_all_processes()
    assert second.sessions_seen == 1
    assert second.sessions_terminated == 0

    resumed = await service.exec_command(
        "reusable",
        str(tmp_path),
        "printf resumed",
    )
    assert resumed.status == "completed"
    assert resumed.output == "resumed"


@pytest.mark.asyncio
async def test_kill_all_propagates_partial_cleanup_failure(monkeypatch):
    service = ShellService()
    process = SimpleNamespace(returncode=None, pid=123)
    service.active_shells["stuck"] = {
        "process": process,
        "reader_task": None,
    }

    async def fail_stop(_process):
        raise TimeoutError("still alive")

    monkeypatch.setattr(service, "_stop_process", fail_stop)
    monkeypatch.setattr(
        service,
        "_process_group_has_members",
        lambda _pid: False,
    )

    with pytest.raises(AppException, match="Failed to terminate all"):
        await service.kill_all_processes()

    assert service.active_shells["stuck"]["process"] is process
