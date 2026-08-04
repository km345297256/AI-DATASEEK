from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.models.skill import SkillScope
from app.domain.models.user import User, UserRole
from app.interfaces.api import admin_routes


class FakeField:
    def __eq__(self, value):
        return value


class FakeSkillDocument:
    skill_id = FakeField()
    current = None

    @classmethod
    async def find_one(cls, skill_id):
        return cls.current


class FakeSkill:
    def __init__(self, path: Path, fail_delete: bool = False):
        self.skill_id = "skill-1"
        self.name = "deleted-skill"
        self.scope = SkillScope.USER
        self.path = str(path)
        self.workspace_id = "personal-user-1"
        self.deleted = False
        self.fail_delete = fail_delete

    async def delete(self):
        if self.fail_delete:
            raise RuntimeError("database delete failed")
        self.deleted = True


class FakeAuditService:
    async def record(self, **kwargs):
        return None


class FakeUserRecord:
    def __init__(self):
        self.installed_skill_ids = ["skill-1", "other-skill"]
        self.auto_enabled_skills = ["DELETED-SKILL", "other-skill"]
        self.saved = False

    async def save(self):
        self.saved = True


class FakeUserQuery:
    async def to_list(self):
        return FakeUserDocument.records


class FakeUserDocument:
    records = []

    @classmethod
    def find(cls):
        return FakeUserQuery()


@pytest.mark.asyncio
async def test_admin_delete_removes_skill_package_before_registry_can_restore_it(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "users" / "user-1" / "deleted-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Deleted skill", encoding="utf-8")
    document = FakeSkill(skill_file)
    user_record = FakeUserRecord()
    FakeSkillDocument.current = document
    FakeUserDocument.records = [user_record]
    monkeypatch.setattr(admin_routes, "SkillDocument", FakeSkillDocument)
    monkeypatch.setattr(admin_routes, "UserDocument", FakeUserDocument)
    monkeypatch.setattr(
        admin_routes,
        "get_settings",
        lambda: SimpleNamespace(
            skills_dir=str(tmp_path / "skills"),
            user_skills_dir=str(tmp_path / "skills" / "users"),
        ),
    )

    admin = User(
        id="admin-1",
        fullname="Admin User",
        email="admin@example.com",
        role=UserRole.ADMIN,
    )
    await admin_routes.delete_skill(
        "skill-1",
        current_user=admin,
        audit_service=FakeAuditService(),
    )

    assert document.deleted is True
    assert not skill_dir.exists()
    assert user_record.installed_skill_ids == ["other-skill"]
    assert user_record.auto_enabled_skills == ["other-skill"]
    assert user_record.saved is True


@pytest.mark.asyncio
async def test_admin_delete_restores_skill_package_when_database_delete_fails(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "global" / "protected-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Protected skill", encoding="utf-8")
    FakeSkillDocument.current = FakeSkill(skill_file, fail_delete=True)
    FakeUserDocument.records = []
    monkeypatch.setattr(admin_routes, "SkillDocument", FakeSkillDocument)
    monkeypatch.setattr(admin_routes, "UserDocument", FakeUserDocument)
    monkeypatch.setattr(
        admin_routes,
        "get_settings",
        lambda: SimpleNamespace(
            skills_dir=str(tmp_path / "skills"),
            user_skills_dir=str(tmp_path / "skills" / "users"),
        ),
    )
    admin = User(
        id="admin-1",
        fullname="Admin User",
        email="admin@example.com",
        role=UserRole.ADMIN,
    )

    with pytest.raises(RuntimeError, match="database delete failed"):
        await admin_routes.delete_skill(
            "skill-1",
            current_user=admin,
            audit_service=FakeAuditService(),
        )

    assert skill_file.exists()
