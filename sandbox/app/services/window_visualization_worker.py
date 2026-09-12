"""Private synchronous JSONL byte-capability worker; no paths or network.

The host grants only ranges of one version-pinned opaque source. Each range is
requested here, where untrusted file headers are parsed inside the existing
networkless/read-only/non-root worker boundary. This protocol is NOT public SSE.
"""
from __future__ import annotations

import base64
import binascii
import json
import sys

from app.services.bounded_signal_readers import (
    BoundedRangeSource, MAX_OUTPUT_BYTES, SignalPreviewError,
    edf_window_preview, validate_options,
)

PROTOCOL = "dataseek-window-v1"
MAX_INIT_BYTES = 8192
PROFILES = {
    "radar-window": ({"h5","hdf5"}, {"tree","image"}, 8*1024**2,128,2*1024**2),
    "ugrid-window": ({"nc","nc4","netcdf","h5","hdf5","hdf"}, {"tree","geometry"}, 8*1024**2,128,2*1024**2),
    "dicom-window": ({"dcm","dicom"}, {"tree","image"}, 8*1024**2,128,2*1024**2),
    "spatial-window": ({"h5ad"}, {"tree","geometry"}, 8*1024**2,128,2*1024**2),
    "pointcloud-window": ({"las"}, {"tree","geometry"}, 8*1024**2,128,2*1024**2),
    "fcs-window": ({"fcs"}, {"tree", "series"}, 8 * 1024**2, 128, 2 * 1024**2),
    "ripple-window": ({"rpl"}, {"tree", "image", "series"}, 8 * 1024**2, 256, 2 * 1024**2),
    "envi-window": ({"hdr"}, {"tree", "image", "series"}, 8 * 1024**2, 256, 2 * 1024**2),
    "grib-window": ({"grib", "grb", "grib2", "grb2"}, {"tree", "image"}, 8 * 1024**2, 128, 2 * 1024**2),
    "seismic-window": ({"mseed", "miniseed", "sac"}, {"tree", "series"}, 8 * 1024**2, 128, 2 * 1024**2),
    "columnar-window": ({"parquet", "parq", "arrow", "feather"}, {"tree", "table"}, 8 * 1024**2, 128, 2 * 1024**2),
    "nexus-window": ({"nxs", "nx", "h5", "hdf5", "hdf"}, {"tree", "series", "image"}, 8 * 1024**2, 128, 2 * 1024**2),
    "edf": ({"edf", "bdf"}, {"series"}, 8 * 1024**2, 128, 2 * 1024**2),
    "array-window": ({"h5", "hdf5", "hdf", "nc", "nc4", "netcdf", "mat"}, {"tree", "series", "image"}, 8 * 1024**2, 128, 2 * 1024**2),
    "czi-window": ({"czi"}, {"tree", "image"}, 32 * 1024**2, 4096, 8 * 1024**2),
    "instrument-window": ({"edf", "spe"}, {"tree", "image"}, 32 * 1024**2, 2048, 8 * 1024**2),
    "ome-zarr": ({"zarr"}, {"tree", "image"}, 32 * 1024**2, 256, 2 * 1024**2),
}


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise SignalPreviewError("无效的信号窗口协议。")
        value[key] = item
    return value


def _constant(_):
    raise SignalPreviewError("无效的信号窗口协议。")


def _receive(stream, maximum):
    line = stream.readline(maximum + 1)
    if not isinstance(line, bytes) or not line.endswith(b"\n") or len(line) > maximum:
        raise SignalPreviewError("无效的信号窗口协议。")
    value = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        raise SignalPreviewError("无效的信号窗口协议。")
    return value


def _send(stream, value, maximum):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > maximum:
        raise SignalPreviewError("信号窗口输出超过预览预算。")
    stream.write(encoded)
    stream.flush()


def run(input_stream, output_stream):
    """Emit exactly one terminal result, exposing no parser diagnostics."""
    try:
        init = _receive(input_stream, 256 * 1024)
        profile = PROFILES.get(init.get("reader")) if isinstance(init.get("reader"), str) else None
        expected = {"protocol", "type", "size", "reader", "kind", "format", "options", "limits"}
        if init.get("reader") in {"ome-zarr", "envi-window", "ripple-window"}:
            expected.add("resources")
        if (set(init) != expected or profile is None
            or init["protocol"] != PROTOCOL or init["type"] != "init"
            or init["kind"] not in profile[1] or init["format"] not in profile[0]
            or (init["reader"] != "ome-zarr" and len(json.dumps(init).encode()) > MAX_INIT_BYTES)
            or type(init["size"]) is not int or not 0 < init["size"] <= 8 * 1024**3):
            raise SignalPreviewError("无效的信号窗口协议。")
        limits = init["limits"]
        maxima = {"max_read_bytes": 1024**2, "max_total_bytes": profile[2], "max_reads": profile[3]}
        if (not isinstance(limits, dict) or set(limits) != set(maxima)
                or any(type(limits[k]) is not int or not 0 < limits[k] <= v for k, v in maxima.items())
                or limits["max_read_bytes"] > limits["max_total_bytes"]):
            raise SignalPreviewError("窗口读取预算无效。")
        sequence = total = 0

        def read_range(offset, length):
            nonlocal sequence, total
            if (type(offset) is not int or type(length) is not int or not 0 <= offset < init["size"]
                    or not 0 < length <= min(limits["max_read_bytes"], init["size"] - offset)
                    or sequence >= limits["max_reads"] or total + length > limits["max_total_bytes"]):
                raise SignalPreviewError("窗口范围超过授权预算。")
            sequence += 1
            total += length
            _send(output_stream, {"type": "read", "id": sequence, "offset": offset, "length": length}, 256)
            response = _receive(input_stream, 4 * ((length + 2) // 3) + 256)
            if (set(response) != {"type", "id", "data_base64"} or response["type"] != "bytes"
                or type(response["id"]) is not int or response["id"] != sequence
                or not isinstance(response["data_base64"], str)
                or len(response["data_base64"]) != 4 * ((length + 2) // 3)):
                raise SignalPreviewError("无效的信号窗口读取响应。")
            try:
                data = base64.b64decode(response["data_base64"], validate=True)
            except (ValueError, binascii.Error):
                raise SignalPreviewError("无效的信号窗口读取响应。") from None
            if len(data) != length or base64.b64encode(data).decode("ascii") != response["data_base64"]:
                raise SignalPreviewError("无效的信号窗口读取响应。")
            return data

        if init["reader"] == "radar-window":
            from app.services.radar_window_reader import radar_window_preview
            result = radar_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "ugrid-window":
            from app.services.ugrid_window_reader import ugrid_window_preview
            result = ugrid_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "dicom-window":
            from app.services.dicom_window_reader import dicom_window_preview
            result = dicom_window_preview(read_range, init["size"], init["kind"], init["options"], limits)
        elif init["reader"] == "spatial-window":
            from app.services.spatial_window_reader import spatial_window_preview
            result = spatial_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "pointcloud-window":
            from app.services.pointcloud_window_reader import pointcloud_window_preview
            result = pointcloud_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "ripple-window":
            from app.services.ripple_window_reader import ripple_window_preview
            result = ripple_window_preview(read_range, init["size"], init["resources"], init["kind"], init["options"], limits)
        elif init["reader"] == "fcs-window":
            from app.services.fcs_window_reader import fcs_window_preview
            result = fcs_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "envi-window":
            from app.services.envi_window_reader import envi_window_preview
            result = envi_window_preview(read_range, init["size"], init["resources"], init["kind"], init["options"], limits)
        elif init["reader"] == "grib-window":
            from app.services.grib_window_reader import grib_window_preview
            result = grib_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "seismic-window":
            from app.services.seismic_window_reader import seismic_window_preview
            result = seismic_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "edf":
            result = edf_window_preview(read_range, init["size"], init["format"], init["options"], limits)
        elif init["reader"] == "array-window":
            from app.services.array_window_reader import array_window_preview
            result = array_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "columnar-window":
            from app.services.columnar_window_reader import columnar_window_preview
            result = columnar_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "nexus-window":
            from app.services.nexus_window_reader import nexus_window_preview
            result = nexus_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        elif init["reader"] == "czi-window":
            from app.services.czi_window_reader import czi_window_preview
            result = czi_window_preview(read_range, init["size"], init["kind"], init["options"], limits)
        elif init["reader"] == "instrument-window":
            from app.services.instrument_window_reader import instrument_window_preview
            result = instrument_window_preview(read_range, init["size"], init["format"], init["kind"], init["options"], limits)
        else:
            from app.services.ome_zarr_reader import ome_zarr_preview
            result = ome_zarr_preview(read_range, init["size"], init["resources"], init["kind"], init["options"], limits)
        _send(output_stream, {"type": "result", "ok": True, "data": result}, profile[4])
    except Exception:
        # Deliberately no raw exception, file bytes, filesystem path or identity.
        _send(output_stream, {"type": "result", "ok": False, "error": "rejected"}, 256)


def main():
    run(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
