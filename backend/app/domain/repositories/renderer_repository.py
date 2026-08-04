from abc import ABC, abstractmethod
from typing import List, Optional

from app.domain.models.renderer import Renderer


class RendererRepository(ABC):
    @abstractmethod
    async def save(self, renderer: Renderer) -> Renderer:
        pass

    @abstractmethod
    async def list_accessible(self, user_id: str) -> List[Renderer]:
        pass

    @abstractmethod
    async def get_accessible_by_id(self, renderer_id: str, user_id: str) -> Optional[Renderer]:
        pass

    @abstractmethod
    async def delete(self, renderer_id: str) -> None:
        pass
