#!/usr/bin/env python3
"""
Copy legacy GridFS files to MinIO and update session file references.

Usage:
  cd /path/to/AI-DataSeek
  FILE_STORAGE_PROVIDER=hybrid uv run --project backend python scripts/migrate_gridfs_to_minio.py --dry-run
  FILE_STORAGE_PROVIDER=hybrid uv run --project backend python scripts/migrate_gridfs_to_minio.py

The script does not delete GridFS files. It is safe to run repeatedly: files
already copied with matching legacy_gridfs_id metadata are reused.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("API_KEY", "migration-not-used")

from beanie import init_beanie  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage  # noqa: E402
from app.infrastructure.external.file.miniofile import MinIOFileStorage  # noqa: E402
from app.infrastructure.models.documents import SessionDocument, StoredFileDocument  # noqa: E402
from app.infrastructure.storage.mongodb import get_mongodb  # noqa: E402


async def migrate(dry_run: bool) -> None:
    settings = get_settings()
    await get_mongodb().initialize()
    await init_beanie(
        database=get_mongodb().client[settings.mongodb_database],
        document_models=[SessionDocument, StoredFileDocument],
    )

    gridfs = GridFSFileStorage(mongodb=get_mongodb())
    minio = MinIOFileStorage()
    files_collection = gridfs._get_files_collection()
    gridfs_files = await files_collection.find({}).to_list(length=None)

    migrated: Dict[str, str] = {}
    copied = 0
    reused = 0

    for gridfs_doc in gridfs_files:
        legacy_id = str(gridfs_doc["_id"])
        existing = await StoredFileDocument.find_one({"metadata.legacy_gridfs_id": legacy_id})
        if existing:
            migrated[legacy_id] = existing.file_id
            reused += 1
            continue

        metadata = gridfs_doc.get("metadata", {}) or {}
        user_id = metadata.get("user_id") or "unknown"
        filename = gridfs_doc.get("filename") or f"file_{legacy_id}"
        content_type = metadata.get("contentType")

        if dry_run:
            print(f"[dry-run] would copy {legacy_id} {filename} user={user_id} size={gridfs_doc.get('length', 0)}")
            continue

        stream, _ = await gridfs.download_file(legacy_id)
        try:
            info = await minio.upload_file(
                stream,
                filename,
                user_id,
                content_type,
                {**metadata, "legacy_gridfs_id": legacy_id, "source_provider": "gridfs"},
            )
        finally:
            if hasattr(stream, "close"):
                stream.close()
        migrated[legacy_id] = info.file_id
        copied += 1
        print(f"[copied] {legacy_id} -> {info.file_id} {filename}")

    updated_sessions = 0
    if not dry_run and migrated:
        sessions = await SessionDocument.find().to_list(length=None)
        for session in sessions:
            changed = False
            for file_info in session.files:
                if file_info.file_id in migrated:
                    file_info.file_id = migrated[file_info.file_id]
                    changed = True
            if changed:
                await session.save()
                updated_sessions += 1

    print(
        f"done dry_run={dry_run} gridfs_files={len(gridfs_files)} copied={copied} "
        f"reused={reused} updated_sessions={updated_sessions}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
