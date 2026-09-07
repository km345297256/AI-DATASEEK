"""Validate a reviewed local catalog, then explicitly apply registrations.

Run with the data root bind-mounted read-only at --inspection-root. The
--host-root is the SAME root in the Docker host namespace, not a container path.
Only existing files are used; this command never downloads or changes files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path, PurePosixPath
import socket
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from beanie import init_beanie
import docker
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.core.config import get_settings
from app.domain.models.dataset import CuratedDatasetSeed
from app.infrastructure.models.documents import DataCenterDatasetDocument, ExecutionNodeDocument
from app.infrastructure.storage.mongodb import get_mongodb


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True, help="Reviewed manifest directory")
    parser.add_argument("--host-root", required=True)
    parser.add_argument("--inspection-root", type=Path, required=True)
    parser.add_argument("--source", choices=["scidb", "tpdc", "chemdc", "ngdc"])
    parser.add_argument("--apply", action="store_true", help="Insert verified registrations (default: validate only)")
    args = parser.parse_args()
    host = PurePosixPath(args.host_root)
    if not host.is_absolute() or ".." in host.parts or str(host) == "/":
        parser.error("host-root must be a specific absolute directory")
    if not args.inspection_root.is_absolute() or not args.inspection_root.is_dir():
        parser.error("inspection-root must be a read-only view of the same host root")
    return args


def verify_inspection_mount(args, *, docker_client=None):
    """Prove the checksum view is the exact, read-only host directory.

    This CLI is intentionally container-only: filenames and sizes cannot
    establish that two independent trees contain the same bytes. Inspect the
    calling container through the same Docker daemon used for host validation.
    Fail closed if its identity or mount information cannot be verified.
    """
    inspection = args.inspection_root
    if not inspection.is_absolute() or not inspection.is_dir() or inspection.resolve() != inspection:
        raise ValueError("inspection-root must be a non-symlink absolute directory")
    host = PurePosixPath(args.host_root)
    if not host.is_absolute() or ".." in host.parts or str(host) == "/":
        raise ValueError("host-root must be a specific absolute directory")
    owns_client = docker_client is None
    try:
        if docker_client is None:
            docker_client = docker.from_env(timeout=20)
        container = docker_client.containers.get(socket.gethostname())
        mounts = container.attrs.get("Mounts")
        if not isinstance(mounts, list):
            raise ValueError("Container mount information is unavailable")
        target = PurePosixPath(str(inspection))
        matches = []
        for mount in mounts:
            destination = mount.get("Destination")
            if not isinstance(destination, str) or not destination.startswith("/"):
                raise ValueError("Container mount information is invalid")
            destination = PurePosixPath(destination)
            if destination == target:
                matches.append(mount)
            elif target in destination.parents:
                raise ValueError("inspection-root must not contain overriding nested mounts")
        if len(matches) != 1:
            raise ValueError("inspection-root must be an exact container bind mount target")
        mount = matches[0]
        source = mount.get("Source")
        if (
            mount.get("Type") != "bind"
            or mount.get("RW") is not False
            or not isinstance(source, str)
            or PurePosixPath(source) != host
        ):
            raise ValueError("inspection-root must be a read-only bind of the exact host-root")
    except ValueError:
        raise
    except Exception:
        raise ValueError("Unable to verify this container's read-only dataset bind mount") from None
    finally:
        if owns_client and docker_client is not None:
            docker_client.close()


async def run(args):
    # Run before opening MongoDB, and enforce it for dry-run as well as apply.
    await asyncio.to_thread(verify_inspection_mount, args)
    seeds = []
    seen = set()
    for path in sorted(args.catalog.glob("*/*.json")):
        seed = CuratedDatasetSeed.model_validate_json(path.read_text(encoding="utf-8"))
        source = seed.metadata.get("source_catalog")
        if source not in {"scidb", "tpdc", "chemdc", "ngdc"}:
            raise ValueError("Unknown reviewed catalog source")
        if args.source and source != args.source:
            continue
        if path.parent.name != source or path.stem != seed.dataset_id:
            raise ValueError("Manifest name must match its source and dataset identity")
        if seed.dataset_id in seen:
            raise ValueError("Duplicate dataset identity")
        seen.add(seed.dataset_id)
        seeds.append((source, seed))
    if not seeds:
        raise ValueError("No reviewed manifests selected")

    mongo = get_mongodb()
    await mongo.initialize()
    try:
        await init_beanie(database=mongo.client[get_settings().mongodb_database],
                         document_models=[DataCenterDatasetDocument, ExecutionNodeDocument],
                         skip_indexes=True)
        service = DataCenterDatasetService()
        # Validate every selected item before the first insert. Revalidate on
        # apply, so a changed file cannot use an earlier successful check.
        for source, seed in seeds:
            await service.register_curated_host_directory(
                seed,
                storage_directory=str(PurePosixPath(args.host_root) / source / seed.dataset_id),
                inspection_directory=args.inspection_root / source / seed.dataset_id,
                dry_run=True,
            )
        results = []
        if args.apply:
            for source, seed in seeds:
                item = await service.register_curated_host_directory(
                    seed,
                    storage_directory=str(PurePosixPath(args.host_root) / source / seed.dataset_id),
                    inspection_directory=args.inspection_root / source / seed.dataset_id,
                )
                results.append({"dataset_id": item.dataset_id, "source": source, "files": len(item.files), "enabled": item.enabled})
        print(json.dumps({"validated": len(seeds), "apply": args.apply, "datasets": results}, ensure_ascii=False))
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
