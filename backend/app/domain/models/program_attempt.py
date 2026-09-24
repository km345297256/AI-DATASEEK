"""Minimal display proof for a confirmed first-party program attempt."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProgramAttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["succeeded", "failed"]
    returncode: int

    @model_validator(mode="after")
    def consistent_status(self):
        if (self.returncode == 0) != (self.state == "succeeded"):
            raise ValueError("Program attempt status must agree with its confirmed exit code")
        return self
