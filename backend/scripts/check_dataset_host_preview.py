"""Read-only real HOST_PATH preview acceptance; no datasets, uploads, or Agent.

Run with the workspace mounted read-only and PYTHONPATH=/workspace/backend in
the existing Compose backend image. Prints only aggregate/type/size results.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from beanie import init_beanie
import docker

from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.dataset_file_preview import DatasetFilePreviewService
from app.core.config import get_settings
from app.domain.models.dataset import DatasetStorageType
from app.infrastructure.external.sandbox.dataset_preview_reader import read_dataset_host_file
from app.infrastructure.models.documents import DataCenterDatasetDocument, TemporaryDatasetDocument
from app.infrastructure.storage.mongodb import get_mongodb
from app.interfaces.dependencies import get_current_user


class ReadOnlyDatasets(DataCenterDatasetService):
    async def ensure_seed_data(self):
        # A diagnostic must never create missing seed records as a side effect.
        return None


async def main():
    logging.disable(logging.CRITICAL)
    mongo = get_mongodb()
    await mongo.initialize()
    result = {"model_calls": 0, "source_writes": 0, "new_datasets": 0, "checks": []}
    try:
        database = mongo.client[get_settings().mongodb_database]
        await init_beanie(database=database, document_models=[DataCenterDatasetDocument, TemporaryDatasetDocument], skip_indexes=True)
        actor = await get_current_user()
        service = ReadOnlyDatasets()
        documents = await database.data_center_datasets.find({
            "enabled": True,
            "$or": [{"is_submission": {"$ne": True}}, {"created_by": actor.id}],
            "locations.storage_type": "host_path",
        }, {"_id": 0, "dataset_id": 1}).to_list()
        result["eligible_host_datasets"] = len(documents)
        catalog_root = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
        extensions = set()
        filenames = set()
        for manifest in catalog_root.glob("*.json"):
            plugin = json.loads(manifest.read_text())
            extensions.update("." + value.lower() for value in plugin.get("extensions", []))
            filenames.update(value.lower() for value in plugin.get("filenames", []))
        selected = None
        for document in documents:
            dataset = await service.get_dataset(document["dataset_id"], user_id=actor.id)
            candidates = sorted(dataset.files, key=lambda item: (item.size, item.path))
            for item in candidates:
                name = item.path.rsplit("/", 1)[-1].lower()
                if not 0 < item.size <= 4 * 1024 * 1024 or not (name in filenames or any(name.endswith(ext) for ext in extensions)):
                    continue
                try:
                    location, relative = DatasetFilePreviewService._source(dataset, item.path)
                except FileNotFoundError:
                    continue
                if location.storage_type == DatasetStorageType.HOST_PATH:
                    selected = (item, location, relative)
                    break
            if selected:
                break
        if selected is None:
            result["status"] = "no_supported_authorized_local_host_file"
        else:
            declaration, location, relative = selected
            kwargs = {"configured_roots": get_settings().dataset_host_path_allowlist, "max_bytes": 4096}
            empty, metadata = await asyncio.to_thread(read_dataset_host_file, location.source_path, relative, 0, 0, **kwargs)
            assert empty == b"" and metadata["size"] == declaration.size, "Stat/inventory size mismatch"
            prefix, prefix_metadata = await asyncio.to_thread(read_dataset_host_file, location.source_path, relative, 0, min(128, metadata["size"]), **kwargs)
            assert len(prefix) == min(128, metadata["size"]) and prefix_metadata == metadata, "Prefix/stat mismatch"
            result["checks"].append({"storage_type": "host_path", "format": Path(declaration.path).suffix.lower(),
                                     "size": metadata["size"], "prefix_bytes": len(prefix), "stat_only": True,
                                     "source_version_consistent": True})
            result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error_type"] = type(error).__name__
    finally:
        await mongo.shutdown()
        client = docker.from_env(timeout=5)
        try:
            residual = client.containers.list(all=True, filters={"label": "ai-dataseek.component=dataset-preview-read"})
            result["remaining_preview_helpers"] = len(residual)
        finally:
            client.close()
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "failed" or result.get("remaining_preview_helpers"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
