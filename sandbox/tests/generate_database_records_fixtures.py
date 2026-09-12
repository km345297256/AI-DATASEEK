"""Regenerate only repository-owned synthetic database-records test artifacts.

Run with PYTHONPATH=sandbox and PyMongo 4.17.0. Redis fixture is explicitly a
wire fixture, not native-engine evidence; generate-native-redis.sh provides the
independent native RDB oracle when a one-off test container is authorized.
"""
from pathlib import Path
import hashlib
import json

from app.services.bson_reader import bson_preview
from app.services.redis_rdb_reader import redis_rdb_preview
from app.services.database_records_payload import group_id
from database_records_fixtures import bson_documents, empty_bson_document, redis_wire_fixture, rdb_file


def main():
    import pymongo
    if pymongo.version != "4.17.0": raise RuntimeError("Expected pinned PyMongo 4.17.0")
    repository = Path(__file__).resolve().parents[2]
    fixtures = repository / "sandbox/tests/fixtures/database-records"
    fixtures.mkdir(parents=True, exist_ok=True)
    artifacts = {"synthetic-pymongo-4.17.0.bson": bson_documents(), "empty-document.bson": empty_bson_document(),
                 "synthetic-rdb11-wire.rdb": redis_wire_fixture(), "empty-rdb11-wire.rdb": rdb_file()}
    for filename, data in artifacts.items(): (fixtures / filename).write_bytes(data)
    report = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                    "producer": "PyMongo 4.17.0 official BSON encoder" if name.endswith(".bson") else "project wire fixture; not native Redis evidence"} for name, data in artifacts.items()}
    native = (fixtures / "native-redis-7.2.7.rdb").read_bytes()
    report["native-redis-7.2.7.rdb"] = {"sha256": hashlib.sha256(native).hexdigest(), "bytes": len(native),
                                        "producer": "Redis 7.2.7 native SAVE + redis-check-rdb", "image_digest": "redis@sha256:96af50b9ce0cd7f44f73266ce90bb63a6d6d5655adb8854d2723a0382524c8f3"}
    (fixtures / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    records = {}
    for reader, function, data, empty, fmt, db in [
        ("bson", bson_preview, artifacts["synthetic-pymongo-4.17.0.bson"], artifacts["empty-document.bson"], "bson", None),
        ("redis-rdb", redis_rdb_preview, artifacts["synthetic-rdb11-wire.rdb"], artifacts["empty-rdb11-wire.rdb"], "rdb", 0),
    ]:
        records[reader] = {"tree": function(data, fmt),
                           "first": function(data, fmt, "table", {"group_id": group_id(reader, db), "offset": 0, "limit": 2}),
                           "second": function(data, fmt, "table", {"group_id": group_id(reader, db), "offset": 2, "limit": 2}),
                           "empty": function(empty, fmt),
                           "emptyPage": function(empty, fmt, "table", {"group_id": group_id(reader, db), "offset": 0, "limit": 2})}
    target = repository / "frontend/tests/browser/database-records-data.json"
    target.write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"artifacts": report, "browser_fixture": str(target.relative_to(repository))}, indent=2))


if __name__ == "__main__": main()
