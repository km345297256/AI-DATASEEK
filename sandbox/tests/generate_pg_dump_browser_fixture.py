"""Regenerate inert frontend fixtures from the committed native pg_dump files."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "sandbox"))
from app.services.pg_dump_reader import pg_dump_preview

fixtures = root / "sandbox/tests/fixtures/database-dumps"
outputs = {
    "treeCustom": pg_dump_preview((fixtures / "synthetic-postgres-gzip.dump").read_bytes(), "dump"),
    "treeTar": pg_dump_preview((fixtures / "synthetic-postgres.tar").read_bytes(), "tar"),
    "treeIdentifiers": pg_dump_preview((fixtures / "synthetic-postgres-identifiers.dump").read_bytes(), "backup"),
}
target = root / "frontend/tests/browser/pg-dump-data.json"
target.write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("Native PostgreSQL browser fixture generated.")
