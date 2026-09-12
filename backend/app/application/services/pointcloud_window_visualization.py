"""Pure bounded LAS window contract; mirrored in the API, without parsers."""
import json
import math

MAX_SOURCE = 8 * 1024**3
MAX_TOTAL = 8 * 1024**2
MAX_READ = 1024**2
MAX_READS = 128
MAX_OUTPUT = 2 * 1024**2
MAX_POINTS = 16384
FORMATS = {"las"}
RECORD_BYTES = {0: 20, 1: 28, 2: 26, 3: 34, 6: 30, 7: 36, 8: 38}
RGB_FORMATS = {2, 3, 7, 8}
SEMANTICS = "XYZ = raw int32 * scale + offset; no CRS transformation"
WARNING = "仅显示显式选择的连续点记录窗口，不代表全文件空间抽样；保留原始坐标、强度、分类及标志，不推断坐标系或单位。"
TREE = [{"path": "/points", "node_type": "array", "attributes": {"label": "LAS point records"}}]


class PointCloudWindowError(ValueError):
    pass


def require(condition, message="LAS 方言、点窗口或资源预算无效。"):
    if not condition: raise PointCloudWindowError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def finite(value):
    return type(value) in {int, float} and math.isfinite(value) and abs(value) <= 1e100


def keys(value, expected):
    return type(value) is dict and set(value) == set(expected)


def triple(value, predicate=finite):
    return type(value) is list and len(value) == 3 and all(predicate(v) for v in value)


def bounds(value):
    return (type(value) is list and len(value) == 3 and all(type(v) is list and len(v) == 2
            and all(finite(s) for s in v) and v[0] <= v[1] for v in value))


def validate_pointcloud_window_options(kind, options):
    require(type(kind) is str and kind in {"tree", "geometry"} and type(options) is dict)
    if kind == "tree":
        require(not options)
        return {}
    require(keys(options, {"point_offset", "point_count"}) and integer(options["point_offset"], 0, MAX_SOURCE // 20)
            and integer(options["point_count"], 1, MAX_POINTS))
    return dict(options)


def validate_pointcloud_window_payload(result, *, kind=None, options=None, fmt=None,
                                      source_bytes=None, read_bytes=None, read_requests=None):
    require(type(result) is dict and result.get("kind") in ("tree", "geometry"))
    view = result["kind"]
    require(keys(result, {"contract_version", "reader", "type", "kind", "media_type", "selected", "metadata", "warnings", "sampled"}
                 | ({"tree"} if view == "tree" else {"array", "point_attributes"})))
    require(integer(result["contract_version"], 2, 2) and result["reader"] == result["type"] == "pointcloud-window"
            and result["media_type"] == "application/json" and result["warnings"] == [WARNING]
            and type(result["sampled"]) is bool and (kind is None or kind == view))
    selected = validate_pointcloud_window_options(view, result["selected"])
    require(options is None or selected == validate_pointcloud_window_options(view, options))
    m = result["metadata"]
    require(keys(m, {"format", "input_mode", "source_bytes", "read_bytes", "read_requests", "metadata_bytes",
                     "las_version", "point_format", "record_bytes", "point_data_offset", "total_points", "extra_bytes_per_point",
                     "vlr_count", "evlr_count", "scales", "offsets", "declared_bounds", "window_bounds",
                     "crs_declarations", "units", "coordinate_semantics", "output_points", "point_bytes"}))
    require(m["format"] == "las" and (fmt is None or fmt == "las") and m["input_mode"] == "window"
            and m["las_version"] in ("1.2", "1.4") and integer(m["point_format"], 0, 8) and m["point_format"] in RECORD_BYTES)
    pf = m["point_format"]
    require(m["las_version"] == "1.4" or pf < 4)
    require(integer(m["source_bytes"], 227, MAX_SOURCE) and integer(m["read_bytes"], 227, MAX_TOTAL)
            and integer(m["read_requests"], 1, MAX_READS) and integer(m["metadata_bytes"], 227, MAX_READ)
            and integer(m["record_bytes"], RECORD_BYTES[pf], 65535)
            and integer(m["extra_bytes_per_point"], 0, 65535) and m["extra_bytes_per_point"] == m["record_bytes"] - RECORD_BYTES[pf]
            and integer(m["point_data_offset"], 227 if m["las_version"] == "1.2" else 375, m["source_bytes"])
            and integer(m["total_points"], 0, MAX_SOURCE // 20)
            and m["point_data_offset"] + m["record_bytes"] * m["total_points"] <= m["source_bytes"]
            and integer(m["vlr_count"], 0, 96) and integer(m["evlr_count"], 0, 96)
            and m["vlr_count"] + m["evlr_count"] <= 96 and (m["las_version"] == "1.4" or not m["evlr_count"])
            and m["metadata_bytes"] == (227 if m["las_version"] == "1.2" else 375) + m["vlr_count"] * 54 + m["evlr_count"] * 60)
    require(triple(m["scales"], lambda v: finite(v) and 1e-100 <= v <= 1e90) and triple(m["offsets"])
            and bounds(m["declared_bounds"]) and m["units"] == "unknown" and m["coordinate_semantics"] == SEMANTICS
            and type(m["crs_declarations"]) is list
            and m["crs_declarations"] == [v for v in ("geotiff", "wkt") if v in m["crs_declarations"]]
            and integer(m["output_points"], 0, MAX_POINTS) and integer(m["point_bytes"], 0, MAX_TOTAL))
    require(m["read_bytes"] == m["metadata_bytes"] + m["point_bytes"] and m["read_bytes"] >= m["read_requests"])
    minimum_reads = (1 if m["las_version"] == "1.2" else 2) + m["vlr_count"] + m["evlr_count"]
    require(m["read_requests"] >= minimum_reads + math.ceil(m["point_bytes"] / MAX_READ))
    for key, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        require(expected is None or type(expected) is int and m[key] == expected)
    if view == "tree":
        require(result["tree"] == TREE and result["sampled"] is False and m["window_bounds"] is None
                and m["output_points"] == m["point_bytes"] == 0 and m["read_requests"] == minimum_reads)
    else:
        n = selected["point_count"]
        require(selected["point_offset"] + n <= m["total_points"] and m["output_points"] == n
                and m["point_bytes"] == n * m["record_bytes"] and result["sampled"] == (n < m["total_points"]))
        a = result["array"]
        require(keys(a, {"shape", "dimensions", "values"}) and type(a["shape"]) is list
                and all(type(v) is int for v in a["shape"]) and a["shape"] == [n, 3]
                and a["dimensions"] == ["X_raw", "Y_raw", "Z_raw"] and type(a["values"]) is list
                and len(a["values"]) == 3 * n and all(integer(v, -2**31, 2**31-1) for v in a["values"]))
        actual_bounds = [[min(a["values"][d::3]) * m["scales"][d] + m["offsets"][d],
                          max(a["values"][d::3]) * m["scales"][d] + m["offsets"][d]] for d in range(3)]
        require(bounds(m["window_bounds"]) and m["window_bounds"] == actual_bounds)
        attrs = result["point_attributes"]
        require(keys(attrs, {"intensity", "classification", "classification_flags", "rgb"}))
        for field, high in {"intensity": 65535, "classification": 31 if pf < 6 else 255,
                            "classification_flags": 7 if pf < 6 else 15}.items():
            require(type(attrs[field]) is list and len(attrs[field]) == n and all(integer(v, 0, high) for v in attrs[field]))
        require((pf not in RGB_FORMATS and attrs["rgb"] is None) or (pf in RGB_FORMATS and type(attrs["rgb"]) is list
                and len(attrs["rgb"]) == n * 3 and all(integer(v, 0, 65535) for v in attrs["rgb"])))
    try:
        require(len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) <= MAX_OUTPUT - 512)
    except (ValueError, TypeError, UnicodeError):
        raise PointCloudWindowError("LAS 输出编码或大小无效。") from None
    return result
