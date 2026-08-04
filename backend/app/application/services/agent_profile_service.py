import logging
import secrets
from typing import Optional, List
from datetime import datetime, UTC

from app.domain.models.agent_profile import AgentPlannerConfig, AgentProfile, AgentSubAgentConfig, default_subagents
from app.domain.models.workspace import personal_workspace_id
from app.domain.repositories.agent_profile_repository import AgentProfileRepository
from app.application.errors.exceptions import NotFoundError, UnauthorizedError

logger = logging.getLogger(__name__)


class AgentProfileService:

    def __init__(self, repository: AgentProfileRepository):
        self._repository = repository

    async def create_profile(
        self,
        user_id: str,
        user_role: str,
        workspace_id: Optional[str],
        name: str,
        model_name: str,
        model_config_id: Optional[str] = None,
        model_provider: str = "openai",
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        system_prompt: Optional[str] = None,
        planner_config: Optional[AgentPlannerConfig] = None,
        subagents: Optional[List[AgentSubAgentConfig]] = None,
        is_global: bool = False,
    ) -> AgentProfile:
        # Only admins can create global profiles
        if is_global and user_role != "admin":
            raise UnauthorizedError("Only admins can create global agent profiles")

        profile = AgentProfile(
            id=AgentProfile.generate_id(),
            user_id=None if is_global else user_id,
            owner_user_id=user_id,
            workspace_id=workspace_id if not is_global else None,
            scope="global" if is_global else "user",
            name=name,
            model_config_id=model_config_id,
            model_name=model_name,
            model_provider=model_provider,
            api_base=api_base,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            planner_config=planner_config or AgentPlannerConfig(),
            subagents=subagents or default_subagents(),
        )
        return await self._repository.create(profile)

    async def list_profiles(self, user_id: str) -> List[AgentProfile]:
        return await self._repository.list_for_user(user_id)

    async def update_profile(
        self,
        user_id: str,
        user_role: str,
        workspace_id: Optional[str],
        profile_id: str,
        **kwargs,
    ) -> AgentProfile:
        profile = await self._repository.get_by_id(profile_id)
        if not profile:
            raise NotFoundError("Agent profile not found")
        # Global profile — only admin can edit
        if profile.scope == "global" and user_role != "admin":
            raise UnauthorizedError("Only admins can edit global agent profiles")
        if profile.user_id is not None and profile.user_id != user_id:
            raise UnauthorizedError("Not authorized to edit this agent profile")
        if profile.workspace_id and workspace_id and profile.workspace_id != workspace_id and user_role != "admin":
            raise UnauthorizedError("Not authorized to edit this agent profile")

        is_global = kwargs.pop("is_global", None)
        if is_global is not None:
            if user_role != "admin":
                raise UnauthorizedError("Only admins can change agent profile visibility")
            owner_user_id = profile.owner_user_id or profile.user_id or user_id
            profile.owner_user_id = owner_user_id
            profile.scope = "global" if is_global else "user"
            profile.user_id = None if is_global else owner_user_id
            profile.workspace_id = None if is_global else personal_workspace_id(owner_user_id)

        for field, value in kwargs.items():
            if hasattr(profile, field) and value is not None:
                setattr(profile, field, value)
        profile.updated_at = datetime.now(UTC)
        return await self._repository.update(profile)

    async def delete_profile(self, user_id: str, user_role: str, profile_id: str, workspace_id: Optional[str]) -> None:
        profile = await self._repository.get_by_id(profile_id)
        if not profile:
            raise NotFoundError("Agent profile not found")
        if profile.scope == "global" and user_role != "admin":
            raise UnauthorizedError("Only admins can delete global agent profiles")
        if profile.user_id is not None and profile.user_id != user_id:
            raise UnauthorizedError("Not authorized to delete this agent profile")
        if profile.workspace_id and workspace_id and profile.workspace_id != workspace_id and user_role != "admin":
            raise UnauthorizedError("Not authorized to delete this agent profile")
        await self._repository.delete(profile_id)

    async def get_profile(self, profile_id: str) -> Optional[AgentProfile]:
        return await self._repository.get_by_id(profile_id)
