import inspect

import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.domain.models.user import UserRole
from app.interfaces.api.routes import create_api_router
from app.interfaces.dependencies import (
    get_current_user,
    get_optional_current_user,
    verify_signature,
    verify_signature_websocket,
)


@pytest.mark.asyncio
async def test_all_callers_use_the_fixed_system_administrator() -> None:
    current_user = await get_current_user()
    optional_user = await get_optional_current_user()

    for user in (current_user, optional_user):
        assert user.id == "anonymous"
        assert user.fullname == "AI-DataSeek System"
        assert user.email == "system@localhost"
        assert user.role == UserRole.ADMIN
        assert user.is_active is True
        assert user.token_balance is None

    assert not inspect.signature(get_current_user).parameters
    assert not inspect.signature(get_optional_current_user).parameters


@pytest.mark.asyncio
async def test_signed_url_dependencies_do_not_require_a_signature() -> None:
    assert await verify_signature() == ""
    assert await verify_signature_websocket() == ""
    assert not inspect.signature(verify_signature).parameters
    assert not inspect.signature(verify_signature_websocket).parameters


def test_authentication_routes_and_security_schemes_are_not_exposed() -> None:
    app = FastAPI()
    app.include_router(create_api_router(), prefix="/api/v1")
    schema = app.openapi()

    paths = schema["paths"]
    assert not any(path.startswith("/api/v1/auth") for path in paths)
    assert not any(path.startswith("/api/v1/api-keys") for path in paths)
    assert not any(path.startswith("/api/v1/admin/users") for path in paths)
    assert not any(path.startswith("/api/v1/admin/token-quotas") for path in paths)
    assert not any("/collaborators" in path for path in paths)
    assert "/api/v1/admin/resource-usage" in paths
    assert "/api/v1/datasets" in paths
    assert "/api/v1/files" in paths
    assert "/api/v1/skills" in paths

    assert "securitySchemes" not in schema.get("components", {})
    file_download = paths["/api/v1/files/{file_id}"]["get"]
    assert not any(
        parameter.get("name") == "signature"
        for parameter in file_download.get("parameters", [])
    )


def test_authentication_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_PROVIDER", raising=False)
    assert Settings(_env_file=None).auth_provider == "none"
