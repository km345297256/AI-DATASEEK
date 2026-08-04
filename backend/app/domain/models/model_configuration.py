from datetime import UTC, datetime
from typing import Any, Dict, Optional
import secrets
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ModelType(str, Enum):
    CHAT = "chat"
    VISION = "vision"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


class ModelConfiguration(BaseModel):
    id: str
    name: str
    description: str = ""
    model_provider: str
    model_name: str
    model_type: ModelType = ModelType.CHAT
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2000
    extra_config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def generate_id() -> str:
        return secrets.token_urlsafe(16)

    @field_validator("name", "model_provider", "model_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized

    def runtime_settings(self) -> Dict[str, Any]:
        settings: Dict[str, Any] = {
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_base:
            settings["api_base"] = self.api_base
        if self.api_key:
            settings["api_key"] = self.api_key
        settings.update({
            key: value
            for key, value in self.extra_config.items()
            if value is not None and key not in settings
        })
        return settings
