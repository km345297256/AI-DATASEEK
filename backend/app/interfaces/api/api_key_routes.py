from fastapi import APIRouter, Depends
from typing import List

from app.application.services.api_key_service import APIKeyService
from app.domain.models.user import User
from app.interfaces.dependencies import get_current_user, get_api_key_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.api_key import (
    CreateAPIKeyRequest, APIKeyResponse, CreateAPIKeyResponse
)
from app.application.errors.exceptions import NotFoundError

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("", response_model=APIResponse[CreateAPIKeyResponse])
async def create_api_key(
    request: CreateAPIKeyRequest,
    current_user: User = Depends(get_current_user),
    api_key_service: APIKeyService = Depends(get_api_key_service),
) -> APIResponse[CreateAPIKeyResponse]:
    api_key, raw_key = await api_key_service.create_api_key(
        user_id=current_user.id,
        name=request.name,
        scopes=request.scopes,
        expires_in_days=request.expires_in_days,
    )
    return APIResponse.success(CreateAPIKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        key_prefix=api_key.key_prefix,
        scopes=api_key.scopes,
        status=api_key.status,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
    ))


@router.get("", response_model=APIResponse[List[APIKeyResponse]])
async def list_api_keys(
    current_user: User = Depends(get_current_user),
    api_key_service: APIKeyService = Depends(get_api_key_service),
) -> APIResponse[List[APIKeyResponse]]:
    keys = await api_key_service.list_api_keys(current_user.id)
    return APIResponse.success([
        APIKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            scopes=k.scopes,
            status=k.status,
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
            created_at=k.created_at,
        ) for k in keys
    ])


@router.delete("/{key_id}", response_model=APIResponse[dict])
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(get_current_user),
    api_key_service: APIKeyService = Depends(get_api_key_service),
) -> APIResponse[dict]:
    await api_key_service.revoke_api_key(current_user.id, key_id)
    return APIResponse.success({})
