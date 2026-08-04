from typing import Any, Dict, List, Optional, Tuple

from app.domain.models.audit import AuditLog, AuditRiskLevel, AuditStatus
from app.infrastructure.models.documents import AuditLogDocument


class AuditService:
    async def record(
        self,
        *,
        actor_user_id: str,
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        status: AuditStatus = AuditStatus.SUCCESS,
        risk_level: AuditRiskLevel = AuditRiskLevel.LOW,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        log = AuditLog(
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            session_id=session_id,
            task_id=task_id,
            ip=ip,
            user_agent=user_agent,
            status=status,
            risk_level=risk_level,
            metadata=self._sanitize_metadata(metadata or {}),
        )
        doc = AuditLogDocument.from_domain(log)
        await doc.insert()
        return doc.to_domain()

    async def list_logs(
        self,
        *,
        actor_user_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[AuditLog], int]:
        docs = await AuditLogDocument.find().sort("-created_at").to_list()
        logs = [doc.to_domain() for doc in docs]
        if actor_user_id:
            logs = [log for log in logs if log.actor_user_id == actor_user_id]
        if action:
            logs = [log for log in logs if log.action == action]
        if resource_type:
            logs = [log for log in logs if log.resource_type == resource_type]
        total = len(logs)
        return logs[offset : offset + min(limit, 200)], total

    def _sanitize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        sensitive_keys = {"api_key", "authorization", "headers", "env", "password", "token", "secret"}
        sanitized: Dict[str, Any] = {}
        for key, value in metadata.items():
            if key.lower() in sensitive_keys:
                sanitized[key] = "***"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_metadata(value)
            else:
                sanitized[key] = value
        return sanitized


def get_audit_service() -> AuditService:
    return AuditService()
