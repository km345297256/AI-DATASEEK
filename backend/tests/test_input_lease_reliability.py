"""Clock jumps and delayed heartbeats must not kill an unchanged live owner."""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import DoneEvent, ErrorEvent
from app.domain.services.input_delivery import InputDeliveryService, InputLeaseLost
from test_input_delivery import FakeTask, MemoryInputs, expired, pending


async def owned_input():
    repository = MemoryInputs()
    service = InputDeliveryService(repository, repository)
    record = await service.claim(await pending(repository))
    task = FakeTask()
    repository.session.task_id = task.id
    await service.bind(record, task)
    await service.start('session', record.event, task)
    await expired(repository, record)
    return repository, service, record, task


@pytest.mark.asyncio
async def test_live_owner_renews_after_clock_jump_without_dispatch_or_interruption():
    repository, service, record, task = await owned_input()
    dispatch = AsyncMock()
    await service.maintain(dispatch)
    current = await repository.get('session', record.key)
    assert current.admission.state == 'running'
    assert current.admission.lease_expires_at > datetime.now(UTC)
    assert current.admission.attempts == record.admission.attempts
    assert not task.cancelled
    dispatch.assert_not_awaited()
    assert not any(isinstance(event, ErrorEvent) for event in repository.events)
    await service.prepare_event('session', record.key, DoneEvent())


@pytest.mark.asyncio
async def test_resuming_output_can_renew_same_owner_before_periodic_heartbeat():
    repository, service, record, _ = await owned_input()
    event = DoneEvent()
    await service.prepare_event('session', record.key, event)
    await repository.add_event('session', event)
    await service.complete('session', record.key, event)
    assert (await repository.get('session', record.key)).admission.state == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancelled', 'interrupted', 'runtime', 'task', 'authorization', 'no_local'])
async def test_expired_owner_never_revives_lost_or_revoked_authority(change):
    repository, service, record, _ = await owned_input()
    current = await repository.get('session', record.key)
    if change in {'cancelled', 'interrupted'}:
        await repository.transition(current, {'state': change})
    elif change == 'runtime':
        await repository.transition(current, {'runtime_id': 'other-runtime'})
    elif change == 'task':
        await repository.transition(current, {'task_id': 'different-task'})
    elif change == 'authorization':
        repository.allow = False
    else:
        service._local.clear()
    with pytest.raises(InputLeaseLost):
        await service.prepare_event('session', record.key, DoneEvent())


@pytest.mark.asyncio
async def test_competing_reaper_winning_cas_cannot_be_undone_by_local_renewal():
    repository, service, record, _ = await owned_input()
    original = repository.transition
    raced = False

    async def race(candidate, updates, **kwargs):
        nonlocal raced
        if not raced and 'lease_expires_at' in updates:
            raced = True
            await original(candidate, {'state': 'interrupted', 'notified': False})
        return await original(candidate, updates, **kwargs)

    repository.transition = race
    with pytest.raises(InputLeaseLost):
        await service.prepare_event('session', record.key, DoneEvent())
    assert (await repository.get('session', record.key)).admission.state == 'interrupted'


@pytest.mark.asyncio
async def test_no_longer_live_local_worker_is_not_renewed():
    repository, service, record, task = await owned_input()
    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    service._local[('session', record.key)].worker = completed
    task.done = True
    with pytest.raises(InputLeaseLost):
        await service.prepare_event('session', record.key, DoneEvent())


@pytest.mark.asyncio
@pytest.mark.parametrize('takeover', ['task', 'attempt', 'both'])
async def test_cas_refresh_cannot_accept_a_new_attempt_in_the_same_runtime(takeover):
    repository, service, record, _ = await owned_input()
    original = repository.transition
    raced = False

    async def race(candidate, updates, **kwargs):
        nonlocal raced
        if not raced and 'lease_expires_at' in updates:
            raced = True
            replacement = {'lease_expires_at': datetime.now(UTC) + timedelta(seconds=30)}
            if takeover in {'task', 'both'}:
                replacement['task_id'] = 'replacement-task'
            if takeover in {'attempt', 'both'}:
                replacement['attempts'] = candidate.admission.attempts + 1
            await original(candidate, replacement)
        return await original(candidate, updates, **kwargs)

    repository.transition = race
    with pytest.raises(InputLeaseLost):
        await service.prepare_event('session', record.key, DoneEvent())
    assert raced
