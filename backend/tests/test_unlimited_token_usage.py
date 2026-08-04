import pytest
from langchain.messages import AIMessage

from app.domain.models.user import UserRole
from app.domain.services.token_quota_service import DEFAULT_ROLE_TOKEN_QUOTAS, TokenQuotaService
from app.domain.services.token_usage_service import TokenUsageService
from app.infrastructure.models.documents import TokenUsageDocument


@pytest.mark.asyncio
async def test_task_execution_is_not_blocked_by_legacy_user_quota(monkeypatch):
    async def fail_find_one(*args, **kwargs):
        raise AssertionError("unlimited execution must not query a user balance")

    monkeypatch.setattr(
        "app.domain.services.token_quota_service.UserDocument.find_one",
        fail_find_one,
    )

    assert await TokenQuotaService().ensure_user_can_run_task("legacy-user-with-zero-balance") is None


@pytest.mark.asyncio
async def test_usage_is_recorded_without_deducting_a_balance(monkeypatch):
    inserted = []

    class FakeUsageDocument:
        def __init__(self, record):
            self.record = record

        async def insert(self):
            inserted.append(self.record)
            return self

        def to_domain(self):
            return self.record

    def fake_from_domain(record):
        return FakeUsageDocument(record)

    async def fail_consume(*args, **kwargs):
        raise AssertionError("usage recording must not deduct a token balance")

    monkeypatch.setattr(TokenUsageDocument, "from_domain", fake_from_domain)
    monkeypatch.setattr(TokenQuotaService, "consume_user_tokens", fail_consume)

    result = await TokenUsageService().record_from_message(
        AIMessage(
            content="done",
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            },
        ),
        user_id="legacy-user-with-zero-balance",
        session_id="session-1",
        model_provider="openai",
        model_name="test-model",
    )

    assert len(inserted) == 1
    assert result is not None
    assert result.total_tokens == 20
    assert result.user_id == "legacy-user-with-zero-balance"


def test_every_legacy_role_defaults_to_unlimited():
    for role in UserRole:
        assert DEFAULT_ROLE_TOKEN_QUOTAS[role] == {
            "initial_tokens": None,
            "daily_refill_tokens": None,
        }
