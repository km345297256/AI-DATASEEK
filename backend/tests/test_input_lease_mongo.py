"""Optional real-Mongo ownership CAS check; only a random disposable database."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from app.domain.models.event import MessageEvent, DoneEvent
from app.domain.models.input_admission import InputAdmission, input_key
from app.domain.services.input_delivery import InputDeliveryService, InputLeaseLost
from app.infrastructure.models.documents import SessionEventDocument
from app.infrastructure.repositories.mongo_input_repository import MongoInputRepository
from test_input_delivery import FakeTask


@pytest.mark.asyncio
async def test_real_mongo_expired_owner_renewal_and_reaper_have_only_one_cas_winner(monkeypatch):
    uri = os.environ.get('ANALYSIS_BUDGET_TEST_MONGODB_URI')
    if not uri:
        pytest.skip('Set ANALYSIS_BUDGET_TEST_MONGODB_URI for isolated real-Mongo verification')
    name = 'dataseek_input_lease_test_' + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        collection = client[name]['session_events']
        monkeypatch.setattr(SessionEventDocument, 'get_pymongo_collection', classmethod(lambda cls: collection))
        repository = MongoInputRepository(None)
        repository.authorized = AsyncMock(return_value=True)
        service = InputDeliveryService(repository, None)
        for index in range(8):
            event = MessageEvent(role='user', message='synthetic lease fixture', seq=1)
            sid, key = f'session-{index}', input_key(event)
            await collection.insert_one({'session_id': sid, 'producer_event_key': key,
                'seq': 1, 'event': event.model_dump(mode='json'),
                'input_admission': InputAdmission(actor_user_id='fixture-owner').model_dump()})
            record = await service.claim(await repository.get(sid, key))
            task = FakeTask(f'task-{index}')
            await service.bind(record, task)
            await service.start(sid, event, task)
            record = await repository.get(sid, key)
            record = await repository.transition(record, {'lease_expires_at': datetime.now(UTC) - timedelta(seconds=1)})
            renewal, reaped = await asyncio.gather(service._renew_local_lease(record),
                repository.transition(record, {'state': 'interrupted', 'notified': False}))
            assert (renewal is not None) != (reaped is not None)
            current = await repository.get(sid, key)
            assert current.admission.attempts == 1
            if renewal is not None:
                assert current.admission.state == 'running'
                await service.prepare_event(sid, key, DoneEvent())
                await repository.transition(current, {'state': 'cancelled'})
            with pytest.raises(InputLeaseLost):
                await service.prepare_event(sid, key, DoneEvent())
    finally:
        await client.drop_database(name)
        await client.close()
