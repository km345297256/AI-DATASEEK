"""Pinned Zeiss CZI decoder, only inside the existing isolated worker.

One authorized whole-file input <=64 MiB, not range I/O. Header-only inspection
precedes explicit C/Z/T + bounded ROI decode. Never opens URLs or metadata paths.
No raw acquisition XML, annotations or user-controlled names cross the boundary.
"""
import base64
import io
import math
import os
import struct
import tempfile
from pathlib import Path

from app.services.czi_payload import CziPreviewError, _fail, validate_czi_options, validate_czi_payload

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_BLOCK_BYTES = 16 * 1024 * 1024
PIXEL_TYPES = {"Gray8": ("u", 1, 1), "Gray16": ("u", 2, 1), "Gray32Float": ("f", 4, 1), "Bgr24": ("u", 1, 3)}


def _preflight(data):
    if type(data) is not bytes or not 112 <= len(data) <= MAX_INPUT_BYTES or data[:16] != b"ZISRAWFILE\x00\x00\x00\x00\x00\x00":
        _fail("文件不是有效的单文件 CZI，或超过 64 MiB 输入预算。")
    # CZI FILE segment: fixed 32-byte segment header + 48 bytes to file-part.
    if struct.unpack_from("<I", data, 80)[0] != 0:
        _fail("CZI 首期不支持分卷文件。")
    offset = count = 0
    while offset < len(data):
        if offset + 32 > len(data):
            _fail("CZI 分段头不完整。")
        allocated, used = struct.unpack_from("<qq", data, offset + 16)
        if allocated < 0 or used < 0 or used > allocated or allocated > len(data) - offset - 32:
            _fail("CZI 分段长度无效。")
        if not data[offset:offset + 16].startswith((b"ZISRAW", b"DELETED")):
            _fail("CZI 分段类型无效。")
        count += 1
        if count > 8192:
            _fail("CZI 分段数量超过预算。")
        offset += 32 + allocated


def _inspect(reader, size):
    dims = reader.total_bounding_box
    if set(dims) - {"X", "Y", "C", "Z", "T", "R", "I", "H", "V", "B"}:
        _fail("CZI 存在未支持的维度。")
    for key, pair in dims.items():
        if not isinstance(pair, tuple) or len(pair) != 2 or any(type(v) is not int for v in pair) or pair[1] <= pair[0]:
            _fail("CZI 维度范围无效。")
        if key not in {"X", "Y"} and (pair[0] != 0 or (key not in {"C", "Z", "T"} and pair[1] != 1)):
            _fail("CZI 首期仅支持 C/Z/T 与单值辅助维度。")
    sizes = {key: dims[key][1] for key in ("C", "Z", "T")}
    if any(not 1 <= value <= (64 if key == "C" else 4096) for key, value in sizes.items()):
        _fail("CZI 通道或 Z/T 维度超过交互预算。")
    scenes = reader.scenes_bounding_rectangle_no_pyramid
    if set(scenes) != {0}:
        _fail("CZI 试点需要且仅支持一个显式 scene 0；不支持多场景或拼接场景推测。")
    scene = scenes[0]
    x, y, width, height = scene.x, scene.y, scene.w, scene.h
    if any(type(v) is not int for v in (x, y, width, height)) or not 1 <= width <= 100000 or not 1 <= height <= 100000 or abs(x) > 2**30 or abs(y) > 2**30:
        _fail("CZI 场景范围超过安全预算。")
    pixel_types = [reader.get_channel_pixel_type(channel) for channel in range(sizes["C"])]
    if any(value not in PIXEL_TYPES for value in pixel_types):
        _fail("CZI 首期仅支持 Gray8、Gray16、Gray32Float 和 Bgr24 像素。")
    blocks = 0

    def block_budget(index, info):
        nonlocal blocks
        blocks += 1
        if blocks > 4096:
            _fail("CZI 子块数量超过 4,096 预算。")
        physical = info.physicalSize
        if info.pixelType.name not in PIXEL_TYPES:
            _fail("CZI 子块包含不受支持或混合异常的像素类型。")
        # Worst-case supported scalar bytes, independent of the first channel's
        # pixel type. The worker memory limit is still the final safety boundary.
        if not 1 <= physical.w <= 100000 or not 1 <= physical.h <= 100000 or physical.w * physical.h * 4 > MAX_BLOCK_BYTES:
            _fail("CZI 单子块解码预算超过 16 MiB。")
        return True

    reader.enumerate_subblocks(block_budget)
    if not blocks:
        _fail("CZI 未包含图像子块。")
    metadata = {"format": "CZI", "engine": "pylibCZIrw 6.1.0", "scene": 0, "scene_shape": [height, width],
                "dimension_sizes": sizes, "pixel_types": pixel_types, "input_mode": "whole", "input_bytes": size,
                "subblocks": blocks, "decoded_block_budget": MAX_BLOCK_BYTES, "selection_mode": "explicit C/Z/T and pixel ROI"}
    return metadata, (x, y)


def czi_preview(data, kind, options):
    validate_czi_options(kind, options)
    _preflight(data)
    # Optional import stays within the one-shot worker, never the API host.
    from pylibCZIrw import czi
    import numpy as np
    from PIL import Image
    from importlib.metadata import version
    if version("pylibCZIrw") != "6.1.0":
        _fail("CZI 解码器版本未与已批准版本一致。")
    with tempfile.TemporaryDirectory(prefix="czi-preview-") as directory:
        path = Path(directory) / "input.czi"
        path.write_bytes(data)
        os.chmod(path, 0o400)
        try:
            with czi.open_czi(str(path), file_input_type=czi.ReaderFileInputTypes.Standard, cache_options=None,
                              reader_options=czi.ReaderOptions(lax_subblock_coordinate_checks=False, enable_mask_awareness=True)) as reader:
                metadata, origin = _inspect(reader, len(data))
                height, width = metadata["scene_shape"]
                dims = metadata["dimension_sizes"]
                selected = {"indices": [0, 0, 0], "roi": [0, 0, min(width, 1024), min(height, 1024)]} if kind == "tree" else options
                result = {"contract_version": 2, "type": "czi", "reader": "czi", "kind": kind,
                          "media_type": "application/json" if kind == "tree" else "image/png", "metadata": metadata,
                          "choices": {"channels": list(range(dims["C"])), "z_count": dims["Z"], "time_count": dims["T"], "max_roi_size": 1024},
                          "selected": selected, "sampled": False,
                          "warnings": ["按授权整文件读取（最多 64 MiB），不是远程 range；只解码所选 C/Z/T 的有界 ROI，原始文件不变。", "仅作图像浏览；切片内未覆盖区域按库默认背景显示为零，不据此判断缺失值或执行定量分析。"]}
                if kind == "tree":
                    result["tree"] = [{"path": "/0", "node_type": "object", "attributes": {"label": "Single-scene CZI", "children_count": 0}}]
                    return validate_czi_payload(result)
                indices, roi = selected.get("indices"), selected.get("roi")
                if not isinstance(indices, list) or len(indices) != 3 or any(type(v) is not int or not 0 <= v < dims[key] for v, key in zip(indices, ["C", "Z", "T"])):
                    _fail("CZI C/Z/T 索引越界。")
                if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi) or min(roi[:2]) < 0 or not 1 <= roi[2] <= 1024 or not 1 <= roi[3] <= 1024 or roi[0] + roi[2] > width or roi[1] + roi[3] > height:
                    _fail("CZI ROI 必须位于场景内，宽高均不超过 1024 像素。")
                pixels = reader.read(roi=(origin[0] + roi[0], origin[1] + roi[1], roi[2], roi[3]),
                                     plane=dict(zip(["C", "Z", "T"], indices)), scene=0, zoom=1,
                                     pixel_type=metadata["pixel_types"][indices[0]])
                pixel_type = metadata["pixel_types"][indices[0]]
                dtype_kind, itemsize, channels = PIXEL_TYPES[pixel_type]
                if not isinstance(pixels, np.ndarray) or pixels.shape != (roi[3], roi[2], channels) or pixels.dtype.kind != dtype_kind or pixels.dtype.itemsize != itemsize:
                    _fail("CZI 解码像素与所选 ROI 不一致。")
                metadata.update(output_shape=[roi[3], roi[2]], roi_origin="scene top-left; x right, y down")
                if pixel_type == "Bgr24":
                    rgb = np.ascontiguousarray(pixels[:, :, ::-1])
                    metadata.update(display_range=[0, 255], normalization="native uint8 BGR to RGB; no scaling", invalid_pixels=0)
                else:
                    values = pixels[:, :, 0].astype(np.float64)
                    valid = np.isfinite(values)
                    if not np.any(valid):
                        _fail("CZI 所选 ROI 不含有限像素值。")
                    minimum, maximum = float(values[valid].min()), float(values[valid].max())
                    scale = max(abs(minimum), abs(maximum), 1)
                    span = maximum / scale - minimum / scale
                    normalized = np.zeros(values.shape, dtype=np.float64)
                    if span:
                        normalized[valid] = (values[valid] / scale - minimum / scale) / span
                    gray = np.rint(np.clip(normalized, 0, 1) * 255).astype(np.uint8)
                    rgb = np.stack([gray, gray, gray, valid.astype(np.uint8) * 255], axis=-1)
                    metadata.update(display_range=[minimum, maximum], normalization="ROI min-max to uint8 grayscale; original data unchanged", invalid_pixels=int((~valid).sum()))
                output = io.BytesIO()
                Image.fromarray(rgb).save(output, format="PNG")
                encoded = output.getvalue()
                if len(encoded) > 5 * 1024 * 1024:
                    _fail("CZI PNG 超过 5 MiB 输出预算。")
                result["data_base64"] = base64.b64encode(encoded).decode("ascii")
                result["sampled"] = roi != [0, 0, width, height] or any(value > 1 for value in dims.values())
                return validate_czi_payload(result)
        except CziPreviewError:
            raise
        except Exception:
            _fail("CZI 解码失败；文件损坏、不受支持或资源预算不足。")
