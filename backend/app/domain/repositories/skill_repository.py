from abc import ABC, abstractmethod
from typing import List, Optional

from app.domain.models.skill import Skill


class SkillRepository(ABC):
    @abstractmethod
    async def save(self, skill: Skill) -> Skill:
        pass

    @abstractmethod
    async def list_accessible(self, user_id: str) -> List[Skill]:
        pass

    @abstractmethod
    async def get_accessible_by_name(self, name: str, user_id: str) -> Optional[Skill]:
        pass
