from functools import lru_cache

from app.domain.services.tool_approval_service import ToolApprovalService
from app.infrastructure.repositories.mongo_tool_approval_repository import (
    MongoToolApprovalRepository,
)


@lru_cache()
def get_tool_approval_service() -> ToolApprovalService:
    return ToolApprovalService(MongoToolApprovalRepository())


__all__ = ["get_tool_approval_service"]
