import json
import logging
import re
import shutil
import zipfile
import hashlib
import tempfile
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, List, Optional

from app.domain.models.skill import Skill, SkillScope
from app.domain.repositories.skill_repository import SkillRepository
from app.domain.services.skills.package import (
    parse_manifest, read_package, package_digest, MAX_PACKAGE_BYTES, MAX_PACKAGE_FILES,
)

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(
        self,
        skills_dir: str,
        enabled: bool = True,
        user_id: Optional[str] = None,
        repository: Optional[SkillRepository] = None,
        user_skills_dir: Optional[str] = None,
    ):
        self.skills_dir = Path(skills_dir)
        self.user_skills_dir = Path(user_skills_dir or self.skills_dir / "users")
        self.enabled = enabled
        self.user_id = user_id
        self.repository = repository
        self._skills: List[Skill] = []
        self._allowed_names: Optional[set[str]] = None
        self._pinned = False
        self._resource_snapshots: dict[str, dict[str, bytes]] = {}
        self.reload()

    async def load(self) -> None:
        if self._pinned:
            return
        self.reload()
        if self.repository and self.user_id:
            db_skills = await self.repository.list_accessible(self.user_id)
            # A scope change can update Mongo before the file is moved or its
            # metadata is rewritten. The package path is the stable identity;
            # database records override file-scan records for that same package.
            by_key = {str(Path(skill.path).resolve()): skill for skill in self._skills}
            for skill in db_skills:
                key = str(Path(skill.path).resolve())
                fresh = by_key.get(key)
                if skill.manifest or (fresh and fresh.manifest):
                    fresh = fresh or self._load_skill(Path(skill.path), skill.scope, skill.user_id)
                    # A missing/changed release must never be restored from an
                    # old Mongo body while its resources come from new files.
                    if not fresh or not fresh.manifest or (
                        skill.manifest and skill.package_sha256 != fresh.package_sha256
                    ):
                        by_key.pop(key, None)
                        continue
                    fresh.id = skill.id
                    for field in ("scope", "user_id", "owner_user_id", "workspace_id", "created_from_session_id"):
                        setattr(fresh, field, getattr(skill, field))
                    by_key[key] = fresh
                else:
                    by_key[key] = skill
            self._skills = list(by_key.values())
            self._apply_restriction()

    def reload(self) -> None:
        if self._pinned:
            return
        self._skills = []
        self._resource_snapshots = {}
        if not self.enabled:
            logger.info("Skill registry disabled")
            return

        self._skills.extend(self._load_dir(self._global_dir(), SkillScope.GLOBAL, None))
        if self.user_id:
            self._skills.extend(self._load_dir(self._user_dir(self.user_id), SkillScope.USER, self.user_id))
        self._apply_restriction()
        logger.info("Loaded %d skills for user %s", len(self._skills), self.user_id or "<global>")

    async def sync_files_to_repository(self) -> None:
        if not self.repository:
            return
        self._skills = [await self.repository.save(skill) for skill in self._skills]

    def _global_dir(self) -> Path:
        legacy_has_skills = any(self.skills_dir.glob("*/SKILL.md")) if self.skills_dir.exists() else False
        global_dir = self.skills_dir / "global"
        return self.skills_dir if legacy_has_skills else global_dir

    def _user_dir(self, user_id: str) -> Path:
        return self.user_skills_dir / self._safe_user_id(user_id)

    def _load_dir(self, directory: Path, scope: SkillScope, user_id: Optional[str]) -> List[Skill]:
        if not directory.exists():
            logger.info("Skill directory does not exist: %s", directory)
            return []
        skills = []
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            skill = self._load_skill(skill_file, scope, user_id)
            if skill:
                skills.append(skill)
        return skills

    def _load_skill(self, skill_file: Path, scope: SkillScope, user_id: Optional[str]) -> Optional[Skill]:
        try:
            raw_content = skill_file.read_text(encoding="utf-8").strip()
            frontmatter, content = self._split_frontmatter(raw_content)
            metadata = {**frontmatter, **self._load_metadata(skill_file.parent)}
            manifest = None
            digest = None
            if (skill_file.parent / "skill.yaml").exists():
                files = read_package(skill_file.parent)
                manifest = parse_manifest(files["skill.yaml"])
                digest = package_digest(files)
                # Manifest metadata is authoritative over legacy frontmatter
                # and local ownership metadata, neither can forge verification.
                metadata.update(name=manifest.selection_name, description=manifest.description, triggers=manifest.triggers)
                metadata["priority"] = 0
                metadata["max_context_chars"] = 6000
            return Skill(
                **({"id": hashlib.sha256(f"{skill_file.resolve()}:{digest}".encode()).hexdigest()[:32]} if digest else {}),
                name=metadata.get("name") or skill_file.parent.name,
                description=metadata.get("description", ""),
                triggers=metadata.get("triggers", []),
                priority=metadata.get("priority", 0),
                max_context_chars=metadata.get("max_context_chars", 6000),
                content=content,
                path=str(skill_file),
                manifest=manifest,
                package_sha256=digest,
                scripts=self._scan_resource_files(skill_file.parent / "scripts"),
                references=self._scan_resource_files(skill_file.parent / "references"),
                templates=self._scan_resource_files(skill_file.parent / "templates"),
                scope=scope,
                user_id=user_id,
                owner_user_id=metadata.get("owner_user_id"),
                workspace_id=metadata.get("workspace_id"),
                created_from_session_id=metadata.get("created_from_session_id"),
            )
        except Exception:
            logger.warning("Failed to load skill package: %s", skill_file.parent.name)
            return None

    def _load_metadata(self, skill_dir: Path) -> dict:
        metadata_file = skill_dir / "metadata.json"
        if not metadata_file.exists():
            return {}
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            return metadata if isinstance(metadata, dict) else {}
        except json.JSONDecodeError as exc:
            logger.warning("Invalid skill metadata %s: %s", metadata_file, exc)
            return {}

    def list_skills(self) -> List[Skill]:
        return list(self._skills)

    def restrict_to(self, names: List[str]) -> None:
        self._allowed_names = {name.strip().lower() for name in names if name and name.strip()}
        self._apply_restriction()

    def clear_restriction(self) -> None:
        self._allowed_names = None

    def _apply_restriction(self) -> None:
        if self._allowed_names is None:
            return
        self._skills = [
            skill for skill in self._skills
            if skill.name.strip().lower() in self._allowed_names
        ]

    def get_skill(self, name: str) -> Optional[Skill]:
        normalized = name.strip().lower()
        normalized, separator, digest = normalized.partition("#sha256=")
        matches = [skill for skill in self._skills if skill.name.lower() == normalized]
        # Do not silently select between two installations of one release.
        if len(matches) != 1:
            return None
        return matches[0] if not separator or matches[0].package_sha256 == digest else None

    def unpin(self) -> None:
        self._pinned = False
        self._resource_snapshots = {}

    def pin(self, *, tool_versions: dict[str, str] | None = None) -> None:
        """Freeze bytes exposed by Skill tools for this Agent turn."""
        if self._pinned:
            return
        snapshots = {}
        digests = {}
        for skill in self._skills:
            if skill.manifest:
                unavailable = [dependency.name for dependency in skill.manifest.tools
                               if (tool_versions or {}).get(dependency.name) != dependency.version]
                if unavailable:
                    raise ValueError("Skill tool dependencies are unavailable or incompatible: " + ", ".join(unavailable))
            files = read_package(Path(skill.path).parent)
            digest = package_digest(files)
            if skill.manifest and digest != skill.package_sha256:
                raise ValueError("Skill package changed before task activation")
            _, body = self._split_frontmatter(files["SKILL.md"].decode("utf-8").strip())
            if body != skill.content:
                raise ValueError("Skill instructions changed before task activation")
            snapshots[skill.name] = files
            digests[skill.name] = digest
        self._resource_snapshots = snapshots
        for skill in self._skills:
            skill.package_sha256 = digests[skill.name]
        self._pinned = True

    def get_skill_by_id(self, skill_id: str) -> Optional[Skill]:
        return next((skill for skill in self._skills if skill.id == skill_id), None)

    def read_skill_body(self, name: str) -> Optional[str]:
        skill = self.get_skill(name)
        if not skill:
            return None
        resource_info = self.format_resource_info(skill)
        if not resource_info:
            return skill.content
        return f"{skill.content.rstrip()}\n\n---\n{resource_info}"

    def read_reference(self, skill_name: str, ref_filename: str) -> tuple[bool, str]:
        skill = self.get_skill(skill_name)
        if not skill:
            return False, f"Skill not found: {skill_name}"
        if ref_filename not in skill.references:
            available = ", ".join(skill.references) or "none"
            return False, f"Reference not found: {ref_filename}. Available references: {available}"
        return self._read_skill_resource(skill, "references", ref_filename)

    def read_script_content(self, skill_name: str, script_filename: str) -> tuple[bool, str]:
        skill = self.get_skill(skill_name)
        if not skill:
            return False, f"Skill not found: {skill_name}"
        if script_filename not in skill.scripts:
            available = ", ".join(skill.scripts) or "none"
            return False, f"Script not found: {script_filename}. Available scripts: {available}"
        return self._read_skill_resource(skill, "scripts", script_filename)

    def list_skill_resources(self, skill_name: str) -> Optional[str]:
        skill = self.get_skill(skill_name)
        if not skill:
            return None
        return self.format_resource_info(skill) or "No additional skill resources."

    def format_resource_info(self, skill: Skill) -> str:
        parts: list[str] = []
        if skill.scripts:
            parts.append("### Available Scripts")
            for script in skill.scripts:
                parts.append(f"- `{script}`")
        if skill.references:
            parts.append("### Available References")
            for reference in skill.references:
                parts.append(f"- `{reference}` (use `skill_read_reference` to load)")
        if skill.templates:
            parts.append("### Available Templates")
            for template in skill.templates:
                parts.append(f"- `{template}`")
        return "\n".join(parts)

    def _read_skill_resource(self, skill: Skill, directory_name: str, filename: str) -> tuple[bool, str]:
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts:
            return False, f"Unsafe resource path: {filename}"
        if self._pinned:
            content = self._resource_snapshots.get(skill.name, {}).get(f"{directory_name}/{path.as_posix()}")
            if content is None:
                return False, f"Resource file not found: {filename}"
            try:
                return True, content.decode("utf-8")
            except UnicodeDecodeError:
                return False, f"Resource is not a UTF-8 text file: {filename}"
        skill_dir = Path(skill.path).parent
        resource_path = skill_dir / directory_name / path
        try:
            resolved_resource = resource_path.resolve()
            resolved_dir = (skill_dir / directory_name).resolve()
            if not resolved_resource.is_relative_to(resolved_dir):
                return False, f"Unsafe resource path: {filename}"
            return True, resolved_resource.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False, f"Resource file not found: {filename}"
        except UnicodeDecodeError:
            return False, f"Resource is not a UTF-8 text file: {filename}"

    def _scan_resource_files(self, directory: Path) -> list[str]:
        if not directory.is_dir():
            return []
        files: list[str] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                files.append(str(path.relative_to(directory)))
            except ValueError:
                continue
        return files

    async def save_markdown_skill(
        self,
        filename: str,
        content: bytes,
        scope: SkillScope = SkillScope.USER,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Skill:
        owner_id = user_id or self.user_id
        skill_dir = self._unique_skill_dir(self._target_root(scope, owner_id), Path(filename).stem or "skill")
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_bytes(content)
        skill = self._load_skill(skill_dir / "SKILL.md", scope, owner_id)
        if not skill:
            raise ValueError(f"Failed to load uploaded skill: {skill_dir.name}")
        skill.owner_user_id = owner_id
        skill.workspace_id = workspace_id
        if self.repository:
            skill = await self.repository.save(skill)
        await self.load()
        return skill

    async def save_zip_skills(
        self,
        fileobj: BinaryIO,
        scope: SkillScope = SkillScope.USER,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> List[Skill]:
        owner_id = user_id or self.user_id
        saved_skills: list[Skill] = []
        await self.load()
        with zipfile.ZipFile(fileobj) as archive:
            self._validate_zip(archive)
            skill_roots = self._find_zip_skill_roots(archive)
            if not skill_roots:
                raise ValueError("Zip file must contain at least one SKILL.md")

            target_root = self._target_root(scope, owner_id)
            target_root.mkdir(parents=True, exist_ok=True)
            # Validate every package before making any uploaded directory
            # visible to the registry. Rejected packages never become legacy.
            with tempfile.TemporaryDirectory(prefix=".skill-upload-", dir=target_root) as staging:
                pending = []
                releases = {(s.manifest.package_id, s.manifest.version): s.package_sha256
                            for s in self._skills if s.manifest}
                if self.repository and owner_id:
                    for stored in await self.repository.list_accessible(owner_id):
                        if stored.manifest:
                            releases[(stored.manifest.package_id, stored.manifest.version)] = stored.package_sha256
                for index, root in enumerate(skill_roots):
                    source_name = root.rstrip("/").split("/")[-1] or "skill"
                    staged = Path(staging) / str(index)
                    staged.mkdir()
                    self._extract_skill_root(archive, root, staged)
                    # Ownership is installation metadata, not supplied by the
                    # uploader's package or another user's archived metadata.
                    self._write_metadata(staged, owner_user_id=owner_id, workspace_id=workspace_id,
                                         created_from_session_id=None)
                    skill = self._load_skill(staged / "SKILL.md", scope, owner_id)
                    if not skill:
                        raise ValueError("No valid skills were loaded: invalid skill package")
                    if skill.manifest:
                        key = (skill.manifest.package_id, skill.manifest.version)
                        if key in releases:
                            raise ValueError("Skill release already exists; use a new version")
                        releases[key] = skill.package_sha256
                    pending.append((source_name, staged))
                for source_name, staged in pending:
                    target_dir = self._unique_skill_dir(target_root, source_name)
                    staged.rename(target_dir)
                    skill = self._load_skill(target_dir / "SKILL.md", scope, owner_id)
                    if skill is None:
                        raise ValueError("No valid skills were loaded: invalid skill package")
                    skill.owner_user_id = owner_id
                    skill.workspace_id = workspace_id
                    try:
                        if self.repository:
                            skill = await self.repository.save(skill)
                    except Exception:
                        shutil.rmtree(target_dir)
                        raise
                    saved_skills.append(skill)

        await self.load()
        return saved_skills

    async def save_generated_skill(
        self,
        name: str,
        description: str,
        triggers: List[str],
        content: str,
        user_id: str,
        created_from_session_id: str,
        workspace_id: Optional[str] = None,
        references: Optional[dict[str, str]] = None,
        scripts: Optional[dict[str, str]] = None,
        assets: Optional[dict[str, bytes]] = None,
    ) -> Skill:
        skill_name = self._normalize_skill_name(name)
        target_dir = self._unique_skill_dir(self._user_dir(user_id), skill_name)
        target_dir.mkdir(parents=True, exist_ok=False)
        skill_md = self._build_skill_markdown(skill_name, description, triggers, content)
        (target_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        for relative_path, text in (references or {}).items():
            target_path = self._safe_child_path(target_dir, relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(text, encoding="utf-8")
        for relative_path, text in (scripts or {}).items():
            target_path = self._safe_child_path(target_dir, relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(text, encoding="utf-8")
            target_path.chmod(0o755)
        for relative_path, data in (assets or {}).items():
            target_path = self._safe_child_path(target_dir, relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(data)
        (target_dir / "metadata.json").write_text(
            json.dumps({"created_from_session_id": created_from_session_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        skill = self._load_skill(target_dir / "SKILL.md", SkillScope.USER, user_id)
        if not skill:
            raise ValueError(f"Failed to load generated skill: {skill_name}")
        skill.owner_user_id = user_id
        skill.workspace_id = workspace_id
        if self.repository:
            skill = await self.repository.save(skill)
        await self.load()
        return skill

    async def change_scope(
        self,
        skill: Skill,
        scope: SkillScope,
        user_id: str,
        workspace_id: Optional[str] = None,
    ) -> Skill:
        if scope not in {SkillScope.USER, SkillScope.GLOBAL}:
            raise ValueError("Skill scope must be user or global")
        owner_id = skill.owner_user_id or skill.user_id
        if owner_id != user_id:
            raise PermissionError("Only the skill owner can change its scope")
        if skill.scope == scope:
            return skill

        conflict = next(
            (
                existing
                for existing in self._skills
                if existing.id != skill.id
                and existing.name.lower() == skill.name.lower()
                and existing.scope == scope
                and (
                    scope == SkillScope.GLOBAL
                    or (existing.owner_user_id or existing.user_id) == user_id
                )
            ),
            None,
        )
        if conflict:
            raise ValueError("A skill with this name already exists in the target scope")

        source_dir = Path(skill.path).resolve().parent
        allowed_roots = [self._global_dir().resolve(), self._user_dir(user_id).resolve()]
        if not any(source_dir.is_relative_to(root) for root in allowed_roots):
            raise ValueError("Skill path is outside the managed skill directories")
        if not source_dir.is_dir() or not (source_dir / "SKILL.md").is_file():
            raise ValueError("Skill files not found")

        target_owner_id = None if scope == SkillScope.GLOBAL else user_id
        target_root = self._target_root(scope, target_owner_id)
        target_dir = self._unique_skill_dir(target_root, source_dir.name)
        old_path = skill.path
        shutil.move(str(source_dir), str(target_dir))

        try:
            skill.path = str(target_dir / "SKILL.md")
            skill.scope = scope
            skill.user_id = target_owner_id
            skill.owner_user_id = user_id
            skill.workspace_id = None if scope == SkillScope.GLOBAL else workspace_id
            skill.updated_at = datetime.now(UTC)
            self._write_metadata(
                target_dir,
                owner_user_id=user_id,
                workspace_id=skill.workspace_id,
            )
            if self.repository:
                skill = await self.repository.save(skill)
        except Exception:
            if target_dir.exists() and not source_dir.exists():
                shutil.move(str(target_dir), str(source_dir))
            skill.path = old_path
            raise

        await self.load()
        return self.get_skill_by_id(skill.id) or skill

    def _target_root(self, scope: SkillScope, user_id: Optional[str]) -> Path:
        if scope == SkillScope.GLOBAL:
            return self._global_dir()
        if not user_id:
            raise ValueError("user_id is required for user skills")
        return self._user_dir(user_id)

    def _write_metadata(self, skill_dir: Path, **updates) -> None:
        metadata = self._load_metadata(skill_dir)
        for key, value in updates.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        (skill_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_skill_markdown(self, name: str, description: str, triggers: List[str], content: str) -> str:
        trigger_text = ", ".join(triggers)
        return (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"triggers: [{trigger_text}]\n"
            "---\n\n"
            f"{content.strip()}\n"
        )

    def _split_frontmatter(self, content: str) -> tuple[dict, str]:
        if not content.startswith("---\n"):
            return {}, content
        end_index = content.find("\n---", 4)
        if end_index == -1:
            return {}, content
        frontmatter_text = content[4:end_index].strip()
        body = content[end_index + len("\n---"):].lstrip()
        return self._parse_frontmatter(frontmatter_text), body

    def _parse_frontmatter(self, text: str) -> dict:
        metadata: dict = {}
        current_key: Optional[str] = None
        block_key: Optional[str] = None
        block_indent: Optional[int] = None
        block_lines: list[str] = []

        def flush_block() -> None:
            nonlocal block_key, block_indent, block_lines
            if block_key:
                metadata[block_key] = "\n".join(block_lines).strip()
            block_key = None
            block_indent = None
            block_lines = []

        for raw_line in text.splitlines():
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            line = raw_line.strip()
            if block_key:
                if line and indent > 0:
                    if block_indent is None or (indent < block_indent and line):
                        block_indent = indent
                    strip_indent = block_indent or indent
                    block_lines.append(raw_line[strip_indent:])
                    continue
                flush_block()
            if not line or line.startswith("#"):
                continue
            if indent > 0:
                continue
            if line.startswith("- ") and current_key:
                if isinstance(metadata.get(current_key), list):
                    metadata[current_key].append(self._clean_yaml_value(line[2:]))
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if value in {"|", ">"}:
                block_key = key
                block_indent = None
                block_lines = []
            elif value == "":
                metadata[key] = []
            elif value.startswith("[") and value.endswith("]"):
                metadata[key] = [
                    self._clean_yaml_value(item.strip())
                    for item in value[1:-1].split(",")
                    if item.strip()
                ]
            elif key in {"priority", "max_context_chars"}:
                try:
                    metadata[key] = int(value)
                except ValueError:
                    metadata[key] = self._clean_yaml_value(value)
            else:
                metadata[key] = self._clean_yaml_value(value)
        flush_block()
        return metadata

    def _clean_yaml_value(self, value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    def _validate_zip(self, archive: zipfile.ZipFile) -> None:
        seen = set()
        total = 0
        if len(archive.infolist()) > MAX_PACKAGE_FILES:
            raise ValueError("Skill archive contains too many entries")
        for info in archive.infolist():
            normalized = info.filename.replace("\\", "/")
            path = Path(normalized)
            total += info.file_size
            if (path.is_absolute() or ".." in path.parts or ":" in normalized
                    or stat.S_ISLNK(info.external_attr >> 16) or normalized in seen):
                raise ValueError("Unsafe zip path or duplicate archive entry")
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Skill archive exceeds size limit")
            seen.add(normalized)

    def _find_zip_skill_roots(self, archive: zipfile.ZipFile) -> list[str]:
        roots = []
        for name in archive.namelist():
            normalized = name.replace("\\", "/")
            if normalized.endswith("/SKILL.md"):
                roots.append(normalized[: -len("SKILL.md")])
            elif normalized == "SKILL.md":
                roots.append("")
        return sorted(set(roots))

    def _extract_skill_root(self, archive: zipfile.ZipFile, root: str, target_dir: Path) -> None:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            if root:
                if not name.startswith(root):
                    continue
                relative = name[len(root):]
            else:
                relative = name
            if relative.startswith("__MACOSX/") or Path(relative).name.startswith("._"):
                continue
            target_path = self._safe_child_path(target_dir, relative)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    def _normalize_skill_name(self, name: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
        return normalized or "skill"

    def _safe_user_id(self, user_id: str) -> str:
        return self._normalize_skill_name(user_id)

    def _safe_child_path(self, root: Path, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe skill resource path: {relative_path}")
        return root / path

    def _unique_skill_dir(self, root: Path, name: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        normalized = self._normalize_skill_name(name)
        candidate = root / normalized
        suffix = 2
        while candidate.exists():
            candidate = root / f"{normalized}-{suffix}"
            suffix += 1
        return candidate
