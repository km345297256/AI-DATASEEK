import pytest
from fastapi import HTTPException

from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport, MCPScope
from app.domain.models.renderer import Renderer, RendererKind, RendererScope
from app.domain.models.user import User, UserRole
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.interfaces.api import mcp_routes, renderer_routes, session_routes


def make_user(**overrides) -> User:
    return User(
        id="user-1",
        fullname="Test User",
        email="test@example.com",
        role=overrides.get("role", UserRole.USER),
        installed_mcp_names=overrides.get("installed_mcp_names", []),
        installed_renderer_ids=overrides.get("installed_renderer_ids", []),
    )


class FakeUserRepository:
    def __init__(self, user: User):
        self.user = user

    async def get_user_by_id(self, user_id: str):
        return self.user

    async def create_user(self, user: User):
        self.user = user
        return user

    async def update_user(self, user: User):
        self.user = user
        return user


class FakeMCPRepository:
    def __init__(self):
        self.config = MCPConfig(mcpServers={
            "official": MCPServerConfig(
                transport=MCPTransport.STDIO,
                command="official-command",
            ),
            "personal": MCPServerConfig(
                transport=MCPTransport.STDIO,
                command="personal-command",
                scope=MCPScope.USER,
                user_id="user-1",
                owner_user_id="user-1",
            ),
            "foreign": MCPServerConfig(
                transport=MCPTransport.SSE,
                url="https://example.com/sse",
                scope=MCPScope.USER,
                user_id="user-2",
                owner_user_id="user-2",
                headers={"Authorization": "Bearer secret"},
                env={"API_KEY": "secret"},
            ),
        })

    async def get_mcp_config(self):
        return self.config


class FakeRendererRepository:
    def __init__(self):
        self.renderers = [
            Renderer(
                id="official-renderer",
                name="Official Renderer",
                kind=RendererKind.API,
                scope=RendererScope.GLOBAL,
                extensions=["demo"],
                api_url="https://example.com/render",
            ),
            Renderer(
                id="personal-renderer",
                name="Personal Renderer",
                kind=RendererKind.API,
                scope=RendererScope.USER,
                extensions=["mine"],
                api_url="https://example.com/personal",
                user_id="user-1",
                owner_user_id="user-1",
            ),
        ]

    async def list_accessible(self, user_id: str):
        return self.renderers

    async def get_accessible_by_id(self, renderer_id: str, user_id: str):
        return next((renderer for renderer in self.renderers if renderer.id == renderer_id), None)


@pytest.mark.asyncio
async def test_mcp_catalog_requires_install_for_official_servers(monkeypatch):
    user = make_user()
    users = FakeUserRepository(user)
    repository = FakeMCPRepository()
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)

    catalog = await mcp_routes.list_mcp_catalog(current_user=user, user_repository=users)
    personal = await mcp_routes.list_mcp_servers(current_user=user, user_repository=users)
    assert [(server.name, server.installed) for server in catalog.data.servers] == [
        ("official", False),
        ("personal", True),
    ]
    assert [server.name for server in personal.data.servers] == ["personal"]

    with pytest.raises(HTTPException) as exc_info:
        await mcp_routes.install_mcp_server("foreign", current_user=user, user_repository=users)
    assert exc_info.value.status_code == 404

    await mcp_routes.install_mcp_server("official", current_user=user, user_repository=users)
    assert users.user.installed_mcp_names == ["official"]
    await mcp_routes.uninstall_mcp_server("official", current_user=user, user_repository=users)
    assert users.user.installed_mcp_names == []


class FakeMCPToolkit:
    def __init__(self):
        self.config = None

    async def cleanup(self):
        return None

    async def initialized(self, config, available_config=None):
        self.config = config


@pytest.mark.asyncio
async def test_admin_can_install_and_run_foreign_mcp_without_classifying_it_as_personal(monkeypatch):
    admin = make_user(role=UserRole.ADMIN)
    users = FakeUserRepository(admin)
    repository = FakeMCPRepository()
    monkeypatch.setattr(mcp_routes, "_repository", lambda: repository)

    catalog = await mcp_routes.list_mcp_catalog(current_user=admin, user_repository=users)
    foreign = next(server for server in catalog.data.servers if server.name == "foreign")
    assert foreign.source == "community"
    assert foreign.installed is False
    assert foreign.headers is None
    assert foreign.env is None

    installed = await mcp_routes.install_mcp_server(
        "foreign",
        current_user=admin,
        user_repository=users,
    )
    assert installed.data.installed is True
    monkeypatch.setattr(session_routes, "MongoMCPRepository", lambda: repository)
    assert await session_routes._installed_mcp_names(admin, ["foreign"]) == ["foreign"]

    runner = object.__new__(AgentTaskRunner)
    runner._user_id = admin.id
    runner._mcp_repository = repository
    runner._mcp_tool = FakeMCPToolkit()
    await runner._initialize_mcp_tool(["foreign"], is_admin=True)
    assert list(runner._mcp_tool.config.mcpServers) == ["foreign"]


@pytest.mark.asyncio
async def test_foreign_install_record_does_not_grant_standard_user_mcp_access(monkeypatch):
    user = make_user(installed_mcp_names=["foreign"])
    repository = FakeMCPRepository()
    monkeypatch.setattr(session_routes, "MongoMCPRepository", lambda: repository)

    selected = await session_routes._installed_mcp_names(user, ["foreign"])
    assert selected == []

    runner = object.__new__(AgentTaskRunner)
    runner._user_id = user.id
    runner._mcp_repository = repository
    runner._mcp_tool = FakeMCPToolkit()
    await runner._initialize_mcp_tool(["foreign"], is_admin=False)
    assert runner._mcp_tool.config.mcpServers == {}


@pytest.mark.asyncio
async def test_renderer_catalog_requires_install_for_official_renderers(monkeypatch):
    user = make_user()
    users = FakeUserRepository(user)
    repository = FakeRendererRepository()
    monkeypatch.setattr(renderer_routes, "_repository", lambda: repository)

    catalog = await renderer_routes.list_renderer_catalog(current_user=user, user_repository=users)
    personal = await renderer_routes.list_renderers(current_user=user, user_repository=users)
    assert [(renderer.id, renderer.installed) for renderer in catalog.data.renderers] == [
        ("official-renderer", False),
        ("personal-renderer", True),
    ]
    assert [renderer.id for renderer in personal.data.renderers] == ["personal-renderer"]

    await renderer_routes.install_renderer("official-renderer", current_user=user, user_repository=users)
    assert users.user.installed_renderer_ids == ["official-renderer"]
    await renderer_routes.uninstall_renderer("official-renderer", current_user=user, user_repository=users)
    assert users.user.installed_renderer_ids == []
