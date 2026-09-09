"""Authenticated, owner-scoped adapter from file storage to preview workers."""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.application.services.file_preview import preview_version, PreviewVersionChanged
from app.application.services.file_service import FileService, _is_private_spill
from app.infrastructure.external.sandbox.visualization_worker import run_visualization_worker, VisualizationWorkerError

_PREVIEW_SLOTS = asyncio.Semaphore(2)
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_FASTQ_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024


class ScientificPreviewRejected(ValueError):
    """Static public rejection or validated worker message, never a storage error."""


class ScientificVisualizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    variable: str | None = Field(default=None, min_length=1, max_length=128)
    x_dimension: str | None = Field(default=None, min_length=1, max_length=128)
    indices: dict[str, StrictInt] = Field(default_factory=dict, max_length=8)
    hdu: StrictInt | None = Field(default=None, ge=0, le=127)
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identifiers(self):
        if any("/" in value or "\\" in value or any(ord(c) < 32 for c in value)
               for value in [self.variable, self.x_dimension, *self.indices] if value is not None):
            raise ValueError("Invalid visualization identifier")
        if any(len(name) > 128 or not name or value < 0 or value > 2**53-1 for name, value in self.indices.items()):
            raise ValueError("Invalid slice index")
        return self


class PreviewDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=128)
    size: int = Field(gt=0, le=2**53-1)


class PreviewVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=128)
    dimensions: list[PreviewDimension] = Field(max_length=8)
    shape: list[int] = Field(max_length=8)
    units: str = Field(max_length=96)


class ScientificVisualizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    contract_version: Literal[1]
    reader: Literal["netcdf", "fits", "fastq"]
    kind: Literal["map", "series", "image", "quality"]
    variables: list[PreviewVariable] = Field(max_length=128)
    selected_variable: str | None = Field(max_length=128)
    x_label: str = Field(max_length=256)
    y_label: str = Field(max_length=256)
    x: list[float] = Field(max_length=1000)
    y: list[float | None] = Field(max_length=1000)
    width: int = Field(ge=0, le=128)
    height: int = Field(ge=0, le=128)
    values: list[float | None] = Field(max_length=16384)
    extent: list[float] | None = Field(max_length=4, min_length=4)
    metadata: dict[str, Any]
    warnings: list[str] = Field(max_length=32)
    sampled: bool
    version: str = ""
    plugin_id: str = ""
    revision: str = ""

    @model_validator(mode="after")
    def shape_matches(self):
        if self.kind in {"series", "quality"}:
            if len(self.x) != len(self.y) or not self.x or self.values or self.width or self.height or self.extent is not None:
                raise ValueError("Invalid series shape")
        else:
            if self.width * self.height != len(self.values) or not self.values:
                raise ValueError("Invalid image shape")
            if self.kind == "map":
                if len(self.x) != self.width or len(self.y) != self.height or not self.extent or any(v is None for v in self.y):
                    raise ValueError("Invalid map coordinates")
                if not all(-180 <= x <= 180 for x in self.x) or not all(-90 <= y <= 90 for y in self.y):
                    raise ValueError("Invalid map coordinate bounds")
                if not all(a < b for a, b in zip(self.x, self.x[1:])) or not all(a > b for a, b in zip(self.y, self.y[1:])):
                    raise ValueError("Invalid map coordinate order")
                if self.extent != [self.x[0], self.y[-1], self.x[-1], self.y[0]]:
                    raise ValueError("Invalid map extent")
            elif self.x or self.y or self.extent is not None:
                raise ValueError("Invalid image axes")
        if any(v.shape != [d.size for d in v.dimensions] for v in self.variables):
            raise ValueError("Invalid variable shape")
        return self


def _safe_result_payload(payload):
    count = 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if count > 40000 or depth > 10:
            raise ValueError("Preview nesting exceeds limit")
        if isinstance(value, str):
            if len(value) > 512 or re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|[A-Za-z]:\\)", value):
                raise ValueError("Unsafe preview label")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Non-finite preview value")
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif value is not None and type(value) not in (bool, int):
            raise ValueError("Unsupported preview value")
    visit(payload)
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("Preview output exceeds limit")


async def scientific_visualization(file_service: FileService, catalog, image: str | None,
                                   file_id: str, user_id: str, request: ScientificVisualizationRequest,
                                   *, worker=run_visualization_worker) -> ScientificVisualizationResult:
    # Catalog failures fail closed. Being present in a manifest is not an
    # implicit read grant, and one user's toggle never grants another access.
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    snapshot = await catalog.list_for_user(user_id)
    revision = snapshot.revision
    if "preview" not in plugin.capabilities.operations or plugin.reader not in {"netcdf", "fits", "fastq"}:
        raise ScientificPreviewRejected("此插件不使用科学数据读取接口。")
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    filename = (info.filename or "").lower()
    if not plugin.matches_filename(filename):
        raise ScientificPreviewRejected("此插件不支持当前文件格式。")
    if info.size is None or info.size <= 0:
        raise ScientificPreviewRejected("文件为空或缺少大小信息。")
    version = preview_version(info)
    if request.version and request.version != version:
        raise PreviewVersionChanged("文件已变化，请重新打开预览。")
    prefix = plugin.reader == "fastq"
    bound = min(plugin.limits.max_input_bytes, MAX_FASTQ_BYTES if prefix else MAX_INPUT_BYTES)
    if info.size > bound and not prefix:
        raise ScientificPreviewRejected("文件超过此交互式插件的读取上限，请使用领域分析工具。")
    if not image:
        raise VisualizationWorkerError("未配置隔离预览镜像。")
    options = request.model_dump(exclude_none=True, exclude={"plugin_id", "version"})
    if not options["indices"]:
        options.pop("indices")
    kind = "quality" if plugin.adapter == "scientific-quality" else plugin.view_kind
    try:
        await asyncio.wait_for(_PREVIEW_SLOTS.acquire(), timeout=5)
    except TimeoutError as error:
        raise VisualizationWorkerError("预览任务繁忙，请稍后重试。") from error
    cancel = threading.Event()
    release_here = True

    async def await_owned(operation):
        nonlocal release_here
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            # Both object-store range reads and Docker readers can own native
            # threads. Cancellation must not release their memory/concurrency
            # admission while those threads are still running.
            release_here = False
            def complete(finished):
                _PREVIEW_SLOTS.release()
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(complete)
            raise

    try:
        # Bound concurrency before materializing file bytes. Never fall back
        # to a provider's potentially full-buffered download implementation.
        read_range = getattr(file_service._file_storage, "download_file_range", None)
        if not callable(read_range):
            raise NotImplementedError("Storage does not support bounded previews")
        data, ranged_info = await await_owned(read_range(file_id, user_id, offset=0, length=min(info.size, bound)))
        if _is_private_spill(ranged_info):
            raise FileNotFoundError("File not found")
        if preview_version(ranged_info) != version:
            raise PreviewVersionChanged("文件已变化，请重新打开预览。")
        if len(data) != min(info.size, bound):
            raise ScientificPreviewRejected("文件预览范围读取不完整。")
        await catalog.require_enabled(user_id, request.plugin_id)
        result = await await_owned(asyncio.to_thread(worker, image, data, reader=plugin.reader, kind=kind,
                                                    options=options, truncated=info.size > len(data), cancelled=cancel))
        # Reject stale results after a user stops the plugin or its definition
        # changes. No worker output bypasses the current catalog preference.
        current = await catalog.require_enabled(user_id, request.plugin_id)
        latest = await catalog.list_for_user(user_id)
        if current != plugin or latest.revision != revision:
            raise PreviewVersionChanged("可视化插件已更新，请重新打开预览。")
        latest_info = await file_service.get_file_info(file_id, user_id)
        if latest_info is None:
            raise FileNotFoundError("File not found")
        if preview_version(latest_info) != version:
            raise PreviewVersionChanged("文件已变化，请重新打开预览。")
        if result.get("ok") is not True:
            detail = result.get("error", "无法预览此文件。")
            try:
                _safe_result_payload(detail)
            except ValueError:
                detail = "无法安全解析此文件。"
            raise ScientificPreviewRejected(detail if isinstance(detail, str) else "无法安全解析此文件。")
        payload = result.get("data")
        try:
            _safe_result_payload(payload)
            if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > min(MAX_OUTPUT_BYTES, plugin.limits.max_output_bytes):
                raise ValueError("Preview exceeds the plugin's declared output budget")
            checked = ScientificVisualizationResult.model_validate(payload)
            if checked.reader != plugin.reader or checked.kind != kind:
                raise ValueError("Worker returned a different reader or view")
        except (TypeError, ValueError) as error:
            raise VisualizationWorkerError("预览结果未通过插件协议校验。") from error
        final_revision = (await catalog.list_for_user(user_id)).revision
        final = await catalog.require_enabled(user_id, request.plugin_id)
        if final != plugin or final_revision != revision:
            raise PreviewVersionChanged("可视化插件已更新，请重新打开预览。")
        return checked.model_copy(update={"version": version, "plugin_id": plugin.id, "revision": revision})
    finally:
        cancel.set()
        if release_here:
            _PREVIEW_SLOTS.release()
