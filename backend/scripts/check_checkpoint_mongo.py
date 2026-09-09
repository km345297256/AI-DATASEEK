"""Opt-in checkpoint concurrency checks in one newly created disposable Mongo DB.

Requires DATASEEK_REGRESSION_MONGO_URI. The database name is generated internally,
never taken from application settings or caller input. No model, Redis queue,
sandbox, production collection, or real user data is used. Output contains only
check counts and whether the isolated database was removed.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beanie import init_beanie
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.domain.models.event import MessageEvent
from app.domain.models.session import Session
from app.infrastructure.models.documents import (
    SessionDocument, SessionEventDocument, SessionEventReservationDocument,
)
from app.infrastructure.repositories.mongo_input_repository import MongoInputRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository


class PausedFenceRepository(MongoSessionRepository):
    """Pause only between real Mongo fence and real immutable-event writes."""

    def __init__(self):
        self.fenced = asyncio.Event()
        self.release = asyncio.Event()

    async def _mark_user_event_progress(self, session_id, event, *, durable):
        await super()._mark_user_event_progress(session_id, event, durable=durable)
        if not durable:
            self.fenced.set()
            await self.release.wait()


class FailedMarkerRepository(MongoSessionRepository):
    """Inject one process-side failure, leaving all Mongo operations real."""

    def __init__(self, *, after_commit):
        self.after_commit = after_commit
        self.inject = True

    async def _mark_user_event_progress(self, session_id, event, *, durable):
        if self.inject and durable and self.after_commit:
            self.inject = False
            raise RuntimeError("Injected durable marker interruption")
        await super()._mark_user_event_progress(session_id, event, durable=durable)
        if self.inject and not durable and not self.after_commit:
            self.inject = False
            raise RuntimeError("Injected pre-commit interruption")


def fixture_checkpoint(source_seq):
    return {"id": uuid4().hex, "owner_id": "fixture-owner", "source_seq": source_seq,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5), "claimed_by": None}


async def check_database(database, progress):
    await init_beanie(database=database, document_models=[
        SessionDocument, SessionEventDocument, SessionEventReservationDocument,
    ])
    sessions, peer = MongoSessionRepository(), MongoSessionRepository()
    inputs = MongoInputRepository(sessions)
    sid, owner = "fixture-session", "fixture-owner"
    await sessions.save(Session(id=sid, user_id=owner, agent_id="fixture-agent"))

    async def markers():
        result = await database.sessions.find_one({"session_id": sid}, {
            "latest_user_input_fence_seq": 1, "latest_user_event_seq": 1,
        })
        return result["latest_user_input_fence_seq"], result["latest_user_event_seq"]

    def passed():
        progress["passed"] += 1

    original = MessageEvent(id="source", role="user", message="Synthetic fixture only")
    first = await inputs.accept(sid, owner, original)
    duplicate = await inputs.accept(sid, owner, original.model_copy(deep=True))
    source = first.event.seq
    assert first.key == duplicate.key and source == duplicate.event.seq
    assert await markers() == (source, source)
    assert await database.session_events.count_documents({"session_id": sid}) == 1
    passed()

    checkpoint = fixture_checkpoint(source)
    await sessions.save_analysis_checkpoint(sid, checkpoint)
    assert (await peer.get_analysis_checkpoint(sid, checkpoint["id"]))["source_seq"] == source
    passed()

    claims = await asyncio.gather(*[
        repo.claim_analysis_checkpoint(sid, checkpoint["id"], owner, client_id,
                                       expected_source_seq=source)
        for repo, client_id in [(sessions, "client-a"), (peer, "client-b")]
    ])
    winners = [value for value in claims if value is not None]
    assert len(winners) == 1
    winner = winners[0]["claimed_by"]
    loser = "client-b" if winner == "client-a" else "client-a"
    assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], owner, winner,
                                                expected_source_seq=source)
    assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], owner, loser,
                                                expected_source_seq=source) is None
    passed()

    assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], "foreign-owner", winner,
                                                expected_source_seq=source) is None
    assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], owner, winner,
                                                expected_source_seq=source + 1) is None
    assert not await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, winner,
                                                         source_seq=source, resume_event_seq=source + 1)
    passed()

    resume = MessageEvent(id="resume", role="user", message="Synthetic continuation",
                          metadata={"client_message_id": winner, "resume_from": checkpoint["id"]})
    accepted = await inputs.accept(sid, owner, resume)
    resume_seq = accepted.event.seq
    params = {"source_seq": source, "resume_event_seq": resume_seq}
    assert resume_seq > source and await markers() == (resume_seq, resume_seq)
    assert await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, winner, **params)
    assert not await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, loser, **params)
    passed()

    same = await inputs.accept(sid, owner, resume.model_copy(deep=True))
    assert same.key == accepted.key and same.event.seq == resume_seq
    assert await database.session_events.count_documents({"session_id": sid}) == 2
    assert await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, winner, **params)
    # After acceptance, input idempotency owns reconnects; an old checkpoint
    # cannot be claimed afresh against a superseded source sequence.
    assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], owner, winner,
                                                expected_source_seq=source) is None
    passed()

    paused = PausedFenceRepository()
    newer = MessageEvent(id="newer", role="user", message="Synthetic new task")
    write = asyncio.create_task(MongoInputRepository(paused).accept(sid, owner, newer))
    try:
        await asyncio.wait_for(paused.fenced.wait(), timeout=5)
        assert await markers() == (newer.seq, resume_seq)
        assert await database.session_events.count_documents({"session_id": sid}) == 2
        assert not await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, winner, **params)
        assert await peer.claim_analysis_checkpoint(sid, checkpoint["id"], owner, loser,
                                                    expected_source_seq=source) is None
        try:
            await peer.save_analysis_checkpoint(sid, fixture_checkpoint(resume_seq))
        except ValueError:
            pass
        else:
            raise AssertionError("Stale checkpoint save was accepted in the pre-commit window")
        passed()
    finally:
        paused.release.set()
        await asyncio.wait_for(write, timeout=5)
    assert await markers() == (newer.seq, newer.seq)
    assert not await peer.is_analysis_checkpoint_current(sid, checkpoint["id"], owner, winner, **params)
    passed()

    await sessions.clear_analysis_checkpoint(sid)
    assert await peer.get_analysis_checkpoint(sid, checkpoint["id"]) is None
    current = fixture_checkpoint(newer.seq)
    await sessions.save_analysis_checkpoint(sid, current)
    await peer.clear_analysis_checkpoint(sid)
    assert await sessions.get_analysis_checkpoint(sid, current["id"])
    passed()

    # Cover both crash windows: no immutable event yet, and an already durable
    # event whose final marker was not written. Identical admission repairs
    # either case without promoting or duplicating the event.
    previous_seq = newer.seq
    for after_commit in (False, True):
        current = fixture_checkpoint(previous_seq)
        await sessions.save_analysis_checkpoint(sid, current)
        event = MessageEvent(id=f"interrupted-{after_commit}", role="user", message="Synthetic interruption")
        failing = MongoInputRepository(FailedMarkerRepository(after_commit=after_commit))
        count_before = await database.session_events.count_documents({"session_id": sid})
        try:
            await failing.accept(sid, owner, event)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Fault injection was not exercised")
        assert await markers() == (event.seq, previous_seq)
        assert await database.session_events.count_documents({"session_id": sid}) == count_before + int(after_commit)
        assert await peer.claim_analysis_checkpoint(sid, current["id"], owner, "client-next",
                                                    expected_source_seq=previous_seq) is None
        passed()
        repaired = await inputs.accept(sid, owner, event.model_copy(deep=True))
        assert repaired.event.seq == event.seq and await markers() == (event.seq, event.seq)
        assert await database.session_events.count_documents({"session_id": sid}) == count_before + 1
        passed()
        previous_seq = event.seq

    await inputs.accept(sid, owner, original.model_copy(deep=True))
    await sessions.add_event(sid, MessageEvent(id="assistant", role="assistant", message="Synthetic response"))
    await sessions.save(Session(id=sid, user_id=owner, agent_id="fixture-agent"))
    assert await markers() == (previous_seq, previous_seq)
    public = (await sessions.find_by_id(sid)).model_dump()
    assert not {"analysis_checkpoint", "latest_user_event_seq", "latest_user_input_fence_seq"} & public.keys()
    passed()

    expired = fixture_checkpoint(previous_seq)
    expired["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    await sessions.save_analysis_checkpoint(sid, expired)
    assert await peer.get_analysis_checkpoint(sid, expired["id"]) is None
    assert await peer.claim_analysis_checkpoint(sid, expired["id"], owner, "client-expired",
                                                expected_source_seq=previous_seq) is None
    passed()


async def main():
    logging.disable(logging.CRITICAL)
    uri = os.environ.get("DATASEEK_REGRESSION_MONGO_URI")
    if not uri:
        print(json.dumps({"passed": 0, "failed": True, "temporary_database_removed": False}))
        return 2
    database_name = "dataseek_checkpoint_test_" + uuid4().hex
    assert re.fullmatch(r"dataseek_checkpoint_test_[0-9a-f]{32}", database_name)
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    progress = {"passed": 0, "failed": False, "temporary_database_removed": False}
    owned = False
    owner_token = uuid4().hex

    async def isolated_database_exists():
        result = await client.admin.command({
            "listDatabases": 1, "nameOnly": True, "filter": {"name": database_name},
        })
        return bool(result["databases"])

    try:
        # Only query the internally generated database name; refuse to touch a
        # pre-existing database even in the exceedingly unlikely UUID collision.
        if await isolated_database_exists():
            raise RuntimeError("Disposable database already exists")
        database = client[database_name]
        marker = await database.create_collection("_regression_owner")
        owned = True
        await marker.insert_one({"_id": owner_token})
        async with asyncio.timeout(45):
            await check_database(database, progress)
    except Exception:
        # Tracebacks can contain connection URIs or test tokens. An exit status
        # and check count are sufficient for this opt-in deployment gate.
        progress["failed"] = True
    finally:
        try:
            if owned:
                assert re.fullmatch(r"dataseek_checkpoint_test_[0-9a-f]{32}", database_name)
                assert await client[database_name]["_regression_owner"].find_one({"_id": owner_token})
                await client.drop_database(database_name)
                assert not await isolated_database_exists()
                progress["temporary_database_removed"] = True
        except Exception:
            progress["failed"] = True
        await client.close()
    print(json.dumps(progress))
    return 1 if progress["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
