"""Opt-in integration check in a fresh, disposable Mongo database.

Requires DATASEEK_REGRESSION_MONGO_URI. Never uses the application's database
name, sessions, users, model, Redis queue or output files. The randomly named
test database is removed in finally, including on a failed assertion.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from beanie import init_beanie
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from app.domain.models.event import DoneEvent, MessageEvent
from app.domain.models.input_admission import terminal_event_id
from app.domain.models.session import Session
from app.domain.services.input_delivery import InputDeliveryService
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument, SessionEventReservationDocument, UserDocument
from app.infrastructure.repositories.mongo_input_repository import MongoInputRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository


async def main():
    uri = os.environ.get("DATASEEK_REGRESSION_MONGO_URI")
    if not uri:
        raise SystemExit("Set DATASEEK_REGRESSION_MONGO_URI explicitly for this isolated integration check")
    database_name = "dataseek_regression_" + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    checks = []
    try:
        database = client[database_name]
        await init_beanie(database=database, document_models=[SessionDocument, SessionEventDocument,
                         SessionEventReservationDocument, UserDocument])
        sessions = MongoSessionRepository()
        inputs = MongoInputRepository(sessions)
        await UserDocument(user_id="fixture-user", fullname="Regression fixture", email="fixture@example.invalid").insert()
        await sessions.save(Session(id="fixture-session", user_id="fixture-user", agent_id="fixture-agent"))
        first_event = MessageEvent(id="fixture-input-1", role="user", message="Only a test, never execute tools")
        first = await inputs.accept("fixture-session", "fixture-user", first_event)
        duplicate = await inputs.accept("fixture-session", "fixture-user", first_event.model_copy(deep=True))
        assert first.key == duplicate.key and first.event.seq == duplicate.event.seq
        assert await database.session_events.count_documents({"session_id": "fixture-session"}) == 1
        assert await inputs.authorized(first)
        checks.append("atomic acceptance, duplicate retry and owner authorization")

        expires = datetime.now(UTC) + timedelta(seconds=30)
        races = await asyncio.gather(inputs.claim(first, "runtime-a", expires), inputs.claim(first, "runtime-b", expires))
        winners = [record for record in races if record is not None]
        assert len(winners) == 1
        claimed = winners[0]
        second = await inputs.accept("fixture-session", "fixture-user",
            MessageEvent(id="fixture-input-2", role="user", message="second fixture"))
        assert await inputs.claim(second, "runtime-c", expires) is None
        try:
            await inputs.transition(second, {"state": "claimed", "runtime_id": "runtime-c", "lease_expires_at": expires})
        except DuplicateKeyError:
            pass
        else:
            raise AssertionError("Mongo unique active-session index did not fence a concurrent input")
        checks.append("real Mongo CAS winner and unique active-session constraint")

        running = await inputs.transition(claimed, {"state": "running"}, require_live=True)
        await inputs.transition(running, {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        recovered = InputDeliveryService(inputs, sessions)
        dispatched = []

        async def dispatch(record):
            dispatched.append(record.key)

        await recovered.maintain(dispatch)
        await recovered.maintain(dispatch)
        interrupted = await inputs.get(first.session_id, first.key)
        assert interrupted.admission.state == "interrupted" and interrupted.admission.notified
        assert first.key not in dispatched and second.key in dispatched
        checks.append("expired running input is interrupted, never automatically replayed")

        pending = await inputs.get(second.session_id, second.key)
        claim = await inputs.claim(pending, "runtime-d", expires)
        running = await inputs.transition(claim, {"state": "running"}, require_live=True)
        terminal = DoneEvent(id=terminal_event_id(second.key, "done"))
        await sessions.add_event(second.session_id, terminal)
        await inputs.transition(running, {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        await recovered.maintain(dispatch)
        assert (await inputs.get(second.session_id, second.key)).admission.state == "completed"
        checks.append("committed terminal repairs a missing settlement after restart")

        await sessions.save(Session(id="history-fixture", user_id="fixture-user", agent_id="fixture-agent"))
        for index in range(24):
            await sessions.add_event("history-fixture", MessageEvent(id=f"history-{index}",
                role="user" if index % 2 == 0 else "assistant", message=f"fixture {index}"))
        newest = await sessions.get_history_page("history-fixture", turns=5)
        older = await sessions.get_history_page("history-fixture", turns=5, before_seq=newest.next_before_seq)
        oldest = await sessions.get_history_page("history-fixture", turns=5, before_seq=older.next_before_seq)
        assert [event.seq for event in oldest.events + older.events + newest.events] == list(range(1, 25))
        assert not oldest.has_more
        checks.append("real indexed history pages retain exact chronological replay")

        public = [event.model_dump(mode="json") for event in await sessions.get_events("fixture-session")]
        encoded = json.dumps(public)
        assert "input_admission" not in encoded and "actor_user_id" not in encoded and "runtime_id" not in encoded
        checks.append("private admission metadata never enters domain history")
    finally:
        # This name was generated locally and never accepts user input.
        assert database_name.startswith("dataseek_regression_") and len(database_name) == 52
        await client.drop_database(database_name)
        await client.close()
    print(json.dumps({"passed": len(checks), "checks": checks, "temporary_database_removed": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
