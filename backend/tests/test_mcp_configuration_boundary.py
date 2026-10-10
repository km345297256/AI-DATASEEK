"""MCP metadata, edit authorization and compare-and-set without external I/O."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.domain.models.mcp_config import (
    MCPConfig, MCPConfigurationConflict, MCPRiskLevel, MCPScope,
    MCPServerConfig, MCPTransport,
)
from app.domain.models.user import User, UserRole
from app.interfaces.api import admin_routes, mcp_routes
from app.interfaces.schemas.mcp import MCPServerRequest
from app.infrastructure.repositories import mongo_mcp_repository as mongo


def actor(user_id="viewer", role=UserRole.USER):
    return User(id=user_id, fullname="Synthetic User", email="test@example.invalid", role=role)


def connection(scope=MCPScope.GLOBAL, owner="owner"):
    return MCPServerConfig(
        transport=MCPTransport.STREAMABLE_HTTP,
        scope=scope, owner_user_id=owner,
        url="https://fake-user:synthetic-secret@example.invalid/private-token?custom=opaque-secret",
        command="/private/fake-path/command", args=["--credential", "argument-secret"],
        headers={"Authorization": "header-secret"}, env={"KEY": "env-secret"},
        description="Public description",
    )


class Repository:
    def __init__(self, server):
        self.config = MCPConfig(mcpServers={"shared": server} if server else {})
        self.upsert_server = AsyncMock(side_effect=self.upsert)
        self.delete_server = AsyncMock(side_effect=self.delete)

    async def get_mcp_config(self):
        return self.config

    async def upsert(self, name, config, *, expected):
        assert self.config.mcpServers.get(name) == expected
        self.config.mcpServers[name] = config
        return self.config

    async def delete(self, name, *, expected):
        assert self.config.mcpServers.get(name) == expected
        self.config.mcpServers.pop(name, None)
        return self.config


@pytest.fixture
def services():
    return SimpleNamespace(record=AsyncMock()), SimpleNamespace(create_request=AsyncMock())


@pytest.mark.parametrize("viewer", [actor(), actor("admin", UserRole.ADMIN), None])
def test_public_projection_excludes_all_private_connection_fields(viewer):
    original = connection()
    before = original.model_dump()
    response = mcp_routes._server_response("shared", original, viewer)
    for field in ("url", "command", "args", "headers", "env"):
        assert getattr(response, field) is None
    assert response.description == "Public description"
    assert original.model_dump() == before  # projection never mutates execution config
    assert "secret" not in response.model_dump_json()
    assert "/private/" not in response.model_dump_json()


@pytest.mark.parametrize("legacy", [False, True])
def test_owner_keeps_exact_private_configuration_for_editing(legacy):
    original = connection()
    if legacy:
        original.user_id, original.owner_user_id = "owner", None
    response = mcp_routes._server_response("shared", original, actor("owner"))
    for field in ("url", "command", "args", "headers", "env"):
        assert getattr(response, field) == getattr(original, field)


def test_admin_metadata_does_not_export_connection_secrets():
    response = admin_routes._mcp_response("shared", connection(), {})
    assert response.command is None and response.args is None and response.url is None
    assert "secret" not in response.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [MCPScope.GLOBAL, MCPScope.USER, MCPScope.SHARED])
@pytest.mark.parametrize("operation", ["delete", "replace"])
async def test_nonowner_cannot_mutate_visible_or_private_server(monkeypatch, services, scope, operation):
    repository = Repository(connection(scope))
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    audit, approval = services
    with pytest.raises(HTTPException) as failure:
        if operation == "delete":
            await mcp_routes.delete_mcp_server("shared", current_user=actor(), audit_service=audit)
        else:
            await mcp_routes.upsert_mcp_server(
                "shared", MCPServerRequest(name="shared", transport=MCPTransport.STDIO, command="fake", is_global=False),
                current_user=actor(), audit_service=audit, approval_service=approval,
            )
    assert failure.value.status_code == 403
    repository.upsert_server.assert_not_awaited()
    repository.delete_server.assert_not_awaited()
    audit.record.assert_not_awaited()
    approval.create_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", [actor("owner"), actor("admin", UserRole.ADMIN)])
async def test_authorized_update_preserves_owner_scope_and_risk(monkeypatch, services, editor):
    original = connection(MCPScope.USER)
    original.risk_level = MCPRiskLevel.SENSITIVE
    original.user_id = "owner"
    repository = Repository(original)
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    audit, approval = services
    response = await mcp_routes.upsert_mcp_server(
        "shared", MCPServerRequest(name="shared", transport=MCPTransport.STDIO, command="fake"),
        current_user=editor, audit_service=audit, approval_service=approval,
    )
    saved = repository.config.mcpServers["shared"]
    assert saved.owner_user_id == "owner" and saved.user_id == "owner"
    assert saved.scope == MCPScope.USER and saved.risk_level == MCPRiskLevel.SENSITIVE
    assert saved.enabled == (editor.role == UserRole.ADMIN)
    assert response.data.server.command == ("fake" if editor.id == "owner" else None)
    assert repository.upsert_server.await_args.kwargs["expected"] == original


@pytest.mark.asyncio
async def test_admin_edit_does_not_demote_global_when_scope_omitted(monkeypatch, services):
    repository = Repository(connection())
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    await mcp_routes.upsert_mcp_server(
        "shared", MCPServerRequest(name="shared", transport=MCPTransport.STDIO, command="fake"),
        current_user=actor("admin", UserRole.ADMIN), audit_service=services[0], approval_service=services[1],
    )
    assert repository.config.mcpServers["shared"].scope == MCPScope.GLOBAL
    assert repository.config.mcpServers["shared"].owner_user_id == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", [actor("owner"), actor("admin", UserRole.ADMIN)])
async def test_authorized_delete_preserves_existing_workflow(monkeypatch, services, editor):
    repository = Repository(connection())
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    await mcp_routes.delete_mcp_server("shared", current_user=editor, audit_service=services[0])
    assert not repository.config.mcpServers
    services[0].record.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_creation_still_works_but_global_creation_requires_admin(monkeypatch, services):
    repository = Repository(None)
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    request = MCPServerRequest(name="shared", transport=MCPTransport.STDIO, command="fake", is_global=True)
    with pytest.raises(HTTPException) as failure:
        await mcp_routes.upsert_mcp_server("shared", request, current_user=actor(), audit_service=services[0], approval_service=services[1])
    assert failure.value.status_code == 403
    repository.upsert_server.assert_not_awaited()
    request.is_global = False
    await mcp_routes.upsert_mcp_server("shared", request, current_user=actor(), audit_service=services[0], approval_service=services[1])
    assert repository.config.mcpServers["shared"].owner_user_id == "viewer"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "replace"])
async def test_changed_authorization_snapshot_returns_conflict_without_audit(monkeypatch, services, operation):
    repository = Repository(connection(owner="viewer"))
    repository.upsert_server.side_effect = MCPConfigurationConflict("changed")
    repository.delete_server.side_effect = MCPConfigurationConflict("changed")
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    with pytest.raises(HTTPException) as failure:
        if operation == "delete":
            await mcp_routes.delete_mcp_server("shared", current_user=actor(), audit_service=services[0])
        else:
            await mcp_routes.upsert_mcp_server(
                "shared", MCPServerRequest(name="shared", transport=MCPTransport.STDIO, command="fake"),
                current_user=actor(), audit_service=services[0], approval_service=services[1],
            )
    assert failure.value.status_code == 409
    services[0].record.assert_not_awaited()
    services[1].create_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_install_and_uninstall_return_metadata_not_secrets(monkeypatch):
    repository = Repository(connection())
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)
    user = actor()
    users = SimpleNamespace(get_user_by_id=AsyncMock(return_value=user), update_user=AsyncMock(side_effect=lambda item: item))
    for operation in (mcp_routes.install_mcp_server, mcp_routes.uninstall_mcp_server):
        response = await operation("shared", current_user=user, user_repository=users)
        assert response.data.url is None and response.data.args is None
    assert repository.config.mcpServers["shared"].headers["Authorization"] == "header-secret"


def fake_mongo(monkeypatch, servers, *, conflict=False, insert_conflict=False):
    state = SimpleNamespace(servers=deepcopy(servers), updates=[], inserts=0)

    class Query:
        def __init__(self, query):
            self.query = query

        def __await__(self):
            async def result():
                return SimpleNamespace(servers=deepcopy(state.servers)) if state.servers is not None else None
            return result().__await__()

        async def update(self, update):
            state.updates.append((self.query, update))
            assert self.query["$expr"]["$eq"][1]["$literal"] == state.servers
            if conflict:
                return SimpleNamespace(matched_count=0)
            state.servers = deepcopy(update["$set"]["servers"])
            return SimpleNamespace(matched_count=1)

    class Document:
        find_one = staticmethod(Query)

        def __init__(self, **kwargs):
            self.servers = kwargs["servers"]

        async def insert(self):
            state.inserts += 1
            if insert_conflict:
                raise DuplicateKeyError("synthetic concurrent creation")
            state.servers = deepcopy(self.servers)

    monkeypatch.setattr(mongo, "MCPConfigDocument", Document)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "replace"])
async def test_repository_rejects_owner_change_between_check_and_write(monkeypatch, operation):
    old = connection(owner="viewer")
    state = fake_mongo(monkeypatch, {"shared": connection(owner="other").model_dump(mode="json")})
    repository = mongo.MongoMCPRepository()
    with pytest.raises(MCPConfigurationConflict):
        if operation == "delete":
            await repository.delete_server("shared", expected=old)
        else:
            await repository.upsert_server("shared", old, expected=old)
    assert state.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["shared", "has.dot", "$ne"])
async def test_repository_cas_preserves_legacy_fields_and_treats_names_as_data(monkeypatch, name):
    legacy = {"transport": "stdio", "command": "fake", "user_id": "viewer", "scope": "user"}
    unrelated = {"transport": "stdio", "command": "other", "extension": {"opaque": True}}
    state = fake_mongo(monkeypatch, {name: legacy, "unrelated": unrelated})
    replacement = connection(owner="viewer")
    await mongo.MongoMCPRepository().upsert_server(name, replacement, expected=MCPServerConfig.model_validate(legacy))
    assert state.servers[name] == replacement.model_dump(mode="json")
    assert state.servers["unrelated"] == unrelated
    assert state.updates[0][0]["$expr"]["$eq"][1]["$literal"][name] == legacy


@pytest.mark.asyncio
async def test_repository_conflict_does_not_overwrite_concurrent_state(monkeypatch):
    old = connection()
    state = fake_mongo(monkeypatch, {"shared": old.model_dump(mode="json")}, conflict=True)
    with pytest.raises(MCPConfigurationConflict):
        await mongo.MongoMCPRepository().delete_server("shared", expected=old)
    assert "shared" in state.servers


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_repository_initial_creation_handles_unique_index_race(monkeypatch, conflict):
    state = fake_mongo(monkeypatch, None, insert_conflict=conflict)
    repository = mongo.MongoMCPRepository()
    if conflict:
        with pytest.raises(MCPConfigurationConflict):
            await repository.upsert_server("shared", connection(), expected=None)
    else:
        await repository.upsert_server("shared", connection(), expected=None)
        assert "shared" in state.servers
    assert state.inserts == 1
