import hashlib
import secrets
import logging
from typing import Optional, List
from datetime import datetime, UTC, timedelta

from app.domain.models.api_key import APIKey, APIKeyScope, APIKeyStatus
from app.domain.repositories.api_key_repository import APIKeyRepository
from app.domain.repositories.user_repository import UserRepository
from app.domain.models.user import User
from app.application.errors.exceptions import NotFoundError, UnauthorizedError

logger = logging.getLogger(__name__)

KEY_PREFIX = "ai-dataseek-sk-"


def _generate_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _make_prefix(raw_key: str) -> str:
    # Show first 20 chars as display prefix, e.g. "ai-dataseek-sk-xxxx"
    return raw_key[:20]


class APIKeyService:

    def __init__(self, api_key_repository: APIKeyRepository, user_repository: UserRepository):
        self.api_key_repository = api_key_repository
        self.user_repository = user_repository

    async def create_api_key(
        self,
        user_id: str,
        name: str,
        scopes: List[APIKeyScope],
        expires_in_days: Optional[int],
    ) -> tuple[APIKey, str]:
        """Returns (APIKey, raw_key). raw_key is shown only once."""
        raw_key = _generate_raw_key()
        expires_at = None
        if expires_in_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

        api_key = APIKey(
            id=secrets.token_urlsafe(16),
            user_id=user_id,
            name=name,
            key_prefix=_make_prefix(raw_key),
            key_hash=_hash_key(raw_key),
            scopes=scopes,
            status=APIKeyStatus.ACTIVE,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        created = await self.api_key_repository.create(api_key)
        logger.info(f"Created API key {created.id} for user {user_id}")
        return created, raw_key

    async def list_api_keys(self, user_id: str) -> List[APIKey]:
        return await self.api_key_repository.list_by_user(user_id)

    async def revoke_api_key(self, user_id: str, key_id: str) -> None:
        api_key = await self.api_key_repository.get_by_id(key_id)
        if not api_key:
            raise NotFoundError("API key not found")
        if api_key.user_id != user_id:
            raise UnauthorizedError("Not authorized to revoke this key")
        api_key.revoke()
        await self.api_key_repository.update(api_key)
        logger.info(f"Revoked API key {key_id} for user {user_id}")

    async def authenticate_by_api_key(self, raw_key: str) -> Optional[User]:
        if not raw_key.startswith(KEY_PREFIX):
            return None
        key_hash = _hash_key(raw_key)
        api_key = await self.api_key_repository.get_by_hash(key_hash)
        if not api_key or not api_key.is_valid():
            return None
        user = await self.user_repository.get_user_by_id(api_key.user_id)
        if not user or not user.is_active:
            return None
        api_key.update_last_used()
        await self.api_key_repository.update(api_key)
        return user
