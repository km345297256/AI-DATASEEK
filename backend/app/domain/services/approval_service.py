from datetime import datetime, UTC
from typing import Any, Dict, List, Optional, Tuple

from app.application.errors.exceptions import AppException, NotFoundError
from app.domain.models.approval import ApprovalRequest, ApprovalStatus
from app.domain.models.mcp_config import MCPConfigurationConflict
from app.infrastructure.models.documents import ApprovalRequestDocument
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository


class ApprovalService:
    async def create_request(
        self,
        *,
        requester_user_id: str,
        resource_type: str,
        requested_permissions: List[str],
        workspace_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            requested_permissions=requested_permissions,
            reason=reason,
            metadata=metadata or {},
        )
        doc = ApprovalRequestDocument.from_domain(approval)
        await doc.insert()
        return doc.to_domain()

    async def list_requests(
        self,
        *,
        status: Optional[ApprovalStatus] = None,
        requester_user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ApprovalRequest], int]:
        docs = await ApprovalRequestDocument.find().sort("-created_at").to_list()
        requests = [doc.to_domain() for doc in docs]
        if status:
            requests = [request for request in requests if request.status == status]
        if requester_user_id:
            requests = [request for request in requests if request.requester_user_id == requester_user_id]
        total = len(requests)
        return requests[offset : offset + min(limit, 200)], total

    async def decide(
        self,
        *,
        approval_id: str,
        reviewer_user_id: str,
        status: ApprovalStatus,
        decision_note: Optional[str] = None,
    ) -> ApprovalRequest:
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.REVOKED}:
            raise ValueError("Invalid approval decision")
        doc = await ApprovalRequestDocument.find_one(ApprovalRequestDocument.approval_id == approval_id)
        if not doc:
            raise NotFoundError("Approval request not found")
        approval = doc.to_domain().model_copy(deep=True)
        approval.status = status
        approval.reviewer_user_id = reviewer_user_id
        approval.reviewed_at = datetime.now(UTC)
        approval.decision_note = decision_note
        approval.updated_at = datetime.now(UTC)
        # A rejected configuration CAS must not persist a successful decision.
        # This remains two stores, not a transaction or an execution retry.
        await self._apply_decision_effects(approval)
        for field in ("status", "reviewer_user_id", "reviewed_at", "decision_note", "updated_at"):
            setattr(doc, field, getattr(approval, field))
        await doc.save()
        return approval

    async def _apply_decision_effects(self, approval: ApprovalRequest) -> None:
        if approval.resource_type != "mcp_server" or not approval.resource_id:
            return

        repository = MongoMCPRepository()
        config = await repository.get_mcp_config()
        server = config.mcpServers.get(approval.resource_id)
        if not server:
            return
        expected = server.model_copy(deep=True)
        server = server.model_copy(deep=True)

        if approval.status == ApprovalStatus.APPROVED:
            server.enabled = True
        elif approval.status in {ApprovalStatus.REJECTED, ApprovalStatus.REVOKED}:
            server.enabled = False
        else:
            return

        try:
            await repository.upsert_server(approval.resource_id, server, expected=expected)
        except MCPConfigurationConflict:
            raise AppException(code=409, msg="MCP configuration changed; reload before deciding", status_code=409) from None


def get_approval_service() -> ApprovalService:
    return ApprovalService()
