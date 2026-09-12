"""Bounded, read-only ESRF EDF and Princeton SPE 2.x image windows.

Independent implementation of the limited on-disk layouts documented by
FabIO (ESRF EDF) and ImageIO's Spec.basic (SPE). No upstream decoder or code
is copied; no paths, sidecar opens, entire-file downloads or writes exist.
https://github.com/silx-kit/fabio/blob/main/src/fabio/edfimage.py
https://github.com/imageio/imageio/blob/master/imageio/plugins/spe.py
"""
from __future__ import annotations

import base64
import json
import math
import re
import struct
import zlib
from array import array
from dataclasses import dataclass

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_HEADER_BYTES = 1024**2
MAX_EDF_HEADER_BYTES = 64 * 1024
MAX_EDF_FRAMES = 256
MAX_FRAMES = 1_000_000
MAX_READS = 2048
MAX_ROI_SIZE = 1024
MAX_DIMENSION = 100_000
MAX_OUTPUT_BYTES = 2 * 1024**2
CALIBRATION = "none; raw detector values"
NORMALIZATION = "ROI min-max to uint8 grayscale; original data unchanged"
WARNINGS = [
    "仅显示显式选择的帧和像素窗口；不执行校准、拟合或重采样。",
    "灰度图按当前 ROI 有限原值范围映射；非有限值显示为黑色，原始数据不变。",
]
TREE = [{"path": "/0", "node_type": "object", "attributes": {"label": "Instrument image dataset", "children_count": 0}}]
_TYPES = {
    "int8": ("b", 1), "uint8": ("B", 1), "int16": ("h", 2), "uint16": ("H", 2),
    "int32": ("i", 4), "uint32": ("I", 4), "float32": ("f", 4), "float64": ("d", 8),
}
_EDF_TYPES = {
    "signedbyte": "int8", "signed8": "int8", "unsignedbyte": "uint8", "unsigned8": "uint8",
    "signedshort": "int16", "signed16": "int16", "unsignedshort": "uint16", "unsigned16": "uint16",
    "unsignedshortinteger": "uint16", "signedinteger": "int32", "signed32": "int32", "signedlong": "int32",
    "unsignedinteger": "uint32", "unsigned32": "uint32", "unsignedlong": "uint32",
    "floatvalue": "float32", "float": "float32", "floatieee32": "float32", "float32": "float32",
    "double": "float64", "doublevalue": "float64", "floatieee64": "float64", "doubleieee64": "float64",
}
_SPE_TYPES = {0: "float32", 1: "int32", 2: "int16", 3: "uint16"}


class InstrumentPreviewError(ValueError):
    """Only fixed safe messages, never untrusted header contents."""


def _require(condition, message="仪器图像格式、选择或资源预算无效。"):
    if not condition:
        raise InstrumentPreviewError(message)


def validate_instrument_options(kind, options):
    _require(kind in {"tree", "image"} and isinstance(options, dict))
    if kind == "tree":
        _require(not options, "结构检查不接受像素读取参数。")
        return {}
    _require(set(options) == {"frame", "roi"}, "图像读取需要显式帧索引和 ROI。")
    _require(type(options["frame"]) is int and 0 <= options["frame"] < MAX_FRAMES)
    roi = options["roi"]
    _require(isinstance(roi, list) and len(roi) == 4 and all(type(v) is int for v in roi))
    _require(all(0 <= v < MAX_DIMENSION for v in roi[:2]) and all(1 <= v <= MAX_ROI_SIZE for v in roi[2:]))
    return {"frame": options["frame"], "roi": list(roi)}


class RangeSource:
    def __init__(self, read_range, size, limits=None, cancelled=None):
        _require(callable(read_range) and type(size) is int and 1 <= size <= MAX_SOURCE_BYTES)
        maxima = {"max_read_bytes": MAX_READ_BYTES, "max_total_bytes": MAX_TOTAL_BYTES, "max_reads": MAX_READS}
        if limits is not None:
            _require(isinstance(limits, dict) and set(limits) == set(maxima))
            _require(all(type(v) is int and 1 <= v <= maxima[k] for k, v in limits.items()))
            maxima = dict(limits)
        self.size, self.limits, self.reader, self.cancelled = size, maxima, read_range, cancelled
        self.read_bytes = self.read_requests = self.header_bytes = 0

    def check(self):
        _require(self.cancelled is None or not self.cancelled.is_set(), "仪器窗口预览已取消。")

    def read(self, offset, length, *, header=False):
        self.check()
        _require(type(offset) is int and type(length) is int and 0 <= offset < self.size
                 and 0 < length <= self.size - offset and length <= self.limits["max_read_bytes"]
                 and self.read_bytes + length <= self.limits["max_total_bytes"]
                 and self.read_requests < self.limits["max_reads"])
        _require(not header or self.header_bytes + length <= MAX_HEADER_BYTES)
        self.read_bytes += length
        self.read_requests += 1
        if header:
            self.header_bytes += length
        data = self.reader(offset, length)
        self.check()
        _require(type(data) is bytes and len(data) == length, "仪器窗口读取长度与声明不一致。")
        return data


@dataclass(frozen=True)
class Layout:
    format: str
    dialect: str
    version: float | None
    width: int
    height: int
    dtype: str
    byte_order: str
    frame_count: int
    offsets: tuple[int, ...] = ()

    @property
    def sample_bytes(self):
        return _TYPES[self.dtype][1]

    @property
    def frame_bytes(self):
        return self.width * self.height * self.sample_bytes

    def offset(self, frame):
        return self.offsets[frame] if self.format == "esrf-edf" else 4100 + frame * self.frame_bytes


def _positive_decimal(value, maximum=MAX_SOURCE_BYTES):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value) is not None)
    result = int(value)
    _require(1 <= result <= maximum)
    return result


def _edf_header(source, offset):
    # This supported dialect has complete headers aligned to 512-byte blocks.
    # The closing brace/newline is at the END of the header, not followed by
    # speculative reads into the binary image. EDFU/general/external blocks
    # and non-block-aligned headers are intentionally rejected.
    chunks = bytearray()
    while len(chunks) < MAX_EDF_HEADER_BYTES:
        chunks.extend(source.read(offset + len(chunks), 512, header=True))
        _require(chunks[:1] == b"{", "不是 ESRF EDF 衍射图像；生理 EDF 请使用信号插件。")
        if b"}" in chunks:
            break
    _require(chunks.endswith((b"}\n", b"}\r\n")) and chunks.count(b"}") == 1 and chunks.count(b"{") == 1)
    _require(all(v in (9, 10, 13) or 32 <= v <= 126 for v in chunks))
    body = chunks[1:chunks.index(b"}")].decode("ascii")
    fields = {}
    parts = body.split(";")
    _require(not parts[-1].strip() and len(parts) <= 257)
    for part in parts[:-1]:
        _require("=" in part)
        key, value = (text.strip() for text in part.split("=", 1))
        key = key.upper()
        _require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) is not None and key not in fields and len(value) <= 4096)
        fields[key] = value
    _require({"DIM_1", "DIM_2", "DATATYPE", "BYTEORDER"} <= fields.keys())
    _require(({"HEADERID", "IMAGE", "SIZE"} <= fields.keys()) or ({"EDF_DATABLOCKID", "EDF_BINARYSIZE"} <= fields.keys()))
    _require(not any(key.startswith("EDF_BINARYFILE") or key in {"EDF_DATAFORMATVERSION", "DIM_3"} for key in fields))
    _require(all(not key.startswith("DIM_") or key in {"DIM_1", "DIM_2"} for key in fields))
    _require(fields.get("COMPRESSION", "NONE").upper() == "NONE", "不支持压缩或外置 EDF 图像数据。")
    if "EDF_HEADERSIZE" in fields:
        _require(_positive_decimal(fields["EDF_HEADERSIZE"], MAX_EDF_HEADER_BYTES) == len(chunks))
    width = _positive_decimal(fields["DIM_1"], MAX_DIMENSION)
    height = _positive_decimal(fields["DIM_2"], MAX_DIMENSION)
    dtype = _EDF_TYPES.get(fields["DATATYPE"].lower())
    order = {"LowByteFirst": "little", "HighByteFirst": "big"}.get(fields["BYTEORDER"])
    _require(dtype is not None and order is not None, "不支持此 EDF 像素类型或字节顺序。")
    data_bytes = width * height * _TYPES[dtype][1]
    _require(0 < data_bytes <= source.size - offset - len(chunks))
    _require(any(key in fields for key in ("SIZE", "EDF_BINARYSIZE")))
    for key in ("SIZE", "EDF_BINARYSIZE"):
        if key in fields:
            _require(_positive_decimal(fields[key]) == data_bytes)
    return width, height, dtype, order, len(chunks), data_bytes


def _edf_layout(source):
    offset, offsets, expected = 0, [], None
    while offset < source.size:
        _require(len(offsets) < MAX_EDF_FRAMES, "EDF 帧数超过结构扫描上限。")
        width, height, dtype, order, header_size, data_size = _edf_header(source, offset)
        signature = (width, height, dtype, order)
        _require(expected is None or signature == expected, "此试点不支持帧间尺寸、类型或字节顺序变化。")
        expected = signature
        offsets.append(offset + header_size)
        offset += header_size + data_size
    _require(offset == source.size and expected is not None)
    return Layout("esrf-edf", "edf-inline-2d-uncompressed", None, *expected, len(offsets), tuple(offsets))


def _spe_layout(source):
    header = source.read(0, 4100, header=True)
    width, height = struct.unpack_from("<H", header, 42)[0], struct.unpack_from("<H", header, 656)[0]
    dtype = _SPE_TYPES.get(struct.unpack_from("<h", header, 108)[0])
    frames = struct.unpack_from("<i", header, 1446)[0]
    version = struct.unpack_from("<f", header, 1992)[0]
    footer = struct.unpack_from("<Q", header, 678)[0]
    rois = struct.unpack_from("<h", header, 1510)[0]
    _require(math.isfinite(version) and 2 <= version < 3 and footer == 0,
             "仅支持明确 SPE 2.x 固定头；不支持 SPE 3/XML footer。")
    _require(dtype is not None and 1 <= width <= 65535 and 1 <= height <= 65535
             and 1 <= frames <= MAX_FRAMES and rois in (0, 1))
    # Nonzero multi-ROI/overscan/image geometric flags require extra layout
    # semantics. Do not silently reconstruct or rotate those frames.
    _require(struct.unpack_from("<H", header, 600)[0] == 0)
    _require(all(struct.unpack_from("<h", header, position)[0] == 0 for position in (98, 100, 102, 104)))
    if rois == 1:
        sx, ex, gx, sy, ey, gy = struct.unpack_from("<6H", header, 1512)
        _require(gx > 0 and gy > 0 and ex >= sx and ey >= sy
                 and (ex - sx + 1) % gx == 0 and (ey - sy + 1) % gy == 0
                 and (ex - sx + 1) // gx == width and (ey - sy + 1) // gy == height)
    layout = Layout("princeton-spe", "spe-2.x-fixed-4100", version, width, height, dtype, "little", frames)
    _require(4100 + frames * layout.frame_bytes == source.size, "SPE 帧数量或大小与源文件不一致。")
    return layout


def _plan(source, layout, selected):
    frame, roi = selected["frame"], selected["roi"]
    x, y, width, height = roi
    _require(frame < layout.frame_count and x + width <= layout.width and y + height <= layout.height)
    start, sample_bytes = layout.offset(frame), layout.sample_bytes
    requests = []
    for row in range(y, y + height):
        offset, length = start + (row * layout.width + x) * sample_bytes, width * sample_bytes
        if requests and requests[-1][0] + requests[-1][1] == offset and requests[-1][1] + length <= source.limits["max_read_bytes"]:
            requests[-1] = (requests[-1][0], requests[-1][1] + length)
        else:
            requests.append((offset, length))
    source.check()
    _require(all(length <= source.limits["max_read_bytes"] and offset + length <= source.size for offset, length in requests))
    _require(source.read_requests + len(requests) <= source.limits["max_reads"]
             and source.read_bytes + sum(length for _, length in requests) <= source.limits["max_total_bytes"],
             "ROI 超过窗口读取预算，请缩小窗口。")
    return requests


def _png(source, values, width, height):
    finite = [v for v in values if math.isfinite(v)]
    low, high = (min(finite), max(finite)) if finite else (0.0, 0.0)
    invalid = len(values) - len(finite)
    del finite
    pixels = bytearray((width + 1) * height)
    # Half-value arithmetic avoids overflow for a full float64 dynamic range.
    span = high / 2 - low / 2
    for row in range(height):
        source.check()
        for column in range(width):
            value = values[row * width + column]
            if math.isfinite(value) and high != low:
                ratio = (value - low) / (high - low) if math.isfinite(high - low) and high - low > 0 else (value / 2 - low / 2) / span
                pixels[row * (width + 1) + column + 1] = round(max(0.0, min(1.0, ratio)) * 255)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(pixels, 1)) + chunk(b"IEND", b""))
    source.check()
    return png, [low, high], invalid


def instrument_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None, cancelled=None):
    options = validate_instrument_options(kind, {} if options is None else options)
    _require(fmt in {"edf", "spe"}, "仅支持 ESRF EDF 或 Princeton SPE 2.x 仪器图像。")
    source = RangeSource(read_range, size, limits, cancelled)
    layout = _edf_layout(source) if fmt == "edf" else _spe_layout(source)
    selected = options if kind == "image" else {"frame": 0, "roi": [0, 0, min(256, layout.width), min(256, layout.height)]}
    meta = {
        "format": layout.format, "dialect": layout.dialect, "format_version": layout.version,
        "frame_count": layout.frame_count, "frame_shape": [layout.height, layout.width],
        "dtype": layout.dtype, "byte_order": layout.byte_order, "input_mode": "window",
        "source_bytes": source.size, "read_bytes": 0, "read_requests": 0, "header_bytes": source.header_bytes,
        "header_text_hidden": True, "calibration": CALIBRATION, "storage_layout": "row-major; x fastest",
        "selection_mode": "explicit frame and pixel ROI",
    }
    result = {"contract_version": 2, "type": "instrument", "reader": "instrument-window", "kind": kind,
              "media_type": "image/png" if kind == "image" else "application/json", "metadata": meta,
              "choices": {"frame_count": layout.frame_count, "max_roi_size": MAX_ROI_SIZE}, "selected": selected,
              "warnings": list(WARNINGS), "sampled": kind == "tree" or layout.frame_count != 1 or selected["roi"] != [0, 0, layout.width, layout.height]}
    if kind == "tree":
        result["tree"] = [{"path": "/0", "node_type": "object", "attributes": dict(TREE[0]["attributes"])}]
    else:
        values = array("d")
        decoder = ("<" if layout.byte_order == "little" else ">") + _TYPES[layout.dtype][0]
        for offset, length in _plan(source, layout, selected):
            data = source.read(offset, length)
            values.extend(value[0] for value in struct.iter_unpack(decoder, data))
        width, height = selected["roi"][2:]
        _require(len(values) == width * height)
        png, bounds, invalid = _png(source, values, width, height)
        meta.update(output_shape=[height, width], display_range=bounds, normalization=NORMALIZATION,
                    invalid_pixels=invalid, roi_origin="frame top-left; x right, y down")
        result["data_base64"] = base64.b64encode(png).decode("ascii")
    meta.update(read_bytes=source.read_bytes, read_requests=source.read_requests)
    source.check()
    _require(len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) + 512 <= MAX_OUTPUT_BYTES)
    return result
