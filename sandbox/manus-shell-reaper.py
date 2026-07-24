#!/usr/bin/python3
"""Privileged reaper for isolated sandbox-shell UIDs."""

import os
import signal
import sys
import time


FIRST_SHELL_UID = 20_000
SHELL_UID_COUNT = 64
MAX_PASSES = 20
PASS_DELAY_SECONDS = 0.02


def _process_identity(process_id: int) -> tuple[int, str] | None:
    if process_id <= 1 or process_id == os.getpid():
        return None
    try:
        user_id = None
        state = None
        with open(f"/proc/{process_id}/status", "r", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("Uid:"):
                    user_id = int(line.split()[1])
                elif line.startswith("State:"):
                    state = line.split()[1]
        if user_id is not None and state is not None:
            return user_id, state
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    return None


def _matching_processes(user_ids: set[int]) -> list[int]:
    try:
        entries = os.scandir("/proc")
    except OSError:
        return []
    with entries:
        return [
            int(entry.name)
            for entry in entries
            if entry.name.isdigit()
            and (
                identity := _process_identity(int(entry.name))
            ) is not None
            and identity[0] in user_ids
            and identity[1] != "Z"
        ]


def _kill_matching_process(process_id: int, user_ids: set[int]) -> None:
    """Signal the exact process object after revalidating its reserved UID."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_open) and callable(pidfd_send_signal):
        process_fd = None
        try:
            # Opening the pidfd first pins the process identity. The following
            # /proc check can no longer validate one PID and signal a later
            # unrelated process that reused the same numeric PID.
            process_fd = pidfd_open(process_id, 0)
            identity = _process_identity(process_id)
            if (
                identity is None
                or identity[0] not in user_ids
                or identity[1] == "Z"
            ):
                return
            pidfd_send_signal(process_fd, signal.SIGKILL, None, 0)
        except (ProcessLookupError, FileNotFoundError, PermissionError, OSError):
            return
        finally:
            if process_fd is not None:
                os.close(process_fd)
        return

    # Compatibility fallback for older kernels/Python builds. Current sandbox
    # images use pidfd; retain exact UID/state validation for local tooling.
    identity = _process_identity(process_id)
    if (
        identity is None
        or identity[0] not in user_ids
        or identity[1] == "Z"
    ):
        return
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        return 2

    if sys.argv[1] == "--all":
        user_ids = set(
            range(FIRST_SHELL_UID, FIRST_SHELL_UID + SHELL_UID_COUNT)
        )
    else:
        try:
            user_id = int(sys.argv[1])
        except ValueError:
            return 2
        if not FIRST_SHELL_UID <= user_id < FIRST_SHELL_UID + SHELL_UID_COUNT:
            return 2
        user_ids = {user_id}

    # Each concurrent shell owns a distinct pre-created UID. That ownership
    # survives setsid, double-fork, environment clearing and PID-1 reparenting.
    # Revalidate the UID immediately before every signal to make PID reuse safe.
    for _ in range(MAX_PASSES):
        matches = _matching_processes(user_ids)
        if not matches:
            return 0
        for process_id in matches:
            _kill_matching_process(process_id, user_ids)
        time.sleep(PASS_DELAY_SECONDS)
    return 1 if _matching_processes(user_ids) else 0


if __name__ == "__main__":
    raise SystemExit(main())
