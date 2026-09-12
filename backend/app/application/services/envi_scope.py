"""Exact dataset header/raw pair; no header-selected path or sibling API."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import PurePosixPath

from app.application.services.dataset_file_preview import PREFIX
from app.application.services.envi_window_visualization import validate_envi_resources, validate_envi_window_options
from app.application.services.ome_zarr_scope import DatasetObjectScope
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.window_visualization import window_visualization
from app.domain.models.file import FileInfo

DATA_SUFFIXES = {".img", ".dat", ".bin", ".raw", ".bsq", ".bil", ".bip"}


class EnviDatasetScope(DatasetObjectScope):
    """Reuse existing identity/stat/range/final fences, not Zarr inventory rules."""
    def _inventory(self, row, dataset):
        header = PurePosixPath(row["path"])
        if header.suffix.lower() != ".hdr":
            raise ScientificPreviewRejected("ENVI 请从已登记数据集内的 .hdr 头文件打开。")
        self.previews._declared(dataset, row["path"])
        self.previews._visible_path(dataset, row["path"])
        base = header.with_suffix("")
        # Supported naming conventions are basename.hdr + basename.ext, and
        # basename.ext.hdr + basename.ext. Ambiguity always fails closed.
        def matches_name(path):
            if path == str(base):
                return True
            candidate = PurePosixPath(path)
            return base.suffix.lower() not in DATA_SUFFIXES and candidate.suffix.lower() in DATA_SUFFIXES and candidate.with_suffix("") == base
        matches = [item for item in dataset.files if matches_name(item.path)]
        if len(matches) != 1:
            raise ScientificPreviewRejected("ENVI 需要同目录唯一配对的原始数据文件；请检查缺失或同名歧义，不会跟随头文件中的路径。")
        entries, location = [], None
        for key, path in (("header", row["path"]), ("data", matches[0].path)):
            declared = self.previews._declared(dataset, path)
            self.previews._visible_path(dataset, path)
            source, relative = self.previews._source(dataset, path)
            if location is not None and source != location:
                raise ScientificPreviewRejected("ENVI 配对文件必须位于同一个已授权的只读位置。")
            location = source
            entries.append({"key": key, "path": path, "relative": relative, "declaration": declared.model_dump(mode="json")})
        signature = json.dumps([row["dataset_id"], row["path"], location.model_dump(mode="json"), entries], sort_keys=True, separators=(",", ":"))
        return entries, location, signature

    async def initialize(self):
        self.row, dataset = await self._dataset()
        self.entries, self.location, self.signature = self._inventory(self.row, dataset)
        self.stats = await self._stats(self.entries, self.location)
        await self.check_live()
        self.resources, offset = [], 0
        for entry, stat in zip(self.entries, self.stats, strict=True):
            if (not isinstance(stat, dict) or set(stat) != {"size", "mtime_ns", "ctime_ns", "inode", "device"}
                    or any(type(v) is not int for v in stat.values()) or any(stat[k] < 0 for k in ("size", "inode", "device"))):
                raise ScientificPreviewRejected("ENVI 配对元信息无效。")
            self.resources.append({"key": entry["key"], "offset": offset, "size": stat["size"]})
            offset += stat["size"]
        try:
            validate_envi_resources(self.resources, offset)
        except ValueError:
            raise ScientificPreviewRejected("ENVI 头文件需不超过 64 KiB，配对源总计不超过 8 GiB。") from None
        marker = hashlib.sha256(json.dumps([self.signature, self.stats], sort_keys=True).encode()).hexdigest()
        self.info = FileInfo(file_id=self.source_id, filename="cube.hdr", size=offset, user_id=self.owner,
            content_type="application/octet-stream", metadata={"source": "dataset_envi_scope", "dataset_file_version": marker})
        return self


async def envi_visualization(file_service, catalog, image, file_id, user_id, request, *, scope_factory=EnviDatasetScope, worker=None):
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    if plugin.reader != "envi-window" or plugin.adapter != "envi-window" or request.operation != "preview":
        raise ScientificPreviewRejected("此插件未获得 ENVI 双文件读取能力。")
    if not isinstance(file_id, str) or not file_id.startswith(PREFIX):
        raise ScientificPreviewRejected("ENVI 需从已登记数据集内的 .hdr 与原始数据配对打开，不支持单独上传头文件。")
    kind = request.kind or "tree"
    try:
        validate_envi_window_options(kind, request.options)
    except ValueError:
        raise ScientificPreviewRejected("ENVI 波段或区域参数无效。") from None
    if kind != "tree" and request.version is None:
        raise ScientificPreviewRejected("请先读取 ENVI 结构，再携带配对文件的同一版本读取窗口。")
    previews = getattr(file_service._file_storage, "previews", None)
    if previews is None:
        raise ScientificPreviewRejected("本存储没有受控数据集配对读取能力。")
    async with asyncio.timeout(70):
        scope = await scope_factory(previews, file_id, user_id).initialize()
        await catalog.require_enabled(user_id, request.plugin_id)
        return await window_visualization(scope, catalog, image, file_id, user_id, request,
            resources=scope.resources, final_fence=scope.verify_snapshot, **({"worker": worker} if worker is not None else {}))
