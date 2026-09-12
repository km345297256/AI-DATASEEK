"""Exact local dataset object scope, projected onto a private virtual byte source.

Only registered numeric NGFF level/chunk keys are granted. No object identities,
host paths, companion tokens, URLs or generic directory API reach the browser.
All member stats form the version; each range and the final snapshot are fenced.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import PurePosixPath

from app.application.errors.exceptions import NotFoundError
from app.application.services.dataset_file_preview import PREFIX
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.window_visualization import window_visualization
from app.domain.models.dataset import DatasetStorageType
from app.domain.models.file import FileInfo
from app.infrastructure.external.sandbox.dataset_preview_reader import (
    read_managed_file, read_dataset_host_file, stat_managed_files, stat_dataset_host_files,
)
from app.infrastructure.external.sandbox.window_visualization_worker import (
    MAX_SOURCE_BYTES, RESOURCE_KEY, validate_resources,
)

_SCOPE_SLOTS = asyncio.Semaphore(2)


async def _native(call):
    try:
        await asyncio.wait_for(_SCOPE_SLOTS.acquire(), 5)
    except TimeoutError:
        raise ScientificPreviewRejected("多对象预览繁忙，请稍后重试。") from None
    task = asyncio.create_task(asyncio.to_thread(call))

    def release(done):
        _SCOPE_SLOTS.release()
        if not done.cancelled():
            done.exception()

    task.add_done_callback(release)
    try:
        return await asyncio.wait_for(asyncio.shield(task), 20)
    except TimeoutError:
        raise ScientificPreviewRejected("多对象范围检查超时。") from None


class DatasetObjectScope:
    def __init__(self, previews, source_id, owner):
        self.previews, self.source_id, self.owner = previews, source_id, owner
        self._file_storage = self

    async def _dataset(self):
        row = await self.previews._row(self.source_id)
        if row["owner_id"] != self.owner or row["path"] != row["anchor"]:
            raise FileNotFoundError("Dataset resource scope not found")
        try:
            dataset = await self.previews.datasets.get_dataset(row["dataset_id"], user_id=self.owner)
        except NotFoundError:
            raise FileNotFoundError("Dataset resource scope not found") from None
        return row, dataset

    def _inventory(self, row, dataset):
        anchor = PurePosixPath(row["path"])
        if anchor.name != ".zattrs" or not anchor.parent.name.lower().endswith(".zarr"):
            raise ScientificPreviewRejected("请从已登记 .zarr 数据目录根部的 .zattrs 打开；不支持单独上传的元数据文件。")
        self.previews._declared(dataset, row["path"])
        self.previews._visible_path(dataset, row["path"])
        base = str(anchor.parent) + "/"
        entries, seen, location = [], set(), None
        for item in dataset.files:
            if not item.path.startswith(base):
                continue
            key = item.path[len(base):]
            if not RESOURCE_KEY.fullmatch(key):
                continue  # No permission to read labels, attachments or arbitrary siblings.
            if key in seen:
                raise ScientificPreviewRejected("数据目录存在重复资源，无法建立唯一作用域。")
            seen.add(key)
            self.previews._declared(dataset, item.path)
            self.previews._visible_path(dataset, item.path)
            source, relative = self.previews._source(dataset, item.path)
            if location is not None and source != location:
                raise ScientificPreviewRejected("同一分块图像必须位于一个已验证的只读位置。")
            location = source
            entries.append({"key": key, "path": item.path, "relative": relative, "declaration": item.model_dump(mode="json")})
            if len(entries) > 2048:
                raise ScientificPreviewRejected("本期每个图像作用域最多 2,048 个已登记资源。")
        if not {".zattrs", ".zgroup"} <= seen:
            raise ScientificPreviewRejected("OME-Zarr 需要同一目录内已登记的 .zattrs 和 .zgroup。")
        entries.sort(key=lambda item: item["key"])
        signature = json.dumps([row["dataset_id"], row["path"], location.model_dump(mode="json"), entries], sort_keys=True, separators=(",", ":"))
        return entries, location, signature

    async def _stats(self, entries, location):
        service = self.previews
        paths = [item["relative"] for item in entries]
        if service.reader is not None:
            return await _native(lambda: [service.reader(location, path, offset=0, length=0, max_bytes=1)[1] for path in paths])
        if location.storage_type == DatasetStorageType.MANAGED_UPLOAD:
            return await _native(lambda: stat_managed_files(service.settings.dataset_storage_root,
                [f"{self.row['dataset_id']}/{path}" for path in paths]))
        return await _native(lambda: stat_dataset_host_files(location.source_path, paths,
            configured_roots=service.settings.dataset_host_path_allowlist))

    async def initialize(self):
        self.row, dataset = await self._dataset()
        self.entries, self.location, self.signature = self._inventory(self.row, dataset)
        self.stats = await self._stats(self.entries, self.location)
        await self.check_live()
        self.resources, offset = [], 0
        for item, stat in zip(self.entries, self.stats, strict=True):
            if (not isinstance(stat, dict) or set(stat) != {"size", "mtime_ns", "ctime_ns", "inode", "device"}
                    or any(type(v) is not int for v in stat.values())
                    or any(stat[k] < 0 for k in ("size", "inode", "device"))):
                raise ScientificPreviewRejected("资源元信息无效。")
            self.resources.append({"key": item["key"], "offset": offset, "size": stat["size"]})
            offset += stat["size"]
        if not 0 < offset <= MAX_SOURCE_BYTES:
            raise ScientificPreviewRejected("图像作用域超过 8 GiB 源预算。")
        validate_resources(self.resources, offset)
        marker = hashlib.sha256(json.dumps([self.signature, self.stats], sort_keys=True).encode()).hexdigest()
        self.info = FileInfo(file_id=self.source_id, filename=".zattrs", size=offset, user_id=self.owner,
            content_type="application/json", metadata={"source": "dataset_zarr_scope", "dataset_file_version": marker})
        return self

    async def check_live(self):
        row, dataset = await self._dataset()
        _, _, signature = self._inventory(row, dataset)
        if signature != self.signature:
            raise PreviewVersionChanged()

    async def verify_snapshot(self):
        await self.check_live()
        current = await self._stats(self.entries, self.location)
        if current != self.stats:
            raise PreviewVersionChanged()
        await self.check_live()

    async def get_file_info(self, file_id, user_id):
        if file_id != self.source_id or user_id != self.owner:
            raise FileNotFoundError("Resource not found")
        await self.check_live()
        return self.info

    async def download_file_range(self, file_id, user_id, *, offset, length):
        await self.get_file_info(file_id, user_id)
        if type(offset) is not int or type(length) is not int or length < 1 or length > 1024**2:
            raise ScientificPreviewRejected("分块范围无效。")
        matches = [(i, resource) for i, resource in enumerate(self.resources)
                   if resource["offset"] <= offset and offset + length <= resource["offset"] + resource["size"]]
        if len(matches) != 1:
            raise ScientificPreviewRejected("分块读取不能跨越已授权对象边界。")
        index, resource = matches[0]
        relative, inner = self.entries[index]["relative"], offset - resource["offset"]
        service, location = self.previews, self.location
        if service.reader is not None:
            call = lambda: service.reader(location, relative, offset=inner, length=length, max_bytes=1024**2)
        elif location.storage_type == DatasetStorageType.MANAGED_UPLOAD:
            call = lambda: read_managed_file(service.settings.dataset_storage_root,
                f"{self.row['dataset_id']}/{relative}", offset=inner, length=length, max_bytes=1024**2)
        else:
            call = lambda: read_dataset_host_file(location.source_path, relative, offset=inner, length=length,
                configured_roots=service.settings.dataset_host_path_allowlist, max_bytes=1024**2)
        data, stat = await _native(call)
        if stat != self.stats[index]:
            raise PreviewVersionChanged()
        if type(data) is not bytes or len(data) != length:
            raise ScientificPreviewRejected("分块读取不完整。")
        await self.check_live()
        return data, self.info


async def ome_zarr_visualization(file_service, catalog, image, file_id, user_id, request, *, scope_factory=DatasetObjectScope, worker=None):
    if not isinstance(file_id, str) or not file_id.startswith(PREFIX):
        raise ScientificPreviewRejected("OME-Zarr 需从已登记本地数据集内的 .zarr/.zattrs 打开，不接受单独上传的文件。")
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    if plugin.reader != "ome-zarr" or plugin.adapter != "ome-zarr" or request.operation != "preview":
        raise ScientificPreviewRejected("此插件未获多对象读取批准。")
    from app.application.services.ome_zarr_payload import validate_ome_options
    kind = request.kind or "tree"
    try:
        validate_ome_options(kind, request.options)
    except ValueError:
        raise ScientificPreviewRejected("OME-Zarr 选择参数无效。") from None
    if kind == "image" and request.version is None:
        raise ScientificPreviewRejected("请先读取目录，再使用同一作用域版本读取像素。")
    previews = getattr(file_service._file_storage, "previews", None)
    if previews is None:
        raise ScientificPreviewRejected("本存储未提供受控数据集对象作用域。")
    async with asyncio.timeout(70):
        scope = await scope_factory(previews, file_id, user_id).initialize()
        await catalog.require_enabled(user_id, request.plugin_id)
        return await window_visualization(scope, catalog, image, file_id, user_id, request,
            resources=scope.resources, final_fence=scope.verify_snapshot, **({"worker": worker} if worker is not None else {}))
