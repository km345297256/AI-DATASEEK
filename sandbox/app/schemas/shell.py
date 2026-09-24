from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
import re
from typing import Optional

class ShellExecRequest(BaseModel):
    """Shell command execution request model"""
    id: Optional[str] = Field(None, description="Unique identifier of the target shell session, if not provided, one will be automatically created")
    exec_dir: Optional[str] = Field(None, description="Working directory for command execution (must use absolute path)")
    command: str = Field(..., description="Shell command to execute")
    operation_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)
    credentials: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, values):
        if len(values) > 8:
            raise ValueError("Invalid credential slots")
        for slot, value in values.items():
            secret = value.get_secret_value()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", slot) or not 8 <= len(secret.encode()) <= 8192 or "\x00" in secret:
                raise ValueError("Invalid credential slots")
        return values


class ShellOperationStatusRequest(BaseModel):
    """Read-only lookup of one original execution, not the current shell."""
    id: str = Field(min_length=1, strict=True)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)


class ProgramExecRequest(BaseModel):
    id: str = Field(min_length=1, strict=True)
    exec_dir: str = Field(min_length=1, strict=True)
    script_path: str = Field(min_length=1, strict=True)
    args: list[str] = Field(default_factory=list, max_length=256)
    operation_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)


class ProgramPreflightRequest(BaseModel):
    exec_dir: str = Field(min_length=1, strict=True)
    script_path: str = Field(min_length=1, strict=True)


class ShellViewRequest(BaseModel):
    """Shell session content view request model"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    console: Optional[bool] = Field(False, description="Whether to return console records")
    operation_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)
    output_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)
    cursor: Optional[int] = Field(None, ge=0, strict=True)
    max_bytes: int = Field(8192, ge=4, le=16384, strict=True)

    @model_validator(mode="after")
    def cursor_identity(self):
        if (self.output_id is None) != (self.cursor is None):
            raise ValueError("Output pagination requires output_id and cursor together")
        return self


class ShellWaitRequest(BaseModel):
    """Shell process wait request model"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    seconds: Optional[int] = Field(None, description="Wait time (seconds)")
    operation_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)


class ShellWriteToProcessRequest(BaseModel):
    """Request model for writing input to a running process"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    input: str = Field(..., description="Input content to write to the process")
    press_enter: bool = Field(..., description="Whether to press enter key after input")


class ShellKillProcessRequest(BaseModel):
    """Request model for terminating a running process"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    operation_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$", strict=True)
