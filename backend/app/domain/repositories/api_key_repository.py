from abc import ABC, abstractmethod
from typing import Optional, List
from app.domain.models.api_key import APIKey


class APIKeyRepository(ABC):

    @abstractmethod
    async def create(self, api_key: APIKey) -> APIKey:
        pass

    @abstractmethod
    async def get_by_id(self, key_id: str) -> Optional[APIKey]:
        pass

    @abstractmethod
    async def get_by_hash(self, key_hash: str) -> Optional[APIKey]:
        pass

    @abstractmethod
    async def list_by_user(self, user_id: str) -> List[APIKey]:
        pass

    @abstractmethod
    async def update(self, api_key: APIKey) -> APIKey:
        pass
