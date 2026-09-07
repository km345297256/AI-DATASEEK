from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.core.config import get_settings
from app.domain.models.dataset import (
    CuratedDatasetFile,
    CuratedDatasetSeed,
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
    TemporaryDatasetDocument,
)


logger = logging.getLogger(__name__)

DATASET_SEED_ROOT = Path(__file__).resolve().parents[2] / "resources" / "datasets"
SANDBOX_DATASET_ROOT = PurePosixPath("/home/ubuntu/datasets")
TEMPORARY_DATASET_TTL = timedelta(hours=24)
TEMPORARY_DATASET_ID_ATTEMPTS = 32
REGISTERED_DATASET_ID_ATTEMPTS = 32
TEMPORARY_DATASET_MAX_ENTRIES = 128
TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER = 16
DATASET_CONTEXT_FILE_LIMIT = 48
DATASET_CONTEXT_GROUP_LIMIT = 16
DATASET_CONTEXT_METADATA_CHARS = 6_000
DATASET_CONTEXT_FIELD_CHARS = 2_000
DATASET_CONTEXT_PATH_CHARS = 512


class _SeedIdentity(BaseModel):
    """Only fetch the identity needed by the idempotent seed check."""

    dataset_id: str


def _name_key(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _new_temporary_dataset_id() -> str:
    return f"tds_{secrets.token_urlsafe(18)}"


def _new_registered_dataset_id() -> str:
    return f"dsr_{secrets.token_hex(16)}"


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
    """Public seeds, persistent owner registrations, and legacy TTL submissions."""

    def __init__(self, seed_root: Path = DATASET_SEED_ROOT):
        self._seed_root = seed_root
        self._settings = get_settings()
        self._storage_root = Path(self._settings.dataset_storage_root)

    async def ensure_seed_data(self) -> None:
        if not self._seed_root.is_dir():
            return
        seeds: list[tuple[Path, CuratedDatasetSeed]] = []
        for manifest_path in sorted(self._seed_root.glob("*/manifest.json")):
            seed = CuratedDatasetSeed.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if manifest_path.parent.name != seed.dataset_id:
                raise BadRequestError("Curated dataset directory must match its dataset ID")
            seeds.append((manifest_path.parent, seed))
        if not seeds:
            return

        # One projected query replaces a sequential lookup per seed on every
        # dataset request. Do not cache database presence: deleted seeds must be
        # recoverable, and archived records must remain archived.
        existing_ids = {
            item.dataset_id
            for item in await DataCenterDatasetDocument.find({
                "dataset_id": {"$in": [seed.dataset_id for _, seed in seeds]},
            }).project(_SeedIdentity).to_list()
        }
        for source_dir, seed in seeds:
            dataset_id = seed.dataset_id
            if dataset_id in existing_ids:
                continue
            source_files = self._verified_managed_files(source_dir, seed.files)
            managed_dir = self._managed_dataset_dir(dataset_id)
            if managed_dir.is_symlink():
                raise BadRequestError("Managed dataset directory must not be a symbolic link")
            try:
                managed_dir.mkdir(parents=True, exist_ok=True)
                storage_root = self._storage_root.resolve(strict=True)
                managed_root = managed_dir.resolve(strict=True)
            except OSError:
                raise BadRequestError("Managed dataset directory could not be prepared") from None
            if managed_root.parent != storage_root or not managed_root.is_dir():
                raise BadRequestError("Managed dataset directory escaped its storage root")
            declarations_by_path = {
                str(_safe_relative_path(declared.path)): declared
                for declared in seed.files
            }
            for item in source_files:
                relative = _safe_relative_path(item.path)
                source = source_dir.joinpath(*relative.parts)
                target_parent = managed_dir
                for component in relative.parts[:-1]:
                    target_parent = target_parent / component
                    if target_parent.is_symlink():
                        raise BadRequestError("Managed dataset directory must not use symbolic links")
                    try:
                        target_parent.mkdir(exist_ok=True)
                        resolved_parent = target_parent.resolve(strict=True)
                    except OSError:
                        raise BadRequestError("Managed dataset directory could not be prepared") from None
                    if (
                        not resolved_parent.is_dir()
                        or (
                            resolved_parent != managed_root
                            and managed_root not in resolved_parent.parents
                        )
                    ):
                        raise BadRequestError("Managed dataset directory escaped its storage root")
                target = target_parent / relative.name
                if target.is_symlink():
                    raise BadRequestError("Managed dataset file must not be a symbolic link")
                if target.exists() and not target.is_file():
                    raise BadRequestError("Managed dataset file target is not a regular file")
                declaration = declarations_by_path[item.path]
                needs_copy = (
                    not target.exists()
                    or target.stat().st_size != source.stat().st_size
                    or (
                        declaration.sha256 is not None
                        and self._file_sha256(target) != declaration.sha256
                    )
                )
                if needs_copy:
                    temporary = target.with_name(
                        f".{target.name}.{secrets.token_hex(8)}.tmp"
                    )
                    try:
                        # Replacing a completed same-directory copy avoids
                        # following a target hard link and never exposes a
                        # partially copied catalog file to a sandbox mount.
                        shutil.copy2(source, temporary)
                        temporary.replace(target)
                    except OSError:
                        raise BadRequestError("Curated dataset file could not be copied") from None
                    finally:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            logger.warning("Failed to remove incomplete curated dataset copy")
            await self.register_curated_managed_directory(seed)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verified_managed_files(
        cls,
        directory: Path,
        declarations: Sequence[CuratedDatasetFile],
    ) -> list[DatasetFile]:
        """Validate a complete declared inventory below one fixed directory."""
        if not declarations:
            raise BadRequestError("A curated dataset must declare at least one file")
        try:
            root = directory.resolve(strict=True)
        except OSError:
            raise BadRequestError("Curated dataset directory was not found") from None
        if not root.is_dir() or directory.is_symlink():
            raise BadRequestError("Curated dataset directory is invalid")

        files: list[DatasetFile] = []
        seen: set[str] = set()
        for declaration in declarations:
            raw_path = declaration.path
            lexical = PurePosixPath(raw_path)
            if (
                raw_path != raw_path.strip()
                or "\\" in raw_path
                or lexical.is_absolute()
                or ".." in lexical.parts
            ):
                raise BadRequestError("Curated dataset file declaration is invalid")
            relative = _safe_relative_path(declaration.path)
            rendered = str(relative)
            if (
                len(rendered.encode("utf-8")) > 1024
                or any(ord(character) < 32 or ord(character) == 127 for character in rendered)
                or rendered in seen
            ):
                raise BadRequestError("Curated dataset file declaration is invalid")
            seen.add(rendered)
            candidate = directory.joinpath(*relative.parts)
            current = directory
            for component in relative.parts:
                current = current / component
                if current.is_symlink():
                    raise BadRequestError("Curated dataset files must not use symbolic links")
            try:
                resolved = candidate.resolve(strict=True)
                details = resolved.stat()
            except OSError:
                raise BadRequestError("A declared curated dataset file was not found") from None
            if root not in resolved.parents or candidate.is_symlink() or not resolved.is_file():
                raise BadRequestError("Curated dataset file escaped its managed directory")
            if declaration.size is not None and declaration.size != details.st_size:
                raise BadRequestError("Curated dataset file size did not match its manifest")
            if declaration.sha256 is not None and cls._file_sha256(resolved) != declaration.sha256:
                raise BadRequestError("Curated dataset file checksum did not match its manifest")
            files.append(DatasetFile(
                path=rendered,
                size=details.st_size,
                role=declaration.role,
                content_type=declaration.content_type,
            ))
        return files

    async def register_curated_managed_directory(
        self,
        seed: CuratedDatasetSeed | dict,
    ) -> DataCenterDataset:
        """Register one repository-owned managed directory without downloading it."""
        seed = CuratedDatasetSeed.model_validate(seed)
        self._validate_domain(seed.domain)
        managed_dir = self._managed_dataset_dir(seed.dataset_id)
        files = self._verified_managed_files(managed_dir, seed.files)
        existing = await DataCenterDatasetDocument.find_one({"dataset_id": seed.dataset_id})
        if existing is not None:
            if existing.is_submission:
                raise BadRequestError("Curated dataset ID conflicts with an owner registration")
            return existing.to_domain()

        values = seed.model_dump(exclude={"files"})
        document = DataCenterDatasetDocument(
            **values,
            name_key=_name_key(seed.name),
            files=files,
            locations=[DatasetLocation(
                node_id=LOCAL_DEFAULT_NODE_ID,
                storage_type=DatasetStorageType.MANAGED_UPLOAD,
                source_path=seed.dataset_id,
                verified=True,
                verification_message="Verified from the bundled dataset catalog",
            )],
            enabled=True,
            is_submission=False,
            created_by=None,
        )
        try:
            await document.insert()
        except DuplicateKeyError:
            existing = await DataCenterDatasetDocument.find_one({"dataset_id": seed.dataset_id})
            if existing is None or existing.is_submission:
                raise BadRequestError("Curated dataset catalog identity already exists") from None
            return existing.to_domain()
        return document.to_domain()

    async def register_curated_host_directory(
        self,
        seed: CuratedDatasetSeed,
        *,
        storage_directory: str,
        inspection_directory: Path,
        dry_run: bool = False,
    ) -> DataCenterDataset:
        """Operator-only import of already downloaded public files.

        The caller supplies a read-only view of the same host directory for
        checksum verification. This method is deliberately not an HTTP route:
        browser registrations cannot set public provenance or curated status.
        It never downloads, copies, replaces or removes source files.
        """
        seed = CuratedDatasetSeed.model_validate(seed)
        self._validate_domain(seed.domain)
        if seed.metadata.get("curated") is not True:
            raise BadRequestError("A curated import must declare its provenance")
        if any(item.size is None or item.sha256 is None for item in seed.files):
            raise BadRequestError("Every imported file requires size and SHA256")
        if dry_run:
            # Validation must not create/reactivate nodes or update their
            # runtime configuration. Mirror the existing-node lookup only.
            from app.infrastructure.models.documents import ExecutionNodeDocument

            node = await ExecutionNodeDocument.find_one({"node_id": LOCAL_DEFAULT_NODE_ID})
            if node is None:
                node = await ExecutionNodeDocument.find_one({"name": "local-default"})
        else:
            node = await ensure_local_default_node()
        configured_roots = (
            (getattr(node, "runtime_config", None) or {}).get("dataset_allowed_roots")
            or self._settings.dataset_host_path_allowlist
        )
        try:
            inventory = await asyncio.to_thread(
                inspect_local_dataset_directory,
                storage_directory,
                configured_roots=configured_roots,
            )
        except DatasetDirectoryInspectionError as exc:
            raise BadRequestError(exc.message) from exc
        verified = await asyncio.to_thread(
            self._verified_managed_files, inspection_directory, seed.files,
        )
        if {item.path: item.size for item in verified} != {
            item.relative_path: item.size for item in inventory.files
        }:
            raise BadRequestError("Imported files must match the complete host inventory")

        manifest_digest = hashlib.sha256(json.dumps(
            seed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        def existing_dataset(document):
            if (
                document.is_submission
                or document.metadata.get("catalog_manifest_sha256") != manifest_digest
                or len(document.locations) != 1
                or document.locations[0].storage_type != DatasetStorageType.HOST_PATH
                or document.locations[0].source_path != inventory.canonical_source_directory
                or document.locations[0].node_id != LOCAL_DEFAULT_NODE_ID
            ):
                raise BadRequestError("Curated import conflicts with an existing dataset")
            # Preserve metadata edits and soft archives; never reset enabled.
            return document.to_domain()

        existing = await DataCenterDatasetDocument.find_one({"dataset_id": seed.dataset_id})
        if existing is not None:
            return existing_dataset(existing)
        location = DatasetLocation(
            node_id=LOCAL_DEFAULT_NODE_ID,
            storage_type=DatasetStorageType.HOST_PATH,
            source_path=inventory.canonical_source_directory,
            # Stable public alias, not the host directory name.
            mount_name="data",
            read_only=True,
            verified=True,
            verification_message="Verified local curated files; read-only mount",
        )
        metadata = {
            **seed.metadata,
            "catalog_manifest_sha256": manifest_digest,
            "inventory_complete": True,
            "recursive_file_count": len(verified),
            "total_size_bytes": sum(item.size for item in verified),
        }
        document = DataCenterDatasetDocument(
            **seed.model_dump(exclude={"files", "metadata"}),
            name_key=f"curated-import:{seed.dataset_id}",
            metadata=metadata,
            files=[item.model_copy(update={
                "path": f"sources/{location.location_id}/data/{item.path}",
            }) for item in verified],
            locations=[location],
            enabled=True,
            is_submission=False,
            created_by=None,
        )
        if dry_run:
            return document.to_domain()
        try:
            await document.insert()
        except DuplicateKeyError:
            existing = await DataCenterDatasetDocument.find_one({"dataset_id": seed.dataset_id})
            if existing is None:
                raise BadRequestError("Curated import identity already exists") from None
            return existing_dataset(existing)
        return document.to_domain()

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
                {"domain": {"$regex": escaped, "$options": "i"}},
                {"tags": {"$regex": escaped, "$options": "i"}},
            ]})
        cursor = DataCenterDatasetDocument.find(*conditions)
        total = await cursor.count()
        docs = await cursor.sort(-DataCenterDatasetDocument.updated_at).skip(offset).limit(limit).to_list()
        return [doc.to_domain() for doc in docs], total

    async def list_managed_datasets(
        self,
        *,
        actor_id: str,
        is_admin: bool,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_archived: bool = False,
    ) -> tuple[list[DataCenterDataset], int]:
        """List public seeds plus registrations visible to the current owner."""
        owner = actor_id.strip()
        if not owner:
            raise BadRequestError("Dataset manager identity is required")
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise BadRequestError("Invalid dataset catalog pagination")
        await self.ensure_seed_data()
        conditions: list[dict] = []
        if not is_admin:
            public_condition: dict = {"is_submission": {"$ne": True}}
            if include_archived:
                public_condition["enabled"] = True
            conditions.append({"$or": [
                public_condition,
                {"is_submission": True, "created_by": owner},
            ]})
        if not include_archived:
            conditions.append({"enabled": True})
        if query and query.strip():
            escaped = __import__("re").escape(query.strip()[:200])
            conditions.append({"$or": [
                {"name": {"$regex": escaped, "$options": "i"}},
                {"description": {"$regex": escaped, "$options": "i"}},
                {"domain": {"$regex": escaped, "$options": "i"}},
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
        nc_view_url: str | None = None,
        sso_uid: str | None = None,
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
        metadata = {
            "temporary": True,
            "inventory_complete": True,
            "inventory_source": "verified_recursive_scan",
            "recursive_file_count": len(inventory.files),
            "total_size_bytes": inventory.total_size,
        }
        if sso_uid:
            metadata["sso_uid"] = sso_uid
        dataset_values = dict(
            external_id=external_id.strip(),
            data_center_id="dataset-chat-demo",
            data_center_name="测试数据集",
            name=name.strip(),
            description=summary.strip(),
            data_type="服务器目录",
            tags=normalized_keywords,
            nc_view_url=nc_view_url,
            files=[
                DatasetFile(
                    path=f"sources/{location.location_id}/{mount_name}/{item.relative_path}",
                    size=item.size,
                    role="data",
                )
                for item in inventory.files
            ],
            metadata=metadata,
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

    async def create_registration(
        self,
        *,
        name: str,
        description: str,
        domain: str,
        storage_directory: str,
        created_by: str,
    ) -> DataCenterDataset:
        """Persist an owner-scoped read-only directory registration."""
        normalized_name = name.strip()
        normalized_domain = domain.strip()
        normalized_directory = storage_directory.strip()
        owner_id = created_by.strip()
        if not normalized_name or not normalized_domain or not normalized_directory or not owner_id:
            raise BadRequestError("Dataset registration fields must not be blank")
        self._validate_domain(normalized_domain)

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
        location = DatasetLocation(
            node_id=LOCAL_DEFAULT_NODE_ID,
            storage_type=DatasetStorageType.HOST_PATH,
            source_path=inventory.canonical_source_directory,
            mount_name=mount_name,
            verified=True,
            verification_message="Directory inspected on the execution node and mounted read-only",
        )
        now = _utc_now()
        for _ in range(REGISTERED_DATASET_ID_ATTEMPTS):
            dataset_id = _new_registered_dataset_id()
            document = DataCenterDatasetDocument(
                dataset_id=dataset_id,
                external_id="",
                data_center_id="owner-registration",
                data_center_name="本地数据集",
                name=normalized_name,
                # Display names are owner-scoped and editable. The unique
                # storage key therefore remains independent of private text.
                name_key=f"registration:{dataset_id}",
                description=description.strip(),
                domain=normalized_domain,
                data_type="本机目录",
                tags=[normalized_domain],
                files=[
                    DatasetFile(
                        path=(
                            f"sources/{location.location_id}/{mount_name}/"
                            f"{item.relative_path}"
                        ),
                        size=item.size,
                        role="data",
                    )
                    for item in inventory.files
                ],
                metadata={
                    "inventory_complete": True,
                    "inventory_source": "verified_recursive_scan",
                    "recursive_file_count": len(inventory.files),
                    "total_size_bytes": inventory.total_size,
                    "registration_kind": "owner_managed_directory",
                },
                locations=[location],
                enabled=True,
                is_submission=True,
                created_by=owner_id,
                created_at=now,
                updated_at=now,
            )
            try:
                await document.insert()
                return document.to_domain()
            except DuplicateKeyError:
                logger.info("Registered dataset ID collision; retrying insertion")
        raise RuntimeError("Failed to generate a unique registered dataset ID")

    @staticmethod
    def _can_manage_document(document, actor_id: str, is_admin: bool) -> bool:
        if is_admin:
            return True
        return bool(document.is_submission and document.created_by == actor_id)

    @staticmethod
    def _validate_domain(domain: str) -> None:
        from app.domain.services.domain_presets import get_domain_preset

        try:
            get_domain_preset(domain)
        except ValueError:
            raise BadRequestError("Unknown dataset domain") from None

    async def _managed_document(self, dataset_id: str, actor_id: str, is_admin: bool):
        # Legacy temporary submissions retain their dedicated owner/TTL
        # contract and are intentionally not promoted through metadata edits.
        if dataset_id.startswith("tds_"):
            raise NotFoundError("Dataset registration was not found")
        document = await DataCenterDatasetDocument.find_one({"dataset_id": dataset_id})
        if document is None or not self._can_manage_document(document, actor_id, is_admin):
            raise NotFoundError("Dataset registration was not found")
        return document

    async def update_registration(
        self,
        dataset_id: str,
        *,
        actor_id: str,
        is_admin: bool,
        name: str | None = None,
        description: str | None = None,
        domain: str | None = None,
    ) -> DataCenterDataset:
        """Update only browser-safe descriptive fields on an authorized record."""
        if name is None and description is None and domain is None:
            raise BadRequestError("At least one editable dataset field is required")
        document = await self._managed_document(dataset_id, actor_id.strip(), is_admin)
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise BadRequestError("Dataset name must not be blank")
            document.name = normalized_name
            if not document.is_submission:
                document.name_key = _name_key(normalized_name)
        if description is not None:
            document.description = description.strip()
        if domain is not None:
            normalized_domain = domain.strip()
            if not normalized_domain:
                raise BadRequestError("Dataset domain must not be blank")
            self._validate_domain(normalized_domain)
            document.domain = normalized_domain
        document.updated_at = _utc_now()
        try:
            await document.save()
        except DuplicateKeyError:
            raise BadRequestError("Dataset name already exists") from None
        return document.to_domain()

    async def archive_dataset(
        self,
        dataset_id: str,
        *,
        actor_id: str,
        is_admin: bool,
    ) -> DataCenterDataset:
        """Soft-disable a catalog record without deleting any source bytes."""
        document = await self._managed_document(dataset_id, actor_id.strip(), is_admin)
        if document.enabled:
            document.enabled = False
            document.updated_at = _utc_now()
            await document.save()
        return document.to_domain()

    async def _allocate_temporary_dataset_slots(
        self,
        owner_id: str,
        now: datetime,
    ) -> tuple[int, int]:
        """Reserve bounded owner/global slots for a temporary submission."""

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

    def _managed_dataset_dir(self, dataset_id: str) -> Path:
        relative = _safe_relative_path(dataset_id)
        if len(relative.parts) != 1:
            raise BadRequestError("Invalid dataset ID")
        return self._storage_root / dataset_id

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
