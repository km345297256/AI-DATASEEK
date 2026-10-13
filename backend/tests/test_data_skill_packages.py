import io
import json
import zipfile

import pytest
from fastapi import HTTPException

from app.domain.models.user import User
from app.domain.services.skills.package import parse_manifest, package_digest, read_package
from app.domain.services.skills.registry import SkillRegistry
from app.interfaces.api import skill_routes, session_routes
from app.interfaces.schemas.skill import UpdateSkillFileRequest


def manifest(**changes):
    return {
        "schema_version": 1, "package_id": "dataseek/netcdf-quality",
        "version": "1.0.0", "title": "NetCDF quality", "description": "Check data semantics",
        "authors": ["DataSeek"], "license": "MIT", "category": "quality",
        "domains": ["geoscience"], "inputs": {"type": "object"},
        "outputs": {"type": "object"}, "validation": ["Check all declared dimensions"],
        **changes,
    }


def write_package(root, **changes):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("# Inspect data\n")
    (root / "skill.yaml").write_text(json.dumps(manifest(**changes)))
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "units.md").write_text("Original unit convention")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "check.py").write_text("print('original')")
    return root


def archive(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as output:
        for name, content in entries.items():
            output.writestr(name, content)
    result.seek(0)
    return result


def release_archive(version="1.0.0", prefix=""):
    return archive({
        prefix + "SKILL.md": "# Inspect data\n",
        prefix + "skill.yaml": json.dumps(manifest(version=version)),
        prefix + "references/units.md": "Kelvin",
    })


@pytest.mark.parametrize("changes", [
    {"version": "latest"}, {"version": "01.0.0"}, {"version": "1.0.0-01"},
    {"schema_version": True}, {"schema_version": 2}, {"category": "other"},
    {"package_id": "../private"}, {"verified": True}, {"authors": [""]},
    {"inputs": {"type": "object", "properties": {"x": {"type": "wrong"}}}},
    {"outputs": {"type": "object", "$ref": "https://example.org/schema"}},
    {"tools": [{"name": "scientific_inspect", "version": ">=1.0.0"}]},
    {"tools": [{"name": "inspect", "version": "1.0.0"}] * 2},
])
def test_reject_invalid_contract(changes):
    with pytest.raises(ValueError):
        parse_manifest(json.dumps(manifest(**changes)).encode())


@pytest.mark.parametrize("suffix", ["\nversion: 2.0.0\nversion: 3.0.0", "\nx: &a [*a]"])
def test_reject_duplicate_yaml_and_recursive_aliases(suffix):
    import yaml
    with pytest.raises(ValueError):
        parse_manifest((yaml.safe_dump(manifest()) + suffix).encode())


def test_manifest_is_authoritative_and_legacy_remains_available(tmp_path):
    root = write_package(tmp_path / "release")
    (root / "metadata.json").write_text(json.dumps({"name": "forged", "description": "wrong", "verified": True}))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("Legacy instructions")
    registry = SkillRegistry(str(tmp_path))
    skill = registry.get_skill("dataseek:netcdf-quality@1.0.0")
    assert skill and skill.manifest.title == "NetCDF quality"
    assert registry.get_skill("forged") is None
    assert registry.get_skill("legacy").manifest is None
    view = skill_routes._skill_response(skill).model_dump()
    assert view["format"] == "data-skill-v1"
    assert view["verification_status"] == "unverified"
    assert "inputs" not in view and "path" not in view
    assert view["package_sha256"] == package_digest(read_package(root))


def test_invalid_declared_package_does_not_fall_back_to_legacy(tmp_path):
    write_package(tmp_path / "bad", version="latest")
    assert SkillRegistry(str(tmp_path)).list_skills() == []


def test_runtime_freezes_resources_and_identity_until_next_turn(tmp_path):
    root = write_package(tmp_path / "release")
    registry = SkillRegistry(str(tmp_path))
    name = "dataseek:netcdf-quality@1.0.0"
    skill = registry.get_skill(name)
    locked_name = f"{name}#sha256={skill.package_sha256}"
    registry.restrict_to([name])
    registry.pin()
    (root / "references" / "units.md").write_text("Changed convention")
    (root / "scripts" / "check.py").write_text("print('changed')")
    (root / "SKILL.md").write_text("Changed instructions")
    registry.reload()
    assert registry.get_skill(locked_name) is skill
    assert registry.read_reference(name, "units.md") == (True, "Original unit convention")
    assert registry.read_script_content(name, "check.py") == (True, "print('original')")
    assert registry.read_skill_body(name).startswith("# Inspect data")
    registry.unpin()
    registry.reload()
    assert registry.get_skill(locked_name) is None
    assert registry.read_reference(name, "units.md") == (True, "Changed convention")


def test_activation_rejects_changed_files(tmp_path):
    root = write_package(tmp_path / "release")
    registry = SkillRegistry(str(tmp_path))
    (root / "references" / "units.md").write_text("Changed before activation")
    with pytest.raises(ValueError, match="changed"):
        registry.pin()


def test_activation_requires_exact_available_tool_version(tmp_path):
    write_package(tmp_path / "release", tools=[{"name": "scientific_inspect", "version": "1.0.0"}])
    registry = SkillRegistry(str(tmp_path))
    with pytest.raises(ValueError, match="dependencies"):
        registry.pin()
    with pytest.raises(ValueError, match="dependencies"):
        registry.pin(tool_versions={"scientific_inspect": "1.1.0"})
    registry.pin(tool_versions={"scientific_inspect": "1.0.0"})
    assert registry.read_skill_body("dataseek:netcdf-quality@1.0.0")


def test_symlink_and_digest_boundaries(tmp_path):
    root = write_package(tmp_path / "release")
    digest = package_digest(read_package(root))
    (root / "metadata.json").write_text('{"owner_user_id":"user"}')
    assert package_digest(read_package(root)) == digest
    (root / "references" / "external").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symbolic"):
        read_package(root)


@pytest.mark.asyncio
async def test_upload_root_resources_and_version_coexistence(tmp_path):
    registry = SkillRegistry(str(tmp_path), user_id="user-1")
    first = (await registry.save_zip_skills(release_archive(), user_id="user-1"))[0]
    assert registry.read_reference(first.name, "units.md") == (True, "Kelvin")
    with pytest.raises(ValueError, match="already exists"):
        await registry.save_zip_skills(release_archive(), user_id="user-1")
    second = (await registry.save_zip_skills(release_archive("1.1.0"), user_id="user-1"))[0]
    assert second.name != first.name
    assert registry.get_skill(first.name) and registry.get_skill(second.name)
    assert registry.get_skill(first.name).package_sha256 == first.package_sha256


@pytest.mark.asyncio
async def test_invalid_batch_never_exposes_partial_install(tmp_path):
    registry = SkillRegistry(str(tmp_path), user_id="user-1")
    content = archive({
        "good/SKILL.md": "Good legacy",
        "bad/SKILL.md": "Bad release",
        "bad/skill.yaml": json.dumps(manifest(version="latest")),
    })
    with pytest.raises(ValueError, match="invalid skill package"):
        await registry.save_zip_skills(content, user_id="user-1")
    registry.reload()
    assert registry.list_skills() == []
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_backslash_traversal_rejected(tmp_path):
    registry = SkillRegistry(str(tmp_path), user_id="user-1")
    with pytest.raises(ValueError, match="Unsafe"):
        await registry.save_zip_skills(archive({"SKILL.md": "x", "..\\escape": "x"}), user_id="user-1")
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.asyncio
async def test_stale_database_cannot_restore_mutated_release(tmp_path):
    root = write_package(tmp_path / "release")
    registry = SkillRegistry(str(tmp_path), user_id="user-1")
    stored = registry.list_skills()[0].model_copy(deep=True)

    class Repository:
        async def list_accessible(self, _user):
            return [stored]

    registry.repository = Repository()
    (root / "scripts" / "check.py").write_text("mutated")
    await registry.load()
    assert registry.list_skills() == []


@pytest.mark.asyncio
async def test_declared_release_edit_is_rejected_before_write(tmp_path, monkeypatch):
    write_package(tmp_path / "release")
    registry = SkillRegistry(str(tmp_path))
    skill = registry.list_skills()[0]
    monkeypatch.setattr(skill_routes, "_registry", lambda _: registry)

    class Permissions:
        async def require(self, *_args):
            return None

    with pytest.raises(HTTPException) as error:
        await skill_routes.update_skill_file(
            skill.name, UpdateSkillFileRequest(path="SKILL.md", content="replacement"),
            current_user=User(id="u", fullname="Test User", email="test@example.org"),
            permission_service=Permissions(), audit_service=None,
        )
    assert error.value.status_code == 409
    assert (tmp_path / "release" / "SKILL.md").read_text() == "# Inspect data\n"


@pytest.mark.asyncio
async def test_installed_release_is_bound_to_content_before_scheduling(tmp_path, monkeypatch):
    write_package(tmp_path / "release")
    registry = SkillRegistry(str(tmp_path))
    skill = registry.list_skills()[0]
    monkeypatch.setattr(session_routes, "SkillRegistry", lambda *a, **kw: registry)
    user = User(id="u", fullname="Test User", email="test@example.org", installed_skill_ids=[skill.id])
    assert await session_routes._installed_skill_names(user, [skill.name]) == [
        f"{skill.name}#sha256={skill.package_sha256}"
    ]
