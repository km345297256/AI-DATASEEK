from datetime import datetime, UTC
from typing import Literal
import uuid

from pydantic import BaseModel, Field


class SafetyReview(BaseModel):
    """Persistable output from the system safety gate."""

    decision: Literal["allow", "reject"]
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    categories: list[str] = Field(default_factory=list)
    reason: str = ""
    suggestion: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class SafetyRule(BaseModel):
    """Administrator-managed rule used by the pre-planner safety gate."""

    id: str = Field(default_factory=lambda: f"safety_rule_{uuid.uuid4().hex[:16]}")
    name: str
    description: str = ""
    category: str
    risk_level: Literal["medium", "high", "critical"] = "high"
    match_type: Literal["keyword", "all_keywords", "regex"] = "keyword"
    patterns: list[str] = Field(default_factory=list)
    enabled: bool = True
    reason: str = ""
    suggestion: str = ""
    priority: int = 100
    built_in: bool = False
    created_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SafetyRuleSeedState(BaseModel):
    id: str = "safety_rule_seed_state"
    version: int = 1
    initialized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
