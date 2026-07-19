"""
File Operation Service Implementation - Async Version
"""
import os
import re
import fnmatch
import asyncio
import subprocess
import mimetypes
from typing import Optional, BinaryIO
from fastapi import UploadFile
from app.models.file import (
    FileReadResult, FileWriteResult, FileReplaceResult,
    FileSearchResult, FileFindResult, FileUploadResult
)
from app.core.exceptions import AppException, ResourceNotFoundException, BadRequestException


class FileService:
    """File Operation Service"""

    _MAX_FIND_RESULTS = 1_000
    _MAX_FIND_VISITED_ENTRIES = 100_000

    @staticmethod
    def _matches_glob(relative_path: str, pattern: str) -> bool:
        """Match a slash-separated glob without allowing ``**`` to escape."""
        path_parts = tuple(part for part in relative_path.split("/") if part)
        pattern_parts = tuple(part for part in pattern.split("/") if part)
        memo = {}

        def matches(path_index: int, pattern_index: int) -> bool:
            key = (path_index, pattern_index)
            if key in memo:
                return memo[key]
            if pattern_index == len(pattern_parts):
                result = path_index == len(path_parts)
            elif pattern_parts[pattern_index] == "**":
                result = matches(path_index, pattern_index + 1) or (
                    path_index < len(path_parts)
                    and not path_parts[path_index].startswith(".")
                    and matches(path_index + 1, pattern_index)
                )
            else:
                path_part = (
                    path_parts[path_index]
                    if path_index < len(path_parts)
                    else ""
                )
                pattern_part = pattern_parts[pattern_index]
                result = (
                    path_index < len(path_parts)
                    and (
                        not path_part.startswith(".")
                        or pattern_part.startswith(".")
                    )
                    and fnmatch.fnmatchcase(
                        path_part, pattern_part
                    )
                    and matches(path_index + 1, pattern_index + 1)
                )
            memo[key] = result
            return result

        return matches(0, 0)

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
        # Check if file exists
        if not os.path.exists(file) and not sudo:
            raise ResourceNotFoundException(f"File does not exist: {file}")
        
        try:
            content = ""
            
            # Read with sudo
            if sudo:
                command = f"sudo cat '{file}'"
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    raise BadRequestException(f"Failed to read file: {stderr.decode()}")
                
                content = stdout.decode('utf-8')
            else:
                # Asynchronously read file
                def read_file_async():
                    try:
                        with open(file, 'r', encoding='utf-8') as f:
                            return f.read()
                    except Exception as e:
                        raise AppException(message=f"Failed to read file: {str(e)}")
                
                # Execute IO operation in thread pool
                content = await asyncio.to_thread(read_file_async)
            
            # Process line range
            if start_line is not None or end_line is not None:
                lines = content.splitlines()
                start = start_line if start_line is not None else 0
                end = end_line if end_line is not None else len(lines)
                content = '\n'.join(lines[start:end])
            
            if max_length is not None and max_length > 0 and len(content) > max_length:
                content = content[:max_length] + "(truncated)"
            
            return FileReadResult(
                content=content,
                file=file
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
            sudo: Whether to use sudo privileges
        """
        try:
            # Prepare content
            if leading_newline:
                content = '\n' + content
            if trailing_newline:
                content = content + '\n'
            
            bytes_written = 0
            
            # Write with sudo
            if sudo:
                mode = '>>' if append else '>'
                # Create temporary file
                temp_file = f"/tmp/file_write_{os.getpid()}.tmp"
                
                # Asynchronously write to temporary file
                def write_temp_file():
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        f.write(content)
                    return len(content.encode('utf-8'))
                
                bytes_written = await asyncio.to_thread(write_temp_file)
                
                # Use sudo to write temporary file content to target file
                command = f"sudo bash -c \"cat {temp_file} {mode} '{file}'\""
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    raise BadRequestException(f"Failed to write file: {stderr.decode()}")
                
                # Clean up temporary file
                os.unlink(temp_file)
            else:
                # Ensure directory exists
                os.makedirs(os.path.dirname(file), exist_ok=True)
                
                # Asynchronously write file
                def write_file_async():
                    mode = 'a' if append else 'w'
                    with open(file, mode, encoding='utf-8') as f:
                        return f.write(content)
                
                bytes_written = await asyncio.to_thread(write_file_async)
            
            return FileWriteResult(
                file=file,
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
            sudo: Whether to use sudo privileges
        """
        # First read file content
        file_result = await self.read_file(file, sudo=sudo)
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
        await self.write_file(file, new_content, sudo=sudo)
        
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
        file_result = await self.read_file(file, sudo=sudo)
        content = file_result.content
        
        # Process line by line
        lines = content.splitlines()
        matches = []
        line_numbers = []
        
        # Compile regular expression
        try:
            pattern = re.compile(regex)
        except Exception as e:
            raise BadRequestException(f"Invalid regular expression: {str(e)}")
        
        # Find matches (use async processing for possibly large files)
        def process_lines():
            nonlocal matches, line_numbers
            for i, line in enumerate(lines):
                if pattern.search(line):
                    matches.append(line)
                    line_numbers.append(i)
        
        await asyncio.to_thread(process_lines)
        
        return FileSearchResult(
            file=file,
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
        normalized_glob = "/".join(glob_parts)

        # A recursive glob from the filesystem root walks mounted pseudo
        # filesystems such as /proc and can occupy a sandbox worker for many
        # minutes.  Root-level, non-recursive lookups remain available.
        if normalized_path == os.path.sep and (
            len(glob_parts) != 1 or glob_parts[0] == "**"
        ):
            raise BadRequestException(
                "Only root-level, non-recursive filesystem searches are allowed"
            )

        # Check if path exists
        if not os.path.exists(normalized_path):
            raise ResourceNotFoundException(
                f"Directory does not exist: {normalized_path}"
            )
        
        # Walk explicitly instead of glob.glob(recursive=True): os.walk with
        # followlinks=False cannot escape through a directory symlink, and the
        # hard limits bound both CPU work and response size.
        def glob_async():
            files = []
            visited_entries = 0
            recursive = "**" in glob_parts
            max_depth = len(glob_parts)
            hidden_directory_patterns = tuple(
                part for part in glob_parts if part.startswith(".")
            )

            for current_root, directories, filenames in os.walk(
                normalized_path,
                followlinks=False,
            ):
                relative_root = os.path.relpath(current_root, normalized_path)
                root_parts = () if relative_root == "." else tuple(
                    relative_root.split(os.path.sep)
                )
                entries = [
                    (name, True) for name in directories
                ] + [
                    (name, False) for name in filenames
                ]

                # Symlinked directories may be returned as matches but must
                # never be traversed.  Ordinary ``**`` also does not enter
                # hidden directories under Python glob semantics.
                directories[:] = [
                    name
                    for name in directories
                    if not os.path.islink(os.path.join(current_root, name))
                    and (
                        not name.startswith(".")
                        or any(
                            fnmatch.fnmatchcase(name, pattern)
                            for pattern in hidden_directory_patterns
                        )
                    )
                ]

                for name, is_directory in entries:
                    visited_entries += 1
                    if visited_entries > self._MAX_FIND_VISITED_ENTRIES:
                        return files
                    relative_parts = (*root_parts, name)
                    relative_path = "/".join(relative_parts)
                    if (
                        (not directory_only or is_directory)
                        and self._matches_glob(relative_path, normalized_glob)
                    ):
                        matched_path = os.path.join(current_root, name)
                        files.append(
                            os.path.join(matched_path, "")
                            if directory_only
                            else matched_path
                        )
                        if len(files) >= self._MAX_FIND_RESULTS:
                            return files

                if not recursive and len(root_parts) >= max_depth - 1:
                    directories.clear()

            return files
        
        files = await asyncio.to_thread(glob_async)
        
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
        try:
            chunk_size = 8192  # 8KB chunks
            total_size = 0
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(path), exist_ok=True)
            
            # Stream write directly to target file
            def write_stream_direct():
                nonlocal total_size
                with open(path, 'wb') as f:
                    while True:
                        chunk = file_stream.file.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        total_size += len(chunk)
            
            await asyncio.to_thread(write_stream_direct)
            
            return FileUploadResult(
                file_path=path,
                file_size=total_size,
                success=True
            )
        except Exception as e:
            raise AppException(message=f"Failed to upload file: {str(e)}")

    def ensure_file(self, path: str) -> None:
        """
        Ensure file exists
        
        Args:
            path: Path of the file to check
        """
        try:
            # Check if file exists
            if not os.path.exists(path):
                raise ResourceNotFoundException(f"File does not exist: {path}")
                    
        except Exception as e:
            if isinstance(e, (BadRequestException, ResourceNotFoundException)):
                raise e
            raise AppException(message=f"Failed to ensure file: {str(e)}")


# Service instance
file_service = FileService()
