from enum import Enum
from typing import Any, Optional, Protocol, BinaryIO
from app.domain.models.tool_result import ToolResult
from app.domain.external.browser import Browser


class SandboxUnavailableError(RuntimeError):
    """A sandbox may still exist, but its provider cannot confirm state now."""


class SandboxShellCleanupUnsupportedError(SandboxUnavailableError):
    """An older runtime lacks the scoped shell-cleanup API.

    Lifecycle provisioners may catch this only after proving exclusive
    ownership and fall back to deleting that exact sandbox generation.
    """

    def __init__(self, status_code: int):
        super().__init__(
            "Sandbox runtime does not support scoped shell cleanup "
            f"(HTTP {status_code})"
        )
        self.status_code = status_code


class SharedSandboxShellCleanupError(RuntimeError):
    """Broad shell cleanup is unsafe for a shared sandbox topology."""


class SandboxProvisioningError(RuntimeError):
    """Provisioning failed and cleanup could not confirm resource deletion."""

    def __init__(self, sandbox_id: str, message: str):
        super().__init__(message)
        self.sandbox_id = sandbox_id


class SandboxShellProcessScope(str, Enum):
    """How shell processes are isolated behind one sandbox API handle."""

    EXCLUSIVE = "exclusive"
    SHARED = "shared"
    UNKNOWN = "unknown"


class Sandbox(Protocol):
    """Sandbox service gateway interface"""

    @property
    def shell_process_scope(self) -> SandboxShellProcessScope:
        """Whether every shell process belongs to this logical sandbox owner."""
        ...

    async def aclose(self) -> None:
        """Close this client handle without deleting the provider resource.

        ``get()`` may return a fresh HTTP/provider handle for an existing,
        persisted sandbox.  Releasing that handle must never stop the
        container or cloud session; destructive cleanup remains ``destroy``.
        """
        ...

    async def ensure_sandbox(self) -> None:
        """Ensure sandbox is ready"""
        ...
    
    async def exec_command(
        self,
        session_id: str,
        exec_dir: str,
        command: str
    ) -> ToolResult:
        """Execute command
        
        Args:
            session_id: Session ID
            exec_dir: Execution directory
            command: Command to execute
            
        Returns:
            Command execution result
        """
        ...
    
    async def view_shell(self, session_id: str, console: bool = False) -> ToolResult:
        """View shell status
        
        Args:
            session_id: Session ID
            console: Whether to return console records

        Returns:
            Shell status information
        """
        ...
    
    async def wait_for_process(
        self,
        session_id: str,
        seconds: Optional[int] = None
    ) -> ToolResult:
        """Wait for process
        
        Args:
            session_id: Session ID
            seconds: Wait seconds
            
        Returns:
            Wait result
        """
        ...
    
    async def write_to_process(
        self,
        session_id: str,
        input_text: str,
        press_enter: bool = True
    ) -> ToolResult:
        """Write input to process
        
        Args:
            session_id: Session ID
            input_text: Input text
            press_enter: Whether to press enter
            
        Returns:
            Write result
        """
        ...
    
    async def kill_process(self, session_id: str) -> ToolResult:
        """Terminate process
        
        Args:
            session_id: Session ID
            
        Returns:
            Termination result
        """
        ...

    async def kill_all_shell_processes(self) -> ToolResult:
        """Terminate every shell process in an exclusively-owned sandbox."""
        ...
    
    async def file_write(
        self, 
        file: str, 
        content: str, 
        append: bool = False, 
        leading_newline: bool = False, 
        trailing_newline: bool = False, 
        sudo: bool = False
    ) -> ToolResult:
        """Write content to file
        
        Args:
            file: File path
            content: Content to write
            append: Whether to append content
            leading_newline: Whether to add newline before content
            trailing_newline: Whether to add newline after content
            sudo: Whether to use sudo privileges
            
        Returns:
            Write operation result
        """
        ...
    
    async def file_read(
        self, 
        file: str, 
        start_line: int = None, 
        end_line: int = None, 
        sudo: bool = False
    ) -> ToolResult:
        """Read file content
        
        Args:
            file: File path
            start_line: Start line number
            end_line: End line number
            sudo: Whether to use sudo privileges
            
        Returns:
            File content
        """
        ...
    
    async def file_exists(self, path: str) -> ToolResult:
        """Check if file exists
        
        Args:
            path: File path
            
        Returns:
            Whether file exists
        """
        ...
    
    async def file_delete(self, path: str) -> ToolResult:
        """Delete file
        
        Args:
            path: File path
            
        Returns:
            Delete operation result
        """
        ...
    
    async def file_list(self, path: str) -> ToolResult:
        """List directory contents
        
        Args:
            path: Directory path
            
        Returns:
            Directory content list
        """
        ...
    
    async def file_replace(
        self, 
        file: str, 
        old_str: str, 
        new_str: str, 
        sudo: bool = False
    ) -> ToolResult:
        """Replace string in file
        
        Args:
            file: File path
            old_str: String to replace
            new_str: Replacement string
            sudo: Whether to use sudo privileges
            
        Returns:
            Replace operation result
        """
        ...
    
    async def file_search(
        self, 
        file: str, 
        regex: str, 
        sudo: bool = False
    ) -> ToolResult:
        """Search in file content
        
        Args:
            file: File path
            regex: Regular expression
            sudo: Whether to use sudo privileges
            
        Returns:
            Search result
        """
        ...
    
    async def file_find(
        self, 
        path: str, 
        glob_pattern: str
    ) -> ToolResult:
        """Find files by name pattern
        
        Args:
            path: Search directory path
            glob_pattern: Glob matching pattern
            
        Returns:
            Found file list
        """
        ...
    
    async def file_upload(
        self,
        file_data: BinaryIO,
        path: str,
        filename: str = None
    ) -> ToolResult:
        """Upload file to sandbox
        
        Args:
            file_data: File content as binary stream
            path: Target file path in sandbox
            filename: Original filename (optional)
            
        Returns:
            Upload operation result
        """
        ...
    
    async def file_download(
        self,
        path: str
    ) -> BinaryIO:
        """Download file from sandbox
        
        Args:
            path: File path in sandbox
            
        Returns:
            File content as binary stream
        """
        ...
    
    async def destroy(self) -> bool:
        """Destroy current sandbox instance
        
        Returns:
            Whether destroyed successfully
        """
        ...
    
    async def get_browser(self) -> Browser:
        """Get browser instance
        
        Returns:
            Browser: Returns a configured browser instance for web automation
        """
        ...

    @property
    def id(self) -> str:
        """Sandbox ID"""
        ...

    @property
    def cdp_url(self) -> str:
        """CDP URL"""
        ...

    @property
    def vnc_url(self) -> str:
        """VNC URL"""
        ...
    
    @classmethod
    async def create(cls) -> 'Sandbox':
        """Create a new sandbox instance"""
        ...
    
    @classmethod
    async def get(cls, id: str) -> 'Sandbox':
        """Get sandbox by ID
        
        Args:
            id: Sandbox ID
            
        Returns:
            Sandbox instance
        """
        ...
