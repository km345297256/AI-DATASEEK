from langchain.tools import tool

from app.domain.models.tool_result import ToolResult
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.skills.session_skill_creator import create_skill_from_session
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.tools.base import BaseToolkit


class SkillToolkit(BaseToolkit):
    name: str = "skill"

    def __init__(
        self,
        registry: SkillRegistry,
        session_id: str | None = None,
        user_id: str | None = None,
        session_repository: SessionRepository | None = None,
    ):
        super().__init__()
        self.registry = registry
        self.session_id = session_id
        self.user_id = user_id
        self.session_repository = session_repository

    @tool
    async def skill_list(self) -> ToolResult:
        """List local agent Skills only. Skills are prompt/workflow guidance, not MCP servers and not MCP tools. Do not use this for MCP questions."""
        skills = [
            {
                "name": skill.name,
                "description": skill.description,
                "triggers": skill.triggers,
                "resources": {
                    "scripts": skill.scripts,
                    "references": skill.references,
                    "templates": skill.templates,
                },
            }
            for skill in self.registry.list_skills()
        ]
        return ToolResult(success=True, data=skills)

    @tool
    async def skill_read(self, name: str) -> ToolResult:
        """Read one local agent Skill instruction file by name. Skills are not MCP tools."""
        skill = self.registry.get_skill(name)
        if not skill:
            return ToolResult(success=False, message=f"Skill not found: {name}")
        content = self.registry.read_skill_body(name)
        return ToolResult(
            success=True,
            data={
                "name": skill.name,
                "description": skill.description,
                "content": content or skill.content,
                "resources": {
                    "scripts": skill.scripts,
                    "references": skill.references,
                    "templates": skill.templates,
                },
            },
        )

    @tool
    async def skill_list_resources(self, name: str) -> ToolResult:
        """List scripts, references, and templates available in a local agent Skill package."""
        resources = self.registry.list_skill_resources(name)
        if resources is None:
            return ToolResult(success=False, message=f"Skill not found: {name}")
        skill = self.registry.get_skill(name)
        return ToolResult(
            success=True,
            data={
                "name": name,
                "resources": resources,
                "scripts": skill.scripts if skill else [],
                "references": skill.references if skill else [],
                "templates": skill.templates if skill else [],
            },
        )

    @tool
    async def skill_read_reference(self, skill_name: str, ref_filename: str) -> ToolResult:
        """Read a reference file from a local agent Skill references/ directory. Call skill_read first to discover references."""
        success, content = self.registry.read_reference(skill_name, ref_filename)
        return ToolResult(
            success=success,
            message=None if success else content,
            data={
                "skill_name": skill_name,
                "ref_filename": ref_filename,
                "content": content,
            } if success else None,
        )

    @tool
    async def skill_read_script(self, skill_name: str, script_filename: str) -> ToolResult:
        """Read a script file from a local agent Skill scripts/ directory to understand its usage before running related commands."""
        success, content = self.registry.read_script_content(skill_name, script_filename)
        return ToolResult(
            success=success,
            message=None if success else content,
            data={
                "skill_name": skill_name,
                "script_filename": script_filename,
                "content": content,
            } if success else None,
        )

    @tool
    async def skill_create_from_session(self) -> ToolResult:
        """Create a private Skill from the current task/session when the user asks to save, create, or turn this task into a Skill. Do not ask the user for name, description, or triggers; infer them from the session."""
        if not self.session_id or not self.user_id or not self.session_repository:
            return ToolResult(success=False, message="Current session context is unavailable")
        try:
            skill = await create_skill_from_session(
                session_id=self.session_id,
                user_id=self.user_id,
                session_repository=self.session_repository,
                registry=self.registry,
            )
        except ValueError as exc:
            return ToolResult(success=False, message=str(exc))

        return ToolResult(
            success=True,
            message=f"Skill created: {skill.name}",
            data={
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "triggers": skill.triggers,
                "scope": skill.scope,
                "user_id": skill.user_id,
                "created_from_session_id": skill.created_from_session_id,
                "path": skill.path,
            },
        )
