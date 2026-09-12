"""Pure, bounded UGRID 1.0 window contract. Kept identical in the API host."""
import json
import math
import re
import struct

FORMATS = {"nc", "nc4", "netcdf", "h5", "hdf5", "hdf"}
LIMITS = {"max_nodes": 4096, "max_faces": 2048, "max_face_nodes": 8, "max_fields": 32}
WARNING = "完整读取所选有界二维拓扑，仅读取显式索引的场切片；原生坐标平面不是地图，不投影、不跨日界线拼接、不插值或解码时间/垂向坐标。"
COORDINATES = "native coordinate plane; no projection or dateline wrapping"
VALUES = "raw storage values; declared fill and nonfinite masked; no CF scaling"
ERROR = "不支持的 UGRID 1.0 二维网格或选区超限；需要根组固定元数据及 NetCDF4 维度标识，可使用原始 HDF5 查看器检查。"

def fail():
    raise ValueError(ERROR)

def need(value):
    if not value:
        fail()

def keys(value, expected):
    need(type(value) is dict and set(value) == set(expected.split()))

def integer(value, low=0, high=2**31-1):
    return type(value) is int and low <= value <= high

def number(value):
    return type(value) in {int, float} and math.isfinite(value) and abs(value) <= (2**53-1 if type(value) is int else 1e100)

def label(value):
    return (type(value) is str and 1 <= len(value) <= 128 and not re.search(r"[<>\x00-\x1f\x7f]|/Users/|/home/|/tmp/|/private/|/var/|https?://|file:|[A-Za-z]:\\", value, re.I))

def identifier(value, prefix):
    return type(value) is str and re.fullmatch(prefix + r"-[0-9a-f]{32}", value) is not None

def dtype(value):
    return type(value) is str and re.fullmatch(r"(?:\|[iu]1|[<>][iu][248]|[<>]f[48])", value) is not None

def typed(value, code):
    if not number(value):
        return False
    if code[1] in "iu":
        bits = int(code[2])*8
        return type(value) is int and (0 <= value < 2**bits if code[1] == "u" else -2**(bits-1) <= value < 2**(bits-1))
    if code[2] == "4":
        try:
            return struct.unpack("f", struct.pack("f", value))[0] == value
        except (OverflowError, struct.error):
            return False
    return True

def face_geometry(coordinates, face):
    """Reject zero edges, degeneracy and self intersections; never repair faces."""
    points = [(coordinates[2*i], coordinates[2*i+1]) for i in face]
    x0, y0 = points[0]
    points = [(x-x0, y-y0) for x, y in points]
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    need(len(set(points)) == len(points))
    area = sum(points[i][0]*points[(i+1)%len(points)][1]-points[(i+1)%len(points)][0]*points[i][1] for i in range(len(points)))
    need(math.isfinite(area) and area != 0)
    def on(a, b, c):
        return cross(a, b, c) == 0 and min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i+1)%n]
        for j in range(i+1, n):
            if j == i+1 or i == 0 and j == n-1:
                continue
            c, d = points[j], points[(j+1)%n]
            need(not (cross(a,b,c)*cross(a,b,d) < 0 and cross(c,d,a)*cross(c,d,b) < 0 or on(a,b,c) or on(a,b,d) or on(c,d,a) or on(c,d,b)))

def validate_ugrid_window_options(kind, options):
    need(type(kind) is str and kind in {"tree", "geometry"})
    if kind == "tree":
        keys(options, "")
    else:
        keys(options, "mesh field indices")
        need(identifier(options["mesh"], "u") and (options["field"] is None or identifier(options["field"], "f")))
        need(type(options["indices"]) is list and len(options["indices"]) <= 7 and all(integer(v) for v in options["indices"]))
        need(options["field"] is not None or not options["indices"])
    return options

def validate_ugrid_window_payload(data, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    need(type(data) is dict)
    actual_kind = data.get("kind")
    need(type(actual_kind) is str and actual_kind in {"tree", "geometry"})
    keys(data, "contract_version type reader kind media_type selected choices metadata warnings sampled " + ("tree" if actual_kind == "tree" else "ugrid"))
    need(type(data["contract_version"]) is int and data["contract_version"] == 2 and data["type"] == data["reader"] == "ugrid-window" and data["media_type"] == "application/json")
    selected = validate_ugrid_window_options(actual_kind, data["selected"])
    need(data["warnings"] == [WARNING] and data["sampled"] is False)
    keys(data["choices"], "meshes")
    meshes = data["choices"]["meshes"]
    need(type(meshes) is list and 1 <= len(meshes) <= 8)
    ids = set()
    for mesh in meshes:
        keys(mesh, "id label node_count face_count max_face_nodes start_index coordinates fields")
        need(identifier(mesh["id"], "u") and mesh["id"] not in ids and label(mesh["label"]))
        ids.add(mesh["id"])
        need(integer(mesh["node_count"], 3, 4096) and integer(mesh["face_count"], 1, 2048) and integer(mesh["max_face_nodes"], 3, 8) and integer(mesh["start_index"], 0, 1))
        need(type(mesh["coordinates"]) is list and len(mesh["coordinates"]) == 2)
        for axis in mesh["coordinates"]:
            keys(axis, "label unit standard_name dtype")
            need(label(axis["label"]) and (axis["unit"] is None or label(axis["unit"])) and (axis["standard_name"] is None or label(axis["standard_name"])) and dtype(axis["dtype"]))
        need(type(mesh["fields"]) is list and len(mesh["fields"]) <= 32)
        field_ids = set()
        for field in mesh["fields"]:
            keys(field, "id label location dtype shape spatial_axis dimensions unit fill_values scale_factor add_offset")
            need(identifier(field["id"], "f") and field["id"] not in field_ids and label(field["label"]))
            field_ids.add(field["id"])
            need(type(field["location"]) is str and field["location"] in {"node", "face"} and dtype(field["dtype"]) and (field["unit"] is None or label(field["unit"])))
            shape = field["shape"]
            need(type(shape) is list and 1 <= len(shape) <= 8 and all(integer(v, 1) for v in shape) and math.prod(shape) <= 2**53-1)
            need(integer(field["spatial_axis"], 0, len(shape)-1) and shape[field["spatial_axis"]] == mesh[field["location"] + "_count"])
            dims = field["dimensions"]
            need(type(dims) is list and len(dims) == len(shape))
            for dim, count in zip(dims, shape):
                keys(dim, "label size")
                need(label(dim["label"]) and type(dim["size"]) is int and dim["size"] == count)
            need(len({v["label"] for v in dims}) == len(dims))
            need(type(field["fill_values"]) is list and len(field["fill_values"]) <= 2 and all(v == "NaN" or number(v) for v in field["fill_values"]))
            need(all("f" in field["dtype"] if v == "NaN" else typed(v, field["dtype"]) for v in field["fill_values"]))
            need(all(field[k] is None or number(field[k]) for k in ("scale_factor", "add_offset")))
    need(sum(len(m["fields"]) for m in meshes) <= 32)
    meta = data["metadata"]
    keys(meta, "format container conventions input_mode source_bytes read_bytes read_requests attribute_bytes chunks_touched decoded_chunk_bytes topology_complete coordinate_semantics value_semantics bounds missing_values limits")
    need(type(meta["format"]) is str and meta["format"] in FORMATS and meta["container"] == "HDF5" and meta["conventions"] == "UGRID-1.0" and meta["input_mode"] == "window")
    need(integer(meta["source_bytes"], 256, 8*1024**3) and integer(meta["read_bytes"], 1, 8*1024**2) and integer(meta["read_requests"], 1, 128))
    need(meta["read_bytes"] <= meta["read_requests"]*1024**2 and integer(meta["attribute_bytes"], 1, 65536) and integer(meta["chunks_touched"], 0, 128) and integer(meta["decoded_chunk_bytes"], 0, 16*1024**2))
    keys(meta["limits"], "max_nodes max_faces max_face_nodes max_fields")
    need(meta["coordinate_semantics"] == COORDINATES and meta["value_semantics"] == VALUES and meta["limits"] == LIMITS and all(type(v) is int for v in meta["limits"].values()))
    need(meta["topology_complete"] is (actual_kind == "geometry"))
    if actual_kind == "tree":
        expected = [{"path": "/"+m["id"], "node_type": "mesh", "attributes": {"label": m["label"]}} for m in meshes]
        need(data["tree"] == expected and meta["bounds"] is None and meta["missing_values"] == 0 and type(meta["missing_values"]) is int and meta["chunks_touched"] == meta["decoded_chunk_bytes"] == 0)
    else:
        mesh = next((m for m in meshes if m["id"] == selected["mesh"]), None)
        need(mesh is not None)
        field = next((f for f in mesh["fields"] if f["id"] == selected["field"]), None)
        need(selected["field"] is None or field is not None)
        if field:
            sizes = [v for i, v in enumerate(field["shape"]) if i != field["spatial_axis"]]
            need(len(sizes) == len(selected["indices"]) and all(v < n for v, n in zip(selected["indices"], sizes)))
        value = data["ugrid"]
        keys(value, "coordinates faces values location")
        coords, faces = value["coordinates"], value["faces"]
        need(type(coords) is list and len(coords) == 2*mesh["node_count"] and all(number(v) for v in coords))
        for a, axis in enumerate(mesh["coordinates"]):
            need(all(typed(v, axis["dtype"]) for v in coords[a::2]))
            if axis["standard_name"] == "longitude":
                need(max(coords[a::2])-min(coords[a::2]) <= 180)
            if axis["standard_name"] == "latitude":
                need(all(-90 <= v <= 90 for v in coords[a::2]))
        need(type(faces) is list and len(faces) == mesh["face_count"])
        for face in faces:
            need(type(face) is list and 3 <= len(face) <= mesh["max_face_nodes"] and all(integer(i, 0, mesh["node_count"]-1) for i in face) and len(set(face)) == len(face))
            face_geometry(coords, face)
        need(value["location"] == (field["location"] if field else None))
        if field:
            need(type(value["values"]) is list and len(value["values"]) == mesh[field["location"] + "_count"] and all(v is None or number(v) for v in value["values"]))
            need(all(v is None or typed(v, field["dtype"]) for v in value["values"]))
            need(not any(v is not None and v in field["fill_values"] for v in value["values"]))
        else:
            need(value["values"] is None)
        need(type(meta["missing_values"]) is int and meta["missing_values"] == sum(v is None for v in value["values"] or []))
        need(type(meta["bounds"]) is list and len(meta["bounds"]) == 2 and all(type(v) is list and len(v) == 2 and all(number(n) for n in v) for v in meta["bounds"]))
        need(meta["bounds"] == [[min(coords[a::2]), max(coords[a::2])] for a in range(2)])
    for expected, actual in ((kind, actual_kind), (options, selected), (fmt, meta["format"]), (source_bytes, meta["source_bytes"]), (read_bytes, meta["read_bytes"]), (read_requests, meta["read_requests"])):
        need(expected is None or expected == actual)
    need(len(json.dumps(data, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()) <= 2*1024**2-512)
    return data
