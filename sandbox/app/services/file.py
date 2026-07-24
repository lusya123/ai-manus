"""
File Operation Service Implementation - Async Version
"""
import os
import fnmatch
import asyncio
import json
import logging
import errno
import stat
import threading
import subprocess
import mimetypes
import secrets
import sys
import tempfile
import time
from typing import Optional, BinaryIO, Iterable
from fastapi import UploadFile
from app.models.file import (
    FileReadResult, FileWriteResult, FileReplaceResult,
    FileSearchResult, FileFindResult, FileUploadResult
)
from app.core.exceptions import AppException, ResourceNotFoundException, BadRequestException


logger = logging.getLogger(__name__)


class FileService:
    """File Operation Service"""

    _MAX_FIND_RESULTS = 1_000
    _MAX_FIND_VISITED_ENTRIES = 100_000
    _MAX_FIND_SECONDS = 5.0
    _MAX_CONCURRENT_FINDS = 2
    _MAX_GLOB_PARTS = 64
    _VIRTUAL_FILESYSTEM_ROOTS = ("/dev", "/proc", "/run", "/sys")
    _MAX_FILE_TRANSFER_BYTES = 25 * 1024 * 1024
    _MAX_TEXT_FILE_BYTES = _MAX_FILE_TRANSFER_BYTES
    # API requests cap this at one million characters in the schema.  Internal
    # replace/search operations may request the whole already byte-bounded
    # text file so they never rewrite a truncated prefix or silently miss the
    # tail of a file.
    _MAX_READ_RESULT_CHARS = _MAX_TEXT_FILE_BYTES
    _FILE_READ_TIMEOUT_SECONDS = 5.0
    _FILE_WRITE_TIMEOUT_SECONDS = 5.0
    _MAX_REGEX_PATTERN_CHARS = 2_048
    _MAX_REGEX_MATCHES = 1_000
    _REGEX_SEARCH_TIMEOUT_SECONDS = 2.0
    _PROCESS_REAP_TIMEOUT_SECONDS = 1.0
    _FIRST_SHELL_UID = 20_000
    _SHELL_UID_COUNT = 64
    # Model-controlled writes are deliberately confined to disposable/user
    # workspace roots.  In particular, neither /etc nor the root-owned shell
    # isolation helpers under /usr/local/sbin are reachable.  /tmp remains
    # available for existing sandbox workflows.
    _DEFAULT_WRITABLE_ROOTS = ("/home/ubuntu", "/tmp")
    _REGEX_SEARCH_SCRIPT = r"""
import json
import re
import sys

try:
    pattern = re.compile(sys.argv[1])
except re.error as exc:
    print(json.dumps({"error": str(exc)}))
    raise SystemExit(2)

limit = int(sys.argv[2])
matches = []
line_numbers = []
for index, line in enumerate(sys.stdin.read().splitlines()):
    if pattern.search(line):
        matches.append(line)
        line_numbers.append(index)
        if len(matches) >= limit:
            break
print(json.dumps({"matches": matches, "line_numbers": line_numbers}))
"""

    def __init__(self, writable_roots: Optional[Iterable[str]] = None) -> None:
        self._find_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_FINDS)
        roots = (
            tuple(writable_roots)
            if writable_roots is not None
            else self._DEFAULT_WRITABLE_ROOTS
        )
        root_aliases: dict[str, str] = {}
        for root in roots:
            if not root:
                continue
            lexical_root = os.path.normpath(
                os.path.abspath(os.path.expanduser(root))
            )
            canonical_root = os.path.realpath(lexical_root)
            # The configured root itself is trusted, so accept both its common
            # spelling (for example /tmp on macOS) and canonical spelling while
            # opening the same pinned canonical directory descriptor.
            root_aliases[lexical_root] = canonical_root
            root_aliases[canonical_root] = canonical_root
        if not root_aliases:
            raise ValueError("At least one writable file root is required")
        # Prefer the most specific root when roots are nested.
        self._writable_roots = tuple(
            sorted(root_aliases.items(), key=lambda item: len(item[0]), reverse=True)
        )

    def _writable_target(self, file: str) -> tuple[str, tuple[str, ...], str]:
        """Resolve an absolute write target without following user symlinks.

        The returned relative components are later walked with directory file
        descriptors and ``O_NOFOLLOW``.  A string ``realpath`` check alone is
        not sufficient because an attacker can swap a checked parent directory
        for a symlink before the write commits.
        """
        try:
            expanded_file = os.path.expanduser(file)
        except (TypeError, ValueError) as exc:
            raise BadRequestException("File path is invalid") from exc
        if not os.path.isabs(expanded_file):
            raise BadRequestException("File path must be absolute")

        normalized_file = os.path.normpath(expanded_file)
        for path_prefix, canonical_root in self._writable_roots:
            if normalized_file == path_prefix:
                raise BadRequestException("File path must name a file")
            if not normalized_file.startswith(f"{path_prefix}{os.path.sep}"):
                continue
            relative = os.path.relpath(normalized_file, path_prefix)
            parts = tuple(part for part in relative.split(os.path.sep) if part)
            if not parts or any(part in {".", ".."} for part in parts):
                break
            return canonical_root, parts, normalized_file
        raise BadRequestException(
            "File writes are limited to sandbox workspace directories"
        )

    @staticmethod
    def _directory_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    def _open_writable_parent(
        self,
        root: str,
        parts: tuple[str, ...],
    ) -> tuple[int, str]:
        """Open/create the parent hierarchy without ever traversing a symlink."""
        flags = self._directory_open_flags()
        directory_fd = None
        try:
            directory_fd = os.open(root, flags)
            for part in parts[:-1]:
                try:
                    child_fd = os.open(
                        part,
                        flags,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o755, dir_fd=directory_fd)
                    except FileExistsError:
                        # A racing creator is safe only if the no-follow open
                        # below proves that it created a real directory.
                        pass
                    child_fd = os.open(
                        part,
                        flags,
                        dir_fd=directory_fd,
                    )
                os.close(directory_fd)
                directory_fd = child_fd
            return directory_fd, parts[-1]
        except OSError as exc:
            if directory_fd is not None:
                os.close(directory_fd)
            raise BadRequestException(
                "File path contains an unsafe or unwritable directory"
            ) from exc

    @staticmethod
    def _write_all(
        descriptor: int,
        content: bytes,
        stop_event: threading.Event,
    ) -> bool:
        offset = 0
        while offset < len(content):
            if stop_event.is_set():
                return False
            written = os.write(
                descriptor,
                content[offset:offset + 64 * 1024],
            )
            if written <= 0:
                raise OSError("File write made no progress")
            offset += written
        return True

    @classmethod
    def _is_owned_sandbox_file(cls, user_id: int) -> bool:
        """Accept the API UID and this container's isolated shell UID pool."""
        return user_id == os.geteuid() or (
            cls._FIRST_SHELL_UID
            <= user_id
            < cls._FIRST_SHELL_UID + cls._SHELL_UID_COUNT
        )

    @staticmethod
    def _open_temporary_file(directory_fd: int) -> tuple[int, str]:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(32):
            name = f".manus-write-{secrets.token_hex(12)}"
            try:
                return os.open(name, flags, 0o600, dir_fd=directory_fd), name
            except FileExistsError:
                continue
        raise OSError("Unable to allocate a temporary file")

    def _write_regular_file(
        self,
        root: str,
        parts: tuple[str, ...],
        encoded_content: bytes,
        append: bool,
        stop_event: threading.Event,
    ) -> None:
        """Write through pinned descriptors, preserving the process UID boundary."""
        directory_fd = None
        target_fd = None
        temporary_fd = None
        temporary_name = None
        target_name = parts[-1]
        try:
            directory_fd, target_name = self._open_writable_parent(root, parts)
            open_flags = (
                os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if append:
                open_flags |= os.O_APPEND
            try:
                target_fd = os.open(
                    target_name,
                    open_flags,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                target_fd = None
            except OSError as exc:
                if exc.errno in {
                    errno.ELOOP,
                    errno.EISDIR,
                    errno.ENXIO,
                    errno.EPERM,
                    errno.EACCES,
                }:
                    raise BadRequestException(
                        "Only writable, owned regular files can be written"
                    ) from exc
                raise

            if target_fd is not None:
                target_stat = os.fstat(target_fd)
                if not stat.S_ISREG(target_stat.st_mode):
                    raise BadRequestException(
                        "Only regular files can be written"
                    )
                if not self._is_owned_sandbox_file(target_stat.st_uid):
                    raise BadRequestException(
                        "Files owned by another user cannot be written"
                    )
                if stop_event.is_set():
                    return
                if not append:
                    # The descriptor pins the validated inode.  Even if the
                    # pathname is swapped now, the write cannot be redirected
                    # to a symlink or a different (for example root-owned) file.
                    os.ftruncate(target_fd, 0)
                    os.lseek(target_fd, 0, os.SEEK_SET)
                self._write_all(target_fd, encoded_content, stop_event)
                return

            # For a new target, stage all bytes under a random sibling name and
            # publish with link(2), whose EEXIST behavior is an atomic
            # no-clobber guarantee if a target appears concurrently.
            temporary_fd, temporary_name = self._open_temporary_file(directory_fd)
            os.fchmod(temporary_fd, 0o644)
            if not self._write_all(temporary_fd, encoded_content, stop_event):
                return
            os.close(temporary_fd)
            temporary_fd = None
            if stop_event.is_set():
                return
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise BadRequestException(
                    "File target changed while the write was in progress"
                ) from exc
        except BadRequestException:
            raise
        except OSError as exc:
            raise BadRequestException("File path is not writable") from exc
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name is not None and directory_fd is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    async def _communicate_with_cleanup(
        process: asyncio.subprocess.Process,
        *,
        timeout: float,
        input_data: Optional[bytes] = None,
    ) -> tuple[Optional[bytes], Optional[bytes]]:
        """Communicate with a child and never leave it behind on cancellation.

        ``asyncio.wait_for`` cancels the coroutine that is waiting on the
        pipes, but it does not terminate the operating-system process.  A
        cancelled HTTP request must therefore explicitly kill and reap the
        child just like a timeout does.
        """
        communication = (
            process.communicate()
            if input_data is None
            else process.communicate(input_data)
        )
        try:
            return await asyncio.wait_for(communication, timeout=timeout)
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            reap_deadline = (
                asyncio.get_running_loop().time()
                + FileService._PROCESS_REAP_TIMEOUT_SECONDS
            )
            while process.returncode is None:
                if asyncio.get_running_loop().time() >= reap_deadline:
                    logger.error(
                        "Killed file worker did not report exit before deadline"
                    )
                    break
                await asyncio.sleep(0.01)
            raise

    @staticmethod
    def _matches_glob(relative_path: str, pattern: str) -> bool:
        """Match a slash-separated glob without allowing ``**`` to escape."""
        path_parts = tuple(part for part in relative_path.split("/") if part)
        pattern_parts = tuple(part for part in pattern.split("/") if part)
        # Dynamic programming avoids Python recursion for a deeply nested
        # but otherwise valid sandbox directory tree.
        matched = [False] * (len(pattern_parts) + 1)
        matched[0] = True
        for pattern_index, pattern_part in enumerate(pattern_parts, start=1):
            if pattern_part == "**":
                matched[pattern_index] = matched[pattern_index - 1]

        for path_part in path_parts:
            next_matched = [False] * (len(pattern_parts) + 1)
            for pattern_index, pattern_part in enumerate(
                pattern_parts,
                start=1,
            ):
                if pattern_part == "**":
                    next_matched[pattern_index] = (
                        next_matched[pattern_index - 1]
                        or (
                            not path_part.startswith(".")
                            and matched[pattern_index]
                        )
                    )
                else:
                    next_matched[pattern_index] = (
                        matched[pattern_index - 1]
                        and (
                            not path_part.startswith(".")
                            or pattern_part.startswith(".")
                        )
                        and fnmatch.fnmatchcase(path_part, pattern_part)
                    )
            matched = next_matched

        return matched[-1]

    async def read_file(self, file: str, start_line: Optional[int] = None, 
                 end_line: Optional[int] = None, sudo: bool = False, max_length: Optional[int] = 10000) -> FileReadResult:
        """
        Asynchronously read file content
        
        Args:
            file: Absolute file path
            start_line: Starting line (0-based)
            end_line: Ending line (not included)
            sudo: Whether to use sudo privileges
        """
        normalized_file = os.path.realpath(
            os.path.abspath(os.path.expanduser(file))
        )
        if any(
            normalized_file == root
            or normalized_file.startswith(f"{root}{os.path.sep}")
            for root in self._VIRTUAL_FILESYSTEM_ROOTS
        ):
            raise BadRequestException(
                "Reading from virtual filesystems is not allowed"
            )

        # Check if file exists
        if not os.path.exists(normalized_file) and not sudo:
            raise ResourceNotFoundException(f"File does not exist: {file}")

        requested_max_length = (
            max_length
            if max_length is not None and max_length > 0
            else self._MAX_READ_RESULT_CHARS
        )
        effective_max_length = min(
            requested_max_length,
            self._MAX_READ_RESULT_CHARS,
        )
        
        try:
            content = ""
            
            # Read with sudo
            if sudo:
                process = await asyncio.create_subprocess_exec(
                    "sudo",
                    "head",
                    "-c",
                    str(self._MAX_TEXT_FILE_BYTES + 1),
                    "--",
                    normalized_file,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await self._communicate_with_cleanup(
                        process,
                        timeout=self._FILE_READ_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    raise BadRequestException("File read exceeded the time limit")
                
                if process.returncode != 0:
                    raise BadRequestException(f"Failed to read file: {stderr.decode()}")
                if len(stdout) > self._MAX_TEXT_FILE_BYTES:
                    raise BadRequestException("File exceeds the text read size limit")
                content = stdout.decode('utf-8')
            else:
                def read_file_async():
                    descriptor = None
                    try:
                        descriptor = os.open(
                            normalized_file,
                            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
                        )
                        file_stat = os.fstat(descriptor)
                        if not stat.S_ISREG(file_stat.st_mode):
                            raise BadRequestException(
                                "Only regular files can be read"
                            )
                        if file_stat.st_size > self._MAX_TEXT_FILE_BYTES:
                            raise BadRequestException(
                                "File exceeds the text read size limit"
                            )
                        with os.fdopen(
                            descriptor,
                            "r",
                            encoding="utf-8",
                        ) as stream:
                            descriptor = None
                            content = stream.read(self._MAX_TEXT_FILE_BYTES + 1)
                        if len(content.encode("utf-8")) > self._MAX_TEXT_FILE_BYTES:
                            raise BadRequestException(
                                "File exceeds the text read size limit"
                            )
                        return content
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)

                try:
                    content = await asyncio.wait_for(
                        asyncio.to_thread(read_file_async),
                        timeout=self._FILE_READ_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    raise BadRequestException("File read exceeded the time limit")
            
            # Process line range
            if start_line is not None or end_line is not None:
                lines = content.splitlines()
                start = start_line if start_line is not None else 0
                end = end_line if end_line is not None else len(lines)
                content = '\n'.join(lines[start:end])
            
            if len(content) > effective_max_length:
                content = content[:effective_max_length] + "(truncated)"
            
            return FileReadResult(
                content=content,
                file=normalized_file
            )
        except Exception as e:
            if isinstance(e, BadRequestException) or isinstance(e, ResourceNotFoundException):
                raise e
            raise AppException(message=f"Failed to read file: {str(e)}")

    async def write_file(self, file: str, content: str, append: bool = False,
                  leading_newline: bool = False, trailing_newline: bool = False,
                  sudo: bool = False) -> FileWriteResult:
        """
        Asynchronously write file content
        
        Args:
            file: Absolute file path
            content: Content to write
            append: Whether to append mode
            leading_newline: Whether to add a leading newline
            trailing_newline: Whether to add a trailing newline
            sudo: Deprecated. Privileged writes are always rejected.
        """
        if sudo:
            # This check intentionally precedes path parsing and subprocess
            # creation.  Keeping the legacy API field yields a deterministic
            # error for old clients without retaining a privilege boundary
            # controlled by model input.
            raise BadRequestException("Privileged file writes are not supported")

        stop_event = threading.Event()
        try:
            # Prepare content
            if leading_newline:
                content = '\n' + content
            if trailing_newline:
                content = content + '\n'
            
            root, parts, normalized_file = self._writable_target(file)

            encoded_content = content.encode("utf-8")
            if len(encoded_content) > self._MAX_FILE_TRANSFER_BYTES:
                raise BadRequestException("File content exceeds the size limit")
            bytes_written = len(encoded_content)
            
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._write_regular_file,
                        root,
                        parts,
                        encoded_content,
                        append,
                        stop_event,
                    ),
                    timeout=self._FILE_WRITE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                stop_event.set()
                raise BadRequestException("File write exceeded the time limit")
            except asyncio.CancelledError:
                stop_event.set()
                raise
            
            return FileWriteResult(
                file=normalized_file,
                bytes_written=bytes_written
            )
        except Exception as e:
            if isinstance(e, BadRequestException):
                raise e
            raise AppException(message=f"Failed to write file: {str(e)}")

    async def str_replace(self, file: str, old_str: str, new_str: str, 
                   sudo: bool = False) -> FileReplaceResult:
        """
        Asynchronously replace string in file
        
        Args:
            file: Absolute file path
            old_str: Original string to be replaced
            new_str: New replacement string
            sudo: Deprecated. Privileged writes are always rejected.
        """
        if sudo:
            raise BadRequestException("Privileged file writes are not supported")

        # First read file content
        file_result = await self.read_file(
            file,
            sudo=False,
            max_length=self._MAX_READ_RESULT_CHARS,
        )
        content = file_result.content
        
        # Calculate replacement count
        replaced_count = content.count(old_str)
        if replaced_count == 0:
            return FileReplaceResult(
                file=file,
                replaced_count=0
            )
        
        # Perform replacement
        new_content = content.replace(old_str, new_str)
        
        # Write back to file
        await self.write_file(file, new_content)
        
        return FileReplaceResult(
            file=file,
            replaced_count=replaced_count
        )

    async def find_in_content(self, file: str, regex: str, 
                       sudo: bool = False) -> FileSearchResult:
        """
        Asynchronously search in file content
        
        Args:
            file: Absolute file path
            regex: Regular expression pattern
            sudo: Whether to use sudo privileges
        """
        # Read file
        file_result = await self.read_file(
            file,
            sudo=sudo,
            max_length=self._MAX_READ_RESULT_CHARS,
        )
        content = file_result.content
        
        if len(regex) > self._MAX_REGEX_PATTERN_CHARS:
            raise BadRequestException("Regular expression is too long")
        if "\x00" in regex:
            raise BadRequestException("Regular expression contains invalid data")

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            self._REGEX_SEARCH_SCRIPT,
            regex,
            str(self._MAX_REGEX_MATCHES),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await self._communicate_with_cleanup(
                process,
                timeout=self._REGEX_SEARCH_TIMEOUT_SECONDS,
                input_data=content.encode("utf-8"),
            )
        except asyncio.TimeoutError:
            raise BadRequestException("Regular expression search exceeded the time limit")

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AppException(message="Regular expression worker returned invalid data")
        if process.returncode == 2:
            raise BadRequestException(
                f"Invalid regular expression: {payload.get('error', 'invalid pattern')}"
            )
        if process.returncode != 0:
            logger.warning(
                "Regular expression worker failed with code %s",
                process.returncode,
            )
            raise AppException(message="Regular expression search failed")

        matches = payload.get("matches", [])
        line_numbers = payload.get("line_numbers", [])
        
        return FileSearchResult(
            file=file_result.file,
            matches=matches,
            line_numbers=line_numbers
        )

    async def find_by_name(self, path: str, glob_pattern: str) -> FileFindResult:
        """
        Asynchronously find files by name pattern
        
        Args:
            path: Directory path to search
            glob_pattern: File name pattern (glob syntax)
        """
        normalized_path = os.path.realpath(
            os.path.abspath(os.path.expanduser(path))
        )
        raw_glob = glob_pattern.replace("\\", "/")
        directory_only = raw_glob.endswith("/")
        raw_glob_parts = tuple(part for part in raw_glob.split("/") if part)

        if (
            not raw_glob
            or os.path.isabs(raw_glob)
            or ".." in raw_glob_parts
        ):
            raise BadRequestException(
                "Glob pattern must be a relative path without parent traversal"
            )
        glob_parts = tuple(part for part in raw_glob_parts if part != ".")
        if not glob_parts:
            raise BadRequestException("Glob pattern must select a file or directory")
        if len(glob_parts) > self._MAX_GLOB_PARTS:
            raise BadRequestException("Glob pattern contains too many path segments")
        normalized_glob = "/".join(glob_parts)
        recursive = "**" in glob_parts

        # A recursive glob from the filesystem root walks mounted pseudo
        # filesystems such as /proc and can occupy a sandbox worker for many
        # minutes.  Root-level, non-recursive lookups remain available.
        if normalized_path == os.path.sep and (
            len(glob_parts) != 1 or glob_parts[0] == "**"
        ):
            raise BadRequestException(
                "Only root-level, non-recursive filesystem searches are allowed"
            )

        # /proc, /sys, /dev and /run expose dynamic kernel-backed trees.  Even
        # a bounded result count cannot make recursive walks over them safe or
        # predictable, and symlinks into them resolve here before this check.
        if recursive and any(
            normalized_path == root
            or normalized_path.startswith(f"{root}{os.path.sep}")
            for root in self._VIRTUAL_FILESYSTEM_ROOTS
        ):
            raise BadRequestException(
                "Recursive searches of virtual filesystems are not allowed"
            )

        # Check if path exists
        if not os.path.exists(normalized_path):
            raise ResourceNotFoundException(
                f"Directory does not exist: {normalized_path}"
            )
        if not os.path.isdir(normalized_path):
            raise BadRequestException(
                f"Search path is not a directory: {normalized_path}"
            )
        
        # Scan one DirEntry at a time.  os.walk() materializes every entry in a
        # directory before yielding, so its limits cannot protect a huge or
        # slow directory.  The stop event also lets request cancellation tell
        # an already-running worker thread to exit cooperatively.
        stop_event = threading.Event()

        def glob_async():
            files = []
            visited_entries = 0
            max_depth = len(glob_parts)
            deadline = time.monotonic() + self._MAX_FIND_SECONDS
            hidden_directory_patterns = tuple(
                part for part in glob_parts if part.startswith(".")
            )

            pending_directories = [(normalized_path, ())]
            while pending_directories:
                if stop_event.is_set():
                    return files, "cancelled"
                if time.monotonic() >= deadline:
                    return files, "time"

                current_root, root_parts = pending_directories.pop()
                child_directories = []
                try:
                    iterator = os.scandir(current_root)
                except OSError:
                    continue

                with iterator:
                    while True:
                        try:
                            entry = next(iterator)
                        except StopIteration:
                            break
                        except OSError:
                            # Directories can disappear or lose permissions
                            # between scandir() and iteration.
                            break
                        if stop_event.is_set():
                            return files, "cancelled"
                        if time.monotonic() >= deadline:
                            return files, "time"
                        visited_entries += 1
                        if visited_entries > self._MAX_FIND_VISITED_ENTRIES:
                            return files, "entries"

                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                            is_symlink = entry.is_symlink()
                        except OSError:
                            continue

                        relative_parts = (*root_parts, entry.name)
                        relative_path = "/".join(relative_parts)
                        if (
                            (not directory_only or is_directory)
                            and self._matches_glob(relative_path, normalized_glob)
                        ):
                            files.append(
                                os.path.join(entry.path, "")
                                if directory_only
                                else entry.path
                            )
                            if len(files) >= self._MAX_FIND_RESULTS:
                                return files, "results"

                        may_descend = recursive or len(root_parts) < max_depth - 1
                        visible_or_explicit = (
                            not entry.name.startswith(".")
                            or any(
                                fnmatch.fnmatchcase(entry.name, pattern)
                                for pattern in hidden_directory_patterns
                            )
                        )
                        if (
                            is_directory
                            and not is_symlink
                            and may_descend
                            and visible_or_explicit
                        ):
                            child_directories.append(
                                (entry.path, relative_parts)
                            )

                # Preserve scandir's natural top-down order while using a
                # LIFO stack and without retaining file entries.
                pending_directories.extend(reversed(child_directories))

            return files, None

        try:
            async with self._find_semaphore:
                files, limit_reason = await asyncio.to_thread(glob_async)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        if limit_reason:
            logger.warning(
                "File search stopped at the %s limit",
                limit_reason,
            )
        
        return FileFindResult(
            path=normalized_path,
            files=files
        )

    async def upload_file(self, path: str, file_stream: UploadFile) -> FileUploadResult:
        """
        Upload file using streaming for large files
        
        Args:
            path: Target file path to save uploaded file
            file_stream: File stream from FastAPI UploadFile
        """
        stop_event = threading.Event()
        commit_lock = threading.Lock()
        try:
            chunk_size = 8192  # 8KB chunks
            total_size = 0
            normalized_path = os.path.realpath(
                os.path.abspath(os.path.expanduser(path))
            )
            if any(
                normalized_path == root
                or normalized_path.startswith(f"{root}{os.path.sep}")
                for root in self._VIRTUAL_FILESYSTEM_ROOTS
            ):
                raise BadRequestException(
                    "Uploading to virtual filesystems is not allowed"
                )
            
            # Ensure directory exists
            destination_directory = os.path.dirname(normalized_path)
            os.makedirs(destination_directory, exist_ok=True)
            
            # Write to a sibling temporary file so an oversized or failed
            # upload never leaves a partial destination behind.
            def write_stream_direct():
                nonlocal total_size
                worker_temp_path = None
                try:
                    target_mode = 0o644
                    if os.path.lexists(normalized_path):
                        target_stat = os.stat(normalized_path)
                        if not stat.S_ISREG(target_stat.st_mode):
                            raise BadRequestException(
                                "Only regular files can be uploaded"
                            )
                        target_mode = stat.S_IMODE(target_stat.st_mode)
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=destination_directory,
                        prefix=".manus-upload-",
                        delete=False,
                    ) as output:
                        worker_temp_path = output.name
                        os.fchmod(output.fileno(), target_mode)
                        while True:
                            if stop_event.is_set():
                                return
                            chunk = file_stream.file.read(chunk_size)
                            if not chunk:
                                break
                            total_size += len(chunk)
                            if total_size > self._MAX_FILE_TRANSFER_BYTES:
                                raise BadRequestException(
                                    "Uploaded file exceeds the size limit"
                                )
                            output.write(chunk)
                    # Cancellation and commit share one small critical section
                    # so a cancellation observed before replace cannot race
                    # into a later commit.
                    with commit_lock:
                        if stop_event.is_set():
                            return
                        os.replace(worker_temp_path, normalized_path)
                        worker_temp_path = None
                finally:
                    if worker_temp_path is not None:
                        try:
                            os.unlink(worker_temp_path)
                        except FileNotFoundError:
                            pass
            
            await asyncio.to_thread(write_stream_direct)
            
            return FileUploadResult(
                file_path=normalized_path,
                file_size=total_size,
                success=True
            )
        except asyncio.CancelledError:
            with commit_lock:
                stop_event.set()
            raise
        except Exception as e:
            with commit_lock:
                stop_event.set()
            if isinstance(e, BadRequestException):
                raise e
            raise AppException(message=f"Failed to upload file: {str(e)}")

    def ensure_file(self, path: str) -> str:
        """
        Ensure file exists
        
        Args:
            path: Path of the file to check
        """
        try:
            normalized_path = os.path.realpath(
                os.path.abspath(os.path.expanduser(path))
            )
            if not os.path.exists(normalized_path):
                raise ResourceNotFoundException(f"File does not exist: {path}")
            file_stat = os.stat(normalized_path)
            if not stat.S_ISREG(file_stat.st_mode):
                raise BadRequestException("Only regular files can be downloaded")
            if file_stat.st_size > self._MAX_FILE_TRANSFER_BYTES:
                raise BadRequestException("File exceeds the download size limit")
            return normalized_path
        except Exception as e:
            if isinstance(e, (BadRequestException, ResourceNotFoundException)):
                raise e
            raise AppException(message=f"Failed to ensure file: {str(e)}")


# Service instance
file_service = FileService()
