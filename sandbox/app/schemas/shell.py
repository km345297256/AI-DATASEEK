from pydantic import BaseModel, Field, SecretStr, field_validator
import re
from typing import Optional

class ShellExecRequest(BaseModel):
    """Shell command execution request model"""
    id: Optional[str] = Field(None, description="Unique identifier of the target shell session, if not provided, one will be automatically created")
    exec_dir: Optional[str] = Field(None, description="Working directory for command execution (must use absolute path)")
    command: str = Field(..., description="Shell command to execute")
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


class ShellViewRequest(BaseModel):
    """Shell session content view request model"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    console: Optional[bool] = Field(False, description="Whether to return console records")


class ShellWaitRequest(BaseModel):
    """Shell process wait request model"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    seconds: Optional[int] = Field(None, description="Wait time (seconds)")


class ShellWriteToProcessRequest(BaseModel):
    """Request model for writing input to a running process"""
    id: str = Field(..., description="Unique identifier of the target shell session")
    input: str = Field(..., description="Input content to write to the process")
    press_enter: bool = Field(..., description="Whether to press enter key after input")


class ShellKillProcessRequest(BaseModel):
    """Request model for terminating a running process"""
    id: str = Field(..., description="Unique identifier of the target shell session")
