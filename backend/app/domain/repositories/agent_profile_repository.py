from abc import ABC, abstractmethod
from typing import Optional, List
from app.domain.models.agent_profile import AgentProfile


class AgentProfileRepository(ABC):

    @abstractmethod
    async def create(self, profile: AgentProfile) -> AgentProfile:
        pass

    @abstractmethod
    async def get_by_id(self, profile_id: str) -> Optional[AgentProfile]:
        pass

    @abstractmethod
    async def list_for_user(self, user_id: str) -> List[AgentProfile]:
        """Returns global profiles + user's own profiles."""
        pass

    @abstractmethod
    async def update(self, profile: AgentProfile) -> AgentProfile:
        pass

    @abstractmethod
    async def delete(self, profile_id: str) -> None:
        pass
