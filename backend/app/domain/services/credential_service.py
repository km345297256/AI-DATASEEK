from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError

from app.domain.models.credential import (
    CredentialErrorCode,
    CredentialRecord,
    CredentialRequirement,
    CredentialView,
)
from app.domain.repositories.credential_repository import (
    CredentialRepository,
    CredentialRepositoryConflictError,
)


_MIN_SECRET_BYTES = 8
_MAX_SECRET_BYTES = 8192
_CAS_RETRIES = 4
_REPOSITORY_TIMEOUT_SECONDS = 5.0
_T = TypeVar("_T")


class CredentialServiceError(RuntimeError):
    """Fixed-code vault failure that never embeds secret or provider payloads."""

    def __init__(self, code: CredentialErrorCode):
        self.code = code
        super().__init__(code.value)


class CredentialVaultUnavailableError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.VAULT_UNAVAILABLE)


class CredentialBindingConflictError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.BINDING_CONFLICT)


class CredentialBindingMissingError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.BINDING_MISSING)


class CredentialBindingChangedError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.BINDING_CHANGED)


class CredentialContractError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.CONTRACT_INVALID)


class CredentialStateConflictError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.STATE_CONFLICT)


class CredentialStateUnavailableError(CredentialServiceError):
    def __init__(self) -> None:
        super().__init__(CredentialErrorCode.STATE_UNAVAILABLE)


class CredentialService:
    """Owner-scoped encrypted bindings resolved only for one exact tool call."""

    def __init__(
        self,
        repository: CredentialRepository,
        *,
        encryption_key: SecretStr | str | bytes | None,
    ) -> None:
        self._repository = repository
        self._fernet = self._build_fernet(encryption_key)

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    @staticmethod
    def _repository_deadline() -> float:
        return asyncio.get_running_loop().time() + _REPOSITORY_TIMEOUT_SECONDS

    @staticmethod
    async def _repository_call(
        operation: Callable[[], Awaitable[_T]],
        *,
        deadline: float,
    ) -> _T:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CredentialStateUnavailableError()
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except asyncio.CancelledError:
            raise
        except CredentialRepositoryConflictError:
            raise
        except Exception:
            # Repository/provider errors are deliberately value-free at the
            # vault boundary; they may otherwise contain query or ciphertext.
            raise CredentialStateUnavailableError() from None

    @staticmethod
    def _build_fernet(
        encryption_key: SecretStr | str | bytes | None,
    ) -> Fernet | None:
        if encryption_key is None:
            return None
        value = (
            encryption_key.get_secret_value()
            if isinstance(encryption_key, SecretStr)
            else encryption_key
        )
        if not isinstance(value, (str, bytes)) or not value:
            return None
        try:
            encoded = (
                value.encode("ascii", errors="strict")
                if isinstance(value, str)
                else value
            )
            return Fernet(encoded)
        except (TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _secret_text(secret: SecretStr) -> str:
        if not isinstance(secret, SecretStr):
            raise CredentialContractError()
        value = secret.get_secret_value()
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise CredentialContractError() from None
        if (
            not _MIN_SECRET_BYTES <= len(encoded) <= _MAX_SECRET_BYTES
            or b"\x00" in encoded
        ):
            raise CredentialContractError()
        return value

    async def create(
        self,
        *,
        user_id: str,
        provider: str,
        tool_name: str,
        slot: str,
        secret: SecretStr,
    ) -> CredentialView:
        fernet = self._fernet
        if fernet is None:
            raise CredentialVaultUnavailableError()
        secret_text = self._secret_text(secret)
        try:
            encrypted = fernet.encrypt(secret_text.encode("utf-8")).decode("ascii")
            record = CredentialRecord(
                user_id=user_id,
                provider=provider,
                tool_name=tool_name,
                slot=slot,
                encrypted_secret=encrypted,
            )
        except CredentialServiceError:
            raise
        except (TypeError, ValueError, UnicodeError, ValidationError):
            raise CredentialContractError() from None

        deadline = self._repository_deadline()
        try:
            persisted = await self._repository_call(
                lambda: self._repository.insert(record),
                deadline=deadline,
            )
        except CredentialRepositoryConflictError:
            raise CredentialBindingConflictError() from None
        return persisted.public_view()

    async def list_for_owner(
        self,
        user_id: str,
        *,
        limit: int = 200,
    ) -> list[CredentialView]:
        deadline = self._repository_deadline()
        records = await self._repository_call(
            lambda: self._repository.list_for_owner(
                user_id,
                limit=max(1, min(int(limit), 200)),
            ),
            deadline=deadline,
        )
        return [record.public_view() for record in records]

    async def revoke(
        self,
        user_id: str,
        reference: str,
    ) -> CredentialView | None:
        deadline = self._repository_deadline()
        for _ in range(_CAS_RETRIES):
            record = await self._repository_call(
                lambda: self._repository.find_for_owner(user_id, reference),
                deadline=deadline,
            )
            if record is None:
                return None
            if record.revoked:
                return record.public_view()
            updated = await self._repository_call(
                lambda: self._repository.compare_and_set_revoke(
                    user_id,
                    reference,
                    expected_revision=record.revision,
                ),
                deadline=deadline,
            )
            if updated is not None:
                return updated.public_view()
        raise CredentialStateConflictError()

    async def describe_bindings(
        self,
        user_id: str,
        tool_name: str,
        requirements: Sequence[CredentialRequirement | Mapping[str, Any]],
    ) -> list[CredentialView]:
        normalized = self._requirements(requirements)
        deadline = self._repository_deadline()
        views: list[CredentialView] = []
        for requirement in normalized:
            record = await self._repository_call(
                lambda: self._repository.find_active_binding(
                    user_id,
                    tool_name,
                    requirement.slot,
                ),
                deadline=deadline,
            )
            if (
                record is None
                or record.revoked
                or record.provider != requirement.provider
                or record.tool_name != tool_name
            ):
                raise CredentialBindingMissingError()
            views.append(record.public_view())
        return views

    async def resolve_bindings(
        self,
        user_id: str,
        tool_name: str,
        expected: Sequence[CredentialView | Mapping[str, Any]],
    ) -> dict[str, str]:
        views = self._expected_views(expected)
        if not views:
            return {}
        fernet = self._fernet
        if fernet is None:
            raise CredentialVaultUnavailableError()

        deadline = self._repository_deadline()
        resolved: dict[str, str] = {}
        for view in views:
            if view.tool_name != tool_name or view.revoked:
                raise CredentialBindingChangedError()
            record = await self._repository_call(
                lambda: self._repository.find_for_owner(
                    user_id,
                    view.reference,
                ),
                deadline=deadline,
            )
            if (
                record is None
                or record.revoked
                or record.revision != view.revision
                or record.provider != view.provider
                or record.tool_name != tool_name
                or record.slot != view.slot
                or record.encrypted_secret is None
            ):
                raise CredentialBindingChangedError()
            try:
                plaintext = fernet.decrypt(
                    record.encrypted_secret.encode("ascii", errors="strict")
                )
                if (
                    not _MIN_SECRET_BYTES <= len(plaintext) <= _MAX_SECRET_BYTES
                    or b"\x00" in plaintext
                ):
                    raise InvalidToken
                value = plaintext.decode("utf-8", errors="strict")
            except (InvalidToken, TypeError, ValueError, UnicodeError):
                raise CredentialVaultUnavailableError() from None
            resolved[view.slot] = value
        return resolved

    @staticmethod
    def _requirements(
        requirements: Sequence[CredentialRequirement | Mapping[str, Any]],
    ) -> tuple[CredentialRequirement, ...]:
        try:
            normalized = tuple(
                item
                if isinstance(item, CredentialRequirement)
                else CredentialRequirement.model_validate(item)
                for item in requirements
            )
        except (TypeError, ValidationError):
            raise CredentialContractError() from None
        slots = [item.slot for item in normalized]
        if len(slots) != len(set(slots)):
            raise CredentialContractError()
        return normalized

    @staticmethod
    def _expected_views(
        expected: Sequence[CredentialView | Mapping[str, Any]],
    ) -> tuple[CredentialView, ...]:
        try:
            normalized = tuple(
                item
                if isinstance(item, CredentialView)
                else CredentialView.model_validate(item)
                for item in expected
            )
        except (TypeError, ValidationError):
            raise CredentialContractError() from None
        slots = [item.slot for item in normalized]
        references = [item.reference for item in normalized]
        if len(slots) != len(set(slots)) or len(references) != len(set(references)):
            raise CredentialContractError()
        return normalized


__all__ = [
    "CredentialBindingChangedError",
    "CredentialBindingConflictError",
    "CredentialBindingMissingError",
    "CredentialContractError",
    "CredentialService",
    "CredentialServiceError",
    "CredentialStateConflictError",
    "CredentialStateUnavailableError",
    "CredentialVaultUnavailableError",
]
