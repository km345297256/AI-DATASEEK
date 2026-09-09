import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.session import Session, SessionStatus
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository


def checkpoint(source=7):
    return {"id": "checkpoint", "owner_id": "owner", "source_seq": source,
            "expires_at": datetime.now(UTC) + timedelta(hours=1), "claimed_by": None}


class AtomicSessionCollection:
    def __init__(self):
        self.document = {"session_id": "session", "user_id": "owner",
                         "latest_user_input_fence_seq": 7, "latest_user_event_seq": 7,
                         "analysis_checkpoint": checkpoint()}
        self.lock = asyncio.Lock()
        self.updates = []

    def value(self, key):
        value = self.document
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def matches(self, query):
        for key, expected in query.items():
            if key == "$expr":
                left, right = expected["$lt"]
                if not self.value(left[1:]) < self.value(right[1:]):
                    return False
                continue
            value = self.value(key)
            if isinstance(expected, dict):
                if "$gt" in expected and (value is None or value <= expected["$gt"]):
                    return False
                if "$in" in expected and value not in expected["$in"]:
                    return False
            elif value != expected:
                return False
        return True

    def apply(self, update):
        self.updates.append(copy.deepcopy(update))
        for key, value in update.get("$max", {}).items():
            self.document[key] = max(self.document.get(key, 0), value)
        for key, value in update.get("$set", {}).items():
            target = self.document
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = copy.deepcopy(value)
        for key in update.get("$unset", {}):
            self.document.pop(key, None)

    async def find_one(self, query, *args, **kwargs):
        async with self.lock:
            return copy.deepcopy(self.document) if self.matches(query) else None

    async def update_one(self, query, update):
        async with self.lock:
            matched = self.matches(query)
            if matched:
                self.apply(update)
            return SimpleNamespace(matched_count=int(matched))

    async def find_one_and_update(self, query, update, **kwargs):
        async with self.lock:
            if not self.matches(query):
                return None
            self.apply(update)
            return copy.deepcopy(self.document)


@pytest.fixture
def repository(monkeypatch):
    collection = AtomicSessionCollection()
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", classmethod(lambda cls: collection))
    return MongoSessionRepository(), collection


@pytest.mark.asyncio
async def test_checkpoint_has_one_cross_runtime_claim_winner_and_same_claim_is_idempotent(repository):
    repo, collection = repository
    first, second = await asyncio.gather(
        repo.claim_analysis_checkpoint("session", "checkpoint", "owner", "first", expected_source_seq=7),
        MongoSessionRepository().claim_analysis_checkpoint("session", "checkpoint", "owner", "second", expected_source_seq=7),
    )
    assert sum(item is not None for item in [first, second]) == 1
    winner = (first or second)["claimed_by"]
    assert await repo.claim_analysis_checkpoint("session", "checkpoint", "owner", winner, expected_source_seq=7)
    assert collection.document["analysis_checkpoint"]["claimed_by"] == winner


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "fence", "durable", "owner", "expired", "legacy"])
async def test_stale_foreign_expired_or_unfenced_checkpoint_is_not_claimed(repository, change):
    repo, collection = repository
    if change == "source":
        collection.document["analysis_checkpoint"]["source_seq"] = 6
    elif change in {"fence", "durable"}:
        collection.document["latest_user_input_fence_seq" if change == "fence" else "latest_user_event_seq"] = 8
    elif change == "owner":
        collection.document["analysis_checkpoint"]["owner_id"] = "other"
    elif change == "expired":
        collection.document["analysis_checkpoint"]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    else:
        collection.document.pop("latest_user_input_fence_seq")
    assert await repo.claim_analysis_checkpoint("session", "checkpoint", "owner", "resume", expected_source_seq=7) is None


@pytest.mark.asyncio
async def test_checkpoint_save_cannot_overwrite_after_new_input_started(repository):
    repo, collection = repository
    newer = {**checkpoint(), "id": "new-checkpoint"}
    await repo.save_analysis_checkpoint("session", newer)
    await repo._mark_user_event_progress("session", MessageEvent(role="user", message="new", seq=8), durable=False)
    with pytest.raises(ValueError):
        await repo.save_analysis_checkpoint("session", checkpoint())
    assert collection.document["analysis_checkpoint"]["id"] == "new-checkpoint"


@pytest.mark.asyncio
async def test_late_cleanup_does_not_erase_new_current_checkpoint(repository):
    repo, collection = repository
    event = MessageEvent(role="user", message="new", seq=8)
    await repo._mark_user_event_progress("session", event, durable=False)
    await repo._mark_user_event_progress("session", event, durable=True)
    await repo.clear_analysis_checkpoint("session")
    assert "analysis_checkpoint" not in collection.document
    await repo.save_analysis_checkpoint("session", {**checkpoint(8), "id": "new-checkpoint"})
    await repo.clear_analysis_checkpoint("session")
    assert collection.document["analysis_checkpoint"]["id"] == "new-checkpoint"


@pytest.mark.asyncio
async def test_execution_recheck_is_bound_to_claim_source_and_latest_resume_input(repository):
    repo, collection = repository
    await repo.claim_analysis_checkpoint("session", "checkpoint", "owner", "resume", expected_source_seq=7)
    event = MessageEvent(role="user", message="continue", seq=10)
    await repo._mark_user_event_progress("session", event, durable=False)
    await repo._mark_user_event_progress("session", event, durable=True)
    params = dict(source_seq=7, resume_event_seq=10)
    assert await repo.is_analysis_checkpoint_current("session", "checkpoint", "owner", "resume", **params)
    assert not await repo.is_analysis_checkpoint_current("session", "checkpoint", "owner", "other", **params)
    await repo._mark_user_event_progress("session", MessageEvent(role="user", message="new", seq=11), durable=False)
    assert not await repo.is_analysis_checkpoint_current("session", "checkpoint", "owner", "resume", **params)


@pytest.mark.asyncio
async def test_user_event_commit_gap_is_fenced_before_durable_insert(repository, monkeypatch):
    repo, collection = repository
    entered, release = asyncio.Event(), asyncio.Event()

    class Events:
        async def find_one(self, *args, **kwargs):
            return None

        async def find_one_and_update(self, query, update, **kwargs):
            entered.set()
            await release.wait()
            return update["$setOnInsert"]

    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: Events()))
    repo.reserve_event_sequence = AsyncMock(return_value=8)
    event = MessageEvent(id="new-input", role="user", message="new task", seq=8)
    write = asyncio.create_task(repo.add_event("session", event))
    await entered.wait()
    assert collection.document["latest_user_input_fence_seq"] == 8
    assert collection.document["latest_user_event_seq"] == 7
    assert await repo.claim_analysis_checkpoint("session", "checkpoint", "owner", "resume", expected_source_seq=7) is None
    release.set()
    await write
    assert collection.document["latest_user_event_seq"] == 8


@pytest.mark.asyncio
async def test_failed_event_write_leaves_conservative_fence_and_idempotent_retry_recovers(repository, monkeypatch):
    repo, collection = repository
    event = MessageEvent(id="new-input", role="user", message="new task", seq=8)
    repo.reserve_event_sequence = AsyncMock(return_value=8)
    events = SimpleNamespace(find_one=AsyncMock(return_value=None),
                             find_one_and_update=AsyncMock(side_effect=RuntimeError("commit failed")))
    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: events))
    with pytest.raises(RuntimeError):
        await repo.add_event("session", event)
    assert collection.document["latest_user_input_fence_seq"] == 8
    assert collection.document["latest_user_event_seq"] == 7
    assert await repo.claim_analysis_checkpoint("session", "checkpoint", "owner", "resume", expected_source_seq=7) is None
    # Model the retry discovering a durable identical event after an uncertain write.
    events.find_one.return_value = {"seq": 8, "event": event.model_dump(),
                                   "payload_digest": repo._event_payload_digest(event)}
    await repo.add_event("session", event)
    assert collection.document["latest_user_event_seq"] == 8
    assert events.find_one_and_update.await_count == 1


@pytest.mark.asyncio
async def test_out_of_order_retries_cannot_roll_back_markers_and_assistant_events_do_not_advance_them(repository):
    repo, collection = repository
    for seq in [12, 8, 10]:
        event = MessageEvent(role="user", message="user", seq=seq)
        await repo._mark_user_event_progress("session", event, durable=False)
        await repo._mark_user_event_progress("session", event, durable=True)
    await repo._mark_user_event_progress("session", MessageEvent(role="assistant", message="answer", seq=20), durable=True)
    assert collection.document["latest_user_input_fence_seq"] == 12
    assert collection.document["latest_user_event_seq"] == 12


@pytest.mark.asyncio
async def test_storage_only_checkpoint_and_fences_are_not_reset_by_session_save(repository, monkeypatch):
    repo, collection = repository
    monkeypatch.setattr(SessionDocument, "find_one", AsyncMock(return_value=SimpleNamespace(title_manually_set=False)))
    session = Session(id="session", user_id="owner", agent_id="agent", status=SessionStatus.PENDING)
    await repo.save(session)
    latest_write = collection.updates[-1]["$set"]
    for name in ["analysis_checkpoint", "latest_user_event_seq", "latest_user_input_fence_seq"]:
        assert name not in latest_write
        assert name not in session.model_dump()
        assert name not in Session.model_json_schema()["properties"]
