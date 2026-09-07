from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.domain.services.credential_service as credential_service_module
from app.domain.models.credential import CredentialRecord, CredentialView
from app.domain.repositories.credential_repository import (
    CredentialRepositoryConflictError,
)
from app.domain.services.credential_service import (
    CredentialBindingChangedError,
    CredentialBindingConflictError,
    CredentialBindingMissingError,
    CredentialContractError,
    CredentialService,
    CredentialStateUnavailableError,
    CredentialVaultUnavailableError,
)
from app.infrastructure.external.credential_factory import get_credential_service
from app.interfaces.api.credential_routes import (
    get_credential_declaration_verifier,
    router as credential_router,
)
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


class InMemoryCredentialRepository:
    def __init__(self) -> None:
        self.records: dict[str, CredentialRecord] = {}
        self._lock = asyncio.Lock()

    async def insert(self, record: CredentialRecord) -> CredentialRecord:
        async with self._lock:
            if any(
                current.user_id == record.user_id
                and current.tool_name == record.tool_name
                and current.slot == record.slot
                and not current.revoked
                for current in self.records.values()
            ):
                raise CredentialRepositoryConflictError()
            if record.reference in self.records:
                raise CredentialRepositoryConflictError()
            stored = record.model_copy(deep=True)
            self.records[stored.reference] = stored
            return stored.model_copy(deep=True)

    async def find_for_owner(
        self,
        user_id: str,
        reference: str,
    ) -> CredentialRecord | None:
        record = self.records.get(reference)
        if record is None or record.user_id != user_id:
            return None
        return record.model_copy(deep=True)

    async def find_active_binding(
        self,
        user_id: str,
        tool_name: str,
        slot: str,
    ) -> CredentialRecord | None:
        for record in self.records.values():
            if (
                record.user_id == user_id
                and record.tool_name == tool_name
                and record.slot == slot
                and not record.revoked
            ):
                return record.model_copy(deep=True)
        return None

    async def list_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
    ) -> list[CredentialRecord]:
        records = [
            record.model_copy(deep=True)
            for record in self.records.values()
            if record.user_id == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.reference), reverse=True)
        return records[:limit]

    async def compare_and_set_revoke(
        self,
        user_id: str,
        reference: str,
        *,
        expected_revision: int,
    ) -> CredentialRecord | None:
        async with self._lock:
            record = self.records.get(reference)
            if (
                record is None
                or record.user_id != user_id
                or record.revoked
                or record.revision != expected_revision
            ):
                return None
            updated = record.model_copy(update={
                "revision": record.revision + 1,
                "revoked": True,
                "revoked_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "encrypted_secret": None,
            })
            self.records[reference] = CredentialRecord.model_validate(updated)
            return self.records[reference].model_copy(deep=True)


class BlockingCredentialRepository(InMemoryCredentialRepository):
    def __init__(self) -> None:
        super().__init__()
        self.block: str | None = None

    async def _wait_if_blocked(self, operation: str) -> None:
        if self.block == operation:
            await asyncio.Event().wait()

    async def insert(self, record: CredentialRecord) -> CredentialRecord:
        await self._wait_if_blocked("insert")
        return await super().insert(record)

    async def find_for_owner(
        self,
        user_id: str,
        reference: str,
    ) -> CredentialRecord | None:
        await self._wait_if_blocked("find_for_owner")
        return await super().find_for_owner(user_id, reference)

    async def find_active_binding(
        self,
        user_id: str,
        tool_name: str,
        slot: str,
    ) -> CredentialRecord | None:
        await self._wait_if_blocked("find_active_binding")
        return await super().find_active_binding(user_id, tool_name, slot)

    async def list_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
    ) -> list[CredentialRecord]:
        await self._wait_if_blocked("list_for_owner")
        return await super().list_for_owner(user_id, limit=limit)


def service(
    repository: InMemoryCredentialRepository | None = None,
    *,
    key: bytes | str | None = None,
) -> tuple[CredentialService, InMemoryCredentialRepository, bytes]:
    repository = repository or InMemoryCredentialRepository()
    actual_key = Fernet.generate_key() if key is None else key
    return (
        CredentialService(repository, encryption_key=actual_key),
        repository,
        actual_key if isinstance(actual_key, bytes) else actual_key.encode(),
    )


async def create(
    vault: CredentialService,
    *,
    user_id: str = "user-1",
    provider: str = "deepseek",
    tool_name: str = "dataset_analysis",
    slot: str = "api_key",
    secret: str = "secret-value",
) -> CredentialView:
    return await vault.create(
        user_id=user_id,
        provider=provider,
        tool_name=tool_name,
        slot=slot,
        secret=SecretStr(secret),
    )


@pytest.mark.asyncio
async def test_secret_is_fernet_encrypted_and_only_public_view_leaves_service():
    vault, repository, key = service()
    view = await create(vault)

    record = repository.records[view.reference]
    assert view.model_dump().keys() == CredentialView.model_fields.keys()
    assert "encrypted_secret" not in view.model_dump()
    assert "secret-value" not in record.encrypted_secret
    assert Fernet(key).decrypt(record.encrypted_secret.encode()).decode() == "secret-value"
    assert await vault.resolve_bindings("user-1", "dataset_analysis", [view]) == {
        "api_key": "secret-value"
    }


@pytest.mark.asyncio
async def test_active_binding_is_unique_under_concurrent_create_and_reusable_after_revoke():
    vault, repository, _ = service()
    outcomes = await asyncio.gather(
        create(vault, secret="first-secret"),
        create(vault, secret="second-secret"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CredentialView) for item in outcomes) == 1
    assert sum(isinstance(item, CredentialBindingConflictError) for item in outcomes) == 1

    active = next(item for item in outcomes if isinstance(item, CredentialView))
    revoked = await vault.revoke("user-1", active.reference)
    assert revoked is not None and revoked.revoked is True and revoked.revision == 2
    assert repository.records[active.reference].encrypted_secret is None
    assert await vault.revoke("user-1", active.reference) == revoked

    replacement = await create(vault, secret="replacement-secret")
    assert replacement.reference != active.reference


@pytest.mark.asyncio
async def test_owner_isolation_applies_to_listing_revoke_describe_and_resolve():
    vault, _, _ = service()
    view = await create(vault, user_id="owner")

    assert await vault.list_for_owner("other") == []
    assert await vault.revoke("other", view.reference) is None
    with pytest.raises(CredentialBindingMissingError):
        await vault.describe_bindings(
            "other",
            "dataset_analysis",
            [{"provider": "deepseek", "slot": "api_key"}],
        )
    with pytest.raises(CredentialBindingChangedError):
        await vault.resolve_bindings("other", "dataset_analysis", [view])


@pytest.mark.asyncio
async def test_describe_requires_exact_provider_and_preserves_requirement_order():
    vault, _, _ = service()
    first = await create(vault, slot="api_key")
    second = await create(vault, provider="storage", slot="access_token", secret="token-value")

    described = await vault.describe_bindings(
        "user-1",
        "dataset_analysis",
        [
            {"provider": "storage", "slot": "access_token"},
            {"provider": "deepseek", "slot": "api_key"},
        ],
    )
    assert described == [second, first]
    with pytest.raises(CredentialBindingMissingError):
        await vault.describe_bindings(
            "user-1",
            "dataset_analysis",
            [{"provider": "other", "slot": "api_key"}],
        )


@pytest.mark.asyncio
async def test_resolve_revalidates_revision_active_provider_tool_and_slot():
    vault, repository, _ = service()
    view = await create(vault)

    changed = view.model_copy(update={"revision": view.revision + 1})
    with pytest.raises(CredentialBindingChangedError):
        await vault.resolve_bindings("user-1", "dataset_analysis", [changed])
    with pytest.raises(CredentialBindingChangedError):
        await vault.resolve_bindings("user-1", "other_tool", [view])

    await vault.revoke("user-1", view.reference)
    with pytest.raises(CredentialBindingChangedError):
        await vault.resolve_bindings("user-1", "dataset_analysis", [view])
    assert repository.records[view.reference].revoked is True


@pytest.mark.asyncio
async def test_missing_or_invalid_vault_key_only_blocks_creation_and_decryption():
    configured, repository, _ = service()
    view = await create(configured)

    missing = CredentialService(repository, encryption_key=None)
    invalid = CredentialService(repository, encryption_key="not-a-fernet-key")
    assert missing.configured is False and invalid.configured is False
    assert await missing.list_for_owner("user-1") == [view]
    with pytest.raises(CredentialVaultUnavailableError):
        await missing.create(
            user_id="user-1",
            provider="deepseek",
            tool_name="other_tool",
            slot="api_key",
            secret=SecretStr("another-secret"),
        )
    with pytest.raises(CredentialVaultUnavailableError):
        await invalid.resolve_bindings("user-1", "dataset_analysis", [view])
    revoked = await missing.revoke("user-1", view.reference)
    assert revoked is not None and revoked.revoked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["short", "valid\x00secret", "界" * 2731])
async def test_secret_utf8_byte_limits_are_enforced_with_fixed_error(secret):
    vault, _, _ = service()
    with pytest.raises(CredentialContractError) as captured:
        await create(vault, secret=secret)
    assert str(captured.value) == "credential_contract_invalid"
    assert secret not in str(captured.value)


@pytest.mark.asyncio
async def test_invalid_requirements_and_expected_duplicates_fail_closed():
    vault, _, _ = service()
    view = await create(vault)
    with pytest.raises(CredentialContractError):
        await vault.describe_bindings(
            "user-1",
            "dataset_analysis",
            [
                {"provider": "deepseek", "slot": "api_key"},
                {"provider": "deepseek", "slot": "api_key"},
            ],
        )
    with pytest.raises(CredentialContractError):
        await vault.resolve_bindings("user-1", "dataset_analysis", [view, view])


@pytest.mark.asyncio
async def test_all_repository_entry_points_have_a_fixed_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        credential_service_module,
        "_REPOSITORY_TIMEOUT_SECONDS",
        0.01,
    )
    repository = BlockingCredentialRepository()
    vault, _, _ = service(repository)
    expected = CredentialView(
        reference="cred_" + "a" * 32,
        provider="deepseek",
        tool_name="dataset_analysis",
        slot="api_key",
    )

    operations = [
        (
            "insert",
            lambda: create(vault, secret="private-secret-value"),
        ),
        ("list_for_owner", lambda: vault.list_for_owner("user-1")),
        (
            "find_for_owner",
            lambda: vault.revoke("user-1", expected.reference),
        ),
        (
            "find_active_binding",
            lambda: vault.describe_bindings(
                "user-1",
                "dataset_analysis",
                [{"provider": "deepseek", "slot": "api_key"}],
            ),
        ),
        (
            "find_for_owner",
            lambda: vault.resolve_bindings(
                "user-1",
                "dataset_analysis",
                [expected],
            ),
        ),
    ]
    for operation, invoke in operations:
        repository.block = operation
        with pytest.raises(CredentialStateUnavailableError) as captured:
            await invoke()
        assert str(captured.value) == "credential_state_unavailable"
        assert "private-secret-value" not in str(captured.value)


@pytest.mark.asyncio
async def test_multiple_binding_queries_share_one_repository_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    class DelayedRepository(InMemoryCredentialRepository):
        async def find_active_binding(
            self,
            user_id: str,
            tool_name: str,
            slot: str,
        ) -> CredentialRecord | None:
            await asyncio.sleep(0.02)
            return await super().find_active_binding(user_id, tool_name, slot)

    repository = DelayedRepository()
    vault, _, _ = service(repository)
    await create(vault, slot="api_key")
    await create(
        vault,
        provider="storage",
        slot="access_token",
        secret="storage-token",
    )
    monkeypatch.setattr(
        credential_service_module,
        "_REPOSITORY_TIMEOUT_SECONDS",
        0.03,
    )

    with pytest.raises(CredentialStateUnavailableError):
        await vault.describe_bindings(
            "user-1",
            "dataset_analysis",
            [
                {"provider": "deepseek", "slot": "api_key"},
                {"provider": "storage", "slot": "access_token"},
            ],
        )


def test_credential_routes_require_write_intent_and_enforce_owner_scope():
    vault, _, _ = service()
    owner = {"id": "owner-a"}
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(credential_router)
    app.dependency_overrides[get_credential_service] = lambda: vault
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=owner["id"]
    )
    app.dependency_overrides[get_credential_declaration_verifier] = (
        lambda: (lambda provider, tool_name, slot: True)
    )
    payload = {
        "provider": "deepseek",
        "tool_name": "dataset_analysis",
        "slot": "api_key",
        "secret": "route-private-secret",
    }

    with TestClient(app) as client:
        rejected = client.post("/credentials", json=payload)
        assert rejected.status_code == 403
        assert payload["secret"] not in rejected.text

        created = client.post(
            "/credentials",
            json=payload,
            headers={"X-Credential-Action": "create"},
        )
        assert created.status_code == 200
        assert payload["secret"] not in created.text
        assert "encrypted_secret" not in created.text
        reference = created.json()["data"]["reference"]

        listed = client.get("/credentials")
        assert listed.status_code == 200
        assert listed.json()["data"]["configured"] is True
        assert [item["reference"] for item in listed.json()["data"]["credentials"]] == [reference]

        owner["id"] = "owner-b"
        assert client.get("/credentials").json()["data"]["credentials"] == []
        hidden = client.post(
            f"/credentials/{reference}/revoke",
            headers={"X-Credential-Action": "revoke"},
        )
        assert hidden.status_code == 404

        owner["id"] = "owner-a"
        missing_intent = client.post(f"/credentials/{reference}/revoke")
        assert missing_intent.status_code == 403
        revoked = client.post(
            f"/credentials/{reference}/revoke",
            headers={"X-Credential-Action": "revoke"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["data"]["revoked"] is True
