"""Read-only deployment gate: print counts/digests, never user content or IDs."""
import asyncio
from collections import Counter
import hashlib
import json

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb


async def main():
    mongo = get_mongodb()
    await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        sessions = await database.sessions.find({}, {"_id": 0, "session_id": 1, "status": 1,
            "updated_at": 1, "dataset_ids": 1, "files": 1}).sort("session_id", 1).to_list()
        statuses = dict(Counter(item["status"] for item in sessions))
        pending_inputs = await database.session_events.count_documents({
            "input_admission.state": {"$in": ["pending", "claimed", "running"]}})
        safe = not any(statuses.get(status, 0) for status in ("running", "waiting", "pending")) and pending_inputs == 0
        state = {"safe_to_restart": safe, "session_status_counts": statuses, "active_inputs": pending_inputs,
                 "session_snapshot_sha256": hashlib.sha256(json.dumps(sessions, sort_keys=True, default=str).encode()).hexdigest()}
        for name in ("sessions", "session_events", "stored_files", "token_usage", "data_center_datasets",
                     "temporary_data_center_datasets", "agent_profiles", "model_traces"):
            state[name + "_count"] = await database[name].count_documents({})
        print(json.dumps(state, sort_keys=True))
        if not safe:
            raise SystemExit("An active session/input exists; do not restart services")
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
