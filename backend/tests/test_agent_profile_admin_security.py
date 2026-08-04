import pytest

from app.application.errors.exceptions import UnauthorizedError
from app.application.services.agent_profile_service import AgentProfileService
from app.domain.models.agent_profile import AgentProfile
from app.domain.models.user import User, UserRole
from app.interfaces.api.agent_profile_routes import _ensure_admin, _response
from app.interfaces.schemas.agent_profile import UpdateAgentProfileRequest


def test_agent_profile_management_requires_admin():
    user = User(id="user-1", fullname="User", email="user@example.com", role=UserRole.USER)

    with pytest.raises(UnauthorizedError):
        _ensure_admin(user)

    admin = User(id="admin-1", fullname="Admin", email="admin@example.com", role=UserRole.ADMIN)
    _ensure_admin(admin)


def test_agent_profile_response_does_not_expose_api_key():
    profile = AgentProfile(id="profile-1", name="Secure Agent", api_key="secret-key")

    response = _response(profile)

    assert response.api_key is None


@pytest.mark.asyncio
async def test_admin_can_change_private_agent_profile_to_global():
    class Repository:
        def __init__(self):
            self.profile = AgentProfile(
                id="profile-1",
                name="Private Agent",
                user_id="admin-1",
                owner_user_id="admin-1",
                workspace_id="personal-admin-1",
                scope="user",
            )

        async def get_by_id(self, profile_id):
            return self.profile if profile_id == self.profile.id else None

        async def update(self, profile):
            self.profile = profile
            return profile

    repository = Repository()
    service = AgentProfileService(repository)
    request = UpdateAgentProfileRequest.model_validate({"is_global": True})

    updated = await service.update_profile(
        user_id="admin-1",
        user_role="admin",
        workspace_id="personal-admin-1",
        profile_id="profile-1",
        **request.model_dump(exclude_none=True),
    )

    assert updated.scope == "global"
    assert updated.user_id is None
    assert updated.workspace_id is None
    assert updated.owner_user_id == "admin-1"

    private_request = UpdateAgentProfileRequest.model_validate({"is_global": False})
    private = await service.update_profile(
        user_id="admin-1",
        user_role="admin",
        workspace_id="personal-admin-1",
        profile_id="profile-1",
        **private_request.model_dump(exclude_none=True),
    )

    assert private.scope == "user"
    assert private.user_id == "admin-1"
    assert private.workspace_id == "personal-admin-1"
    assert private.owner_user_id == "admin-1"
