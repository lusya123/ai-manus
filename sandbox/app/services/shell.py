"""
Shell Service Implementation - Async Version
"""
import os
import codecs
import uuid
import getpass
import socket
import logging
import asyncio
import re
import signal
import shlex
import time
import shutil
from typing import Dict, Any, Optional, List
from app.models.shell import (
    ShellExecResult, ShellViewResult, ShellWaitResult,
    ShellWriteResult, ShellKillResult, ShellKillAllResult, ShellTask,
    ConsoleRecord
)
from app.core.exceptions import AppException, ResourceNotFoundException, BadRequestException

# Set up logger
logger = logging.getLogger(__name__)

_SHELL_UID_LEASES_BY_LOOP: Dict[
    asyncio.AbstractEventLoop,
    Dict[int, object],
] = {}

class ShellService:
    _MAX_CURRENT_OUTPUT_CHARS = 256_000
    _MAX_CONSOLE_OUTPUT_CHARS = 128_000
    _MAX_CONSOLE_RECORDS = 20
    _MAX_SHELL_SESSIONS = 64
    _COMPLETED_SHELL_TTL_SECONDS = 300.0
    _MAX_COMMAND_CHARS = 65_536
    _MAX_STDIN_BYTES = 65_536
    _MAX_SHELL_CLEANUP_CONCURRENCY = 8
    _STDIN_DRAIN_TIMEOUT_SECONDS = 5.0
    _PROCESS_STOP_WAIT_SECONDS = 2.0
    _READER_STOP_WAIT_SECONDS = 0.5
    _PRIVILEGED_KILL_TIMEOUT_SECONDS = 1.0
    _PRIVILEGED_REAP_ALL_TIMEOUT_SECONDS = 2.0
    _SHELL_REAPER_PATH = "/usr/local/sbin/manus-shell-reaper"
    _SHELL_LAUNCHER_PATH = "/usr/local/sbin/manus-shell-launcher"
    _FIRST_SHELL_UID = 20_000
    _SHELL_UID_COUNT = 64

    # Store active shell sessions
    active_shells: Dict[str, Dict[str, Any]] = {}
    
    # Store shell tasks
    shell_tasks: Dict[str, ShellTask] = {}

    def __init__(self) -> None:
        # Keep the registry scoped to one sandbox service instance.  The
        # module-level singleton still serves all API requests, while tests
        # and future app instances cannot leak sessions into one another.
        self.active_shells = {}
        self.shell_tasks = {}
        self._session_lock = asyncio.Lock()

    @staticmethod
    def _read_process_identity(
        process_id: int,
    ) -> Optional[tuple[str, int, int, int, int]]:
        """Return state, parent, process group, session and start ticks."""
        if process_id <= 0 or not os.path.isdir("/proc"):
            return None
        try:
            with open(
                f"/proc/{process_id}/stat",
                "r",
                encoding="utf-8",
            ) as stat_file:
                raw_stat = stat_file.read()
            fields = raw_stat[raw_stat.rfind(")") + 2:].split()
            return (
                fields[0],
                int(fields[1]),
                int(fields[2]),
                int(fields[3]),
                int(fields[19]),
            )
        except (OSError, ValueError, IndexError):
            return None

    @classmethod
    def _session_fingerprint_matches(
        cls,
        session_id: int,
        expected_start_time: Optional[int],
    ) -> bool:
        if expected_start_time is None:
            return True
        identity = cls._read_process_identity(session_id)
        if identity is None:
            # A session leader may exit before its descendants. Linux keeps
            # that numeric SID/PGID reserved while members still exist, so it
            # cannot be recycled as an unrelated PID during this cleanup.
            return True
        _, _, _, member_session_id, start_time = identity
        return (
            member_session_id == session_id
            and start_time == expected_start_time
        )

    @classmethod
    def _process_groups_for_session(
        cls,
        session_id: int,
        expected_start_time: Optional[int] = None,
    ) -> set[int]:
        process_groups: set[int] = set()
        if session_id <= 0:
            return process_groups
        if not cls._session_fingerprint_matches(
            session_id,
            expected_start_time,
        ):
            logger.error(
                "Refusing to inspect a shell session after PID reuse: %s",
                session_id,
            )
            return process_groups
        proc_root = "/proc"
        if os.path.isdir(proc_root):
            processes: dict[int, tuple[str, int, int, int]] = {}
            try:
                process_entries = os.scandir(proc_root)
            except OSError:
                return process_groups
            with process_entries:
                for entry in process_entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        with open(
                            os.path.join(entry.path, "stat"),
                            "r",
                            encoding="utf-8",
                        ) as stat_file:
                            raw_stat = stat_file.read()
                        fields = raw_stat[raw_stat.rfind(")") + 2:].split()
                        process_state = fields[0]
                        parent_process = int(fields[1])
                        process_group = int(fields[2])
                        member_session_id = int(fields[3])
                    except (OSError, ValueError, IndexError):
                        continue
                    processes[int(entry.name)] = (
                        process_state,
                        parent_process,
                        process_group,
                        member_session_id,
                    )

            # Include both the original POSIX session and direct/indirect
            # descendants. The ancestry branch catches commands such as
            # ``sudo setsid ... &`` which deliberately create a new session
            # but are still children of the recorded shell wrapper.
            descendants = {session_id}
            changed = True
            while changed:
                changed = False
                for process_id, (_, parent_id, _, _) in processes.items():
                    if process_id not in descendants and parent_id in descendants:
                        descendants.add(process_id)
                        changed = True
            for process_id, (
                process_state,
                _,
                process_group,
                member_session_id,
            ) in processes.items():
                if process_state != "Z" and (
                    member_session_id == session_id
                    or process_id in descendants
                ):
                    process_groups.add(process_group)
            return process_groups
        try:
            os.killpg(session_id, 0)
            process_groups.add(session_id)
        except (ProcessLookupError, PermissionError):
            pass
        return process_groups

    @staticmethod
    def _process_group_has_members(process_group_id: int) -> bool:
        return bool(
            ShellService._process_groups_for_session(process_group_id)
        )

    @classmethod
    async def _privileged_kill_process_groups(
        cls,
        session_id: int,
        expected_start_time: Optional[int],
        requested_groups: set[int],
    ) -> None:
        """Kill only revalidated groups belonging to the recorded session."""
        current_groups = cls._process_groups_for_session(
            session_id,
            expected_start_time,
        )
        validated_groups = sorted(current_groups.intersection(requested_groups))
        if not validated_groups:
            return

        helper = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "/bin/kill",
            "-KILL",
            "--",
            *(f"-{process_group}" for process_group in validated_groups),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                helper.communicate(),
                timeout=cls._PRIVILEGED_KILL_TIMEOUT_SECONDS,
            )
        except BaseException:
            if helper.returncode is None:
                helper.kill()
                try:
                    await asyncio.shield(helper.wait())
                except asyncio.CancelledError:
                    await helper.wait()
            raise

        if helper.returncode == 0:
            return
        remaining_groups = cls._process_groups_for_session(
            session_id,
            expected_start_time,
        ).intersection(validated_groups)
        if remaining_groups:
            detail = stderr.decode("utf-8", errors="replace")[-512:]
            raise PermissionError(
                "Privileged shell process-group cleanup failed: " + detail
            )

    @classmethod
    async def _privileged_reap_shell_uid(cls, user_id: int) -> None:
        helper = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            cls._SHELL_REAPER_PATH,
            str(user_id),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                helper.communicate(),
                timeout=cls._PRIVILEGED_KILL_TIMEOUT_SECONDS,
            )
        except BaseException:
            if helper.returncode is None:
                helper.kill()
                try:
                    await asyncio.shield(helper.wait())
                except asyncio.CancelledError:
                    await helper.wait()
            raise
        if helper.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-512:]
            raise PermissionError(
                "UID-scoped shell cleanup failed: " + detail
            )

    @classmethod
    async def _privileged_reap_all_shell_uids(cls) -> None:
        """Remove shell processes whose in-memory leases died with the app."""
        helper = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            cls._SHELL_REAPER_PATH,
            "--all",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                helper.communicate(),
                timeout=cls._PRIVILEGED_REAP_ALL_TIMEOUT_SECONDS,
            )
        except BaseException:
            if helper.returncode is None:
                helper.kill()
                try:
                    await asyncio.shield(helper.wait())
                except asyncio.CancelledError:
                    await helper.wait()
            raise
        if helper.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-512:]
            raise PermissionError(
                "Sandbox startup shell cleanup failed: " + detail
            )

    @classmethod
    def _isolation_helpers_available(cls) -> bool:
        return (
            os.path.isfile(cls._SHELL_REAPER_PATH)
            and os.path.isfile(cls._SHELL_LAUNCHER_PATH)
            and shutil.which("sudo") is not None
        )

    @staticmethod
    def _parse_worker_count(raw_value: str, source: str) -> int:
        try:
            worker_count = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid sandbox worker count in {source}"
            ) from exc
        if worker_count < 1:
            raise RuntimeError(
                f"Invalid sandbox worker count in {source}"
            )
        return worker_count

    @classmethod
    def _validate_single_worker_configuration(cls) -> None:
        """UID leases are process-local, so more than one worker is unsafe."""
        configured_counts: list[tuple[str, int]] = []
        web_concurrency = os.getenv("WEB_CONCURRENCY")
        if web_concurrency:
            configured_counts.append(
                (
                    "WEB_CONCURRENCY",
                    cls._parse_worker_count(
                        web_concurrency,
                        "WEB_CONCURRENCY",
                    ),
                )
            )

        try:
            uvicorn_args = shlex.split(os.getenv("UVI_ARGS", ""))
        except ValueError as exc:
            raise RuntimeError("Invalid quoting in sandbox UVI_ARGS") from exc
        index = 0
        while index < len(uvicorn_args):
            argument = uvicorn_args[index]
            if argument == "--workers":
                index += 1
                if index >= len(uvicorn_args):
                    raise RuntimeError(
                        "Missing sandbox worker count after --workers"
                    )
                configured_counts.append(
                    (
                        "UVI_ARGS",
                        cls._parse_worker_count(
                            uvicorn_args[index],
                            "UVI_ARGS",
                        ),
                    )
                )
            elif argument.startswith("--workers="):
                configured_counts.append(
                    (
                        "UVI_ARGS",
                        cls._parse_worker_count(
                            argument.partition("=")[2],
                            "UVI_ARGS",
                        ),
                    )
                )
            index += 1

        unsafe_sources = [
            source
            for source, worker_count in configured_counts
            if worker_count != 1
        ]
        if unsafe_sources:
            raise RuntimeError(
                "Sandbox shell isolation requires exactly one app worker; "
                + ", ".join(sorted(set(unsafe_sources)))
                + " configures multiple workers"
            )

    async def initialize_process_isolation(self) -> None:
        """Fail closed and reclaim fixed UIDs before serving any request."""
        isolation_required = (
            os.getenv("MANUS_SHELL_ISOLATION_REQUIRED") == "1"
        )
        isolation_available = self._isolation_helpers_available()
        if not isolation_available and not isolation_required:
            return
        if not isolation_available:
            raise RuntimeError("Sandbox shell isolation helper is unavailable")
        self._validate_single_worker_configuration()
        await self._privileged_reap_all_shell_uids()

        # A lifespan can be restarted in-process by test harnesses. Once the
        # privileged sweep succeeds, every previous handle and lease is stale.
        self.active_shells.clear()
        self.shell_tasks.clear()
        _SHELL_UID_LEASES_BY_LOOP.clear()
        logger.info("Sandbox shell isolation initialized with a clean UID pool")

    @classmethod
    def _release_shell_uid(
        cls,
        loop: asyncio.AbstractEventLoop,
        user_id: int,
        owner: object,
    ) -> None:
        leases = _SHELL_UID_LEASES_BY_LOOP.get(loop)
        if leases is None or leases.get(user_id) is not owner:
            return
        leases.pop(user_id, None)
        if not leases:
            _SHELL_UID_LEASES_BY_LOOP.pop(loop, None)

    @classmethod
    def _finish_process_shell_uid_cleanup(
        cls,
        process: asyncio.subprocess.Process,
        loop: Optional[asyncio.AbstractEventLoop],
        user_id: Optional[int],
    ) -> None:
        """Release one UID exactly once and make stale handles harmless.

        Completed shell sessions remain queryable for a short TTL.  Once their
        UID is returned to the pool, a later ``kill`` on that old session must
        never reap a different command that has since leased the same UID.
        """
        if user_id is None or loop is None:
            return
        cls._release_shell_uid(loop, user_id, process)
        setattr(process, "_manus_shell_uid_cleaned", True)

    @classmethod
    async def _signal_process_tree(
        cls,
        process: asyncio.subprocess.Process,
        session_id: int,
        expected_start_time: Optional[int],
    ) -> None:
        process_groups = cls._process_groups_for_session(
            session_id,
            expected_start_time,
        )
        if not process_groups and process.returncode is None:
            # The asyncio Process object still owns the exact live child even
            # when /proc inspection is unavailable (or a test watcher is
            # synthetic). Do not invent an unvalidated process-group ID.
            process.kill()
            return

        # Kill detached/job-control groups before the wrapper's own group.
        # Once the wrapper exits, a detached child can be reparented and its
        # ancestry is no longer available for exact validation.
        ordered_groups = sorted(
            process_groups,
            key=lambda process_group: process_group == session_id,
        )
        for process_group in ordered_groups:
            # Revalidate immediately before every signal. This prevents an old
            # async handle from targeting an unrelated, recycled process ID.
            if process_group not in cls._process_groups_for_session(
                session_id,
                expected_start_time,
            ):
                continue
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError:
                await cls._privileged_kill_process_groups(
                    session_id,
                    expected_start_time,
                    {process_group},
                )

            # A mixed-UID group can report success while only killing the
            # unprivileged members. Reap a surviving exact group before its
            # parent dies and ancestry information is lost.
            await asyncio.sleep(0)
            if (
                os.path.isdir("/proc")
                and process_group
                in cls._process_groups_for_session(
                    session_id,
                    expected_start_time,
                )
            ):
                await cls._privileged_kill_process_groups(
                    session_id,
                    expected_start_time,
                    {process_group},
                )

        # Catch a subgroup created during the first scan without ever passing
        # an unvalidated numeric ID to the privileged helper.
        await asyncio.sleep(0)
        remaining_groups = cls._process_groups_for_session(
            session_id,
            expected_start_time,
        ).intersection(process_groups)
        if remaining_groups and os.path.isdir("/proc"):
            await cls._privileged_kill_process_groups(
                session_id,
                expected_start_time,
                remaining_groups,
            )

        if not process_groups and process.returncode is None:
            process.kill()

    @classmethod
    async def _stop_process(cls, process: asyncio.subprocess.Process) -> None:
        pid = getattr(process, "pid", None)
        expected_start_time = getattr(
            process,
            "_manus_session_start_time",
            None,
        )
        shell_user_id = (
            None
            if getattr(process, "_manus_shell_uid_cleaned", False)
            else getattr(process, "_manus_shell_uid", None)
        )
        shell_loop = getattr(process, "_manus_shell_loop", None)
        if shell_user_id is not None:
            # A unique UID survives setsid, double-fork, environment clearing
            # and PID-1 reparenting. Run this even after the wrapper completed.
            await cls._privileged_reap_shell_uid(shell_user_id)
        if process.returncode is not None and (
            pid is None
            or not cls._process_groups_for_session(
                pid,
                expected_start_time,
            )
        ):
            cls._finish_process_shell_uid_cleanup(
                process,
                shell_loop,
                shell_user_id,
            )
            return

        # Every command starts a new process group.  Kill it as one unit while
        # its leader is still alive; waiting for a graceful wrapper exit first
        # can leave SIGTERM-ignoring descendants behind or make a later PGID
        # lookup race with PID reuse.
        if pid is not None:
            await cls._signal_process_tree(
                process,
                pid,
                expected_start_time,
            )
        elif process.returncode is None:
            process.kill()

        # SIGKILL has already been delivered.  Poll both the child-watcher
        # state and the complete process-session tree: the shell wrapper can
        # report its own exit before job-control subgroups have disappeared.
        # Never await ``process.wait()`` without a bound; an abnormal watcher
        # or uninterruptible process must not hold the sandbox-wide session
        # lock forever.
        deadline = (
            asyncio.get_running_loop().time()
            + ShellService._PROCESS_STOP_WAIT_SECONDS
        )
        while True:
            groups_remaining = (
                pid is not None
                and bool(
                    cls._process_groups_for_session(
                        pid,
                        expected_start_time,
                    )
                )
            )
            if process.returncode is not None and not groups_remaining:
                cls._finish_process_shell_uid_cleanup(
                    process,
                    shell_loop,
                    shell_user_id,
                )
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "Shell process tree did not report exit after SIGKILL"
                )
            await asyncio.sleep(0.01)

    async def _stop_reader_task(
        self,
        reader_task: Optional[asyncio.Task],
    ) -> None:
        if (
            reader_task is None
            or reader_task is asyncio.current_task()
            or reader_task.done()
        ):
            return
        done, _ = await asyncio.wait(
            (reader_task,),
            timeout=self._READER_STOP_WAIT_SECONDS,
        )
        if done:
            return
        reader_task.cancel()
        done, _ = await asyncio.wait(
            (reader_task,),
            timeout=self._READER_STOP_WAIT_SECONDS,
        )
        if not done:
            logger.error("Shell output reader did not stop after cancellation")

    async def _discard_session(self, session_id: str) -> None:
        shell = self.active_shells.get(session_id)
        if not shell:
            return
        await self._stop_process(shell["process"])
        await self._stop_reader_task(shell.get("reader_task"))
        if self.active_shells.get(session_id) is shell:
            self.active_shells.pop(session_id, None)

    async def _prune_sessions_for_creation(self) -> None:
        """Expire completed sessions and enforce a sandbox-wide hard cap."""
        now = time.monotonic()
        expired = [
            session_id
            for session_id, shell in self.active_shells.items()
            if shell["process"].returncode is not None
            and now - shell.get("last_access", now)
            >= self._COMPLETED_SHELL_TTL_SECONDS
        ]
        for session_id in expired:
            await self._discard_session(session_id)

        while len(self.active_shells) >= self._MAX_SHELL_SESSIONS:
            # Prefer the oldest completed process.  If every slot is still
            # running, terminate the least recently used one before admitting
            # another ID so model-generated IDs cannot grow memory forever.
            session_id, _ = min(
                self.active_shells.items(),
                key=lambda item: (
                    item[1]["process"].returncode is None,
                    item[1].get("last_access", 0.0),
                ),
            )
            logger.warning("Evicting shell session at sandbox session limit")
            await self._discard_session(session_id)

    def _touch_session(self, session_id: str) -> None:
        shell = self.active_shells.get(session_id)
        if shell is not None:
            shell["last_access"] = time.monotonic()

    def _remove_ansi_escape_codes(self, text: str) -> str:
        """Remove ANSI escape codes from text"""
        # Pattern to match ANSI escape sequences
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    @staticmethod
    def _bounded_tail(current: str, addition: str, limit: int) -> str:
        if len(addition) >= limit:
            return addition[-limit:]
        overflow = len(current) + len(addition) - limit
        if overflow > 0:
            current = current[overflow:]
        return current + addition

    def _append_output(self, shell: Dict[str, Any], output: str) -> None:
        shell["output"] = self._bounded_tail(
            shell["output"],
            output,
            self._MAX_CURRENT_OUTPUT_CHARS,
        )
        if shell["console"]:
            record = shell["console"][-1]
            record.output = self._bounded_tail(
                record.output,
                output,
                self._MAX_CONSOLE_OUTPUT_CHARS,
            )

    def _append_console_record(
        self,
        shell: Dict[str, Any],
        record: ConsoleRecord,
    ) -> None:
        shell["console"].append(record)
        overflow = len(shell["console"]) - self._MAX_CONSOLE_RECORDS
        if overflow > 0:
            del shell["console"][:overflow]

    def _get_display_path(self, path: str) -> str:
        """Get the path for display, replacing user home directory with ~"""
        home_dir = os.path.expanduser("~")
        logger.debug(f"Home directory: {home_dir} , path: {path}")
        if path.startswith(home_dir):
            return path.replace(home_dir, "~", 1)
        return path

    def _format_ps1(self, exec_dir: str) -> str:
        """Format the command prompt"""
        username = getpass.getuser()
        hostname = socket.gethostname()
        display_dir = self._get_display_path(exec_dir)
        return f"{username}@{hostname}:{display_dir} $"

    async def _create_process(self, command: str, exec_dir: str) -> asyncio.subprocess.Process:
        """Create a new async subprocess"""
        logger.debug(f"Creating process for command: {command} in directory: {exec_dir}")
        # Keep the wrapper alive for ordinary background jobs.  That makes its
        # process-group ID safe to signal until every child finishes and stops
        # ``cmd &`` from escaping the shell-session lifecycle by accident.
        wrapped_command = (
            "umask 0002\n"
            f"{command}\n"
            "_manus_command_status=$?\n"
            "wait\n"
            "exit $_manus_command_status"
        )
        isolation_available = self._isolation_helpers_available()
        if (
            os.getenv("MANUS_SHELL_ISOLATION_REQUIRED") == "1"
            and not isolation_available
        ):
            raise RuntimeError("Sandbox shell isolation helper is unavailable")
        if isolation_available:
            loop = asyncio.get_running_loop()
            leases = _SHELL_UID_LEASES_BY_LOOP.setdefault(loop, {})
            shell_user_id = next(
                (
                    candidate
                    for candidate in range(
                        self._FIRST_SHELL_UID,
                        self._FIRST_SHELL_UID + self._SHELL_UID_COUNT,
                    )
                    if candidate not in leases
                ),
                None,
            )
            if shell_user_id is None:
                raise RuntimeError("Sandbox shell isolation capacity reached")
            reservation = object()
            leases[shell_user_id] = reservation
            try:
                process = await asyncio.create_subprocess_exec(
                    "sudo",
                    "-n",
                    self._SHELL_LAUNCHER_PATH,
                    str(shell_user_id),
                    "/bin/bash",
                    "-c",
                    wrapped_command,
                    cwd=exec_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                    start_new_session=True,
                )
            except BaseException:
                self._release_shell_uid(
                    loop,
                    shell_user_id,
                    reservation,
                )
                raise
            leases[shell_user_id] = process
            setattr(process, "_manus_shell_uid", shell_user_id)
            setattr(process, "_manus_shell_loop", loop)
        else:
            # Local unit tests outside the sandbox image do not have the
            # dedicated account/helper. Production images fail closed above.
            process = await asyncio.create_subprocess_shell(
                wrapped_command,
                executable="/bin/bash",
                cwd=exec_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                start_new_session=True,
            )
        identity = self._read_process_identity(process.pid)
        if identity is not None:
            setattr(process, "_manus_session_start_time", identity[4])
        return process

    async def _start_output_reader(self, session_id: str, process: asyncio.subprocess.Process):
        """Start a coroutine to continuously read process output and store it"""
        logger.debug(f"Starting output reader for session: {session_id}")
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            if process.stdout:
                try:
                    buffer = await process.stdout.read(4096)
                    if not buffer:
                        final_output = decoder.decode(b"", final=True)
                        shell = self.active_shells.get(session_id)
                        if shell and final_output:
                            self._append_output(shell, final_output)
                        # Process output ended
                        break
                    
                    output = decoder.decode(buffer)
                    # Add output to shell session
                    shell = self.active_shells.get(session_id)
                    if shell and output:
                        self._append_output(shell, output)
                except Exception as e:
                    logger.error(f"Error reading process output: {str(e)}", exc_info=True)
                    break
            else:
                break
        
        logger.debug(f"Output reader for session {session_id} has finished")

    async def exec_command(self, session_id: str, exec_dir: Optional[str], command: str) -> ShellExecResult:
        """
        Asynchronously execute a command in the specified shell session
        """
        logger.info(f"Executing command in session {session_id}: {command}")
        if len(command) > self._MAX_COMMAND_CHARS:
            raise BadRequestException("Shell command exceeds the size limit")
        if not exec_dir:
            exec_dir = os.path.expanduser("~")
        # Ensure directory exists
        if not os.path.exists(exec_dir):
            logger.error(f"Directory does not exist: {exec_dir}")
            raise BadRequestException(f"Directory does not exist: {exec_dir}")
        
        try:
            # Create PS1 format
            ps1 = self._format_ps1(exec_dir)
            
            async with self._session_lock:
                # If it's a new session, create a new process.
                if session_id not in self.active_shells:
                    await self._prune_sessions_for_creation()
                    logger.debug(f"Creating new shell session: {session_id}")
                    process = await self._create_process(command, exec_dir)
                    shell = {
                        "process": process,
                        "exec_dir": exec_dir,
                        "output": "",
                        "console": [
                            ConsoleRecord(ps1=ps1, command=command, output="")
                        ],
                        "last_access": time.monotonic(),
                    }
                    self.active_shells[session_id] = shell
                else:
                    # Execute command in an existing session.
                    logger.debug(f"Using existing shell session: {session_id}")
                    # Keep ownership until the complete process tree is known
                    # stopped. If privileged cleanup fails, a later request
                    # can retry instead of silently leaking a root descendant.
                    shell = self.active_shells[session_id]
                    old_process = shell["process"]
                    logger.debug(f"Terminating previous process in session: {session_id}")
                    await self._stop_process(old_process)
                    await self._stop_reader_task(shell.get("reader_task"))

                    process = await self._create_process(command, exec_dir)
                    shell["process"] = process
                    shell["exec_dir"] = exec_dir
                    shell["output"] = ""  # Clear previous output
                    shell["last_access"] = time.monotonic()
                    self._append_console_record(
                        shell,
                        ConsoleRecord(ps1=ps1, command=command, output=""),
                    )
                    self.active_shells[session_id] = shell

                shell["reader_task"] = asyncio.create_task(
                    self._start_output_reader(session_id, process)
                )
            
            # Try to wait for the process to complete (max 5 seconds)
            try:
                logger.debug(f"Waiting for process completion in session: {session_id}")
                wait_result = await self.wait_for_process(session_id, seconds=5)
                if wait_result.returncode is not None:
                    # Process has completed, get the output
                    logger.debug(f"Process completed with code: {wait_result.returncode}")
                    view_result = await self.view_shell(session_id)
                    
                    return ShellExecResult(
                        session_id=session_id,
                        command=command,
                        status="completed",
                        returncode=wait_result.returncode,
                        output=view_result.output,
                    )
            except BadRequestException:
                # Wait timeout, process still running
                logger.debug(f"Process still running after timeout in session: {session_id}")
                pass
            except Exception as e:
                # Other exceptions, ignore and continue
                logger.warning(f"Exception while waiting for process: {str(e)}")
                pass
            
            # Get current console records
            console = self.get_console_records(session_id)
            
            return ShellExecResult(
                session_id=session_id,
                command=command,
                status="running",
            )
        except Exception as e:
            logger.error(f"Command execution failed: {str(e)}", exc_info=True)
            raise AppException(
                message=f"Command execution failed: {str(e)}",
                data={"session_id": session_id, "command": command}
            )

    async def view_shell(self, session_id: str, console: bool = False) -> ShellViewResult:
        """
        Asynchronously view the content of the specified shell session
        """
        logger.debug(f"Viewing shell content for session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        self._touch_session(session_id)
        
        shell = self.active_shells[session_id]
        
        # Get raw output and filter ANSI escape codes
        raw_output = shell["output"]
        clean_output = self._remove_ansi_escape_codes(raw_output)
        
        # Get command console records with filtered output
        if console:
            console = self.get_console_records(session_id)
        else:
            console = None
        
        return ShellViewResult(
            output=clean_output,
            session_id=session_id,
            console=console
        )

    def get_console_records(self, session_id: str) -> List[ConsoleRecord]:
        """
        Get command console records for the specified session (this method doesn't need to be async)
        """
        logger.debug(f"Getting console records for session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        self._touch_session(session_id)
        
        # Get raw console records and filter ANSI escape codes
        raw_console = self.active_shells[session_id]["console"]
        clean_console = []
        for record in raw_console:
            clean_record = ConsoleRecord(
                ps1=record.ps1,
                command=record.command,
                output=self._remove_ansi_escape_codes(record.output)
            )
            clean_console.append(clean_record)
        
        return clean_console

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ShellWaitResult:
        """
        Asynchronously wait for the process in the specified shell session to return
        """
        logger.debug(f"Waiting for process in session: {session_id}, timeout: {seconds}s")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        self._touch_session(session_id)
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            # Asynchronously wait for process to complete
            if seconds is None:
                seconds = 60
            await asyncio.wait_for(process.wait(), timeout=seconds)
            
            logger.info(f"Process completed with return code: {process.returncode}")
            return ShellWaitResult(
                returncode=process.returncode
            )
        except asyncio.TimeoutError:
            logger.warning(f"Process wait timeout expired: {seconds}s")
            raise BadRequestException(f"Wait timeout: {seconds} seconds")
        except Exception as e:
            logger.error(f"Failed to wait for process: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to wait for process: {str(e)}")

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool) -> ShellWriteResult:
        """
        Asynchronously write input to the process in the specified shell session
        """
        logger.debug(f"Writing to process in session: {session_id}, press_enter: {press_enter}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        self._touch_session(session_id)
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            # Check if the process is still running
            if process.returncode is not None:
                logger.error(f"Process has already terminated, cannot write input")
                raise BadRequestException("Process has ended, cannot write input")
            
            # Prepare input data
            if press_enter:
                input_data = f"{input_text}\n".encode()
            else:
                input_data = input_text.encode()
            if len(input_data) > self._MAX_STDIN_BYTES:
                raise BadRequestException("Shell input exceeds the size limit")
            
            # Add input to output and console records
            input_str = input_data.decode('utf-8')
            self._append_output(shell, input_str)
            
            # Asynchronously write input
            process.stdin.write(input_data)
            try:
                await asyncio.wait_for(
                    process.stdin.drain(),
                    timeout=self._STDIN_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                process.stdin.close()
                raise BadRequestException(
                    "Shell input write exceeded the time limit"
                )
            except asyncio.CancelledError:
                process.stdin.close()
                raise
            
            logger.info(f"Successfully wrote input to process")
            
            return ShellWriteResult(
                status="success"
            )
        except BadRequestException:
            raise
        except Exception as e:
            logger.error(f"Failed to write input: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to write input: {str(e)}")

    async def kill_process(self, session_id: str) -> ShellKillResult:
        """
        Asynchronously terminate the process in the specified shell session
        """
        logger.info(f"Killing process in session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        self._touch_session(session_id)
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            has_live_group = (
                process.returncode is None
                or self._process_group_has_members(
                    getattr(process, "pid", -1)
                )
            )
            has_unreaped_shell_uid = (
                getattr(process, "_manus_shell_uid", None) is not None
                and not getattr(
                    process,
                    "_manus_shell_uid_cleaned",
                    False,
                )
            )
            if has_live_group or has_unreaped_shell_uid:
                logger.debug("Attempting to terminate process group")
                await self._stop_process(process)
                
                logger.info(f"Process terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="terminated",
                    returncode=process.returncode
                )
            else:
                logger.info(f"Process was already terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="already_terminated",
                    returncode=process.returncode
                )
        except Exception as e:
            logger.error(f"Failed to kill process: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to terminate process: {str(e)}")

    async def kill_all_processes(self) -> ShellKillAllResult:
        """Terminate every registered shell process in this sandbox instance.

        The caller must establish that the whole sandbox is exclusively owned.
        Holding the registry lock prevents a new command from being admitted
        between the snapshot and completion of the cleanup.
        """

        async with self._session_lock:
            shells = list(self.active_shells.items())
            cleanup_slots = asyncio.Semaphore(
                self._MAX_SHELL_CLEANUP_CONCURRENCY
            )

            async def stop_one(shell: Dict[str, Any]) -> bool:
                process = shell["process"]
                was_live = (
                    process.returncode is None
                    or self._process_group_has_members(
                        getattr(process, "pid", -1)
                    )
                    or (
                        getattr(process, "_manus_shell_uid", None) is not None
                        and not getattr(
                            process,
                            "_manus_shell_uid_cleaned",
                            False,
                        )
                    )
                )
                async with cleanup_slots:
                    await self._stop_process(process)
                    await self._stop_reader_task(shell.get("reader_task"))
                return was_live

            results = await asyncio.gather(
                *(stop_one(shell) for _session_id, shell in shells),
                return_exceptions=True,
            )

        failures = [
            result for result in results if isinstance(result, BaseException)
        ]
        if failures:
            for failure in failures:
                logger.error(
                    "Failed to terminate one sandbox shell process: %s",
                    type(failure).__name__,
                )
            raise AppException(
                message=(
                    "Failed to terminate all shell processes "
                    f"({len(failures)} failed)"
                )
            )
        return ShellKillAllResult(
            sessions_seen=len(shells),
            sessions_terminated=sum(result is True for result in results),
        )

    def create_session_id(self) -> str:
        """
        Create a new session ID (this method doesn't need to be async)
        """
        session_id = str(uuid.uuid4())
        logger.debug(f"Created new session ID: {session_id}")
        return session_id

shell_service = ShellService()
