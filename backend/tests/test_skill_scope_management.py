import pytest

from app.domain.models.skill import SkillScope
from app.domain.services.skills import SkillRegistry


class InMemorySkillRepository:
    def __init__(self):
        self.skills = {}

    async def save(self, skill):
        saved = skill.model_copy(deep=True)
        self.skills[saved.id] = saved
        return saved.model_copy(deep=True)

    async def list_accessible(self, user_id):
        return [
            skill.model_copy(deep=True)
            for skill in self.skills.values()
            if skill.scope == SkillScope.GLOBAL
            or skill.user_id == user_id
            or skill.owner_user_id == user_id
        ]

    async def get_accessible_by_name(self, name, user_id):
        normalized = name.strip().lower()
        return next(
            (
                skill
                for skill in await self.list_accessible(user_id)
                if skill.name.lower() == normalized
            ),
            None,
        )


@pytest.mark.asyncio
async def test_owner_can_publish_and_unpublish_skill_while_other_user_cannot(tmp_path):
    repository = InMemorySkillRepository()
    registry = SkillRegistry(
        str(tmp_path / "skills"),
        user_id="user-1",
        repository=repository,
    )
    skill = await registry.save_markdown_skill(
        "My Skill.md",
        b"# My Skill\n\nInstructions.",
        user_id="user-1",
        workspace_id="personal-user-1",
    )
    original_id = skill.id
    original_path = skill.path

    published = await registry.change_scope(
        skill,
        SkillScope.GLOBAL,
        "user-1",
        "personal-user-1",
    )

    assert published.id == original_id
    assert published.scope == SkillScope.GLOBAL
    assert published.user_id is None
    assert published.owner_user_id == "user-1"
    assert published.workspace_id is None
    assert not (tmp_path / "skills" / "users" / "user-1" / "my-skill").exists()
    assert (tmp_path / "skills" / "global" / "my-skill" / "SKILL.md").exists()

    other_registry = SkillRegistry(
        str(tmp_path / "skills"),
        user_id="user-2",
        repository=repository,
    )
    await other_registry.load()
    other_users_view = other_registry.get_skill_by_id(original_id)
    assert other_users_view is not None
    with pytest.raises(PermissionError, match="Only the skill owner"):
        await other_registry.change_scope(
            other_users_view,
            SkillScope.USER,
            "user-2",
            "personal-user-2",
        )

    unpublished = await registry.change_scope(
        published,
        SkillScope.USER,
        "user-1",
        "personal-user-1",
    )

    assert unpublished.id == original_id
    assert unpublished.scope == SkillScope.USER
    assert unpublished.user_id == "user-1"
    assert unpublished.owner_user_id == "user-1"
    assert unpublished.workspace_id == "personal-user-1"
    assert unpublished.path == original_path
    assert (tmp_path / "skills" / "users" / "user-1" / "my-skill" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "global" / "my-skill").exists()


@pytest.mark.asyncio
async def test_skill_scope_change_rejects_duplicate_name_in_target_scope(tmp_path):
    global_dir = tmp_path / "skills" / "global" / "existing"
    global_dir.mkdir(parents=True)
    (global_dir / "SKILL.md").write_text(
        "---\nname: duplicate\n---\n\nGlobal instructions.",
        encoding="utf-8",
    )
    repository = InMemorySkillRepository()
    registry = SkillRegistry(
        str(tmp_path / "skills"),
        user_id="user-1",
        repository=repository,
    )
    skill = await registry.save_markdown_skill(
        "duplicate.md",
        b"---\nname: duplicate\n---\n\nPersonal instructions.",
        user_id="user-1",
    )

    with pytest.raises(ValueError, match="already exists"):
        await registry.change_scope(skill, SkillScope.GLOBAL, "user-1")


@pytest.mark.asyncio
async def test_admin_scope_change_does_not_duplicate_file_backed_skill(tmp_path):
    repository = InMemorySkillRepository()
    registry = SkillRegistry(
        str(tmp_path / "skills"),
        user_id="user-1",
        repository=repository,
    )
    uploaded = await registry.save_markdown_skill(
        "landsat-8-ndvi.md",
        b"---\nname: landsat-8-ndvi\n---\n\nCalculate NDVI.",
        user_id="user-1",
        workspace_id="personal-user-1",
    )

    # System management updates Mongo ownership/scope but does not move the file.
    promoted = repository.skills[uploaded.id]
    promoted.scope = SkillScope.GLOBAL
    promoted.user_id = None
    promoted.owner_user_id = None
    promoted.workspace_id = None
    repository.skills[uploaded.id] = promoted

    reloaded = SkillRegistry(
        str(tmp_path / "skills"),
        user_id="user-1",
        repository=repository,
    )
    await reloaded.load()

    matches = [skill for skill in reloaded.list_skills() if skill.name == "landsat-8-ndvi"]
    assert len(matches) == 1
    assert matches[0].id == uploaded.id
    assert matches[0].scope == SkillScope.GLOBAL
