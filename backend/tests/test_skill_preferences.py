import pytest
from fastapi import HTTPException

from app.domain.models.skill import Skill
from app.domain.models.user import User
from app.interfaces.api import skill_routes
from app.interfaces.api import session_routes
from app.interfaces.api.session_routes import merge_skill_names
from app.interfaces.schemas.skill import UpdateSkillPreferencesRequest


def make_user(**overrides) -> User:
    return User(
        id=overrides.get("id", "user-1"),
        fullname="Test User",
        email="test@example.com",
        auto_enabled_skills=overrides.get("auto_enabled_skills", []),
        installed_skill_ids=overrides.get("installed_skill_ids", []),
    )


class FakeUserRepository:
    def __init__(self, user: User | None):
        self.user = user

    async def get_user_by_id(self, user_id: str):
        return self.user if self.user and self.user.id == user_id else None

    async def create_user(self, user: User):
        self.user = user
        return user

    async def update_user(self, user: User):
        self.user = user
        return user


class FakeRegistry:
    def __init__(self, names: list[str]):
        self.skills = [
            Skill(id=f"skill-{name}", name=name, description="", content="instructions", path=f"/{name}/SKILL.md")
            for name in names
        ]

    async def load(self):
        return None

    async def sync_files_to_repository(self):
        return None

    def list_skills(self):
        return self.skills

    def get_skill_by_id(self, skill_id: str):
        return next((skill for skill in self.skills if skill.id == skill_id), None)


def test_user_defaults_to_manual_skill_selection():
    assert make_user().auto_enabled_skills == []


def test_merge_skill_names_combines_defaults_and_manual_selection():
    assert merge_skill_names(
        ["pdf-maker", "data-analysis"],
        ["PDF-MAKER", "game-dev"],
    ) == ["pdf-maker", "data-analysis", "game-dev"]


@pytest.mark.asyncio
async def test_skill_preferences_persist_only_accessible_skills(monkeypatch):
    user = make_user(installed_skill_ids=["skill-pdf-maker", "skill-game-dev"])
    repository = FakeUserRepository(user)
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: FakeRegistry(["pdf-maker", "game-dev"]))

    response = await skill_routes.update_skill_preferences(
        UpdateSkillPreferencesRequest(auto_enabled_skills=["PDF-MAKER", "game-dev", "game-dev"]),
        current_user=user,
        user_repository=repository,
    )

    assert repository.user.auto_enabled_skills == ["pdf-maker", "game-dev"]
    assert response.data.auto_enabled_skills == ["pdf-maker", "game-dev"]


@pytest.mark.asyncio
async def test_skill_preferences_reject_inaccessible_skill(monkeypatch):
    user = make_user()
    repository = FakeUserRepository(user)
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: FakeRegistry(["pdf-maker"]))

    with pytest.raises(HTTPException) as exc_info:
        await skill_routes.update_skill_preferences(
            UpdateSkillPreferencesRequest(auto_enabled_skills=["private-skill"]),
            current_user=user,
            user_repository=repository,
        )

    assert exc_info.value.status_code == 400
    assert repository.user.auto_enabled_skills == []


@pytest.mark.asyncio
async def test_stale_auto_enabled_name_does_not_install_skill(monkeypatch):
    user = make_user(auto_enabled_skills=["pdf-maker"], installed_skill_ids=[])
    monkeypatch.setattr(session_routes, "SkillRegistry", lambda *args, **kwargs: FakeRegistry(["pdf-maker"]))

    effective = await session_routes._installed_skill_names(user, ["pdf-maker"])

    assert effective == []


@pytest.mark.asyncio
async def test_installed_auto_enabled_skill_is_authorized(monkeypatch):
    user = make_user(
        auto_enabled_skills=["pdf-maker"],
        installed_skill_ids=["skill-pdf-maker"],
    )
    monkeypatch.setattr(session_routes, "SkillRegistry", lambda *args, **kwargs: FakeRegistry(["pdf-maker"]))

    effective = await session_routes._installed_skill_names(user, ["pdf-maker"])

    assert effective == ["pdf-maker"]


@pytest.mark.asyncio
async def test_get_preferences_removes_stale_uninstalled_skills(monkeypatch):
    user = make_user(auto_enabled_skills=["pdf-maker"], installed_skill_ids=[])
    repository = FakeUserRepository(user)
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: FakeRegistry(["pdf-maker"]))

    response = await skill_routes.get_skill_preferences(
        current_user=user,
        user_repository=repository,
    )

    assert response.data.auto_enabled_skills == []
    assert repository.user.auto_enabled_skills == []


@pytest.mark.asyncio
async def test_skill_catalog_and_personal_list_are_separate(monkeypatch):
    user = make_user(installed_skill_ids=["skill-pdf-maker"])
    repository = FakeUserRepository(user)
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: FakeRegistry(["pdf-maker", "game-dev"]))

    catalog = await skill_routes.list_skill_catalog(current_user=user, user_repository=repository)
    personal = await skill_routes.list_skills(current_user=user, user_repository=repository)

    assert [(skill.name, skill.installed) for skill in catalog.data.skills] == [
        ("pdf-maker", True),
        ("game-dev", False),
    ]
    assert [skill.name for skill in personal.data.skills] == ["pdf-maker"]


@pytest.mark.asyncio
async def test_install_and_uninstall_skill_updates_personal_list(monkeypatch):
    user = make_user()
    repository = FakeUserRepository(user)
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: FakeRegistry(["game-dev"]))

    installed = await skill_routes.install_skill(
        "skill-game-dev", current_user=user, user_repository=repository,
    )
    assert installed.data.installed is True
    assert repository.user.installed_skill_ids == ["skill-game-dev"]

    removed = await skill_routes.uninstall_skill(
        "skill-game-dev", current_user=user, user_repository=repository,
    )
    assert removed.data.installed is False
    assert repository.user.installed_skill_ids == []
