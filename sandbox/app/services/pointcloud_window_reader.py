"""Read-only ASPRS LAS 1.2/1.4 fixed point records over an exact range capability.

No laspy/native parser, path, URL, codec or temporary file in production. VLR
and EVLR bodies and unknown extra dimensions are skipped, never interpreted.
"""
import copy
import struct

from .pointcloud_window_payload import (
    FORMATS, MAX_POINTS, MAX_READ, MAX_READS, MAX_SOURCE, MAX_TOTAL, RECORD_BYTES,
    RGB_FORMATS, SEMANTICS, TREE, WARNING, bounds, finite, integer, require,
    validate_pointcloud_window_options, validate_pointcloud_window_payload,
)


class _Source:
    def __init__(self, callback, size, limits):
        require(callable(callback) and integer(size, 227, MAX_SOURCE))
        maximum = {"max_read_bytes": MAX_READ, "max_total_bytes": MAX_TOTAL, "max_reads": MAX_READS}
        require(limits is None or type(limits) is dict and set(limits) == set(maximum)
                and all(integer(v, 1, maximum[k]) for k, v in limits.items()))
        self.callback, self.size, self.limits = callback, size, limits or maximum
        self.read_bytes = self.read_requests = 0

    def read(self, offset, length):
        require(integer(offset, 0, self.size - 1) and integer(length, 1, self.limits["max_read_bytes"])
                and offset + length <= self.size and self.read_bytes + length <= self.limits["max_total_bytes"]
                and self.read_requests < self.limits["max_reads"])
        self.read_requests += 1
        self.read_bytes += length
        value = self.callback(offset, length)
        require(type(value) is bytes and len(value) == length)
        return value


def _u(data, pos, width):
    return int.from_bytes(data[pos:pos + width], "little")


def _directory(source):
    h = source.read(0, 227)
    require(h[:4] == b"LASF", "首版仅支持未压缩 LAS，不支持 LAZ/COPC。")
    require(h[24] == 1 and h[25] in (2, 4), "首版仅支持 LAS 1.2/1.4 点格式 0–3、6–8。")
    version, minimum = "1." + str(h[25]), 227 if h[25] == 2 else 375
    if minimum > 227: h += source.read(227, minimum - 227)
    global_encoding, header_size, point_start, vlrs = _u(h, 6, 2), _u(h, 94, 2), _u(h, 96, 4), _u(h, 100, 4)
    pf, record_size, legacy_count = h[104], _u(h, 105, 2), _u(h, 107, 4)
    require(pf in RECORD_BYTES and (version == "1.4" or pf < 4), "不支持压缩点、COPC、波形点格式或此 LAS 版本。")
    require(not global_encoding & (~31 if version == "1.4" else ~1) and not global_encoding & 6,
            "首版不读取内置或外置波形。")
    require(minimum <= header_size <= point_start <= source.size and RECORD_BYTES[pf] <= record_size <= 65535)
    count = _u(h, 247, 8) if version == "1.4" else legacy_count
    require(integer(count, 0, MAX_SOURCE // 20) and point_start + count * record_size <= source.size)
    require(version != "1.4" or (legacy_count == 0 if pf >= 6 else legacy_count in (0, count)), "LAS 新旧点数声明冲突。")
    scales, offsets = list(struct.unpack_from("<3d", h, 131)), list(struct.unpack_from("<3d", h, 155))
    declared = [[struct.unpack_from("<d", h, 187 + d * 16)[0], struct.unpack_from("<d", h, 179 + d * 16)[0]] for d in range(3)]
    require(all(finite(v) and 1e-100 <= v <= 1e90 for v in scales) and all(finite(v) for v in offsets) and bounds(declared))
    evlr_start, evlrs = (_u(h, 235, 8), _u(h, 243, 4)) if version == "1.4" else (0, 0)
    require(version != "1.4" or _u(h, 227, 8) == 0, "首版不读取波形包。")
    require(vlrs + evlrs <= 96 and ((not evlrs and evlr_start == 0) or
            evlrs and point_start + count * record_size <= evlr_start < source.size))
    declarations = set()

    def inspect_record(raw, extended):
        user = raw[2:18].split(b"\0", 1)[0].rstrip(b" ").lower()
        identifier = _u(raw, 18, 2)
        require(user not in {b"laszip encoded", b"copc"}, "首版不支持 LAZ/COPC，不能把压缩块当作 LAS 点。")
        # Only an inert presence marker. Never return/free-parse WKT, GeoTIFF,
        # user strings, creation names, provenance or opaque extension bodies.
        if user == b"lasf_projection":
            if identifier in (34735, 34736, 34737): declarations.add("geotiff")
            if identifier in (2111, 2112): declarations.add("wkt")
        require(_u(raw, 0, 2) == 0)
        return _u(raw, 20, 8 if extended else 2)

    pos = header_size
    for _ in range(vlrs):
        require(pos + 54 <= point_start)
        length = inspect_record(source.read(pos, 54), False)
        require(pos + 54 + length <= point_start)
        pos += 54 + length
    pos = evlr_start
    for _ in range(evlrs):
        require(pos + 60 <= source.size)
        length = inspect_record(source.read(pos, 60), True)
        require(pos + 60 + length <= source.size)
        pos += 60 + length
    return {"format": "las", "input_mode": "window", "source_bytes": source.size,
            "las_version": version, "point_format": pf, "record_bytes": record_size, "point_data_offset": point_start,
            "total_points": count, "extra_bytes_per_point": record_size - RECORD_BYTES[pf], "vlr_count": vlrs, "evlr_count": evlrs,
            "scales": scales, "offsets": offsets, "declared_bounds": declared, "window_bounds": None,
            "crs_declarations": sorted(declarations), "units": "unknown", "coordinate_semantics": SEMANTICS,
            "metadata_bytes": source.read_bytes, "point_bytes": 0, "output_points": 0}


def pointcloud_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    require(type(fmt) is str and fmt in FORMATS, "首版仅支持 .las；LAZ/COPC 不在支持范围内。")
    selection = validate_pointcloud_window_options(kind, {} if options is None else options)
    source = _Source(read_range, size, limits)
    m = _directory(source)
    result = {"contract_version": 2, "type": "pointcloud-window", "reader": "pointcloud-window", "kind": kind,
              "media_type": "application/json", "selected": selection, "metadata": m, "warnings": [WARNING], "sampled": False}
    if kind == "tree":
        result["tree"] = copy.deepcopy(TREE)
    else:
        offset, count, stride, pf = selection["point_offset"], selection["point_count"], m["record_bytes"], m["point_format"]
        require(offset + count <= m["total_points"], "点窗口超出文件声明点数。")
        point_bytes = count * stride
        require(source.read_bytes + point_bytes <= source.limits["max_total_bytes"], "所选点窗口含额外维度，读取量超预算；请减少点数。")
        per_read = source.limits["max_read_bytes"] // stride
        require(per_read > 0 and source.read_requests + (count + per_read - 1) // per_read <= source.limits["max_reads"])
        raw_xyz, intensity, classification, flags = [], [], [], []
        rgb = [] if pf in RGB_FORMATS else None
        for first in range(0, count, per_read):
            rows = min(per_read, count - first)
            chunk = source.read(m["point_data_offset"] + (offset + first) * stride, rows * stride)
            for row in range(rows):
                base = row * stride
                raw_xyz.extend(struct.unpack_from("<3i", chunk, base))
                intensity.append(_u(chunk, base + 12, 2))
                classification.append(chunk[base + 15] & 31 if pf < 6 else chunk[base + 16])
                flags.append(chunk[base + 15] >> 5 if pf < 6 else chunk[base + 15] & 15)
                if rgb is not None:
                    rgb_offset = 20 if pf == 2 else 28 if pf == 3 else 30
                    rgb.extend(struct.unpack_from("<3H", chunk, base + rgb_offset))
        m.update(point_bytes=point_bytes, output_points=count,
                 window_bounds=[[min(raw_xyz[d::3]) * m["scales"][d] + m["offsets"][d],
                                 max(raw_xyz[d::3]) * m["scales"][d] + m["offsets"][d]] for d in range(3)])
        result["array"] = {"shape": [count, 3], "dimensions": ["X_raw", "Y_raw", "Z_raw"], "values": raw_xyz}
        result["point_attributes"] = {"intensity": intensity, "classification": classification, "classification_flags": flags, "rgb": rgb}
        result["sampled"] = count < m["total_points"]
    m.update(read_bytes=source.read_bytes, read_requests=source.read_requests)
    return validate_pointcloud_window_payload(result, kind=kind, options=selection, fmt=fmt, source_bytes=size,
                                             read_bytes=source.read_bytes, read_requests=source.read_requests)
