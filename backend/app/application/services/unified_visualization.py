"""One capability-driven public protocol for every trusted visualization.

Worker transport formats remain private implementation details. This boundary
owns authorization, lifecycle fences, typed results and bounded byte delivery;
it is deliberately independent of AgentLoop and SSE.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.application.services.extended_visualization import ExtendedPreviewRequest, extended_visualization
from app.application.services.file_preview import CSV_PAGE_BYTES, TEXT_PAGE_BYTES, PreviewVersionChanged, preview_version
from app.application.services.file_service import _is_private_spill
from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError
from app.application.services.scientific_visualization import (
    ScientificPreviewRejected, ScientificVisualizationRequest, VisualizationWorkerError,
    scientific_visualization,
)
from app.interfaces.schemas.file import public_filename

_BYTE_SLOTS = asyncio.Semaphore(2)
_CHUNK_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 8 * 1024 * 1024
_SCIENCE_READERS = {"netcdf", "fits", "fastq"}
_PROFILE_ADAPTERS = {"tiff", "plotly", "h5web", "viv", "openlayers"}
_MOLECULAR_FORMATS = {"cif": "cif", "pdb": "pdb", "ent": "pdb", "mol": "sdf", "sdf": "sdf", "xyz": "xyz", "mol2": "mol2", "vasp": "vasp"}


class VisualizationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["table", "series", "heatmap", "tree", "image", "pdf", "report", "map", "quality", "graph", "geometry"] | None = None
    options: dict[str, Any] = Field(default_factory=dict, max_length=10)


class VisualizationRequest(VisualizationJobRequest):
    operation: Literal["bytes", "page", "preview", "prepare"]


class VisualizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    contract_version: Literal[2] = 2
    plugin_id: str
    version: str
    revision: str
    kind: Literal["page", "series", "raster", "table", "array", "tree", "media", "report", "molecule", "resources", "features", "graph", "geometry"]
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    sampled: bool = False


class _PageOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offset: StrictInt = Field(default=0, ge=0)
    delimiter: Literal[",", "\t"] | None = None
    header_pending: bool = False


@dataclass
class VisualizationBytes:
    stream: BinaryIO
    size: int
    version: str
    revision: str
    plugin_id: str
    content_type: str
    _closed: bool = False

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.stream.close()
            finally:
                _BYTE_SLOTS.release()

    async def aclose(self):
        self.close()

    async def chunks(self):
        try:
            while data := self.stream.read(_CHUNK_BYTES):
                yield data
        finally:
            self.close()


def normalize_result(result) -> VisualizationResult:
    """Adapt validated internal worker results, never arbitrary worker output."""
    data = result.model_dump() if isinstance(result, BaseModel) else dict(result)
    common = {key: data.pop(key) for key in ("plugin_id", "version", "revision", "metadata", "warnings", "sampled")}
    data.pop("contract_version", None)
    reader = data.pop("reader", None)
    data.pop("type", None)
    view_kind = data.pop("kind")
    data["view_kind"] = view_kind
    from app.application.services.main_migration_visualization import KINDS as WORKBENCH_KINDS
    if reader in WORKBENCH_KINDS:
        if view_kind not in WORKBENCH_KINDS[reader]:
            raise VisualizationWorkerError("工作台结果不符合已声明的视图。")
        field = {"matrix-workbench":"matrix", "astronomy-workbench":"workbench", "alignment-browser":"alignment", "sequence-browser":"sequence", "genome-tracks":"tracks", "blast-hits":"hits"}[reader]
        if view_kind != "tree" and not isinstance(data.get(field), list if reader == "genome-tracks" else dict):
            raise VisualizationWorkerError("工作台结果缺少已声明的数据结构。")
        kind = "tree" if view_kind == "tree" else "array" if reader == "matrix-workbench" and view_kind == "image" else "raster" if reader == "astronomy-workbench" and view_kind == "image" else "features" if reader == "genome-tracks" else view_kind
    elif reader in {"spatial-window", "pointcloud-window", "gro-trajectory", "simulation-mesh", "ugrid-window"} and view_kind == "geometry":
        field = {"spatial-window":"spatial", "pointcloud-window":"array", "gro-trajectory":"trajectory", "simulation-mesh":"mesh", "ugrid-window":"ugrid"}[reader]
        if not isinstance(data.get(field), dict):
            raise VisualizationWorkerError("几何结果缺少已声明的结构。")
        kind = "geometry"
    elif reader in _SCIENCE_READERS:
        kind = "series" if view_kind in {"quality", "series"} else "raster"
    elif reader in {"edf", "seismic-window", "fcs-window", "diffraction"} and isinstance(data.get("series"), list):
        kind = "series"
    elif reader == "phylogeny" and isinstance(data.get("phylogeny"), dict):
        kind = "tree"
    elif reader == "geoformat" and isinstance(data.get("geojson"), dict):
        kind = "features"
    elif reader == "scientific-graph" and isinstance(data.get("graph"), dict):
        kind = "graph"
    elif isinstance(data.get("data_base64"), str):
        kind = "media"
    elif isinstance(data.get("sections"), list):
        kind = "report"
    elif isinstance(data.get("array"), dict):
        kind = "array"
    elif isinstance(data.get("table"), dict):
        kind = "table"
    elif isinstance(data.get("tree"), list):
        kind = "tree"
    else:
        raise VisualizationWorkerError("预览结果缺少已声明的结构化内容。")
    return VisualizationResult(kind=kind, payload=data, **common)


def check_output_budget(result: VisualizationResult, plugin):
    if len(json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False).encode()) > plugin.limits.max_output_bytes:
        raise ScientificPreviewRejected("预览结果超过此插件的输出上限。")


async def _file(file_service, file_id, user_id):
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    return info


async def _fence(file_service, catalog, file_id, user_id, plugin, revision, version):
    current = await _file(file_service, file_id, user_id)
    if preview_version(current) != version:
        raise PreviewVersionChanged()
    snapshot = await catalog.list_for_user(user_id)
    enabled = next((item for item in snapshot.plugins if item.id == plugin.id), None)
    if enabled is None:
        raise VisualizationNotFoundError("Visualization plugin not found")
    if not enabled.enabled:
        raise VisualizationDisabledError("Visualization plugin is disabled")
    if enabled.model_dump(exclude={"enabled"}) != plugin.model_dump() or snapshot.revision != revision:
        raise PreviewVersionChanged()


async def _related(file_service, source, resource, user_id, plugin):
    """A source's resource grant never becomes a generic owner-file reader."""
    a, b = source.metadata or {}, resource.metadata or {}
    if a.get("source") == "dataset_preview" or b.get("source") == "dataset_preview":
        check = getattr(file_service._file_storage, "authorize_visualization_resource", None)
        if not callable(check) or not await check(source.file_id, resource.file_id, user_id, plugin.id):
            raise FileNotFoundError("Resource not found")
        return
    if plugin.reader == "shapefile":
        def stem(info):
            metadata = info.metadata or {}
            value = str(metadata.get("logical_path") or metadata.get("file_path") or info.file_path or info.filename or "").replace("\\", "/")
            path = PurePosixPath(value)
            if ".." in path.parts or path.suffix.lower() not in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                raise FileNotFoundError("Resource not found")
            return str(path.with_suffix("")).casefold()
        if stem(source) != stem(resource):
            raise FileNotFoundError("Resource not found")
        if a.get("session_id") != b.get("session_id") or a.get("source_archive") != b.get("source_archive"):
            raise FileNotFoundError("Resource not found")
        return
    if plugin.id in {"html", "markdown"}:
        if a.get("session_id") and a.get("session_id") == b.get("session_id"):
            return
        if not a.get("session_id") and not b.get("session_id"):
            def internal_path(info):
                metadata = info.metadata or {}
                value = metadata.get("file_path") or info.file_path or metadata.get("logical_path")
                if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                    return None
                path = PurePosixPath(value.replace("\\", "/"))
                return path if ".." not in path.parts else None
            source_path, resource_path = internal_path(source), internal_path(resource)
            if source_path is not None and resource_path is not None:
                parent = source_path.parent
                # Bare filenames and a filesystem root do not prove a bundle.
                if any(part not in {"/", "."} for part in parent.parts) and resource_path.is_relative_to(parent):
                    return
    raise FileNotFoundError("Resource not found")


async def _bytes(file_service, catalog, file_id, user_id, plugin, revision, info, version):
    if type(info.size) is not int or info.size < 0 or info.size > plugin.limits.max_input_bytes:
        raise ScientificPreviewRejected("文件超过此插件的读取上限，请下载文件或使用分析工具。")
    if plugin.reader == "molecular" and info.size > 50 * 1024 * 1024:
        raise ScientificPreviewRejected("分子结构超过 50 MB 预览上限。")
    media_limit = {"video-player": 64 * 1024 * 1024, "audio-waveform": 16 * 1024 * 1024}.get(plugin.adapter)
    if media_limit is not None and info.size > media_limit:
        raise ScientificPreviewRejected("媒体文件超过此交互式预览的硬限制。")
    read = getattr(file_service._file_storage, "download_file_range", None)
    if not callable(read):
        raise NotImplementedError("Bounded storage required")
    try:
        await asyncio.wait_for(_BYTE_SLOTS.acquire(), timeout=5)
    except TimeoutError:
        raise VisualizationWorkerError("预览任务繁忙，请稍后重试。") from None
    spool = tempfile.SpooledTemporaryFile(max_size=_CHUNK_BYTES, mode="w+b")
    release_here = True
    transferred = False
    try:
        if info.size > _CHUNK_BYTES:
            spool.rollover()
        # Keep native/remote range setup to one request per 8 MiB while HTTP
        # output and in-memory spooling stay at 1 MiB. Total source budgets are
        # descriptor-specific; no source is materialized as one giant buffer.
        for offset in range(0, info.size, _READ_CHUNK_BYTES):
            length = min(_READ_CHUNK_BYTES, info.size - offset)
            operation = asyncio.create_task(read(file_id, user_id, offset=offset, length=length))
            try:
                data, ranged = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A cancelled coroutine must not free admission while a native
                # range-read thread still owns its bytes.
                release_here = False
                def complete(task):
                    _BYTE_SLOTS.release()
                    if not task.cancelled():
                        task.exception()
                operation.add_done_callback(complete)
                raise
            if _is_private_spill(ranged):
                raise FileNotFoundError("File not found")
            if preview_version(ranged) != version:
                raise PreviewVersionChanged()
            if len(data) != length:
                raise ScientificPreviewRejected("文件范围读取不完整。")
            spool.write(data)
            del data
            # Stop large reads promptly when the plugin is stopped.
            if await catalog.require_enabled(user_id, plugin.id) != plugin:
                raise PreviewVersionChanged()
        await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
        spool.seek(0)
        transferred = True
        return VisualizationBytes(spool, info.size, version, revision, plugin.id, info.content_type or "application/octet-stream")
    finally:
        if not transferred:
            spool.close()
            if release_here:
                _BYTE_SLOTS.release()


async def unified_visualization(file_service, catalog, image, file_id, user_id,
                                request: VisualizationRequest):
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    revision = (await catalog.list_for_user(user_id)).revision
    if request.operation not in plugin.capabilities.operations:
        raise ScientificPreviewRejected("此插件未声明该操作能力。")
    if plugin.reader == "ome-zarr":
        from app.application.services.ome_zarr_scope import ome_zarr_visualization
        result = normalize_result(await ome_zarr_visualization(file_service, catalog, image, file_id, user_id, request))
        check_output_budget(result, plugin)
        return result
    if plugin.reader == "envi-window":
        from app.application.services.envi_scope import envi_visualization
        result = normalize_result(await envi_visualization(file_service, catalog, image, file_id, user_id, request))
        check_output_budget(result, plugin)
        return result
    if plugin.reader == "ripple-window":
        from app.application.services.ripple_scope import ripple_visualization
        result = normalize_result(await ripple_visualization(file_service, catalog, image, file_id, user_id, request))
        check_output_budget(result, plugin)
        return result
    info = await _file(file_service, file_id, user_id)
    if not plugin.matches_filename(info.filename or ""):
        raise ScientificPreviewRejected("此插件不支持当前文件格式。")
    version = preview_version(info)
    if request.version and request.version != version:
        raise PreviewVersionChanged()
    from app.application.services.main_migration_visualization import KINDS as WORKBENCH_KINDS
    if plugin.reader in WORKBENCH_KINDS and request.kind not in {None, "tree"} and request.version is None:
        raise ScientificPreviewRejected("请先读取目录，再携带同一文件版本明确选择工作台数据。")
    if plugin.reader == "archive-member" and "member_id" in request.options and request.version is None:
        raise ScientificPreviewRejected("读取归档成员必须携带首次目录返回的文件版本。")
    if plugin.reader == "czi" and request.kind == "image" and request.version is None:
        raise ScientificPreviewRejected("请先读取 CZI 目录，再携带同一文件版本选择像素区域。")
    if plugin.reader in {"sqlite-table", "database-table", "sql-dump", "pg-dump", "bson", "redis-rdb"} and request.kind == "table" and request.version is None:
        raise ScientificPreviewRejected("请先读取目录，再携带文件版本选择数据表。")
    if plugin.reader in {"dicom-window", "spatial-window", "pointcloud-window", "gro-trajectory", "simulation-mesh", "radar-window", "ugrid-window"} and request.kind in {"geometry", "image"} and request.version is None:
        raise ScientificPreviewRejected("请先读取目录，再携带同一文件版本明确选择影像或几何数据。")
    if plugin.reader in {"array-window", "czi-window", "instrument-window", "nexus-window", "columnar-window", "seismic-window", "grib-window", "fcs-window", "mass-spectrum", "diffraction"} and request.kind in {"image", "series", "table"} and request.version is None:
        raise ScientificPreviewRejected("请先读取结构，再携带同一文件版本明确选择数据窗口。")
    if request.kind in {None, "tree"} and request.version is None and ((plugin.reader == "seismic-window" and request.options) or (plugin.reader in {"grib-window", "mass-spectrum"} and request.options.get("offset", 0) != 0)):
        raise ScientificPreviewRejected("目录分页必须携带首次目录的文件版本。")
    if request.operation == "bytes":
        if set(request.options) - {"resource_id"} or request.kind is not None:
            raise ScientificPreviewRejected("原始数据读取不接受额外参数。")
        resource_id = request.options.get("resource_id")
        if resource_id is not None:
            if not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 128 or any(ord(c) < 32 for c in resource_id):
                raise ScientificPreviewRejected("关联资源标识无效。")
            if resource_id != file_id:
                resource = await _file(file_service, resource_id, user_id)
                await _related(file_service, info, resource, user_id, plugin)
                result = await _bytes(file_service, catalog, resource_id, user_id, plugin, revision, resource, preview_version(resource))
                try:
                    await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
                    await _related(file_service, await _file(file_service, file_id, user_id),
                        await _file(file_service, resource_id, user_id), user_id, plugin)
                    await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
                    return result
                except BaseException:
                    result.close()
                    raise
        return await _bytes(file_service, catalog, file_id, user_id, plugin, revision, info, version)
    if request.operation == "page":
        if request.kind is not None or plugin.reader not in {"text", "csv"}:
            raise ScientificPreviewRejected("此插件不支持分页读取。")
        options = _PageOptions.model_validate(request.options)
        page_limit = CSV_PAGE_BYTES if plugin.reader == "csv" else TEXT_PAGE_BYTES
        if type(info.size) is int and min(page_limit, max(0, info.size - options.offset)) > plugin.limits.max_input_bytes:
            raise ScientificPreviewRejected("分页请求超过此插件的读取上限。")
        try:
            page = await file_service.preview_file(file_id, user_id, mode=plugin.reader,
                version=version, **options.model_dump())
        except ValueError as error:
            if isinstance(error, PreviewVersionChanged):
                raise
            raise ScientificPreviewRejected("此页无法预览；请使用 UTF-8 文本或 CSV，且单条记录小于 128 KiB。") from None
        if page.bytes_read > plugin.limits.max_input_bytes:
            raise ScientificPreviewRejected("分页结果超过此插件的读取上限。")
        result = VisualizationResult(plugin_id=plugin.id, version=version, revision=revision,
            kind="page", payload=page.model_dump(exclude={"version"}), sampled=page.next_offset is not None)
    elif request.operation == "prepare":
        if plugin.adapter in _PROFILE_ADAPTERS:
            if request.options or request.kind is not None:
                raise ScientificPreviewRejected("内容识别不接受路径、读取器或额外参数。")
            from app.application.services.visualization_probe import probe_visualization_file, VisualizationProbeRejected
            try:
                profile = await probe_visualization_file(file_service, catalog, file_id, user_id, plugin.id, version=version)
            except VisualizationProbeRejected:
                raise ScientificPreviewRejected("无法安全识别此文件头；可选择已有视图验证，或下载后分析。") from None
            result = VisualizationResult(plugin_id=plugin.id, version=version, revision=revision, kind="resources",
                payload={"profile": profile.model_dump(exclude={"plugin_id", "version", "revision"})},
                metadata={"purpose": "content-profile", "read_only": True}, sampled=profile.truncated)
            check_output_budget(result, plugin)
            await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
            return result
        if plugin.reader == "office-viewer":
            if request.options or request.kind is not None:
                raise ScientificPreviewRejected("办公查看器不接受额外地址或执行参数。")
            from app.application.services.onlyoffice_viewer import prepare_office_viewer
            return await prepare_office_viewer(file_service, catalog, file_id, user_id, plugin, revision, version)
        if plugin.reader != "molecular" or request.options or request.kind is not None:
            raise ScientificPreviewRejected("此插件不支持该准备操作。")
        name = public_filename(info.filename)
        format = "vasp" if name.casefold() in {"poscar", "contcar"} else _MOLECULAR_FORMATS.get(Path(name).suffix.casefold().lstrip("."))
        if format is None or type(info.size) is not int or not 0 <= info.size <= min(plugin.limits.max_input_bytes, 50 * 1024 * 1024):
            raise ScientificPreviewRejected("无法预览此分子结构，或文件超过 50 MB 上限。")
        periodic = format in {"cif", "vasp"}
        result = VisualizationResult(plugin_id=plugin.id, version=version, revision=revision, kind="molecule",
            payload={"source_name": name, "source_format": format, "content_type": info.content_type,
                "size_bytes": info.size, "periodic": periodic, "supports_unit_cell": periodic})
    elif plugin.capabilities.input_mode == "window":
        from app.application.services.window_visualization import window_visualization
        internal = ExtendedPreviewRequest(plugin_id=plugin.id, version=version, kind=request.kind, options=request.options)
        result = normalize_result(await window_visualization(file_service, catalog, image, file_id, user_id, internal))
    elif plugin.reader in _SCIENCE_READERS:
        kind = "quality" if plugin.reader == "fastq" else plugin.view_kind
        if request.kind not in {None, kind}:
            raise ScientificPreviewRejected("此插件不支持该视图。")
        if set(request.options) - {"variable", "x_dimension", "indices", "hdu"}:
            raise ScientificPreviewRejected("此读取器不接受这些参数。")
        internal = ScientificVisualizationRequest(plugin_id=plugin.id, version=version, **request.options)
        result = normalize_result(await scientific_visualization(file_service, catalog, image, file_id, user_id, internal))
    else:
        internal = ExtendedPreviewRequest(plugin_id=plugin.id, version=version, kind=request.kind, options=request.options)
        result = normalize_result(await extended_visualization(file_service, catalog, image, file_id, user_id, internal))
    check_output_budget(result, plugin)
    await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
    return result
