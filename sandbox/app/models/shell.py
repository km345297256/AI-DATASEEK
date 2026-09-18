"""
Shell business model definitions
"""
from typing import Any, Literal, Optional, List
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


class ShellExecutionReceipt(BaseModel):
    """Server-observed execution identity; never inferred from command output."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    server_instance_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    state: Literal["starting", "running", "exited", "not_started", "unknown"]
    returncode: Optional[StrictInt] = None
    process_tree_quiescent: StrictBool = False

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Invalid receipt version")
        return value

    @model_validator(mode="after")
    def consistent_execution_state(self):
        if (self.state == "exited") != (self.returncode is not None):
            raise ValueError("Return code requires a confirmed exited process")
        if self.process_tree_quiescent and self.state not in {"exited", "not_started"}:
            raise ValueError("Active or unknown execution is not quiescent")
        return self


class ConsoleRecord(BaseModel):
    """Shell command console record model"""
    ps1: str = Field(..., description="Command prompt")
    command: str = Field(..., description="Executed command")
    output: str = Field(default="", description="Command output")


class ShellTask(BaseModel):
    """Shell task model"""
    id: str = Field(..., description="Task unique identifier")
    command: str = Field(..., description="Executed command")
    status: str = Field(..., description="Task status")
    created_at: str = Field(..., description="Task creation time")
    output: Optional[str] = Field(None, description="Task output")


class ShellExecResult(BaseModel):
    """Shell command execution result model"""
    session_id: str = Field(..., description="Shell session ID")
    command: str = Field(..., description="Executed command")
    status: str = Field(..., description="Command execution status")
    returncode: Optional[int] = Field(None, description="Process return code, only has value when status is completed")
    output: Optional[str] = Field(None, description="Command execution output, only has value when status is completed")
    execution_receipt: Optional[ShellExecutionReceipt] = None
    program_execution: Optional[dict[str, Any]] = None


class ShellViewResult(BaseModel):
    """Shell session content view result model"""
    output: str = Field(..., description="Shell session output content")
    session_id: str = Field(..., description="Shell session ID")
    console: Optional[List[ConsoleRecord]] = Field(None, description="Console command records")
    program_execution: Optional[dict[str, Any]] = None


class ShellWaitResult(BaseModel):
    """Process wait result model"""
    status: Literal["running", "completed"] = Field(
        ...,
        description="Process state after waiting",
    )
    returncode: Optional[int] = Field(
        None,
        description="Process return code when status is completed",
    )


class ShellWriteResult(BaseModel):
    """Process input write result model"""
    status: str = Field(..., description="Write status")


class ShellKillResult(BaseModel):
    """Process termination result model"""
    status: str = Field(..., description="Process status")
    returncode: int = Field(..., description="Process return code")
