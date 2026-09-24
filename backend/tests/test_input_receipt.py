"""Receipt reads are owner-scoped observations, never execution triggers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.agent_service import AgentService
from app.domain.models.event import MessageEvent
from app.domain.models.input_admission import AcceptedInput, InputAdmission, input_key
from app.domain.services.agent_domain_service import AgentDomainService
from app.interfaces.api.session_routes import get_input_receipt
from app.interfaces.schemas.session import InputReceiptResponse


def subject(record=None):
    service = object.__new__(AgentService)
    service.get_session = AsyncMock(return_value=SimpleNamespace(id="s"))
    service._input_repository = SimpleNamespace(get=AsyncMock(return_value=record), accept=AsyncMock())
    service._session_repository = SimpleNamespace(resolve_event_sequence=AsyncMock(return_value=None))
    return service


def record(state="pending", actor="u"):
    event = MessageEvent(id=AgentDomainService._client_message_event_id("s", "client"),
                         seq=12, role="user", message="private payload /host/secret",
                         metadata={"client_message_id": "client"})
    return AcceptedInput(session_id="s", key=input_key(event), event=event,
                         admission=InputAdmission(actor_user_id=actor, state=state))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "claimed", "running", "completed", "cancelled", "interrupted"])
async def test_receipt_reports_durable_state_without_private_payload_or_mutation(state):
    accepted = record(state)
    service = subject(accepted)
    response = await get_input_receipt("s", "client", SimpleNamespace(id="u"), service)
    assert response.data.model_dump() == {"client_message_id": "client", "accepted": True,
                                         "event_seq": 12, "state": state}
    assert "private" not in response.model_dump_json() and "/host" not in response.model_dump_json()
    service._input_repository.get.assert_awaited_once_with("s", accepted.key)
    service._input_repository.accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_observation_is_not_invented_rejection_and_late_commit_is_visible():
    service = subject()
    assert await service.get_input_receipt("s", "u", "client") == {"client_message_id": "client", "accepted": False}
    service._input_repository.get.return_value = record()
    assert (await service.get_input_receipt("s", "u", "client"))["accepted"]


@pytest.mark.asyncio
async def test_legacy_durable_event_counts_but_bare_claim_does_not():
    service = subject()
    service._session_repository.resolve_event_sequence.return_value = 7
    receipt = await service.get_input_receipt("s", "u", "client")
    assert receipt == {"client_message_id": "client", "accepted": True, "event_seq": 7}


@pytest.mark.asyncio
async def test_non_owner_never_reads_admission():
    service = subject(record())
    service.get_session.return_value = None
    with pytest.raises(NotFoundError):
        await service.get_input_receipt("s", "other", "client")
    service._input_repository.get.assert_not_awaited()
    service._session_repository.resolve_event_sequence.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_admission_actor_is_not_exposed():
    with pytest.raises(NotFoundError):
        await subject(record(actor="other")).get_input_receipt("s", "u", "client")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["", " ", "x" * 129, None])
async def test_invalid_identity_rejected_without_repository_access(identity):
    service = subject()
    with pytest.raises(BadRequestError):
        await service.get_input_receipt("s", "u", identity)
    service._input_repository.get.assert_not_awaited()
