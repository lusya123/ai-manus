#!/usr/bin/python3
"""Drop privileges to one validated sandbox-shell UID and exec a command."""

import os
import sys


FIRST_SHELL_UID = 20_000
SHELL_UID_COUNT = 64
SHARED_GID = 1_000


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) < 3:
        return 2
    try:
        user_id = int(sys.argv[1])
    except ValueError:
        return 2
    if not FIRST_SHELL_UID <= user_id < FIRST_SHELL_UID + SHELL_UID_COUNT:
        return 2

    os.setgroups([SHARED_GID])
    os.setgid(SHARED_GID)
    os.setuid(user_id)
    environment = os.environ.copy()
    environment["HOME"] = "/home/ubuntu"
    environment["USER"] = f"manus-shell-{user_id - FIRST_SHELL_UID}"
    environment["LOGNAME"] = environment["USER"]
    os.execvpe(sys.argv[2], sys.argv[2:], environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
