import logging
from typing import Optional, List
from app.domain.models.api_key import APIKey
from app.domain.repositories.api_key_repository import APIKeyRepository
from app.infrastructure.models.documents import APIKeyDocument

logger = logging.getLogger(__name__)


class MongoAPIKeyRepository(APIKeyRepository):

    async def create(self, api_key: APIKey) -> APIKey:
        doc = APIKeyDocument.from_domain(api_key)
        await doc.create()
        return doc.to_domain()

    async def get_by_id(self, key_id: str) -> Optional[APIKey]:
        doc = await APIKeyDocument.find_one(APIKeyDocument.key_id == key_id)
        return doc.to_domain() if doc else None

    async def get_by_hash(self, key_hash: str) -> Optional[APIKey]:
        doc = await APIKeyDocument.find_one(APIKeyDocument.key_hash == key_hash)
        return doc.to_domain() if doc else None

    async def list_by_user(self, user_id: str) -> List[APIKey]:
        docs = await APIKeyDocument.find(APIKeyDocument.user_id == user_id).to_list()
        return [doc.to_domain() for doc in docs]

    async def update(self, api_key: APIKey) -> APIKey:
        doc = await APIKeyDocument.find_one(APIKeyDocument.key_id == api_key.id)
        if not doc:
            raise ValueError(f"APIKey not found: {api_key.id}")
        doc.update_from_domain(api_key)
        await doc.save()
        return doc.to_domain()
