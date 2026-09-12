"""Authorized, true range-based previews; never downloads a complete source.

Window input budgets mean bytes fetched for this invocation, not permission to
materialize the source. Parsing stays in the networkless one-shot worker.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import re
import threading
import time

from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import _is_private_spill
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError
from app.infrastructure.external.sandbox.window_visualization_worker import (
    MAX_OUTPUT_BYTES, MAX_READ_BYTES, MAX_READS, MAX_SOURCE_BYTES, MAX_TOTAL_BYTES,
    WINDOW_PROFILES, run_window_visualization_worker,
)

_SLOTS = asyncio.Semaphore(2)
_CLEANUP_TASKS: set[asyncio.Task] = set()
READ_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 50
CALIBRATION = "physical_min+(digital-digital_min)*(physical_max-physical_min)/(digital_max-digital_min)"
WARNINGS = {"仅显示所选通道和相对时间窗；不重采样、不滤波、不用于诊断。", "标注与状态通道不作为波形读取，患者及记录身份字段不返回。"}
_SAFE_ERRORS = (FileNotFoundError, PreviewVersionChanged, ScientificPreviewRejected,
                VisualizationDisabledError, VisualizationNotFoundError, VisualizationWorkerError)


def _reader_options(reader, kind, options):
    try:
        if reader == "radar-window":
            from app.application.services.radar_window_visualization import validate_radar_window_options
            return validate_radar_window_options(kind, options)
        if reader == "ugrid-window":
            from app.application.services.ugrid_window_visualization import validate_ugrid_window_options
            return validate_ugrid_window_options(kind, options)
        if reader == "dicom-window":
            from app.application.services.dicom_window_visualization import validate_dicom_window_options
            return validate_dicom_window_options(kind, options)
        if reader == "spatial-window":
            from app.application.services.spatial_window_visualization import validate_spatial_window_options
            return validate_spatial_window_options(kind, options)
        if reader == "pointcloud-window":
            from app.application.services.pointcloud_window_visualization import validate_pointcloud_window_options
            return validate_pointcloud_window_options(kind, options)
        if reader == "ripple-window":
            from app.application.services.ripple_window_visualization import validate_ripple_window_options
            return validate_ripple_window_options(kind, options)
        if reader == "fcs-window":
            from app.application.services.fcs_window_visualization import validate_fcs_window_options
            return validate_fcs_window_options(kind, options)
        if reader == "envi-window":
            from app.application.services.envi_window_visualization import validate_envi_window_options
            return validate_envi_window_options(kind, options)
        if reader == "grib-window":
            from app.application.services.grib_window_visualization import validate_grib_window_options
            return validate_grib_window_options(kind, options)
        if reader == "seismic-window":
            from app.application.services.seismic_window_visualization import validate_seismic_window_options
            return validate_seismic_window_options(kind, options)
        if reader == "edf":
            return validate_window_options(options)
        if reader == "array-window":
            from app.application.services.array_window_visualization import validate_array_window_options
            return validate_array_window_options(kind, options)
        if reader == "columnar-window":
            from app.application.services.columnar_window_visualization import validate_columnar_window_options
            return validate_columnar_window_options(kind, options)
        if reader == "nexus-window":
            from app.application.services.nexus_window_visualization import validate_nexus_window_options
            return validate_nexus_window_options(kind, options)
        if reader == "czi-window":
            from app.application.services.czi_window_visualization import validate_czi_window_options
            return validate_czi_window_options(kind, options)
        if reader == "instrument-window":
            from app.application.services.instrument_visualization import validate_instrument_options
            return validate_instrument_options(kind, options)
        if reader == "ome-zarr":
            from app.application.services.ome_zarr_payload import validate_ome_options
            return validate_ome_options(kind, options)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ScientificPreviewRejected("分块预览参数无效，请重新选择变量、帧或区域。") from None
    raise ScientificPreviewRejected("窗口读取器未获批准。")


def _reader_payload(reader, value, *, kind, options, format, size, read_bytes, read_requests, limit):
    if reader == "radar-window":
        from app.application.services.radar_window_visualization import validate_radar_window_payload
        return validate_radar_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "ugrid-window":
        from app.application.services.ugrid_window_visualization import validate_ugrid_window_payload
        return validate_ugrid_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "dicom-window":
        from app.application.services.dicom_window_visualization import validate_dicom_window_payload
        return validate_dicom_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests, limit=limit)
    if reader == "spatial-window":
        from app.application.services.spatial_window_visualization import validate_spatial_window_payload
        return validate_spatial_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "pointcloud-window":
        from app.application.services.pointcloud_window_visualization import validate_pointcloud_window_payload
        return validate_pointcloud_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "ripple-window":
        from app.application.services.ripple_window_visualization import validate_ripple_window_payload
        return validate_ripple_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "fcs-window":
        from app.application.services.fcs_window_visualization import validate_fcs_window_payload
        return validate_fcs_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "envi-window":
        from app.application.services.envi_window_visualization import validate_envi_window_payload
        return validate_envi_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "grib-window":
        from app.application.services.grib_window_visualization import validate_grib_window_payload
        return validate_grib_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "seismic-window":
        from app.application.services.seismic_window_visualization import validate_seismic_window_payload
        return validate_seismic_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "edf":
        value = validate_window_payload(value, size=size, read_bytes=read_bytes, read_requests=read_requests, limit=limit)
        if value["metadata"]["format"] != format or any(value["selected"][k] != v for k, v in options.items()):
            raise ValueError("Window selection mismatch")
        return value
    if reader == "array-window":
        from app.application.services.array_window_visualization import validate_array_window_payload
        return validate_array_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "columnar-window":
        from app.application.services.columnar_window_visualization import validate_columnar_window_payload
        return validate_columnar_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "nexus-window":
        from app.application.services.nexus_window_visualization import validate_nexus_window_payload
        return validate_nexus_window_payload(value, kind=kind, options=options, fmt=format, source_bytes=size, read_bytes=read_bytes, read_requests=read_requests)
    if reader == "czi-window":
        from app.application.services.czi_window_visualization import validate_czi_window_payload
        value = validate_czi_window_payload(value, size=size, read_bytes=read_bytes, read_requests=read_requests, limit=limit)
        if value["kind"] != kind or (kind == "image" and value["selected"] != options):
            raise ValueError("CZI selection mismatch")
        return value
    if reader == "instrument-window":
        from app.application.services.instrument_visualization import validate_instrument_payload
        return validate_instrument_payload(value, size=size, read_bytes=read_bytes, read_requests=read_requests, kind=kind, options=options, format=format)
    if reader == "ome-zarr":
        from app.application.services.ome_zarr_payload import validate_ome_payload
        return validate_ome_payload(value, kind=kind, options=options, size=size, read_bytes=read_bytes, read_requests=read_requests)
    raise ValueError("Unsupported window result")


def _finite(value, *, positive=False):
    try:
        return type(value) in {int, float} and math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def validate_window_options(options):
    if not isinstance(options, dict) or set(options) - {"channels", "start_seconds", "duration_seconds"}:
        raise ScientificPreviewRejected("此窗口读取器不接受这些参数。")
    if "channels" in options:
        channels = options["channels"]
        if (not isinstance(channels, list) or not 1 <= len(channels) <= 8
                or any(type(value) is not int or not 0 <= value <= 255 for value in channels)
                or len(set(channels)) != len(channels)):
            raise ScientificPreviewRejected("请选择最多 8 个不同的有效通道。")
    if "start_seconds" in options and not _finite(options["start_seconds"]):
        raise ScientificPreviewRejected("窗口起始时间无效。")
    if "duration_seconds" in options and (not _finite(options["duration_seconds"], positive=True) or options["duration_seconds"] > 60):
        raise ScientificPreviewRejected("窗口时长需大于 0 且不超过 60 秒。")


def validate_window_payload(value, *, size, read_bytes, read_requests, limit):
    """Strict private EDF schema; identity fields and arbitrary metadata forbidden."""
    def require(ok):
        if not ok:
            raise ValueError("Invalid window result")

    def label(item, maximum):
        return (isinstance(item, str) and len(item) <= maximum
                and not re.search(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", item, re.I))

    require(isinstance(value, dict) and set(value) == {"contract_version", "type", "reader", "kind", "media_type", "series", "choices", "selected", "metadata", "warnings", "sampled"})
    require(type(value["contract_version"]) is int and value["contract_version"] == 2
            and value["type"] == value["reader"] == "edf" and value["kind"] == "series"
            and value["media_type"] == "application/json" and type(value["sampled"]) is bool)
    require(isinstance(value["warnings"], list) and len(value["warnings"]) <= 2 and all(isinstance(w, str) and w in WARNINGS for w in value["warnings"]))
    metadata, selected, choices, series = (value[k] for k in ("metadata", "selected", "choices", "series"))
    require(isinstance(metadata, dict) and set(metadata) == {"format", "variant", "total_duration_seconds", "record_duration_seconds", "records", "signal_count", "header_bytes", "source_bytes", "read_bytes", "read_requests", "identity_fields_hidden", "annotations_hidden", "no_resampling", "calibration", "limits", "time_origin", "continuity"})
    require(metadata["format"] in {"edf", "bdf"} and metadata["variant"] in {"EDF", "EDF+C", "BDF", "BDF+C"}
            and metadata["variant"].lower().startswith(metadata["format"]))
    require(all(metadata[k] is True for k in ("identity_fields_hidden", "annotations_hidden", "no_resampling")) and metadata["calibration"] == CALIBRATION)
    require(metadata["time_origin"] == "first data record; relative seconds" and metadata["continuity"] == "header-declared; annotation timeline not read")
    for key, minimum, maximum in (("records", 1, 2**63 - 1), ("signal_count", 1, 256), ("header_bytes", 256, MAX_READ_BYTES),
                                  ("source_bytes", size, size), ("read_bytes", read_bytes, read_bytes), ("read_requests", read_requests, read_requests)):
        require(type(metadata[key]) is int and minimum <= metadata[key] <= maximum)
    require(_finite(metadata["total_duration_seconds"], positive=True) and _finite(metadata["record_duration_seconds"], positive=True))
    require(math.isclose(metadata["total_duration_seconds"], metadata["record_duration_seconds"] * metadata["records"], rel_tol=1e-9))
    require(metadata["limits"] == {"max_samples": 16384, "max_channels": 8, "max_duration_seconds": 60, "max_header_bytes": MAX_READ_BYTES, "max_source_bytes": MAX_SOURCE_BYTES}
            and all(type(v) is int for v in metadata["limits"].values()))
    require(isinstance(choices, dict) and set(choices) == {"channels"} and isinstance(choices["channels"], list)
            and len(choices["channels"]) == metadata["signal_count"])
    by_id = {}
    for choice in choices["channels"]:
        require(isinstance(choice, dict) and set(choice) == {"id", "label", "unit", "sample_rate", "samples_per_record", "selectable", "channel_type", "physical_min", "physical_max", "digital_min", "digital_max"})
        require(type(choice["id"]) is int and 0 <= choice["id"] < metadata["signal_count"] and choice["id"] not in by_id)
        require(label(choice["label"], 16) and label(choice["unit"], 8) and _finite(choice["sample_rate"], positive=True)
                and type(choice["samples_per_record"]) is int and 0 < choice["samples_per_record"] <= 2**31 - 1
                and type(choice["selectable"]) is bool and choice["channel_type"] in {"signal", "annotation", "status"}
                and choice["selectable"] == (choice["channel_type"] == "signal"))
        require(math.isclose(choice["sample_rate"], choice["samples_per_record"] / metadata["record_duration_seconds"], rel_tol=1e-9))
        bits = 16 if metadata["format"] == "edf" else 24
        require(all(type(choice[k]) in {int, float} and math.isfinite(choice[k]) for k in ("physical_min", "physical_max"))
                and all(type(choice[k]) is int and -(2**(bits - 1)) <= choice[k] < 2**(bits - 1) for k in ("digital_min", "digital_max"))
                and choice["digital_min"] < choice["digital_max"])
        if choice["selectable"]:
            require(choice["physical_min"] != choice["physical_max"])
        by_id[choice["id"]] = choice
    require(isinstance(selected, dict) and set(selected) == {"channels", "start_seconds", "duration_seconds"})
    validate_window_options(selected)
    require(set(selected) == {"channels", "start_seconds", "duration_seconds"} and selected["start_seconds"] < metadata["total_duration_seconds"])
    require(selected["start_seconds"] + selected["duration_seconds"] <= metadata["total_duration_seconds"] + 1e-9)
    require(isinstance(series, list) and len(series) == len(selected["channels"]))
    samples = 0
    for index, item in enumerate(series):
        require(isinstance(item, dict) and set(item) == {"channel", "label", "unit", "sample_rate", "x", "y"})
        require(type(item["channel"]) is int and item["channel"] == selected["channels"][index] and item["channel"] in by_id)
        choice = by_id[item["channel"]]
        require(choice["selectable"] and _finite(item["sample_rate"], positive=True)
                and all(item[key] == choice[key] for key in ("label", "unit", "sample_rate")))
        require(isinstance(item["x"], list) and isinstance(item["y"], list) and 0 < len(item["x"]) == len(item["y"]) <= 16384)
        samples += len(item["x"])
        require(samples <= 16384 and all(_finite(x) for x in item["x"]) and all(type(y) in {int, float} and math.isfinite(y) for y in item["y"]))
        tolerance = max(1e-9, 1 / item["sample_rate"] * 1e-6)
        require(item["x"][0] >= selected["start_seconds"] - tolerance and item["x"][-1] < selected["start_seconds"] + selected["duration_seconds"] + tolerance)
        require(all(b > a and math.isclose(b - a, 1 / item["sample_rate"], rel_tol=1e-5, abs_tol=1e-9) for a, b in zip(item["x"], item["x"][1:])))
    require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit)
    return value


async def window_visualization(file_service, catalog, image, file_id, user_id, request,
                               *, worker=run_window_visualization_worker, resources=None, final_fence=None):
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    revision = (await catalog.list_for_user(user_id)).revision
    profile = WINDOW_PROFILES.get(plugin.reader)
    kind = request.kind or (profile["default_kind"] if profile else None)
    if (profile is None or plugin.adapter != profile["adapter"]
            or plugin.capabilities.input_mode != "window" or "preview" not in plugin.capabilities.operations
            or plugin.capabilities.shared or kind not in profile["kinds"]):
        raise ScientificPreviewRejected("此插件不支持受控窗口读取。")
    if plugin.reader != "edf" and kind != "tree" and request.version is None:
        raise ScientificPreviewRejected("请先读取结构，再携带同一版本明确选择数据窗口。")
    if kind == "tree" and request.version is None and ((plugin.reader == "seismic-window" and request.options) or (plugin.reader == "grib-window" and request.options.get("offset", 0) != 0)):
        raise ScientificPreviewRejected("目录分页必须携带首次目录的文件版本。")
    _reader_options(plugin.reader, kind, request.options)
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info) or info.user_id != user_id:
        raise FileNotFoundError("File not found")
    if not plugin.matches_filename(info.filename or ""):
        raise ScientificPreviewRejected("此插件不支持当前格式。")
    format = "zarr" if plugin.reader == "ome-zarr" else (info.filename or "").lower().rsplit(".", 1)[-1]
    if format not in profile["formats"] or type(info.size) is not int or not 0 < info.size <= MAX_SOURCE_BYTES:
        raise ScientificPreviewRejected("文件格式或源大小超过窗口预览边界。")
    version = preview_version(info)
    if request.version is not None and request.version != version:
        raise PreviewVersionChanged()
    if not image:
        raise VisualizationWorkerError("未配置隔离读取器镜像。")
    read = getattr(file_service._file_storage, "download_file_range", None)
    if not callable(read):
        raise NotImplementedError("Bounded storage required")
    budget = min(profile["total"], plugin.limits.max_input_bytes)
    output_budget = min(profile["output"], plugin.limits.max_output_bytes)
    limits = {"max_read_bytes": min(MAX_READ_BYTES, budget), "max_total_bytes": budget, "max_reads": profile["reads"]}
    if type(budget) is not int or budget <= 0 or type(output_budget) is not int or output_budget <= 0:
        raise ScientificPreviewRejected("窗口读取预算无效。")

    async def fence():
        if await catalog.require_enabled(user_id, request.plugin_id) != plugin:
            raise PreviewVersionChanged()
        latest = await file_service.get_file_info(file_id, user_id)
        if latest is None or _is_private_spill(latest) or latest.user_id != user_id:
            raise FileNotFoundError("File not found")
        if preview_version(latest) != version:
            raise PreviewVersionChanged()
        if (await catalog.list_for_user(user_id)).revision != revision or await catalog.require_enabled(user_id, request.plugin_id) != plugin:
            raise PreviewVersionChanged()

    await fence()
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=5)
    except TimeoutError:
        raise VisualizationWorkerError("窗口预览繁忙，请稍后重试。") from None
    cancelled = threading.Event()
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    pending = []
    counts = {"bytes": 0, "reads": 0}
    failures = []
    busy = False

    def check():
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise VisualizationWorkerError("窗口预览已取消或超时。")

    async def authorized_range(offset, length):
        nonlocal busy
        try:
            check()
            if (busy or type(offset) is not int or type(length) is not int or not 0 <= offset < info.size
                    or not 0 < length <= min(limits["max_read_bytes"], info.size - offset)
                    or counts["reads"] >= limits["max_reads"] or counts["bytes"] + length > budget):
                raise ScientificPreviewRejected("窗口范围读取越界或超过预算。")
            busy = True
            try:
                counts["reads"] += 1
                counts["bytes"] += length
                await fence()
                check()
                data, ranged = await read(file_id, user_id, offset=offset, length=length)
                check()
                if ranged is None or _is_private_spill(ranged) or ranged.user_id != user_id:
                    raise FileNotFoundError("File not found")
                if preview_version(ranged) != version:
                    raise PreviewVersionChanged()
                if type(data) is not bytes or len(data) != length:
                    raise ScientificPreviewRejected("窗口范围读取不完整。")
                await fence()
                check()
                return data
            finally:
                busy = False
        except Exception as error:
            safe = error if isinstance(error, _SAFE_ERRORS) else VisualizationWorkerError("窗口存储读取暂不可用。")
            failures.append(safe)
            raise safe from None

    def broker(offset, length):
        check()
        future = asyncio.run_coroutine_threadsafe(authorized_range(offset, length), loop)
        pending.append(future)
        read_deadline = min(deadline, time.monotonic() + READ_TIMEOUT_SECONDS)
        while True:
            check()
            if time.monotonic() >= read_deadline:
                raise VisualizationWorkerError("窗口单次范围读取超时。")
            try:
                return future.result(timeout=.1)
            except concurrent.futures.TimeoutError:
                continue

    async def owned():
        try:
            await fence()
            check()
            try:
                response = await asyncio.to_thread(worker, image, size=info.size, reader=plugin.reader, kind=kind, format=format,
                    options=request.options, limits=limits, read_range=broker, cancelled=cancelled, max_output_bytes=output_budget,
                    **({"resources": resources} if resources is not None else {}))
            except Exception:
                if failures:
                    raise failures[0]
                raise VisualizationWorkerError("隔离窗口预览暂不可用。") from None
            if failures:
                raise failures[0]
            check()
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise ScientificPreviewRejected("此文件无法安全预览；请检查格式、通道或时间窗。")
            try:
                result = _reader_payload(plugin.reader, response.get("data"), kind=kind, options=request.options, format=format,
                    size=info.size, read_bytes=counts["bytes"], read_requests=counts["reads"], limit=output_budget)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise VisualizationWorkerError("窗口结果未通过插件协议校验。") from None
            await fence()
            if final_fence is not None:
                await final_fence()
                await fence()
            check()
            output = {**result, "plugin_id": plugin.id, "version": version, "revision": revision}
            # Reserve and account for the public envelope before handing off to
            # normalize_result (which adds only kind/view_kind wrapper fields).
            if len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode()) + 256 > output_budget:
                raise VisualizationWorkerError("窗口结果超过此插件的输出预算。")
            return output
        finally:
            cancelled.set()

    def release_when_idle(done):
        if not done.cancelled():
            done.exception()
        if all(future.done() for future in pending):
            _SLOTS.release()
            return

        async def drain():
            try:
                # Do not cancel storage futures: cancellation may outlive native
                # I/O. A per-read timeout can return promptly while its slot is
                # still held through both container removal and native cleanup.
                await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)
            finally:
                _SLOTS.release()

        cleanup = asyncio.create_task(drain())
        _CLEANUP_TASKS.add(cleanup)
        cleanup.add_done_callback(_CLEANUP_TASKS.discard)

    task = asyncio.create_task(owned())
    release_here = True
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=REQUEST_TIMEOUT_SECONDS)
    except (asyncio.CancelledError, TimeoutError) as error:
        cancelled.set()
        release_here = False

        task.add_done_callback(release_when_idle)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise VisualizationWorkerError("窗口预览已取消或超时。") from None
    finally:
        cancelled.set()
        if release_here:
            release_when_idle(task)
