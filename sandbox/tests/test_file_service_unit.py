import asyncio
import io
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import BadRequestException
from app.services.file import FileService


@pytest.mark.asyncio
async def test_recursive_find_rejects_filesystem_root():
    service = FileService()

    with pytest.raises(BadRequestException, match="root-level"):
        await service.find_by_name("/tmp/../..", "**/PLAN.md")

    with pytest.raises(BadRequestException, match="root-level"):
        await service.find_by_name("/", "*/*/PLAN.md")


@pytest.mark.asyncio
async def test_recursive_find_rejects_virtual_filesystems():
    service = FileService()

    for path in ("/dev", "/proc", "/run", "/sys"):
        with pytest.raises(BadRequestException, match="virtual filesystems"):
            await service.find_by_name(path, "**/*")


@pytest.mark.asyncio
async def test_non_recursive_find_allows_filesystem_root():
    service = FileService()

    result = await service.find_by_name("/", "definitely-not-present-*.txt")

    assert result.path == "/"
    assert result.files == []


@pytest.mark.asyncio
async def test_find_rejects_absolute_and_parent_traversal_globs(tmp_path):
    service = FileService()

    for pattern in ("/**/PLAN.md", "\\**\\PLAN.md", "../**/PLAN.md"):
        with pytest.raises(BadRequestException, match="relative path"):
            await service.find_by_name(str(tmp_path), pattern)


@pytest.mark.asyncio
async def test_recursive_find_does_not_follow_directory_symlinks(tmp_path):
    service = FileService()
    search_root = tmp_path / "search"
    outside_root = tmp_path / "outside"
    search_root.mkdir()
    outside_root.mkdir()
    (outside_root / "secret.md").write_text("secret")
    (search_root / "outside-link").symlink_to(
        outside_root,
        target_is_directory=True,
    )

    result = await service.find_by_name(str(search_root), "**/secret.md")

    assert result.files == []


@pytest.mark.asyncio
async def test_recursive_find_matches_zero_or_more_directories(tmp_path):
    service = FileService()
    nested = tmp_path / "nested"
    nested.mkdir()
    root_file = tmp_path / "PLAN.md"
    nested_file = nested / "PLAN.md"
    root_file.write_text("root")
    nested_file.write_text("nested")

    result = await service.find_by_name(str(tmp_path), "**/PLAN.md")

    assert set(result.files) == {str(root_file), str(nested_file)}


@pytest.mark.asyncio
async def test_find_preserves_glob_hidden_file_rules(tmp_path):
    service = FileService()
    visible = tmp_path / "visible.md"
    hidden = tmp_path / ".hidden.md"
    visible.write_text("visible")
    hidden.write_text("hidden")

    ordinary_result = await service.find_by_name(str(tmp_path), "**/*.md")
    hidden_result = await service.find_by_name(str(tmp_path), "**/.hidden.md")

    assert ordinary_result.files == [str(visible)]
    assert hidden_result.files == [str(hidden)]


@pytest.mark.asyncio
async def test_find_prunes_hidden_directories(tmp_path):
    service = FileService()
    hidden = tmp_path / ".cache"
    project = tmp_path / "project"
    hidden.mkdir()
    project.mkdir()
    (hidden / "hidden.md").write_text("hidden")
    (project / "report.md").write_text("report")

    result = await service.find_by_name(str(tmp_path), "**/*.md")

    assert result.files == [str(tmp_path / "project" / "report.md")]


@pytest.mark.asyncio
async def test_find_worker_stops_at_wall_clock_budget(monkeypatch, tmp_path):
    service = FileService()
    service._MAX_FIND_SECONDS = 0
    scandir_called = False

    def fail_scandir(path):
        nonlocal scandir_called
        scandir_called = True
        raise AssertionError("deadline must be checked before opening a directory")

    monkeypatch.setattr("app.services.file.os.scandir", fail_scandir)

    result = await service.find_by_name(str(tmp_path), "**/*.md")

    assert result.files == []
    assert scandir_called is False


@pytest.mark.asyncio
async def test_find_worker_stops_at_visited_entry_budget(tmp_path):
    service = FileService()
    service._MAX_FIND_VISITED_ENTRIES = 1
    (tmp_path / "first.md").write_text("first")
    (tmp_path / "second.md").write_text("second")

    result = await service.find_by_name(str(tmp_path), "*.md")

    assert len(result.files) == 1


@pytest.mark.asyncio
async def test_cancelling_find_stops_scanner_thread(monkeypatch, tmp_path):
    service = FileService()
    service._MAX_FIND_SECONDS = 30
    visited = 0

    class Entry:
        def __init__(self, index):
            self.name = f"file-{index}.txt"
            self.path = str(tmp_path / self.name)

        def is_dir(self, follow_symlinks=False):
            return False

        def is_symlink(self):
            return False

    class EndlessScandir:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal visited
            time.sleep(0.001)
            visited += 1
            return Entry(visited)

    monkeypatch.setattr(
        "app.services.file.os.scandir",
        lambda path: EndlessScandir(),
    )

    task = asyncio.create_task(
        service.find_by_name(str(tmp_path), "**/*.md")
    )
    while visited < 3:
        await asyncio.sleep(0.002)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    count_after_cancel = visited
    await asyncio.sleep(0.02)

    assert visited <= count_after_cancel + 2


@pytest.mark.asyncio
async def test_find_treats_iteration_oserror_as_unreadable_directory(
    monkeypatch,
    tmp_path,
):
    service = FileService()

    class BrokenScandir:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("directory changed")

    monkeypatch.setattr(
        "app.services.file.os.scandir",
        lambda path: BrokenScandir(),
    )

    result = await service.find_by_name(str(tmp_path), "*.md")

    assert result.files == []


@pytest.mark.asyncio
async def test_find_normalizes_current_directory_segments(tmp_path):
    service = FileService()
    nested = tmp_path / "nested"
    nested.mkdir()
    root_file = tmp_path / "root.md"
    nested_file = nested / "nested.md"
    root_file.write_text("root")
    nested_file.write_text("nested")

    root_result = await service.find_by_name(str(tmp_path), "./*.md")
    nested_result = await service.find_by_name(str(tmp_path), "nested/./*.md")

    assert root_result.files == [str(root_file)]
    assert nested_result.files == [str(nested_file)]


@pytest.mark.asyncio
async def test_find_trailing_slash_matches_directories_only(tmp_path):
    service = FileService()
    directory = tmp_path / "folder"
    file_path = tmp_path / "file.txt"
    directory.mkdir()
    file_path.write_text("file")

    result = await service.find_by_name(str(tmp_path), "*/")

    assert result.files == [f"{directory}{os.path.sep}"]


def test_glob_matching_handles_deep_paths_without_recursion():
    relative_path = "/".join(["nested"] * 2_000 + ["PLAN.md"])

    assert FileService._matches_glob(relative_path, "**/PLAN.md") is True


@pytest.mark.asyncio
async def test_read_file_rejects_fifo_and_virtual_devices(tmp_path):
    service = FileService()
    fifo = tmp_path / "blocking.fifo"
    os.mkfifo(fifo)

    with pytest.raises(BadRequestException, match="regular files"):
        await service.read_file(str(fifo))
    with pytest.raises(BadRequestException, match="regular files"):
        service.ensure_file(str(fifo))

    with pytest.raises(BadRequestException, match="virtual filesystems"):
        await service.read_file("/dev/zero")


@pytest.mark.asyncio
async def test_read_file_rejects_oversized_source_before_buffering(tmp_path):
    service = FileService()
    service._MAX_TEXT_FILE_BYTES = 4
    source = tmp_path / "large.txt"
    source.write_text("12345")

    with pytest.raises(BadRequestException, match="size limit"):
        await service.read_file(str(source))


@pytest.mark.asyncio
async def test_cancelling_sudo_read_kills_worker(monkeypatch, tmp_path):
    service = FileService()
    source = tmp_path / "source.txt"
    source.write_text("content")
    communicate_started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(
        "app.services.file.asyncio.create_subprocess_exec",
        create_process,
    )

    task = asyncio.create_task(service.read_file(str(source), sudo=True))
    await communicate_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_regex_search_runs_in_killable_worker(tmp_path):
    service = FileService()
    service._REGEX_SEARCH_TIMEOUT_SECONDS = 0.2
    source = tmp_path / "catastrophic.txt"
    source.write_text("a" * 9_000 + "!")

    with pytest.raises(BadRequestException, match="time limit"):
        await service.find_in_content(str(source), r"(a+)+$")


@pytest.mark.asyncio
async def test_regex_search_rejects_embedded_null(tmp_path):
    service = FileService()
    source = tmp_path / "source.txt"
    source.write_text("content")

    with pytest.raises(BadRequestException, match="invalid data"):
        await service.find_in_content(str(source), "\x00")


@pytest.mark.asyncio
async def test_regex_search_preserves_matches_and_line_numbers(tmp_path):
    service = FileService()
    source = tmp_path / "lines.txt"
    source.write_text("alpha\nbeta\nalphabet\n")

    result = await service.find_in_content(str(source), r"^alpha")

    assert result.matches == ["alpha", "alphabet"]
    assert result.line_numbers == [0, 2]


@pytest.mark.asyncio
async def test_regex_search_checks_content_after_default_preview_limit(tmp_path):
    service = FileService()
    source = tmp_path / "long.txt"
    source.write_text("x" * 12_000 + "\nTAIL-MATCH\n")

    result = await service.find_in_content(str(source), r"^TAIL-MATCH$")

    assert result.matches == ["TAIL-MATCH"]


@pytest.mark.asyncio
async def test_str_replace_preserves_tail_of_long_file(tmp_path):
    service = FileService(writable_roots=(str(tmp_path),))
    source = tmp_path / "long.txt"
    source.write_text("HEAD" + "x" * 12_000 + "TAIL")

    await service.str_replace(str(source), "HEAD", "START")

    assert source.read_text() == "START" + "x" * 12_000 + "TAIL"


@pytest.mark.asyncio
async def test_write_rejects_fifo_and_preserves_existing_mode(tmp_path):
    service = FileService(writable_roots=(str(tmp_path),))
    fifo = tmp_path / "blocking.fifo"
    os.mkfifo(fifo)

    with pytest.raises(BadRequestException, match="regular files"):
        await service.write_file(str(fifo), "data")
    with pytest.raises(BadRequestException, match="regular files"):
        await service.write_file(str(fifo), "data", append=True)

    executable = tmp_path / "run.sh"
    executable.write_text("old")
    executable.chmod(0o755)
    await service.write_file(str(executable), "new")

    assert executable.read_text() == "new"
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/sudoers.d/manus-pwn",
        "/usr/local/sbin/manus-shell-launcher",
        "../../etc/sudoers.d/manus-pwn",
    ],
)
async def test_write_rejects_system_and_relative_paths(tmp_path, unsafe_path):
    service = FileService(writable_roots=(str(tmp_path),))

    with pytest.raises(
        BadRequestException,
        match="workspace directories|must be absolute",
    ):
        await service.write_file(unsafe_path, "pwned")


@pytest.mark.asyncio
async def test_privileged_write_and_replace_are_rejected_before_subprocess(
    monkeypatch,
    tmp_path,
):
    service = FileService(writable_roots=(str(tmp_path),))
    target = tmp_path / "legal.txt"
    target.write_text("old")

    async def fail_create_process(*args, **kwargs):
        raise AssertionError("a privileged write subprocess must never start")

    monkeypatch.setattr(
        "app.services.file.asyncio.create_subprocess_exec",
        fail_create_process,
    )

    with pytest.raises(BadRequestException, match="not supported"):
        await service.write_file(str(target), "new", sudo=True)
    with pytest.raises(BadRequestException, match="not supported"):
        await service.str_replace(str(target), "old", "new", sudo=True)

    assert target.read_text() == "old"


@pytest.mark.asyncio
async def test_write_rejects_final_and_parent_symlinks(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    service = FileService(writable_roots=(str(allowed),))

    outside_file = outside / "protected.txt"
    outside_file.write_text("protected")
    final_link = allowed / "final-link.txt"
    final_link.symlink_to(outside_file)
    parent_link = allowed / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)

    for target in (final_link, parent_link / "protected.txt"):
        with pytest.raises(BadRequestException, match="unsafe|regular files"):
            await service.write_file(str(target), "pwned")

    assert outside_file.read_text() == "protected"


@pytest.mark.asyncio
@pytest.mark.parametrize("append", [False, True])
async def test_write_rejects_existing_file_owned_by_another_uid(
    monkeypatch,
    tmp_path,
    append,
):
    """Simulate a root-owned image file without requiring root in pytest."""
    service = FileService(writable_roots=(str(tmp_path),))
    target = tmp_path / "root-owned"
    target.write_text("protected")
    original_fstat = os.fstat

    def root_owned_fstat(descriptor):
        file_stat = original_fstat(descriptor)
        return SimpleNamespace(st_mode=file_stat.st_mode, st_uid=0)

    monkeypatch.setattr(os, "fstat", root_owned_fstat)
    monkeypatch.setattr(os, "geteuid", lambda: 1_000)

    with pytest.raises(BadRequestException, match="another user"):
        await service.write_file(str(target), "pwned", append=append)

    assert target.read_text() == "protected"


@pytest.mark.asyncio
async def test_write_allows_file_owned_by_isolated_shell_uid(
    monkeypatch,
    tmp_path,
):
    service = FileService(writable_roots=(str(tmp_path),))
    target = tmp_path / "shell-created.txt"
    target.write_text("old")
    original_fstat = os.fstat

    def shell_owned_fstat(descriptor):
        file_stat = original_fstat(descriptor)
        return SimpleNamespace(
            st_mode=file_stat.st_mode,
            st_uid=service._FIRST_SHELL_UID,
        )

    monkeypatch.setattr(os, "fstat", shell_owned_fstat)

    await service.write_file(str(target), "new")

    assert target.read_text() == "new"


@pytest.mark.asyncio
async def test_write_pins_existing_inode_across_target_symlink_swap(
    monkeypatch,
    tmp_path,
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    service = FileService(writable_roots=(str(allowed),))
    target = allowed / "target.txt"
    detached_target = allowed / "detached.txt"
    outside_file = outside / "protected.txt"
    target.write_text("old")
    outside_file.write_text("protected")
    original_ftruncate = os.ftruncate
    raced = False

    def swap_target_then_truncate(descriptor, length):
        nonlocal raced
        if not raced:
            raced = True
            target.rename(detached_target)
            target.symlink_to(outside_file)
        return original_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "ftruncate", swap_target_then_truncate)

    await service.write_file(str(target), "safe")

    assert raced is True
    assert detached_target.read_text() == "safe"
    assert outside_file.read_text() == "protected"


@pytest.mark.asyncio
async def test_write_allows_nested_workspace_create_overwrite_and_append(tmp_path):
    service = FileService(writable_roots=(str(tmp_path),))
    target = tmp_path / "nested" / "report.txt"

    await service.write_file(str(target), "first")
    await service.write_file(str(target), "second")
    await service.write_file(str(target), "+tail", append=True)

    assert target.read_text() == "second+tail"


def test_sandbox_sudoers_does_not_grant_tee():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

    assert "/usr/bin/tee *" not in dockerfile.read_text()


@pytest.mark.asyncio
async def test_cancelling_regex_search_kills_worker(monkeypatch, tmp_path):
    service = FileService()
    source = tmp_path / "lines.txt"
    source.write_text("content")
    communicate_started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, input_data):
            communicate_started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(
        "app.services.file.asyncio.create_subprocess_exec",
        create_process,
    )

    task = asyncio.create_task(
        service.find_in_content(str(source), "content")
    )
    await communicate_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_cancelled_file_worker_reap_has_hard_deadline(monkeypatch):
    monkeypatch.setattr(FileService, "_PROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    class Process:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

    process = Process()
    task = asyncio.create_task(
        FileService._communicate_with_cleanup(process, timeout=10)
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)

    assert process.killed is True


@pytest.mark.asyncio
async def test_upload_rejects_oversize_without_replacing_destination(tmp_path):
    service = FileService()
    service._MAX_FILE_TRANSFER_BYTES = 4
    destination = tmp_path / "upload.bin"
    destination.write_bytes(b"old")
    upload = SimpleNamespace(file=io.BytesIO(b"12345"))

    with pytest.raises(BadRequestException, match="size limit"):
        await service.upload_file(str(destination), upload)

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".manus-upload-*")) == []


@pytest.mark.asyncio
async def test_cancelled_upload_never_commits_and_cleans_temp_file(tmp_path):
    service = FileService()
    destination = tmp_path / "upload.bin"
    destination.write_bytes(b"old")
    read_started = threading.Event()
    release_read = threading.Event()

    class BlockingStream:
        calls = 0

        def read(self, size):
            self.calls += 1
            if self.calls == 1:
                read_started.set()
                release_read.wait(timeout=2)
                return b"new"
            return b""

    upload = SimpleNamespace(file=BlockingStream())
    task = asyncio.create_task(service.upload_file(str(destination), upload))
    await asyncio.wait_for(asyncio.to_thread(read_started.wait), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release_read.set()

    for _ in range(100):
        if not list(tmp_path.glob(".manus-upload-*")):
            break
        await asyncio.sleep(0.01)

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".manus-upload-*")) == []


@pytest.mark.asyncio
async def test_upload_preserves_existing_file_mode(tmp_path):
    service = FileService()
    destination = tmp_path / "run.sh"
    destination.write_bytes(b"old")
    destination.chmod(0o755)
    upload = SimpleNamespace(file=io.BytesIO(b"new"))

    await service.upload_file(str(destination), upload)

    assert destination.read_bytes() == b"new"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
