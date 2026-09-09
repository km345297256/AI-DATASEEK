"""Contract v2: owner- and capability-scoped, bounded offline previews.

Deliberately independent of AgentLoop and SSE. Native parsers only run in a
one-shot container with no dataset mounts, credentials or network access.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import _is_private_spill
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.infrastructure.external.sandbox.extended_visualization_worker import run_extended_visualization_worker
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError

MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
_SLOTS = asyncio.Semaphore(2)
_KINDS = {
    "tabular": {"table", "series", "heatmap"}, "hdf5": {"tree", "series", "heatmap"},
    "excel": {"table"}, "rdkit": {"image"}, "metpy": {"image"},
    "office": {"pdf"}, "fastqc": {"report"},
    "root": {"series", "heatmap"}, "jcamp": {"series"},
}
_OPTIONS = {
    "tabular": {"variable", "row_offset", "column_offset", "x_column", "y_columns", "indices"},
    "hdf5": {"path", "indices"}, "excel": {"sheet", "row_offset", "column_offset"},
    "rdkit": {"molecule"}, "metpy": {"pressure_column", "temperature_column", "dewpoint_column", "pressure_unit", "temperature_unit", "dewpoint_unit"},
    "office": set(), "fastqc": {"confirm"}, "binary": set(),
    "root": {"path"}, "jcamp": set(),
}
_DEFAULT_KIND = {"tabular": "table", "hdf5": "tree", "excel": "table", "rdkit": "image", "metpy": "image", "office": "pdf", "fastqc": "report", "root": "series", "jcamp": "series"}


class ExtendedPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["table", "series", "heatmap", "tree", "image", "pdf", "report"] | None = None
    options: dict[str, Any] = Field(default_factory=dict, max_length=10)


def validate_options(reader: str, options: dict) -> None:
    if set(options) - _OPTIONS.get(reader, set()):
        raise ScientificPreviewRejected("此读取器不接受这些参数。")
    for key, value in options.items():
        if key in {"row_offset", "column_offset", "x_column", "molecule"}:
            maximum = {"row_offset": 100000, "column_offset": 16383, "x_column": 99, "molecule": 99}[key]
            if type(value) is not int or not 0 <= value <= maximum:
                raise ScientificPreviewRejected("预览索引超出范围。")
        elif key in {"indices", "y_columns"}:
            if not isinstance(value, list) or len(value) > (6 if key == "indices" else 8) or any(type(v) is not int or v < 0 or v > (2**31 - 1 if key == "indices" else 99) for v in value):
                raise ScientificPreviewRejected("预览切片无效。")
        elif key == "confirm":
            if value is not True:
                raise ScientificPreviewRejected("质控任务需要明确确认。")
        elif not isinstance(value, str) or not value or len(value) > (512 if key == "path" else 256) or any(ord(c) < 32 for c in value) or "\\" in value:
            raise ScientificPreviewRejected("预览参数无效。")
        elif key == "path":
            if (reader == "hdf5" and not value.startswith("/")) or ".." in value.split("/"):
                raise ScientificPreviewRejected("仅支持 HDF5 内部节点路径。")
        elif "/" in value:
            raise ScientificPreviewRejected("预览参数不能包含文件路径。")
    if reader == "metpy":
        if set(options) != _OPTIONS[reader] or options["pressure_unit"] not in {"Pa", "hPa"} or any(options[key] not in {"K", "degC"} for key in ("temperature_unit", "dewpoint_unit")):
            raise ScientificPreviewRejected("请明确选择气压、温度、露点列及其单位。")
    if reader == "fastqc" and options.get("confirm") is not True:
        raise ScientificPreviewRejected("质控任务需要明确确认。")


def validate_payload(payload: Any, reader: str, kind: str, limit: int) -> dict:
    if not isinstance(payload, dict) or payload.get("contract_version") != 2 or payload.get("type") != reader or payload.get("reader") != reader or payload.get("kind") != kind:
        raise ValueError("Invalid preview envelope")
    allowed = {"contract_version", "type", "reader", "kind", "media_type", "data_base64", "table", "array", "tree", "choices", "selected", "metadata", "warnings", "sampled", "sections"}
    if set(payload) - allowed:
        raise ValueError("Unknown payload fields")
    if (type(payload.get("sampled")) is not bool or not isinstance(payload.get("metadata"), dict)
        or not isinstance(payload.get("warnings"), list) or len(payload["warnings"]) > 32
        or any(not isinstance(value, str) for value in payload["warnings"])):
        raise ValueError("Invalid preview diagnostics")
    count = 0
    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > 100000:
            raise ValueError("Preview complexity exceeded")
        if isinstance(value, str):
            if len(value) > 2048 or re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|[A-Za-z]:\\)", value):
                raise ValueError("Unsafe preview label")
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Nonfinite preview")
        elif value is not None and type(value) not in {bool, int}:
            raise ValueError("Unsupported preview value")
    visit({key: value for key, value in payload.items() if key != "data_base64"})
    if "data_base64" in payload:
        encoded = payload["data_base64"]
        if not isinstance(encoded, str) or len(encoded) > limit:
            raise ValueError("Invalid binary preview")
        decoded = base64.b64decode(encoded, validate=True)
        signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "application/pdf": b"%PDF-"}
        signature = signatures.get(payload.get("media_type"))
        if not signature or not decoded.startswith(signature):
            raise ValueError("Unsupported preview media")
    if "table" in payload:
        table = payload["table"]
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or len(table["columns"]) > 100 or not isinstance(table.get("rows"), list) or len(table["rows"]) > 200 or any(not isinstance(row, list) or len(row) > 100 for row in table["rows"]):
            raise ValueError("Invalid table window")
    if "array" in payload:
        array = payload["array"]
        if not isinstance(array, dict) or not isinstance(array.get("shape"), list) or len(array["shape"]) > 8 or not isinstance(array.get("values"), list) or len(array["values"]) > 16384 or any(type(v) is not int or v < 0 for v in array["shape"]) or math.prod(array["shape"]) != len(array["values"]):
            raise ValueError("Invalid numeric array")
        if any(value is not None and type(value) not in {int, float} for value in array["values"]):
            raise ValueError("Array values must be numeric or missing")
    if "tree" in payload and (not isinstance(payload["tree"], list) or len(payload["tree"]) > 256):
        raise ValueError("Invalid hierarchy")
    if "sections" in payload:
        if reader != "fastqc" or not isinstance(payload["sections"], list) or len(payload["sections"]) > 32:
            raise ValueError("Invalid QC sections")
        for section in payload["sections"]:
            if (not isinstance(section, dict) or set(section) != {"name", "status", "columns", "rows"}
                or not isinstance(section["name"], str) or section["status"] not in {"pass", "warn", "fail"}
                or not isinstance(section["columns"], list) or len(section["columns"]) > 20
                or any(not isinstance(value, str) for value in section["columns"])
                or not isinstance(section["rows"], list) or len(section["rows"]) > 1000
                or any(not isinstance(row, list) or len(row) > 20 or any(not isinstance(value, str) for value in row) for row in section["rows"])):
                raise ValueError("Invalid QC section table")
    if len(json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()) > limit:
        raise ValueError("Preview output limit exceeded")
    return payload


async def extended_visualization(file_service, catalog, image: str | None, file_id: str,
                                 user_id: str, request: ExtendedPreviewRequest, *, binary=False, allow_job=False,
                                 worker=run_extended_visualization_worker):
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    revision = (await catalog.list_for_user(user_id)).revision
    operation = "bytes" if binary else "job" if allow_job else "preview"
    if operation not in plugin.capabilities.operations or binary != (plugin.reader == "binary"):
        raise ScientificPreviewRejected("此插件不支持该读取接口。")
    reader = plugin.reader
    if reader == "fastqc" and not allow_job:
        raise ScientificPreviewRejected("完整质控必须通过明确启动的 AnalysisJob 执行。")
    validate_options(reader, request.options)
    kind = request.kind or _DEFAULT_KIND.get(reader)
    if not binary and kind not in _KINDS.get(reader, set()):
        raise ScientificPreviewRejected("此读取器不支持该视图。")
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    if not plugin.matches_filename(info.filename or ""):
        raise ScientificPreviewRejected("此插件不支持当前格式。")
    if type(info.size) is not int or not 0 < info.size <= min(MAX_INPUT, plugin.limits.max_input_bytes):
        raise ScientificPreviewRejected("文件超过此交互式插件的安全上限，请使用分析工具处理。")
    version = preview_version(info)
    if request.version and request.version != version:
        raise PreviewVersionChanged()
    if not binary and not image:
        raise VisualizationWorkerError("未配置隔离读取器镜像。")
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=5)
    except TimeoutError:
        raise VisualizationWorkerError("预览繁忙，请稍后重试。") from None
    cancel = threading.Event()
    release_here = True
    async def owned(operation):
        nonlocal release_here
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            release_here = False
            def completed(done):
                _SLOTS.release()
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(completed)
            raise
    try:
        read = getattr(file_service._file_storage, "download_file_range", None)
        if not callable(read):
            raise NotImplementedError("Bounded storage required")
        data, ranged = await owned(read(file_id, user_id, offset=0, length=info.size))
        if _is_private_spill(ranged):
            raise FileNotFoundError("File not found")
        if preview_version(ranged) != version:
            raise PreviewVersionChanged()
        if len(data) != info.size:
            raise ScientificPreviewRejected("范围读取不完整。")
        await catalog.require_enabled(user_id, request.plugin_id)
        if not binary:
            format = (info.filename or "").lower().rsplit(".", 1)[-1]
            if not re.fullmatch(r"[a-z0-9]{1,12}", format):
                raise ScientificPreviewRejected("无法识别文件格式。")
            response = await owned(asyncio.to_thread(worker, image, data, reader=reader, kind=kind,
                options=request.options, format=format, truncated=False, cancelled=cancel))
            if response.get("ok") is not True:
                # Parser messages can contain paths/data. Never forward them.
                raise ScientificPreviewRejected("此文件无法安全预览；请检查格式、参数及读取器依赖。")
            try:
                result = validate_payload(response.get("data"), reader, kind, min(MAX_OUTPUT, plugin.limits.max_output_bytes))
            except (ValueError, TypeError, OverflowError):
                raise VisualizationWorkerError("读取结果未通过 v2 插件协议校验。") from None
        current = await catalog.require_enabled(user_id, request.plugin_id)
        if current != plugin or (await catalog.list_for_user(user_id)).revision != revision:
            raise PreviewVersionChanged()
        latest = await file_service.get_file_info(file_id, user_id)
        if latest is None:
            raise FileNotFoundError("File not found")
        if preview_version(latest) != version:
            raise PreviewVersionChanged()
        # File providers may await I/O. Fence a disable occurring during that
        # final storage check before handing any bytes back to the browser.
        final_revision = (await catalog.list_for_user(user_id)).revision
        final_plugin = await catalog.require_enabled(user_id, request.plugin_id)
        if final_plugin != plugin or final_revision != revision:
            raise PreviewVersionChanged()
        if binary:
            return data, version
        return {**result, "version": version, "plugin_id": plugin.id, "revision": revision}
    finally:
        cancel.set()
        if release_here:
            _SLOTS.release()
