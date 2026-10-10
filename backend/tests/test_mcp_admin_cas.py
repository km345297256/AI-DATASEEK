"""All MCP mutation owners use the same expected-state boundary, without I/O."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.application.errors.exceptions import AppException
from app.domain.models.approval import ApprovalRequest, ApprovalStatus
from app.domain.models.mcp_config import MCPConfig, MCPConfigurationConflict, MCPServerConfig, MCPTransport, MCPScope
from app.domain.models.user import User, UserRole
from app.domain.services import approval_service
from app.interfaces.api import admin_routes
from app.interfaces.schemas.admin import AdminMCPServerUpdateRequest


def server(owner="owner", *, enabled=False):
    return MCPServerConfig(transport=MCPTransport.STDIO, command="synthetic", owner_user_id=owner,
                           user_id=owner, enabled=enabled, scope=MCPScope.USER)


class Repository:
    def __init__(self, *, conflict=False):
        self.original = server()
        self.state = MCPConfig(mcpServers={"target": self.original.model_copy(deep=True), "unrelated": server("other")})
        self.conflict = conflict
        self.writes = []

    async def get_mcp_config(self):
        snapshot = self.state.model_copy(deep=True)
        # Concurrent unrelated changes after the route's read must survive.
        self.state.mcpServers["unrelated"].description = "concurrent change"
        return snapshot

    async def _check(self, name, expected):
        assert expected == self.original  # Route cannot mutate its CAS input.
        if self.conflict:
            self.state.mcpServers[name] = server("replacement-owner", enabled=True)
        if self.state.mcpServers.get(name) != expected:
            raise MCPConfigurationConflict("synthetic changed ownership")

    async def upsert_server(self, name, replacement, *, expected):
        await self._check(name, expected)
        self.writes.append("upsert")
        self.state.mcpServers[name] = replacement.model_copy(deep=True)
        return self.state

    async def delete_server(self, name, *, expected):
        await self._check(name, expected)
        self.writes.append("delete")
        self.state.mcpServers.pop(name)
        return self.state


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("conflict", [False, True])
async def test_admin_mutations_do_not_overwrite_changed_owner_or_unrelated_server(monkeypatch, operation, conflict):
    repository = Repository(conflict=conflict)
    monkeypatch.setattr(admin_routes, "MongoMCPRepository", lambda: repository)
    monkeypatch.setattr(admin_routes, "_user_names", AsyncMock(return_value={}))
    admin = User(id="admin", fullname="Admin", email="admin@example.invalid", role=UserRole.ADMIN)
    audit = SimpleNamespace(record=AsyncMock())
    if operation == "update":
        action = admin_routes.update_mcp_server("target", AdminMCPServerUpdateRequest(enabled=True),
            current_user=admin, audit_service=audit)
    else:
        action = admin_routes.delete_mcp_server("target", current_user=admin, audit_service=audit)
    if conflict:
        with pytest.raises(HTTPException) as raised:
            await action
        assert raised.value.status_code == 409
        assert repository.state.mcpServers["target"].owner_user_id == "replacement-owner"
        assert not repository.writes
        audit.record.assert_not_awaited()
    else:
        await action
        audit.record.assert_awaited_once()
        assert repository.writes == ["upsert" if operation == "update" else "delete"]
    assert repository.state.mcpServers["unrelated"].description == "concurrent change"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.REVOKED])
@pytest.mark.parametrize("conflict", [False, True])
async def test_approval_effect_uses_unmodified_expected_state(monkeypatch, status, conflict):
    repository = Repository(conflict=conflict)
    monkeypatch.setattr(approval_service, "MongoMCPRepository", lambda: repository)
    request = ApprovalRequest(requester_user_id="owner", resource_type="mcp_server", resource_id="target", status=status)
    if conflict:
        with pytest.raises(AppException) as raised:
            await approval_service.ApprovalService()._apply_decision_effects(request)
        assert raised.value.status_code == 409
        assert repository.state.mcpServers["target"].owner_user_id == "replacement-owner"
        assert not repository.writes
    else:
        await approval_service.ApprovalService()._apply_decision_effects(request)
        assert repository.writes == ["upsert"]
        assert repository.state.mcpServers["target"].enabled == (status == ApprovalStatus.APPROVED)
    assert repository.state.mcpServers["unrelated"].description == "concurrent change"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_approval_decision_is_saved_only_after_effect_succeeds(monkeypatch, conflict):
    repository = Repository(conflict=conflict)
    monkeypatch.setattr(approval_service, "MongoMCPRepository", lambda: repository)
    request = ApprovalRequest(requester_user_id="owner", resource_type="mcp_server", resource_id="target")
    durable = request.model_copy(deep=True)

    class Document:
        def __init__(self):
            self.__dict__.update(request.model_dump())
            self.save = AsyncMock(side_effect=self._save)

        def to_domain(self):
            return ApprovalRequest.model_validate({key: getattr(self, key) for key in ApprovalRequest.model_fields})

        async def _save(self):
            nonlocal durable
            assert repository.writes == ["upsert"]
            durable = self.to_domain()

    doc = Document()
    monkeypatch.setattr(approval_service, "ApprovalRequestDocument", SimpleNamespace(
        approval_id="synthetic-field", find_one=AsyncMock(return_value=doc)))
    operation = approval_service.ApprovalService().decide(approval_id=request.id,
        reviewer_user_id="admin", status=ApprovalStatus.APPROVED, decision_note="synthetic")
    if conflict:
        with pytest.raises(AppException) as raised:
            await operation
        assert raised.value.status_code == 409
        doc.save.assert_not_awaited()
        assert doc.status == durable.status == ApprovalStatus.PENDING
        assert doc.reviewer_user_id is None
    else:
        response = await operation
        doc.save.assert_awaited_once()
        assert response.status == durable.status == ApprovalStatus.APPROVED
        assert durable.reviewer_user_id == "admin"
