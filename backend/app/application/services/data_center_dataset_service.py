from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.core.config import get_settings
from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetLocation,
    DatasetMount,
    DatasetStorageType,
    MountedDataset,
)
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID, ensure_local_default_node
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    DatasetDirectoryInspectionError,
    inspect_local_dataset_directory,
)
from app.infrastructure.models.documents import (
    DataCenterDatasetDocument,
    ExecutionNodeDocument,
    TemporaryDatasetDocument,
)


logger = logging.getLogger(__name__)

DATASET_SEED_ROOT = Path(__file__).resolve().parents[2] / "resources" / "datasets"
SANDBOX_DATASET_ROOT = PurePosixPath("/home/ubuntu/datasets")
TEMPORARY_DATASET_TTL = timedelta(hours=24)
TEMPORARY_DATASET_ID_ATTEMPTS = 32
TEMPORARY_DATASET_MAX_ENTRIES = 128
TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER = 16
DATASET_CONTEXT_FILE_LIMIT = 48
DATASET_CONTEXT_GROUP_LIMIT = 16
DATASET_CONTEXT_METADATA_CHARS = 6_000
DATASET_CONTEXT_FIELD_CHARS = 2_000
DATASET_CONTEXT_PATH_CHARS = 512


def _name_key(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _new_temporary_dataset_id() -> str:
    return f"tds_{secrets.token_urlsafe(18)}"


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or normalized in {".", ".."} or ".." in path.parts:
        raise BadRequestError(f"Unsafe dataset file path: {value}")
    return path


def _unique_mount_names(source_paths: Sequence[str]) -> list[str]:
    """Return stable, filename-only mount names without exposing source directories."""
    used: set[str] = set()
    result: list[str] = []
    for source_path in source_paths:
        normalized = source_path.rstrip("/").replace("\\", "/")
        base_name = PurePosixPath(normalized).name or "source"
        candidate = base_name
        suffix = PurePosixPath(base_name).suffix
        stem = base_name[:-len(suffix)] if suffix else base_name
        index = 2
        while candidate in used:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        used.add(candidate)
        result.append(candidate)
    return result


class DataCenterDatasetService:
    """Catalog datasets plus owner-scoped, short-lived persisted submissions."""

    def __init__(self, seed_root: Path = DATASET_SEED_ROOT):
        self._seed_root = seed_root
        self._settings = get_settings()
        self._storage_root = Path(self._settings.dataset_storage_root)

    async def ensure_seed_data(self) -> None:
        if not self._seed_root.is_dir():
            return
        for manifest_path in sorted(self._seed_root.glob("*/manifest.json")):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            dataset_id = payload["dataset_id"]
            if await DataCenterDatasetDocument.find_one({"dataset_id": dataset_id}):
                continue
            source_dir = manifest_path.parent
            managed_dir = self._managed_dataset_dir(dataset_id)
            managed_dir.mkdir(parents=True, exist_ok=True)
            files: list[DatasetFile] = []
            for item in payload.pop("files", []):
                relative = _safe_relative_path(item.get("path") or item.get("name") or "")
                source = source_dir.joinpath(*relative.parts)
                if not source.is_file():
                    logger.warning("Skipping missing seed dataset file: %s", source)
                    continue
                target = managed_dir.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size != source.stat().st_size:
                    shutil.copy2(source, target)
                files.append(DatasetFile(path=str(relative), size=source.stat().st_size, role=item.get("role", "data")))
            document = DataCenterDatasetDocument(
                **payload,
                name_key=_name_key(payload["name"]),
                files=files,
                locations=[DatasetLocation(
                    node_id=LOCAL_DEFAULT_NODE_ID,
                    storage_type=DatasetStorageType.MANAGED_UPLOAD,
                    source_path=dataset_id,
                    verified=True,
                    verification_message="Imported from bundled dataset catalog",
                )],
            )
            try:
                await document.insert()
            except DuplicateKeyError:
                logger.info("Dataset seed already exists: %s", dataset_id)

    async def list_datasets(
        self,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_disabled: bool = False,
    ) -> tuple[list[DataCenterDataset], int]:
        await self.ensure_seed_data()
        conditions: list[dict] = []
        conditions.append({"is_submission": {"$ne": True}})
        if not include_disabled:
            conditions.append({"enabled": True})
        if query and query.strip():
            escaped = __import__("re").escape(query.strip())
            conditions.append({"$or": [
                {"name": {"$regex": escaped, "$options": "i"}},
                {"data_center_name": {"$regex": escaped, "$options": "i"}},
                {"tags": {"$regex": escaped, "$options": "i"}},
            ]})
        cursor = DataCenterDatasetDocument.find(*conditions)
        total = await cursor.count()
        docs = await cursor.sort(-DataCenterDatasetDocument.updated_at).skip(offset).limit(limit).to_list()
        return [doc.to_domain() for doc in docs], total

    async def get_dataset(
        self,
        dataset_id: str,
        include_disabled: bool = False,
        user_id: str | None = None,
    ) -> DataCenterDataset:
        if dataset_id.startswith("tds_"):
            if user_id is None:
                raise NotFoundError(f"Dataset '{dataset_id}' was not found in the data-center catalog")
            now = _utc_now()
            temporary_doc = await TemporaryDatasetDocument.find_one({
                "dataset_id": dataset_id,
                "owner_id": user_id,
                "expires_at": {"$gt": now},
            })
            # Keep the local checks as defense in depth and for deterministic
            # expiration at the boundary while MongoDB's TTL monitor catches up.
            if (
                not temporary_doc
                or temporary_doc.owner_id != user_id
                or _as_utc(temporary_doc.expires_at) <= now
            ):
                raise NotFoundError(f"Dataset '{dataset_id}' was not found in the data-center catalog")
            return temporary_doc.to_domain()

        await self.ensure_seed_data()
        doc = await DataCenterDatasetDocument.find_one({"dataset_id": dataset_id})
        submission_unavailable = bool(
            doc
            and doc.is_submission
            and (
                user_id is None
                or doc.created_by != user_id
            )
        )
        if (
            not doc
            or (not include_disabled and not doc.enabled)
            or submission_unavailable
        ):
            raise NotFoundError(f"Dataset '{dataset_id}' was not found in the data-center catalog")
        return doc.to_domain()

    async def create_submission(
        self,
        *,
        external_id: str,
        name: str,
        summary: str,
        keywords: Sequence[str],
        storage_directory: str,
        created_by: str,
    ) -> DataCenterDataset:
        normalized_directory = storage_directory.strip()
        if not normalized_directory:
            raise BadRequestError("A server storage directory is required")
        owner_id = created_by.strip()
        if not owner_id:
            raise BadRequestError("Dataset owner is required")

        node = await ensure_local_default_node()
        configured_roots = (
            node.runtime_config.get("dataset_allowed_roots")
            or self._settings.dataset_host_path_allowlist
        )
        try:
            inventory = await asyncio.to_thread(
                inspect_local_dataset_directory,
                normalized_directory,
                configured_roots=configured_roots,
            )
        except DatasetDirectoryInspectionError as exc:
            raise BadRequestError(exc.message) from exc

        mount_name = _unique_mount_names([inventory.canonical_source_directory])[0]
        normalized_keywords = list(dict.fromkeys(item.strip() for item in keywords if item.strip()))
        location = DatasetLocation(
            node_id=LOCAL_DEFAULT_NODE_ID,
            storage_type=DatasetStorageType.HOST_PATH,
            source_path=inventory.canonical_source_directory,
            mount_name=mount_name,
            verified=True,
            verification_message="Directory inspected on the execution node and mounted read-only",
        )
        now = _utc_now()
        dataset_values = dict(
            external_id=external_id.strip(),
            data_center_id="dataset-chat-demo",
            data_center_name="测试数据集",
            name=name.strip(),
            description=summary.strip(),
            data_type="服务器目录",
            tags=normalized_keywords,
            files=[
                DatasetFile(
                    path=f"sources/{location.location_id}/{mount_name}/{item.relative_path}",
                    size=item.size,
                    role="data",
                )
                for item in inventory.files
            ],
            metadata={
                "temporary": True,
                "recursive_file_count": len(inventory.files),
                "total_size_bytes": inventory.total_size,
            },
            locations=[location],
            enabled=True,
            is_submission=True,
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
        expires_at = now + TEMPORARY_DATASET_TTL
        for _ in range(TEMPORARY_DATASET_ID_ATTEMPTS):
            dataset_id = _new_temporary_dataset_id()
            owner_slot, global_slot = await self._allocate_temporary_dataset_slots(
                owner_id,
                now,
            )
            dataset = DataCenterDataset(
                **dataset_values,
                dataset_id=dataset_id,
                name_key="temporary-submission",
            )
            document = TemporaryDatasetDocument(
                dataset_id=dataset_id,
                owner_id=owner_id,
                dataset=dataset,
                owner_slot=owner_slot,
                global_slot=global_slot,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                await document.insert()
                return document.to_domain()
            except DuplicateKeyError:
                logger.info(
                    "Temporary dataset ID or quota-slot collision; retrying insertion",
                )
        raise RuntimeError("Failed to generate a unique temporary dataset ID")

    async def _allocate_temporary_dataset_slots(
        self,
        owner_id: str,
        now: datetime,
    ) -> tuple[int, int]:
        """Reserve bounded owner/global slots for a temporary submission.

        MongoDB unique indexes are the final concurrency guard. Multiple
        backend replicas may select the same free slots, but only one insert
        can win; the caller retries after ``DuplicateKeyError``. When a limit
        is full we preserve the previous registry behavior by evicting the
        oldest entry first.
        """

        expired_documents = await TemporaryDatasetDocument.find({
            "expires_at": {"$lte": now},
        }).to_list()
        for document in expired_documents:
            await document.delete()

        active_documents = await TemporaryDatasetDocument.find({
            "expires_at": {"$gt": now},
        }).sort("+created_at").to_list()

        owner_documents = [
            document
            for document in active_documents
            if document.owner_id == owner_id
        ]
        delete_ids = {
            document.dataset_id
            for document in owner_documents[
                : max(
                    0,
                    len(owner_documents)
                    - TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER
                    + 1,
                )
            ]
        }

        remaining_documents = [
            document
            for document in active_documents
            if document.dataset_id not in delete_ids
        ]
        global_excess = max(
            0,
            len(remaining_documents) - TEMPORARY_DATASET_MAX_ENTRIES + 1,
        )
        delete_ids.update(
            document.dataset_id
            for document in remaining_documents[:global_excess]
        )

        if delete_ids:
            for document in active_documents:
                if document.dataset_id in delete_ids:
                    await document.delete()
            active_documents = [
                document
                for document in active_documents
                if document.dataset_id not in delete_ids
            ]

        owner_slots = {
            document.owner_slot
            for document in active_documents
            if document.owner_id == owner_id
            and isinstance(document.owner_slot, int)
        }
        global_slots = {
            document.global_slot
            for document in active_documents
            if isinstance(document.global_slot, int)
        }
        owner_slot = next(
            slot
            for slot in range(TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER)
            if slot not in owner_slots
        )
        global_slot = next(
            slot
            for slot in range(TEMPORARY_DATASET_MAX_ENTRIES)
            if slot not in global_slots
        )
        return owner_slot, global_slot

    async def create_dataset(self, values: dict, created_by: str) -> DataCenterDataset:
        name = str(values.get("name") or "").strip()
        if not name:
            raise BadRequestError("Dataset name is required")
        payload = dict(values)
        payload.update(
            name=name,
            name_key=_name_key(name),
            created_by=created_by,
        )
        document = DataCenterDatasetDocument(**payload)
        try:
            await document.insert()
        except DuplicateKeyError as exc:
            raise BadRequestError("A dataset with the same name already exists") from exc
        return document.to_domain()

    async def update_dataset(self, dataset_id: str, values: dict) -> DataCenterDataset:
        doc = await self._document(dataset_id)
        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"]:
                raise BadRequestError("Dataset name is required")
            values["name_key"] = _name_key(values["name"])
        for key, value in values.items():
            setattr(doc, key, value)
        doc.updated_at = datetime.now(UTC)
        try:
            await doc.save()
        except DuplicateKeyError as exc:
            raise BadRequestError("A dataset with the same name already exists") from exc
        return doc.to_domain()

    async def delete_dataset(self, dataset_id: str) -> None:
        doc = await self._document(dataset_id)
        managed_locations = [item for item in doc.locations if item.storage_type == DatasetStorageType.MANAGED_UPLOAD]
        await doc.delete()
        if managed_locations:
            shutil.rmtree(self._managed_dataset_dir(dataset_id), ignore_errors=True)

    async def add_location(self, dataset_id: str, location: DatasetLocation) -> DataCenterDataset:
        doc = await self._document(dataset_id)
        node = await ExecutionNodeDocument.find_one(ExecutionNodeDocument.node_id == location.node_id)
        if not node:
            raise BadRequestError("Execution node does not exist")
        if location.storage_type == DatasetStorageType.HOST_PATH:
            self._validate_host_path(location.source_path, node.runtime_config.get("dataset_allowed_roots"))
            if not location.mount_name:
                existing_names = {item.mount_name for item in doc.locations if item.mount_name}
                location.mount_name = _unique_mount_names([
                    *(item.source_path for item in doc.locations if item.mount_name in existing_names),
                    location.source_path,
                ])[-1]
        if any(item.node_id == location.node_id and item.source_path == location.source_path for item in doc.locations):
            raise BadRequestError("This storage location is already registered")
        doc.locations.append(location)
        if location.storage_type == DatasetStorageType.HOST_PATH:
            doc.files.append(DatasetFile(
                path=f"sources/{location.location_id}/{location.mount_name}",
                role="data",
            ))
        doc.updated_at = datetime.now(UTC)
        await doc.save()
        return doc.to_domain()

    async def remove_location(self, dataset_id: str, location_id: str) -> DataCenterDataset:
        doc = await self._document(dataset_id)
        locations = [item for item in doc.locations if item.location_id != location_id]
        if len(locations) == len(doc.locations):
            raise NotFoundError("Dataset storage location was not found")
        removed = next(item for item in doc.locations if item.location_id == location_id)
        if removed.storage_type == DatasetStorageType.MANAGED_UPLOAD:
            raise BadRequestError("Managed upload locations are maintained by the platform and cannot be removed directly")
        doc.locations = locations
        doc.files = [
            item for item in doc.files
            if not item.path.startswith(f"sources/{removed.location_id}/")
        ]
        doc.updated_at = datetime.now(UTC)
        await doc.save()
        return doc.to_domain()

    async def upload_files(self, dataset_id: str, uploads: Sequence[tuple[str, object]]) -> DataCenterDataset:
        doc = await self._document(dataset_id)
        target_root = self._managed_dataset_dir(dataset_id)
        target_root.mkdir(parents=True, exist_ok=True)
        file_map = {item.path: item for item in doc.files}
        for relative_name, upload in uploads:
            relative = _safe_relative_path(relative_name)
            target = target_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    output.write(chunk)
                    size += len(chunk)
            file_map[str(relative)] = DatasetFile(
                path=str(relative),
                size=size,
                role="data",
                content_type=getattr(upload, "content_type", None),
            )
        doc.files = sorted(file_map.values(), key=lambda item: item.path)
        if not any(item.storage_type == DatasetStorageType.MANAGED_UPLOAD for item in doc.locations):
            doc.locations.append(DatasetLocation(
                node_id=LOCAL_DEFAULT_NODE_ID,
                storage_type=DatasetStorageType.MANAGED_UPLOAD,
                source_path=dataset_id,
                verified=True,
                verification_message="Managed upload is available on the local Docker node",
            ))
        doc.updated_at = datetime.now(UTC)
        await doc.save()
        return doc.to_domain()

    async def upload_preview(self, dataset_id: str, upload: object) -> DataCenterDataset:
        doc = await self._document(dataset_id)
        suffix = Path(getattr(upload, "filename", "") or "").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise BadRequestError("Preview must be PNG, JPEG, or WebP")
        target_root = self._managed_dataset_dir(dataset_id)
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / f"preview{suffix}"
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
        doc.preview_url = f"/api/v1/datasets/{dataset_id}/preview"
        doc.updated_at = datetime.now(UTC)
        await doc.save()
        return doc.to_domain()

    async def preview_path(self, dataset_id: str, user_id: str | None = None) -> Path:
        dataset = await self.get_dataset(dataset_id, user_id=user_id)
        root = self._managed_dataset_dir(dataset.dataset_id)
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            path = root / f"preview{suffix}"
            if path.is_file():
                return path
        raise NotFoundError("Dataset preview was not found")

    async def candidate_node_ids(self, dataset_ids: Iterable[str], user_id: str | None = None) -> set[str]:
        candidates: set[str] | None = None
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            node_ids = {item.node_id for item in dataset.locations if item.verified}
            if not node_ids:
                raise BadRequestError(f"Dataset '{dataset.name}' has no verified storage location")
            candidates = node_ids if candidates is None else candidates & node_ids
        if not candidates:
            raise BadRequestError("Selected datasets are not available on a common execution node")
        return candidates

    async def resolve_mounts(
        self,
        dataset_ids: Iterable[str],
        node_id: str,
        user_id: str | None = None,
    ) -> list[DatasetMount]:
        mounts: list[DatasetMount] = []
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            locations = [item for item in dataset.locations if item.node_id == node_id and item.verified]
            if not locations:
                raise BadRequestError(f"Dataset '{dataset.name}' is not available on execution node '{node_id}'")
            derived_names = _unique_mount_names([item.source_path for item in locations])
            for location, derived_name in zip(locations, derived_names):
                dataset_root = SANDBOX_DATASET_ROOT / dataset.dataset_id
                target = (
                    dataset_root
                    if location.storage_type == DatasetStorageType.MANAGED_UPLOAD
                    else dataset_root / "sources" / location.location_id / (location.mount_name or derived_name)
                )
                source = (
                    self._settings.dataset_managed_volume
                    if location.storage_type == DatasetStorageType.MANAGED_UPLOAD
                    else location.source_path
                )
                mounts.append(DatasetMount(
                    dataset_id=dataset.dataset_id,
                    source_id=location.location_id,
                    display_name=location.mount_name or derived_name,
                    node_id=node_id,
                    storage_type=location.storage_type,
                    source=source,
                    target=str(target),
                    read_only=True,
                    version=location.version,
                ))
        return mounts

    async def mounted_datasets(
        self,
        dataset_ids: Iterable[str],
        user_id: str | None = None,
    ) -> list[MountedDataset]:
        mounted: list[MountedDataset] = []
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            payload = dataset.model_dump()
            # Host source paths are needed only while resolving Docker mounts.
            # Never carry them into the agent message/context object.
            payload["locations"] = []
            mounted.append(MountedDataset(
                **payload,
                sandbox_path=str(SANDBOX_DATASET_ROOT / dataset.dataset_id),
            ))
        return mounted

    async def _document(self, dataset_id: str) -> DataCenterDatasetDocument:
        doc = await DataCenterDatasetDocument.find_one({"dataset_id": dataset_id})
        if not doc:
            raise NotFoundError("Dataset was not found")
        return doc

    def _managed_dataset_dir(self, dataset_id: str) -> Path:
        relative = _safe_relative_path(dataset_id)
        if len(relative.parts) != 1:
            raise BadRequestError("Invalid dataset ID")
        return self._storage_root / dataset_id

    def _validate_host_path(self, source_path: str, configured_roots: object = None) -> None:
        if any(ord(character) < 32 for character in source_path):
            raise BadRequestError("Server path contains invalid control characters")
        path = PurePosixPath(source_path)
        if not path.is_absolute() or ".." in path.parts:
            raise BadRequestError("Server path must be an absolute path without '..'")
        raw_roots = configured_roots if isinstance(configured_roots, list) else self._settings.dataset_host_path_allowlist.split(",")
        roots = [PurePosixPath(str(item).strip()) for item in raw_roots if str(item).strip()]
        if not roots:
            raise BadRequestError("DATASET_HOST_PATH_ALLOWLIST is not configured")
        if not any(path == root or root in path.parents for root in roots):
            raise BadRequestError("Server path is outside DATASET_HOST_PATH_ALLOWLIST")


def _bounded_context_text(value: object, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[truncated from {len(text)} characters]"


def _inventory_group(item: DatasetFile) -> tuple[str, str]:
    role = item.role or "unknown"
    suffix = PurePosixPath(item.path).suffix.lower() or "[no extension]"
    return role, suffix


def _render_inventory_context(files: list[DatasetFile]) -> str:
    if not files:
        return "  Inventory summary: 0 files\n  Inventory sample: (empty)"

    total_size = sum(max(0, item.size) for item in files)
    all_groups: Counter[tuple[str, str]] = Counter(_inventory_group(item) for item in files)

    # Pick at least one representative of as many role/type groups as possible,
    # then fill the remaining slots in stable catalog order.
    selected_indexes: list[int] = []
    represented_groups: set[tuple[str, str]] = set()
    for index, item in enumerate(files):
        group = _inventory_group(item)
        if group in represented_groups:
            continue
        represented_groups.add(group)
        selected_indexes.append(index)
        if len(selected_indexes) >= DATASET_CONTEXT_FILE_LIMIT:
            break
    if len(selected_indexes) < DATASET_CONTEXT_FILE_LIMIT:
        selected_set = set(selected_indexes)
        for index in range(len(files)):
            if index in selected_set:
                continue
            selected_indexes.append(index)
            if len(selected_indexes) >= DATASET_CONTEXT_FILE_LIMIT:
                break
    selected_indexes.sort()

    selected = [files[index] for index in selected_indexes]
    selected_index_set = set(selected_indexes)
    omitted = [item for index, item in enumerate(files) if index not in selected_index_set]
    omitted_groups: Counter[tuple[str, str]] = Counter(_inventory_group(item) for item in omitted)
    omitted_sizes: Counter[tuple[str, str]] = Counter()
    for item in omitted:
        omitted_sizes[_inventory_group(item)] += max(0, item.size)

    group_summary = ", ".join(
        f"{role}/{suffix}: {count}"
        for (role, suffix), count in all_groups.most_common(DATASET_CONTEXT_GROUP_LIMIT)
    )
    sample = "\n".join(
        "  - "
        + _bounded_context_text(item.path, DATASET_CONTEXT_PATH_CHARS)
        + f" ({item.role}, {item.size} bytes)"
        for item in selected
    )
    lines = [
        f"  Inventory summary: {len(files)} files, {total_size} bytes total",
        f"  Inventory groups: {group_summary or '(none)'}",
        f"  Inventory sample ({len(selected)} of {len(files)} files):",
        sample or "  - (empty)",
    ]
    if omitted:
        omitted_summary = ", ".join(
            f"{role}/{suffix}: {count} files, {omitted_sizes[(role, suffix)]} bytes"
            for (role, suffix), count in omitted_groups.most_common(DATASET_CONTEXT_GROUP_LIMIT)
        )
        lines.extend([
            f"  Omitted from prompt: {len(omitted)} files ({omitted_summary})",
            "  The sample is representative, not exhaustive. For an exact lookup, inspect the "
            "read-only mounted directory with one compact find/list command; do not install tools.",
        ])
    return "\n".join(lines)


def render_dataset_context(datasets: list[MountedDataset]) -> str:
    if not datasets:
        return ""
    blocks = []
    for dataset in datasets:
        inventory = _render_inventory_context(dataset.files)
        metadata = _bounded_context_text(
            json.dumps(dataset.metadata, ensure_ascii=False, indent=2, default=str),
            DATASET_CONTEXT_METADATA_CHARS,
        )
        blocks.append(
            f"- Dataset ID: {dataset.dataset_id}\n"
            f"  Data center: {dataset.data_center_name} ({dataset.data_center_id})\n"
            f"  Name: {_bounded_context_text(dataset.name, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Description: {_bounded_context_text(dataset.description, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Spatial coverage: {_bounded_context_text(dataset.spatial_coverage, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Temporal coverage: {_bounded_context_text(dataset.temporal_coverage, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Data type: {_bounded_context_text(dataset.data_type, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Read-only mounted directory: {dataset.sandbox_path}\n"
            f"  Write generated outputs to: /home/ubuntu/output\n"
            f"{inventory}\n"
            f"  Metadata: {metadata}"
        )
    return (
        "<data_center_datasets>\n"
        "These are coherent datasets published by scientific data centers, not user uploads. "
        "Source directories are read-only. Never modify them; write all generated results under /home/ubuntu/output. "
        "Use the mounted directory directly and preserve sidecar files with primary data.\n\n"
        + "\n\n".join(blocks)
        + "\n</data_center_datasets>"
    )
