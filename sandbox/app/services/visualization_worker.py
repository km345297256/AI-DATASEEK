"""Versioned, bounded scientific preview worker; no Agent or shell execution.

Production runs this module in a one-shot, networkless, read-only container.
stdin contains one bounded JSON header line followed by exactly ``size`` bytes.
Only the fixed result envelope is written to stdout; parser diagnostics never
cross the browser boundary. Native readers live here, not in the API host.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_FASTQ_BYTES = 2 * 1024 * 1024
MAX_AXIS = 128
MAX_POINTS = 1000


class PreviewError(ValueError):
    """Only static, user-safe messages may be used here."""


def label(value, maximum=96):
    text = str(value or "")[:maximum]
    if re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|[A-Za-z]:\\)", text):
        return "[redacted]"
    return "".join(c for c in text if ord(c) >= 32)


def numbers(values, significant=10):
    import numpy as np
    array = np.ma.asarray(values, dtype=float).filled(float("nan")).ravel()
    return [
        (float(value) if significant is None else float(f"{float(value):.{significant}g}"))
        if math.isfinite(float(value)) else None for value in array
    ]


def base_result(reader, kind):
    return dict(contract_version=1, reader=reader, kind=kind, variables=[],
                selected_variable=None, x_label="", y_label="", x=[], y=[],
                width=0, height=0, values=[], extent=None, metadata={},
                warnings=[], sampled=False)


def _index(value, size):
    if type(value) is not int or not 0 <= value < size:
        raise PreviewError("切片索引超出维度范围。")
    return value


def _validate_options(options):
    if not isinstance(options, dict) or set(options) - {"variable", "x_dimension", "indices", "hdu"}:
        raise PreviewError("不支持的可视化参数。")
    for key in ("variable", "x_dimension"):
        if key in options and (not isinstance(options[key], str) or not 0 < len(options[key]) <= 128
                               or "/" in options[key] or "\\" in options[key] or label(options[key], 128) != options[key]):
            raise PreviewError("变量或维度标识无效。")
    indices = options.get("indices", {})
    if not isinstance(indices, dict) or len(indices) > 8 or any(
        not isinstance(k, str) or not 0 < len(k) <= 128 or "/" in k or "\\" in k
        or label(k, 128) != k or type(v) is not int or v < 0 for k, v in indices.items()
    ):
        raise PreviewError("切片参数无效。")
    if "hdu" in options and (type(options["hdu"]) is not int or not 0 <= options["hdu"] < 128):
        raise PreviewError("HDU 索引无效。")


def _coordinate_role(variable, name):
    standard = str(getattr(variable, "standard_name", "")).lower()
    units = str(getattr(variable, "units", "")).lower()
    short = name.lower()
    if (standard == "latitude" or (not standard and short in {"lat", "latitude"})) and units in {
        "", "degrees_north", "degree_north", "degrees_n", "degree_n", "degrees", "degree"
    }:
        return "lat"
    if (standard == "longitude" or (not standard and short in {"lon", "longitude"})) and units in {
        "", "degrees_east", "degree_east", "degrees_e", "degree_e", "degrees", "degree"
    }:
        return "lon"
    return None


def _axis(variable):
    import numpy as np
    if variable.ndim != 1 or not 2 <= variable.shape[0] <= 65536:
        raise PreviewError("地图需要有界的一维经纬度坐标；曲线网格请使用专用地图插件。")
    values = np.ma.asarray(variable[:], dtype=float)
    if np.ma.getmaskarray(values).any() or not np.isfinite(values).all():
        raise PreviewError("地图坐标包含缺失或无效值。")
    values = np.asarray(values)
    differences = np.diff(values)
    if not (np.all(differences > 0) or np.all(differences < 0)):
        raise PreviewError("地图坐标必须严格单调。")
    if not np.allclose(differences, differences[0], rtol=1e-4, atol=1e-7):
        raise PreviewError("本地图插件仅支持规则经纬网，不能把不规则坐标当等间距像素绘制。")
    return values


def _netcdf_default_variable(dataset, variables, kind):
    """Prefer actual data, not CF coordinate bounds or climatology helpers."""
    coordinate_names = {name for name, value in dataset.variables.items() if value.dimensions == (name,)}
    auxiliary_names = set()
    for value in dataset.variables.values():
        for attribute in ("bounds", "climatology", "ancillary_variables"):
            reference = getattr(value, attribute, "")
            if isinstance(reference, str):
                auxiliary_names.update(reference.split())
        reference = getattr(value, "coordinates", "")
        if isinstance(reference, str):
            coordinate_names.update(reference.split())

    eligible = list(variables)
    if kind == "map":
        latitude_dims, longitude_dims = set(), set()
        for name, coordinate in dataset.variables.items():
            if coordinate.ndim != 1:
                continue
            role = _coordinate_role(coordinate, name)
            if role == "lat":
                latitude_dims.add(coordinate.dimensions[0])
            elif role == "lon":
                longitude_dims.add(coordinate.dimensions[0])
        eligible = [name for name, value in variables.items()
                    if any(lat != lon and lat in value.dimensions and lon in value.dimensions
                           for lat in latitude_dims for lon in longitude_dims)]
    preferred = [name for name in eligible if name not in coordinate_names and name not in auxiliary_names]
    return next(iter(preferred or eligible), None)


def netcdf_preview(path, kind, options):
    import numpy as np
    from netCDF4 import Dataset
    if "hdu" in options or (kind == "map" and "x_dimension" in options):
        raise PreviewError("参数不适用于当前 NetCDF 视图。")
    result = base_result("netcdf", kind)
    with Dataset(path, mode="r") as dataset:
        if len(dataset.variables) > 1024 or len(dataset.dimensions) > 128:
            raise PreviewError("数据结构超出交互式预览上限，请使用分析工具。")
        variables = {}
        for name, variable in dataset.variables.items():
            if "/" in name or "\\" in name or label(name, 128) != name:
                continue
            if len(variable.dimensions) > 8 or any(label(d, 128) != d for d in variable.dimensions):
                continue
            try:
                numeric = np.dtype(variable.dtype).kind in "iuf"
            except TypeError:
                numeric = False
            if numeric and variable.ndim and all(variable.shape):
                variables[name] = variable
            if len(variables) == 128:
                result["warnings"].append("仅列出前 128 个可预览数值变量。")
                break
        if dataset.groups:
            result["warnings"].append("当前插件仅列出根组变量，嵌套组请使用领域分析工具。")
        result["variables"] = [dict(name=name, dimensions=[dict(name=d, size=int(s)) for d, s in zip(v.dimensions, v.shape)],
                                    shape=list(v.shape), units=label(getattr(v, "units", ""))) for name, v in variables.items()]
        selected = options.get("variable") or _netcdf_default_variable(dataset, variables, kind)
        if selected not in variables:
            if selected is None and kind == "map" and variables:
                raise PreviewError("此数据未找到具有独立一维经纬度的变量，请切换数值曲线视图。")
            raise PreviewError("未找到可预览的数值变量。")
        variable = variables[selected]
        result["selected_variable"] = selected
        result["y_label"] = label(selected, 128) + (f" ({label(getattr(variable, 'units', ''))})" if getattr(variable, "units", "") else "")
        indices = options.get("indices", {})
        if set(indices) - set(variable.dimensions):
            raise PreviewError("切片参数包含当前变量不存在的维度。")
        if kind == "series":
            axis = options.get("x_dimension") or next((d for d in variable.dimensions if d.lower() == "time"), variable.dimensions[0])
            if axis not in variable.dimensions:
                raise PreviewError("横轴不属于当前变量。")
            if axis in indices:
                raise PreviewError("横轴不能同时设置为固定切片。")
            n = variable.shape[variable.dimensions.index(axis)]
            step = max(1, math.ceil(n / MAX_POINTS))
            slicing = tuple(slice(None, None, step) if d == axis else _index(indices.get(d, 0), s)
                            for d, s in zip(variable.dimensions, variable.shape))
            result["y"] = numbers(variable[slicing])
            coordinate = dataset.variables.get(axis)
            if coordinate is not None and coordinate.dimensions == (axis,) and np.dtype(coordinate.dtype).kind in "iuf":
                x = numbers(coordinate[::step], significant=None)
                if any(value is None for value in x):
                    raise PreviewError("横轴坐标包含缺失值，请选择其他维度。")
                result["x"] = x
                result["x_label"] = axis + (f" ({label(getattr(coordinate, 'units', ''))})" if getattr(coordinate, "units", "") else "")
                calendar = getattr(coordinate, "calendar", None)
                if calendar:
                    result["metadata"]["calendar"] = label(calendar)
            else:
                result["x"] = list(range(0, n, step))
                result["x_label"] = axis + " (索引)"
                result["warnings"].append("未发现数值坐标，横轴使用索引，不代表物理时间或距离。")
            result["sampled"] = step > 1
            result["metadata"]["x_dimension"] = axis
            result["metadata"]["indices"] = {d: int(indices.get(d, 0)) for d in variable.dimensions if d != axis}
        elif kind == "map":
            mapping_name = getattr(variable, "grid_mapping", None)
            if mapping_name:
                mapping = dataset.variables.get(mapping_name)
                if mapping is None or getattr(mapping, "grid_mapping_name", None) != "latitude_longitude":
                    raise PreviewError("当前数据声明了非经纬度或未知投影，请使用支持坐标转换的地图插件。")
            coords = {}
            for name, candidate in dataset.variables.items():
                role = _coordinate_role(candidate, name)
                if role and candidate.ndim == 1 and candidate.dimensions[0] in variable.dimensions:
                    if role in coords:
                        raise PreviewError("经纬度坐标有歧义，请使用专用领域地图插件。")
                    coords[role] = candidate
            if set(coords) != {"lat", "lon"} or coords["lat"].dimensions == coords["lon"].dimensions:
                raise PreviewError("此变量没有可识别的独立一维经纬度，请切换数值曲线视图。")
            lat, lon = _axis(coords["lat"]), _axis(coords["lon"])
            if np.any(np.abs(lat) > 90) or np.any(lon < -180) or np.any(lon > 360) or np.ptp(lon) > 360:
                raise PreviewError("坐标超出合法经纬度范围。")
            normalized_lon = (lon + 180) % 360 - 180
            lon_order = np.argsort(normalized_lon)
            sorted_lon = normalized_lon[lon_order]
            if not np.allclose(np.diff(sorted_lon), abs(lon[1] - lon[0]), rtol=1e-4, atol=1e-7):
                raise PreviewError("当前区域跨越日期变更线或含重复经线，请使用支持该网格的地图插件。")
            lat_order = np.argsort(lat)[::-1]
            lat_dim, lon_dim = coords["lat"].dimensions[0], coords["lon"].dimensions[0]
            if set(indices) & {lat_dim, lon_dim}:
                raise PreviewError("地图经纬度不能同时设置为固定切片。")
            # Use evenly spaced index samples including both endpoints. The
            # browser consumes explicit x/y cell centres, not invented spacing.
            li = lat_order[np.linspace(0, len(lat)-1, min(MAX_AXIS, len(lat)), dtype=int)]
            lj = lon_order[np.linspace(0, len(lon)-1, min(MAX_AXIS, len(lon)), dtype=int)]
            slicing = tuple(li if d == lat_dim else lj if d == lon_dim else _index(indices.get(d, 0), s)
                            for d, s in zip(variable.dimensions, variable.shape))
            values = variable[slicing]
            kept_dims = [d for d in variable.dimensions if d in {lat_dim, lon_dim}]
            if kept_dims != [lat_dim, lon_dim]:
                values = values.T
            result.update(width=len(lj), height=len(li), values=numbers(values), x=numbers(normalized_lon[lj], significant=None), y=numbers(lat[li], significant=None),
                          x_label="经度 (°E)", y_label="纬度 (°N)",
                          extent=[float(sorted_lon[0]), float(min(lat)), float(sorted_lon[-1]), float(max(lat))],
                          sampled=len(li) < len(lat) or len(lj) < len(lon))
            result["metadata"].update(crs="EPSG:4326", indices={d: int(indices.get(d, 0)) for d in variable.dimensions if d not in {lat_dim, lon_dim}},
                                       spatial_dimensions=[lat_dim, lon_dim],
                                       value_label=label(selected, 128), units=label(getattr(variable, "units", "")))
        else:
            raise PreviewError("不支持的 NetCDF 视图。")
    if result["sampled"]:
        result["warnings"].append("这是有界索引抽样预览，不是全量统计或插值分析。")
    return result


def fits_preview(path, kind, options):
    import numpy as np
    from astropy.io import fits
    if "variable" in options or (kind == "image" and "x_dimension" in options):
        raise PreviewError("FITS 通过 HDU 选择数据；参数不适用于当前视图。")
    result = base_result("fits", kind)
    with fits.open(path, memmap=True, do_not_scale_image_data=True, lazy_load_hdus=True) as hdus:
        choices = []
        for index, hdu in enumerate(hdus):
            if index >= 128:
                result["warnings"].append("仅检查前 128 个 HDU。")
                break
            if (isinstance(hdu, (fits.PrimaryHDU, fits.ImageHDU))
                    and not isinstance(hdu, fits.CompImageHDU)
                    and hdu.shape and all(hdu.shape) and len(hdu.shape) <= 8):
                choices.append(dict(index=index, shape=list(hdu.shape), kind="image"))
        result["metadata"]["hdus"] = choices
        preferred = next((c["index"] for c in choices if len(c["shape"]) >= (2 if kind == "image" else 1)), None)
        chosen = options.get("hdu", preferred)
        choice = next((c for c in choices if c["index"] == chosen), None)
        if not choice:
            raise PreviewError("未找到适用的非压缩数值图像 HDU；表格或压缩 HDU 请使用领域分析工具。")
        hdu = hdus[chosen]
        shape = hdu.shape
        dims = [f"axis{i}" for i in range(len(shape))]
        result["variables"] = [dict(name=f"HDU {chosen}", dimensions=[dict(name=d, size=int(s)) for d, s in zip(dims, shape)], shape=list(shape), units=label(hdu.header.get("BUNIT", "")))]
        result["selected_variable"] = f"HDU {chosen}"
        result["metadata"]["hdu"] = chosen
        indices = options.get("indices", {})
        if set(indices) - set(dims):
            raise PreviewError("HDU 切片包含不存在的维度。")
        if kind == "image":
            if len(shape) < 2:
                raise PreviewError("该 HDU 为一维数据，请切换曲线视图。")
            keep = dims[-2:]
            if set(indices) & set(keep):
                raise PreviewError("图像横纵轴不能同时设置为固定切片。")
            slicing = tuple(slice(None, None, max(1, math.ceil(s / MAX_AXIS))) if d in keep else _index(indices.get(d, 0), s) for d, s in zip(dims, shape))
        elif kind == "series":
            axis = options.get("x_dimension") or dims[-1]
            if axis not in dims or axis in indices:
                raise PreviewError("HDU 曲线横轴或切片无效。")
            keep = [axis]
            slicing = tuple(slice(None, None, max(1, math.ceil(s / MAX_POINTS))) if d == axis else _index(indices.get(d, 0), s) for d, s in zip(dims, shape))
        else:
            raise PreviewError("不支持的 FITS 视图。")
        # section reads only selected pixels. Disable automatic scaling before
        # slicing; apply physical scaling and integer BLANK masking afterward.
        raw = np.asarray(hdu.section[slicing])
        values = raw.astype(float)
        if raw.dtype.kind in "iu" and "BLANK" in hdu.header:
            values[raw == hdu.header["BLANK"]] = np.nan
        values = values * float(hdu.header.get("BSCALE", 1)) + float(hdu.header.get("BZERO", 0))
        result["sampled"] = any(isinstance(s, slice) and (s.step or 1) > 1 for s in slicing)
        result["metadata"]["indices"] = {d: indices.get(d, 0) for d in dims if d not in keep}
        if kind == "image":
            result.update(width=int(values.shape[1]), height=int(values.shape[0]), values=numbers(values), x_label="像素 X", y_label="像素 Y")
            result["metadata"].update(spatial_dimensions=keep, orientation="array-row-zero-at-top")
            result["warnings"].append("显示数组像素平面，第 0 行在上方；未应用 FITS 天文显示方向或 WCS 天空坐标。")
        else:
            dim = dims.index(keep[0])
            result.update(x=list(range(0, shape[dim], slicing[dim].step)), y=numbers(values), x_label=keep[0] + " (像素索引)", y_label=label(hdu.header.get("BUNIT", "数值")))
            result["metadata"]["x_dimension"] = keep[0]
        if result["sampled"]:
            result["warnings"].append("这是索引抽样预览，不是重采样或积分光谱。")
    return result


def fastq_preview(data, options, truncated=False):
    if options:
        raise PreviewError("质量预览不接受变量或切片参数。")
    result = base_result("fastq", "quality")
    sums, counts = [0] * 500, [0] * 500
    reads, bases, gc = 0, 0, 0
    lengths = []
    stream = io.BytesIO(data)
    while reads < 1000:
        header = stream.readline()
        if not header:
            break
        sequence, plus, quality = stream.readline(), stream.readline(), stream.readline()
        # Only full four-line records are interpreted. Truncated final records
        # in a prefix are explicitly omitted, never counted as zero quality.
        if not sequence or not plus or not quality or (not quality.endswith(b"\n") and truncated and stream.tell() == len(data)):
            if truncated:
                break
            raise PreviewError("FASTQ 记录不完整；本插件支持标准四行记录。")
        sequence, quality = sequence.rstrip(b"\r\n"), quality.rstrip(b"\r\n")
        if not header.startswith(b"@") or not plus.startswith(b"+") or not sequence or len(sequence) != len(quality):
            raise PreviewError("FASTQ 格式无效；本插件支持未压缩的标准四行记录。")
        if any(c < 33 or c > 126 for c in quality) or any(c not in b"ACGTUNacgtunRYKMSWBDHVrykmswbdhv" for c in sequence):
            raise PreviewError("FASTQ 序列或质量字符无效。")
        reads += 1
        bases += len(sequence)
        gc += sum(c in b"GCgc" for c in sequence)
        lengths.append(len(sequence))
        for position, value in enumerate(quality[:500]):
            sums[position] += value - 33
            counts[position] += 1
    if not reads:
        raise PreviewError("有界预览范围内没有完整 FASTQ 记录。")
    length = max(i + 1 for i, n in enumerate(counts) if n)
    result.update(x=list(range(1, length + 1)), y=[round(sums[i] / counts[i], 5) for i in range(length)],
                  x_label="碱基位置", y_label="平均质量 (Phred+33)", sampled=truncated or stream.tell() < len(data) or max(lengths) > 500)
    result["metadata"].update(reads_sampled=reads, bases_sampled=bases, gc_percent=round(gc * 100 / bases, 3),
                               min_length=min(lengths), max_length=max(lengths), quality_encoding="Phred+33 (用户需确认)", position_counts=counts[:length])
    result["warnings"].append("按 Phred+33 解释质量；编码不能仅凭字符可靠推断。最多读取前 2 MiB、1,000 条记录和前 500 个碱基位置，结果不是全文件 QC。")
    return result


def preview_bytes(data, reader, kind, options=None, *, truncated=False):
    options = {} if options is None else options
    _validate_options(options)
    if type(truncated) is not bool:
        raise PreviewError("预览截断标记无效。")
    if not data or len(data) > MAX_INPUT_BYTES:
        raise PreviewError("文件为空或超出交互式预览限制。")
    if reader == "fastq" and kind == "quality":
        if len(data) > MAX_FASTQ_BYTES:
            raise PreviewError("FASTQ 输入超过有界采样上限。")
        return fastq_preview(data, options, truncated)
    if truncated:
        raise PreviewError("二进制格式预览需要完整的有界文件。")
    with tempfile.TemporaryDirectory(prefix="visualization-") as directory:
        path = Path(directory) / "input.bin"
        path.write_bytes(data)
        if reader == "netcdf" and kind in {"map", "series"}:
            return netcdf_preview(path, kind, options)
        if reader == "fits" and kind in {"image", "series"}:
            return fits_preview(path, kind, options)
    raise PreviewError("未注册的科学格式或视图。")


def _encoded_result():
    try:
        line = sys.stdin.buffer.readline(8193)
        if len(line) > 8192 or not line.endswith(b"\n"):
            raise PreviewError("无效的预览协议头。")
        header = json.loads(line)
        if (not isinstance(header, dict) or set(header) - {"contract_version", "size", "reader", "kind", "options", "truncated"}
                or type(header.get("contract_version")) is not int or header["contract_version"] != 1
                or type(header.get("size")) is not int or not 0 < header["size"] <= MAX_INPUT_BYTES):
            raise PreviewError("无效的预览协议版本或大小。")
        data = sys.stdin.buffer.read(header["size"])
        if len(data) != header["size"]:
            raise PreviewError("预览文件传输不完整。")
        if sys.stdin.buffer.read(1):
            raise PreviewError("预览文件传输包含多余数据。")
        result = {"ok": True, "data": preview_bytes(data, header["reader"], header["kind"], header.get("options"), truncated=header.get("truncated", False))}
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise PreviewError("可视化结果超出显示上限。")
    except PreviewError as error:
        encoded = json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False).encode()
    except Exception:
        encoded = json.dumps({"ok": False, "error": "无法安全解析此文件；请检查格式或使用领域分析工具。"}, ensure_ascii=False).encode()
    return encoded


@contextmanager
def _parser_output_to_stderr():
    """Keep Python and native parser diagnostics outside the JSON channel."""
    stdout_fd = sys.stdout.fileno()
    original_fd = os.dup(stdout_fd)
    try:
        sys.stdout.flush()
        os.dup2(sys.stderr.fileno(), stdout_fd)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        # Native readers may buffer C stdout independently of Python. Flush
        # while descriptor 1 still points at stderr, before restoring JSON.
        try:
            import ctypes
            ctypes.CDLL(None).fflush(None)
            sys.stderr.flush()
        finally:
            os.dup2(original_fd, stdout_fd)
            os.close(original_fd)


def main():
    with _parser_output_to_stderr():
        encoded = _encoded_result()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
