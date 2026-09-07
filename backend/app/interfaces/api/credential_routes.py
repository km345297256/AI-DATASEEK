from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.domain.models.credential import (
    CREDENTIAL_PROVIDER_PATTERN,
    CREDENTIAL_REFERENCE_PATTERN,
    CREDENTIAL_SLOT_PATTERN,
    CREDENTIAL_TOOL_NAME_PATTERN,
    CredentialErrorCode,
    CredentialView,
)
from app.domain.models.user import User
from app.domain.services.credential_service import (
    CredentialBindingConflictError,
    CredentialContractError,
    CredentialService,
    CredentialStateConflictError,
    CredentialStateUnavailableError,
    CredentialVaultUnavailableError,
)
from app.infrastructure.external.credential_factory import get_credential_service
from app.interfaces.dependencies import get_current_user, get_plugin_runtime
from app.interfaces.schemas.base import APIResponse


router = APIRouter(prefix="/credentials", tags=["credentials"])
CredentialReference = Annotated[
    str,
    Path(pattern=CREDENTIAL_REFERENCE_PATTERN),
]
CredentialDeclarationVerifier = Callable[
    [str, str, str],
    bool | Awaitable[bool],
]


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=CREDENTIAL_PROVIDER_PATTERN,
    )
    tool_name: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=CREDENTIAL_TOOL_NAME_PATTERN,
    )
    slot: str = Field(
        strict=True,
        min_length=1,
        max_length=32,
        pattern=CREDENTIAL_SLOT_PATTERN,
    )
    secret: SecretStr


class CredentialListResponse(BaseModel):
    configured: bool
    credentials: list[CredentialView]


async def _verify_current_catalog_credential(
    provider: str,
    tool_name: str,
    slot: str,
) -> bool:
    runtime = get_plugin_runtime()
    if runtime is None or not runtime.healthy:
        raise RuntimeError("credential catalog unavailable")
    snapshot = runtime.current_snapshot
    for tool in snapshot.tools:
        if tool.name != tool_name:
            continue
        return any(
            requirement.slot == slot and requirement.provider == provider
            for requirement in tool.execution.credentials
        )
    return False


def get_credential_declaration_verifier() -> CredentialDeclarationVerifier:
    """Integration hook for the trusted current Cordis catalog."""

    return _verify_current_catalog_credential


def _require_action(actual: str | None, expected: str) -> None:
    if actual != expected:
        raise HTTPException(status_code=403, detail="credential_action_rejected")


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, CredentialVaultUnavailableError):
        return HTTPException(
            status_code=503,
            detail=CredentialErrorCode.VAULT_UNAVAILABLE.value,
        )
    if isinstance(error, CredentialBindingConflictError):
        return HTTPException(
            status_code=409,
            detail=CredentialErrorCode.BINDING_CONFLICT.value,
        )
    if isinstance(error, CredentialStateConflictError):
        return HTTPException(
            status_code=409,
            detail=CredentialErrorCode.STATE_CONFLICT.value,
        )
    if isinstance(error, CredentialStateUnavailableError):
        return HTTPException(
            status_code=503,
            detail=CredentialErrorCode.STATE_UNAVAILABLE.value,
        )
    if isinstance(error, CredentialContractError):
        return HTTPException(
            status_code=422,
            detail=CredentialErrorCode.CONTRACT_INVALID.value,
        )
    return HTTPException(status_code=500, detail="credential_operation_failed")


@router.get("", response_model=APIResponse[CredentialListResponse])
async def list_credentials(
    current_user: User = Depends(get_current_user),
    credentials: CredentialService = Depends(get_credential_service),
) -> APIResponse[CredentialListResponse]:
    try:
        views = await credentials.list_for_owner(current_user.id)
    except CredentialStateUnavailableError as error:
        raise _service_error(error) from None
    return APIResponse.success(CredentialListResponse(
        configured=credentials.configured,
        credentials=views,
    ))


@router.post("", response_model=APIResponse[CredentialView])
async def create_credential(
    request: CredentialCreateRequest,
    x_credential_action: Annotated[
        str | None,
        Header(alias="X-Credential-Action"),
    ] = None,
    current_user: User = Depends(get_current_user),
    credentials: CredentialService = Depends(get_credential_service),
    declaration_verifier: CredentialDeclarationVerifier = Depends(
        get_credential_declaration_verifier
    ),
) -> APIResponse[CredentialView]:
    _require_action(x_credential_action, "create")
    try:
        declared = declaration_verifier(
            request.provider,
            request.tool_name,
            request.slot,
        )
        if inspect.isawaitable(declared):
            declared = await declared
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="credential_catalog_unavailable",
        ) from None
    if declared is not True:
        raise HTTPException(
            status_code=409,
            detail="credential_declaration_not_found",
        )

    try:
        view = await credentials.create(
            user_id=current_user.id,
            provider=request.provider,
            tool_name=request.tool_name,
            slot=request.slot,
            secret=request.secret,
        )
    except (
        CredentialVaultUnavailableError,
        CredentialBindingConflictError,
        CredentialContractError,
        CredentialStateConflictError,
        CredentialStateUnavailableError,
    ) as error:
        raise _service_error(error) from None
    return APIResponse.success(view)


@router.post(
    "/{reference}/revoke",
    response_model=APIResponse[CredentialView],
)
async def revoke_credential(
    reference: CredentialReference,
    x_credential_action: Annotated[
        str | None,
        Header(alias="X-Credential-Action"),
    ] = None,
    current_user: User = Depends(get_current_user),
    credentials: CredentialService = Depends(get_credential_service),
) -> APIResponse[CredentialView]:
    _require_action(x_credential_action, "revoke")
    try:
        view = await credentials.revoke(current_user.id, reference)
    except (CredentialStateConflictError, CredentialStateUnavailableError) as error:
        raise _service_error(error) from None
    if view is None:
        raise HTTPException(status_code=404, detail="credential_not_found")
    return APIResponse.success(view)


__all__ = [
    "CredentialCreateRequest",
    "CredentialDeclarationVerifier",
    "CredentialListResponse",
    "get_credential_declaration_verifier",
    "router",
]
