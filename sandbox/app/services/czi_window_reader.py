"""True byte-range CZI 1.0 / DV / uncompressed ROI preview.

An independent restricted reader based on the on-disk structures documented in
Zeiss libCZI CziStructs.h and CziParse.cpp, not a claim of a Python IStream API:
https://github.com/ZEISS/libczi/blob/main/Src/libCZI/CziStructs.h
https://github.com/ZEISS/libczi/blob/main/Src/libCZI/CziParse.cpp
The locked pylibCZIrw 6.1.0 binding accepts filenames/standard or curl only:
https://github.com/ZEISS/pylibczirw/blob/v6.1.0/_pylibCZIrw/src/bindings/CZIrw.cpp

No file/path/URL or native libCZI is opened. Directory structure is fully checked
within budget, then selected subblock headers are matched byte-for-byte against
that directory. All pixel reads are planned before any pixel is requested.
Coverage must be exact: no zero-filled missing ranges or ambiguous tile merges.
Pixels are row-major according to X/Y StoredSize; only full-resolution planes
with singleton non-spatial dimensions are supported. Source metadata and mask
attachments are never interpreted. This is not a general CZI implementation.
"""
from __future__ import annotations

import base64
import io
import math
import struct
from dataclasses import dataclass

from app.services.czi_window_payload import (
    CziWindowError, ENGINE, VARIANT, WARNINGS, MAX_SOURCE_BYTES, MAX_READ_BYTES,
    MAX_TOTAL_BYTES, MAX_READS, MAX_DIRECTORY_BYTES, MAX_ENTRIES,
    _fail, validate_czi_window_options, validate_czi_window_payload,
)

PIXELS = {0: ("Gray8", "u1", 1), 1: ("Gray16", "<u2", 2),
          2: ("Gray32Float", "<f4", 4), 3: ("Bgr24", "u1", 3)}
FILE_MAGIC = b"ZISRAWFILE\0\0\0\0\0\0"
DIRECTORY_MAGIC = b"ZISRAWDIRECTORY\0"
BLOCK_MAGIC = b"ZISRAWSUBBLOCK\0\0"


class CziRangeSource:
    def __init__(self, size, read_range, limits=None):
        maxima = {"max_read_bytes": MAX_READ_BYTES, "max_total_bytes": MAX_TOTAL_BYTES, "max_reads": MAX_READS}
        if type(size) is not int or not 544 <= size <= MAX_SOURCE_BYTES or not callable(read_range):
            _fail("CZI 源大小超出 8 GiB 范围预览边界。")
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(maxima) or any(type(v) is not int or not 1 <= v <= maxima[k] for k, v in limits.items()):
                _fail("CZI 范围预算无效。")
            maxima = dict(limits)
        self.size, self.reader, self.limits = size, read_range, maxima
        self.read_bytes = self.read_requests = 0

    def check_plan(self, plan):
        if (len(plan) + self.read_requests > self.limits["max_reads"]
            or sum(length for _, length in plan) + self.read_bytes > self.limits["max_total_bytes"]
            or any(type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= self.limits["max_read_bytes"] or offset + length > self.size for offset, length in plan)):
            _fail("CZI 区域读取超过预算，请缩小 ROI。")

    def read(self, offset, length):
        self.check_plan([(offset, length)])
        self.read_bytes += length
        self.read_requests += 1
        data = self.reader(offset, length)
        if type(data) is not bytes or len(data) != length:
            _fail("CZI 范围响应不完整。")
        return data

    def chunks(self, offset, length):
        plan = []
        while length:
            count = min(length, self.limits["max_read_bytes"])
            plan.append((offset, count)); offset += count; length -= count
            if len(plan) > self.limits["max_reads"]:
                _fail("CZI 目录读取超过次数预算。")
        self.check_plan(plan)
        return b"".join(self.read(pos, count) for pos, count in plan)


def _segment(data, position, size, magic, minimum):
    if len(data) < 32 or data[:16] != magic or position % 32:
        _fail("CZI 分段签名或对齐无效。")
    allocated, used = struct.unpack_from("<qq", data, 16)
    # Strict modern dialect: UsedSize=0 (legacy fallback) is not accepted.
    if not minimum <= used <= allocated or allocated % 32 or position + 32 + allocated > size:
        _fail("CZI 分段大小或范围无效。")
    return used, position + 32 + allocated


@dataclass(frozen=True)
class Block:
    raw: bytes
    position: int
    pixel: int
    dimensions: dict

    def dim(self, key):
        return self.dimensions.get(key, (0, 1, 1))

    @property
    def plane(self):
        return tuple(self.dim(key)[0] for key in ("C", "Z", "T"))

    @property
    def rectangle(self):
        x, width, _ = self.dim("X")
        y, height, _ = self.dim("Y")
        return x, y, width, height


def _entry(data, offset, source_size):
    if offset + 32 > len(data) or data[offset:offset + 2] != b"DV":
        _fail("仅支持 CZI DV 目录，不支持旧 DE 或不完整记录。")
    pixel, position, part, compression = struct.unpack_from("<iqii", data, offset + 2)
    count = struct.unpack_from("<i", data, offset + 28)[0]
    if pixel not in PIXELS or part != 0 or compression != 0 or data[offset + 22:offset + 28] != bytes(6):
        _fail("仅支持未压缩、无扩展标志的单文件 CZI 像素。")
    if not 3 <= count <= 10 or offset + 32 + 20 * count > len(data) or position < 544 or position % 32 or position + 288 > source_size:
        _fail("CZI 子块目录范围无效。")
    dimensions = {}
    for i in range(count):
        name, start, extent, coordinate, stored = struct.unpack_from("<4siifi", data, offset + 32 + i * 20)
        if name[1:] != bytes(3) or name[:1] not in (b"X", b"Y", b"C", b"Z", b"T", b"S", b"M", b"R", b"I", b"H"):
            _fail("CZI 区域存在未支持的维度。")
        key = name[:1].decode("ascii")
        if key in dimensions or not math.isfinite(coordinate):
            _fail("CZI 维度重复或坐标无效。")
        if key in {"X", "Y"}:
            if abs(start) > 2**30 or not 1 <= extent <= 100000 or stored != extent:
                _fail("CZI 区域仅支持原始分辨率，不支持金字塔或无效尺寸。")
        elif extent != 1 or stored != 1 or not 0 <= start < (64 if key == "C" else 4096 if key in {"Z", "T"} else MAX_ENTRIES if key == "M" else 1):
            _fail("CZI 区域仅支持单 scene 0 和单值子块 C/Z/T 维度。")
        dimensions[key] = (start, extent, stored)
    if not {"X", "Y", "S"} <= set(dimensions):
        _fail("CZI 区域需要显式单 scene 0 以及 X/Y。")
    stop = offset + 32 + 20 * count
    return Block(data[offset:stop], position, pixel, dimensions), stop


def _directory(source):
    header = source.read(0, 544)
    _, header_end = _segment(header, 0, source.size, FILE_MAGIC, 512)
    major, minor, reserved1, reserved2 = struct.unpack_from("<iiii", header, 32)
    part, directory_position, metadata_position, pending, attachments_position = struct.unpack_from("<iqqiq", header, 80)
    if (major, minor, reserved1, reserved2, part, pending) != (1, 0, 0, 0, 0, 0):
        _fail("CZI 区域仅支持已完成的 1.0 单文件。")
    if directory_position < header_end or directory_position % 32 or directory_position + 160 > source.size:
        _fail("CZI 子块目录位置无效。")
    for position in (metadata_position, attachments_position):
        if position and (position < header_end or position % 32 or position + 32 > source.size or position == directory_position):
            _fail("CZI 元信息分段位置无效。")
    head = source.read(directory_position, 160)
    used, directory_end = _segment(head, directory_position, source.size, DIRECTORY_MAGIC, 128)
    count = struct.unpack_from("<i", head, 32)[0]
    if not 1 <= count <= MAX_ENTRIES or used > MAX_DIRECTORY_BYTES or used < 128 + count * 92:
        _fail("CZI 完整目录超过 4 MiB / 16384 子块预算。")
    raw = source.chunks(directory_position + 160, used - 128)
    entries, offset, positions = [], 0, set()
    for _ in range(count):
        entry, offset = _entry(raw, offset, source.size)
        if entry.position in positions or entry.position < header_end or directory_position <= entry.position < directory_end or entry.position in {metadata_position, attachments_position}:
            _fail("CZI 子块位置重复或与索引分段冲突。")
        positions.add(entry.position); entries.append(entry)
    if offset != len(raw):
        _fail("CZI 目录尾部存在未支持的记录。")
    sizes = {}
    for key in ("C", "Z", "T"):
        values = {entry.dim(key)[0] for entry in entries}
        if values != set(range(max(values) + 1)):
            _fail("CZI 首期索引必须从零连续编号。")
        sizes[key] = len(values)
    types = []
    for channel in range(sizes["C"]):
        channel_types = {entry.pixel for entry in entries if entry.plane[0] == channel}
        if len(channel_types) != 1:
            _fail("CZI 同一通道包含不一致的像素类型。")
        types.append(PIXELS[channel_types.pop()][0])
    x0 = min(e.rectangle[0] for e in entries); y0 = min(e.rectangle[1] for e in entries)
    width = max(e.rectangle[0] + e.rectangle[2] for e in entries) - x0
    height = max(e.rectangle[1] + e.rectangle[3] for e in entries) - y0
    if not 1 <= width <= 100000 or not 1 <= height <= 100000:
        _fail("CZI 场景跨度超出 100000 像素预算。")
    meta = {"format": "CZI", "variant": VARIANT, "engine": ENGINE, "scene": 0, "scene_shape": [height, width],
            "dimension_sizes": sizes, "pixel_types": types, "input_mode": "window", "source_bytes": source.size,
            "directory_bytes": used + 32, "subblocks": len(entries), "metadata_hidden": True, "coverage": "not decoded",
            "limits": {**source.limits, "max_source_bytes": MAX_SOURCE_BYTES, "max_directory_bytes": MAX_DIRECTORY_BYTES, "max_entries": MAX_ENTRIES, "max_roi_size": 1024}}
    protected = [(0, header_end), (directory_position, directory_end)]
    protected.extend((position, position + 32) for position in (metadata_position, attachments_position) if position)
    return entries, meta, (x0, y0), protected


def _pixel_plan(source, entries, selected, origin, protected):
    import numpy as np
    indices, roi = selected["indices"], selected["roi"]
    rx, ry, width, height = roi; rx += origin[0]; ry += origin[1]
    coverage = np.zeros((height, width), dtype=np.bool_)
    merged, intervals = [], []
    for entry in entries:
        if entry.plane != tuple(indices):
            continue
        x, y, ew, eh = entry.rectangle
        left, top, right, bottom = max(rx, x), max(ry, y), min(rx + width, x + ew), min(ry + height, y + eh)
        if left >= right or top >= bottom:
            continue
        region = coverage[top - ry:bottom - ry, left - rx:right - rx]
        if region.any():
            _fail("CZI ROI 含重叠子块，不能猜测拼接顺序。")
        region[:] = True
        block_header = source.read(entry.position, 288)
        used, end = _segment(block_header, entry.position, source.size, BLOCK_MAGIC, 256)
        metadata_size, attachment_size, data_size = struct.unpack_from("<iiq", block_header, 32)
        actual, _ = _entry(block_header, 48, source.size)
        if actual != entry or metadata_size < 0 or attachment_size != 0:
            _fail("CZI 子块与目录不一致，或包含未支持的掩码/附件。")
        # <=10 dimensions guarantees the DV structure fits in the fixed 256 B.
        if data_size != ew * eh * PIXELS[entry.pixel][2] or 256 + metadata_size + data_size != used:
            _fail("CZI 子块像素长度不符合未压缩布局。")
        if any(entry.position < stop and start < end for start, stop in protected + intervals) or any(entry.position < other.position < end for other in entries):
            _fail("CZI 子块分段范围发生冲突。")
        intervals.append((entry.position, end))
        start = entry.position + 288 + metadata_size
        bpp = PIXELS[entry.pixel][2]
        for row in range(top, bottom):
            offset = start + ((row - y) * ew + left - x) * bpp
            length = (right - left) * bpp
            dest = ((row - ry) * width + left - rx) * bpp
            if merged and offset == merged[-1][0] + merged[-1][1] and dest == merged[-1][2] + merged[-1][1] and length + merged[-1][1] <= source.limits["max_read_bytes"]:
                previous = merged[-1]; merged[-1] = (previous[0], previous[1] + length, previous[2])
            else:
                merged.append((offset, length, dest))
            if len(merged) > source.limits["max_reads"]:
                _fail("CZI ROI 碎片过多，请缩小区域。")
    if not coverage.all():
        _fail("CZI ROI 存在未覆盖像素；请选取实际存在的区域，不补零。")
    # Coalesce only contiguous source AND destination bytes, never unselected
    # columns or gaps. This reduces whole-tile ROI calls without hiding reads.
    source.check_plan([(offset, length) for offset, length, _ in merged])
    return merged


def czi_window_preview(read_range, size, kind, options=None, limits=None):
    options = validate_czi_window_options(kind, {} if options is None else options)
    source = CziRangeSource(size, read_range, limits)
    entries, metadata, origin, protected = _directory(source)
    dims = metadata["dimension_sizes"]; height, width = metadata["scene_shape"]
    first = entries[0]; x, y, ew, eh = first.rectangle
    selected = {"indices": list(first.plane), "roi": [x - origin[0], y - origin[1], min(ew, 1024), min(eh, 1024)]} if kind == "tree" else options
    indices, roi = selected["indices"], selected["roi"]
    if any(v >= dims[key] for v, key in zip(indices, ("C", "Z", "T"))) or roi[0] + roi[2] > width or roi[1] + roi[3] > height:
        _fail("CZI 区域选择超出场景维度。")
    result = {"contract_version": 2, "type": "czi-window", "reader": "czi-window", "kind": kind,
              "media_type": "application/json" if kind == "tree" else "image/png", "metadata": metadata,
              "choices": {"channels": list(range(dims["C"])), "z_count": dims["Z"], "time_count": dims["T"], "max_roi_size": 1024},
              "selected": selected, "sampled": False, "warnings": list(WARNINGS)}
    if kind == "tree":
        result["tree"] = [{"path": "/0", "node_type": "object", "attributes": {"label": "Uncompressed CZI directory", "children_count": 0}}]
    else:
        import numpy as np
        from PIL import Image
        plan = _pixel_plan(source, entries, selected, origin, protected)
        pixel = next(e.pixel for e in entries if e.plane[0] == indices[0])
        pixel_name, dtype, bpp = PIXELS[pixel]
        # Allocation is bounded to ROI, and every byte is assigned from the
        # validated exact coverage plan before it is interpreted or published.
        raw = bytearray(roi[2] * roi[3] * bpp)
        for offset, length, dest in plan:
            raw[dest:dest + length] = source.read(offset, length)
        if pixel_name == "Bgr24":
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(roi[3], roi[2], 3)[:, :, ::-1].copy()
            metadata.update(display_range=[0, 255], normalization="native uint8 BGR to RGB; no scaling", invalid_pixels=0)
        else:
            values = np.frombuffer(raw, dtype=dtype).reshape(roi[3], roi[2]).astype(np.float64)
            finite = np.isfinite(values)
            if not finite.any():
                _fail("CZI ROI 不含有限像素。")
            low, high = float(values[finite].min()), float(values[finite].max())
            gray = np.zeros(values.shape, dtype=np.uint8)
            if high != low:
                gray[finite] = np.rint(np.clip((values[finite] - low) / (high - low), 0, 1) * 255).astype(np.uint8)
            rgb = np.stack((gray, gray, gray, finite.astype(np.uint8) * 255), axis=-1)
            metadata.update(display_range=[low, high], normalization="ROI min-max to uint8 grayscale; original data unchanged", invalid_pixels=int((~finite).sum()))
        output = io.BytesIO(); Image.fromarray(rgb).save(output, format="PNG")
        if output.tell() > 5 * 1024**2:
            _fail("CZI 区域 PNG 超出输出预算。")
        result["data_base64"] = base64.b64encode(output.getvalue()).decode("ascii")
        result["sampled"] = roi != [0, 0, width, height] or any(v > 1 for v in dims.values())
        metadata.update(output_shape=[roi[3], roi[2]], coverage="complete and non-overlapping")
    metadata.update(read_bytes=source.read_bytes, read_requests=source.read_requests)
    return validate_czi_window_payload(result)
