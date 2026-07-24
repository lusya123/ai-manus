from pydantic import BaseModel, Field
from typing import Optional

class ShellExecRequest(BaseModel):
    """Shell command execution request model"""
    id: Optional[str] = Field(None, max_length=128, description="Unique identifier of the target shell session, if not provided, one will be automatically created")
    exec_dir: Optional[str] = Field(None, max_length=4_096, description="Working directory for command execution (must use absolute path)")
    command: str = Field(..., min_length=1, max_length=65_536, description="Shell command to execute")


class ShellViewRequest(BaseModel):
    """Shell session content view request model"""
    id: str = Field(..., min_length=1, max_length=128, description="Unique identifier of the target shell session")
    console: Optional[bool] = Field(False, description="Whether to return console records")


class ShellWaitRequest(BaseModel):
    """Shell process wait request model"""
    id: str = Field(..., min_length=1, max_length=128, description="Unique identifier of the target shell session")
    seconds: Optional[int] = Field(None, ge=1, le=300, description="Wait time (seconds)")


class ShellWriteToProcessRequest(BaseModel):
    """Request model for writing input to a running process"""
    id: str = Field(..., min_length=1, max_length=128, description="Unique identifier of the target shell session")
    input: str = Field(..., max_length=65_536, description="Input content to write to the process")
    press_enter: bool = Field(..., description="Whether to press enter key after input")


class ShellKillProcessRequest(BaseModel):
    """Request model for terminating a running process"""
    id: str = Field(..., min_length=1, max_length=128, description="Unique identifier of the target shell session")
