"""Dataset inventory to existing FileInfo, without materializing uploaded files."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import mimetypes
import re
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.errors.exceptions import NotFoundError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.core.config import get_settings
from app.domain.models.dataset import DatasetStorageType
from app.domain.models.file import FileInfo
from app.domain.services.dataset_file_paths import public_dataset_file_path, dataset_host_file_relative_path
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID
from app.infrastructure.repositories.mongo_dataset_preview_repository import MongoDatasetPreviewRepository

PREFIX = "dataset-preview:"
MAX_READ_BYTES = 64 * 1024 * 1024
_READ_SLOTS = asyncio.Semaphore(2)
_SIDECARS = {".shp", ".dbf", ".shx", ".prj", ".cpg"}


def strict_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError("Invalid dataset file path")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or "\\" in value
            or any(part in {".", ".."} for part in path.parts)
            or not path.parts or ":" in path.parts[0]
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Invalid dataset file path")
    return path


class DatasetFilePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        strict_relative_path(value)
        return value


class DatasetFilePreviewResponse(BaseModel):
    file: FileInfo
    related_files: list[FileInfo] = Field(default_factory=list)


def _default_catalog():
    # Lazy dependency prevents a factory/dependencies import cycle.
    from app.interfaces.dependencies import get_visualization_catalog
    return get_visualization_catalog()


class DatasetFilePreviewService:
    def __init__(self, *, datasets=None, catalog=None, repository=None, reader=None, settings=None):
        self.datasets = datasets or DataCenterDatasetService()
        self._catalog = catalog
        self.repository = repository or MongoDatasetPreviewRepository()
        self.reader = reader
        self.settings = settings or get_settings()

    @property
    def catalog(self):
        return self._catalog or _default_catalog()

    def _id(self, owner: str, dataset_id: str, path: str, anchor: str) -> str:
        identity = json.dumps([owner, dataset_id, path, anchor], ensure_ascii=False).encode()
        return PREFIX + hmac.new(self.settings.jwt_secret_key.encode(), b"dataset-preview-v1\0" + identity, hashlib.sha256).hexdigest()

    @staticmethod
    def _declared(dataset, path):
        strict_relative_path(path)
        matches = [item for item in dataset.files if item.path == path]
        if len(matches) != 1:
            raise FileNotFoundError("Dataset file not found")
        return matches[0]

    @classmethod
    def _public_declared(cls, dataset, path):
        """The browser submits its public path, never synthetic mount details."""
        requested = strict_relative_path(path)
        matches = [item for item in dataset.files if public_dataset_file_path(item.path, dataset.locations) == requested]
        if len(matches) != 1:
            raise FileNotFoundError("Dataset file not found or its public path is ambiguous")
        return cls._declared(dataset, matches[0].path)

    @classmethod
    def _visible_path(cls, dataset, internal_path):
        public_path = public_dataset_file_path(internal_path, dataset.locations)
        if public_path is None or cls._public_declared(dataset, str(public_path)).path != internal_path:
            raise FileNotFoundError("Dataset file not found")
        return str(public_path)

    @staticmethod
    def _source(dataset, path):
        logical = strict_relative_path(path)
        candidates = []
        for location in dataset.locations:
            if not location.verified or not location.read_only or location.node_id != LOCAL_DEFAULT_NODE_ID:
                continue
            if location.storage_type == DatasetStorageType.MANAGED_UPLOAD:
                # Managed seeds live only in the configured dataset volume.
                if location.source_path != dataset.dataset_id:
                    continue
                candidates.append((location, str(logical)))
            elif location.storage_type == DatasetStorageType.HOST_PATH:
                relative = dataset_host_file_relative_path(logical, location)
                if relative is not None:
                    candidates.append((location, str(strict_relative_path(str(relative)))))
        if len(candidates) != 1:
            raise FileNotFoundError("Dataset file location is unavailable")
        return candidates[0]

    async def prepare(self, dataset_id: str, owner: str, request: DatasetFilePreviewRequest):
        dataset = await self.datasets.get_dataset(dataset_id, user_id=owner)
        selected = self._public_declared(dataset, request.path)
        plugin = await self.catalog.require_enabled(owner, request.plugin_id)
        if not plugin.matches_filename(selected.path):
            raise ValueError("此可视化插件不支持当前文件格式。")
        anchor = selected.path
        paths = [anchor]
        if plugin.adapter == "shapefile":
            selected_path = strict_relative_path(anchor)
            # Same full logical path stem, never basename-only across folders.
            stem = str(selected_path.with_suffix(""))
            paths.extend(item.path for item in dataset.files
                         if item.path != anchor and PurePosixPath(item.path).suffix.lower() in _SIDECARS
                         and str(PurePosixPath(item.path).with_suffix("")) == stem)
        if len(paths) > 5:
            raise ValueError("Shapefile 配套文件声明不明确。")
        infos, references = [], []
        for path in paths:
            self._declared(dataset, path)
            row = {"owner_id": owner, "dataset_id": dataset_id, "path": path, "anchor": anchor}
            file_id = self._id(owner, dataset_id, path, anchor)
            # Probe safely before persisting a reference. No source bytes read.
            _, info = await self._read({"_id": file_id, **row}, owner, offset=0, length=0)
            references.append((file_id, row))
            infos.append(info)
        latest_plugin = await self.catalog.require_enabled(owner, request.plugin_id)
        if latest_plugin != plugin:
            raise PreviewVersionChanged("Visualization configuration changed; reopen the preview")
        for file_id, row in references:
            await self.repository.put(file_id, row)
        return DatasetFilePreviewResponse(file=infos[0], related_files=infos)

    async def _read(self, row, user_id, *, offset, length):
        owner = row["owner_id"]
        if user_id is not None and owner != user_id:
            raise FileNotFoundError("File not found")
        try:
            dataset = await self.datasets.get_dataset(row["dataset_id"], user_id=owner)
        except NotFoundError:
            raise FileNotFoundError("File not found") from None
        declaration = self._declared(dataset, row["path"])
        self._declared(dataset, row["anchor"])
        public_path = self._visible_path(dataset, row["path"])
        if row["anchor"] != row["path"]:
            self._visible_path(dataset, row["anchor"])
        catalog = await self.catalog.list_for_user(owner)
        matches = [item for item in catalog.plugins if item.enabled and item.matches_filename(row["anchor"])]
        if row["anchor"] != row["path"]:
            # Sidecars cannot be used as a way to read arbitrary sibling files.
            if (PurePosixPath(row["path"]).suffix.lower() not in _SIDECARS
                    or str(PurePosixPath(row["path"]).with_suffix("")) != str(PurePosixPath(row["anchor"]).with_suffix(""))):
                raise FileNotFoundError("File not found")
            matches = [item for item in matches if item.adapter == "shapefile"]
        if not matches:
            raise VisualizationDisabledError("当前文件没有已启用的可视化插件。")
        # Paging/FASTQ allow large sources but each read still obeys its budget.
        budget = min(MAX_READ_BYTES, max(item.limits.max_input_bytes for item in matches))
        if (type(offset) is not int or offset < 0 or offset > 2**63-1
                or (length is not None and (type(length) is not int or length < 0 or length > budget))):
            raise ValueError("Preview read exceeds its resource budget")
        location, relative = self._source(dataset, row["path"])
        if self.reader is None:
            from app.infrastructure.external.sandbox.dataset_preview_reader import read_dataset_host_file, read_managed_file
            if location.storage_type == DatasetStorageType.MANAGED_UPLOAD:
                reader = lambda: read_managed_file(self.settings.dataset_storage_root, f"{dataset.dataset_id}/{relative}", offset=offset, length=length, max_bytes=budget)
            else:
                reader = lambda: read_dataset_host_file(location.source_path, relative, offset=offset, length=length, configured_roots=self.settings.dataset_host_path_allowlist, max_bytes=budget)
        else:
            reader = lambda: self.reader(location, relative, offset=offset, length=length, max_bytes=budget)
        # Acquire before any bytes materialize and retain the slot until native
        # reads have actually stopped, including a cancelled browser request.
        try:
            await asyncio.wait_for(_READ_SLOTS.acquire(), timeout=5)
        except TimeoutError:
            raise ValueError("预览请求繁忙，请稍后重试。") from None
        task = asyncio.create_task(asyncio.to_thread(reader))
        try:
            data, stat = await asyncio.shield(task)
        except asyncio.CancelledError:
            def release(finished):
                _READ_SLOTS.release()
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(release)
            raise
        except BaseException:
            _READ_SLOTS.release()
            raise
        else:
            _READ_SLOTS.release()
        # Recheck catalog and dataset after native work, closing archive/toggle
        # races. No data is returned after access has been revoked.
        try:
            latest = await self.datasets.get_dataset(row["dataset_id"], user_id=owner)
        except NotFoundError:
            raise FileNotFoundError("File not found") from None
        self._declared(latest, row["path"])
        if self._visible_path(latest, row["path"]) != public_path:
            raise FileNotFoundError("Dataset file location changed")
        if row["anchor"] != row["path"]:
            self._visible_path(latest, row["anchor"])
        latest_location, latest_relative = self._source(latest, row["path"])
        if latest_location != location or latest_relative != relative:
            raise FileNotFoundError("Dataset file location changed")
        latest_catalog = await self.catalog.list_for_user(owner)
        if latest_catalog.revision != catalog.revision:
            raise PreviewVersionChanged("Visualization configuration changed; reopen the preview")
        enabled_ids = {item.id for item in latest_catalog.plugins if item.enabled and item.matches_filename(row["anchor"])}
        if not any(item.id in enabled_ids for item in matches):
            raise VisualizationDisabledError("当前文件没有已启用的可视化插件。")
        marker = hashlib.sha256(json.dumps([stat, location.location_id, location.version], sort_keys=True).encode()).hexdigest()
        info = FileInfo(file_id=row["_id"], filename=PurePosixPath(row["path"]).name,
                        size=stat["size"], content_type=declaration.content_type or mimetypes.guess_type(row["path"])[0] or "application/octet-stream",
                        user_id=owner, metadata={"source": "dataset_preview", "logical_path": public_path, "dataset_file_version": marker})
        return data, info

    async def _row(self, file_id):
        if not re.fullmatch(r"dataset-preview:[0-9a-f]{64}", file_id):
            raise FileNotFoundError("File not found")
        row = await self.repository.get(file_id)
        if not row:
            raise FileNotFoundError("File not found")
        return row

    async def get_file_info(self, file_id, user_id=None):
        try:
            _, info = await self._read(await self._row(file_id), user_id, offset=0, length=0)
            return info
        except (FileNotFoundError, VisualizationDisabledError):
            return None

    async def authorize_visualization_resource(self, source_id, resource_id, user_id, plugin_id):
        """Authorize a declared sidecar using private reference identities.

        Public logical paths alone are not identities: separate datasets can
        contain identically named files. This hook never exposes those private
        identities or reads source bytes.
        """
        try:
            source = await self._row(source_id)
            resource = await self._row(resource_id)
            if not user_id or any(row["owner_id"] != user_id for row in (source, resource)):
                return False
            if any(source[key] != resource[key] for key in ("dataset_id", "anchor")):
                return False
            source_path = strict_relative_path(source["path"])
            resource_path = strict_relative_path(resource["path"])
            if (source["path"] != source["anchor"] or source_path.suffix.lower() != ".shp"
                    or resource_path.suffix.lower() not in _SIDECARS
                    or source_path.with_suffix("") != resource_path.with_suffix("")):
                return False
            plugin = await self.catalog.require_enabled(user_id, plugin_id)
            if plugin.adapter != "shapefile":
                return False
            # Validate current dataset inventory, location, allowlist and
            # enabled capability, not merely the existence of a saved row.
            for row in (source, resource):
                await self._read(row, user_id, offset=0, length=0)
            return await self.catalog.require_enabled(user_id, plugin_id) == plugin
        except (FileNotFoundError, NotFoundError, VisualizationDisabledError, ValueError):
            return False

    async def download_file_range(self, file_id, user_id, *, offset, length):
        return await self._read(await self._row(file_id), user_id, offset=offset, length=length)

    async def download_file(self, file_id, user_id=None):
        data, info = await self._read(await self._row(file_id), user_id, offset=0, length=None)
        return io.BytesIO(data), info
