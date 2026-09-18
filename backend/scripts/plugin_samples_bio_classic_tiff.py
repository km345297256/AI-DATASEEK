"""Offline, pixel-exact classic-TIFF companion for the reviewed Light My Cells image.

No network, database, model, downloaded code or production data is accessed.
Original metadata remains in the preserved OME-BigTIFF. The companion is a
single grayscale TIFF-6 raster with no invented physical calibration.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from pathlib import Path


GENERATOR = "backend/scripts/plugin_samples_bio_classic_tiff.py"
SOURCE_SHA256 = "61892dc211cf96032c14752a734c61d414980006490e65795faec14483c06048"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def put(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Symlink output refused")
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Changed staged artifact will not be overwritten")
        return
    with path.open("xb") as stream:
        stream.write(content)


def build(source: Path, reviewed_descriptors: Path, output_root: Path, descriptor_out: Path):
    import numpy as np
    from PIL import Image

    if source.is_symlink() or not source.is_file():
        raise ValueError("Explicit regular source file required")
    raw = source.read_bytes()
    if digest(raw) != SOURCE_SHA256 or len(raw) != 2239243:
        raise ValueError("Reviewed source hash/size mismatch")
    items = json.loads(reviewed_descriptors.read_text())
    original = next(x for x in items if x["dataset_id"] == "viz-bio-lightmycells-formats")
    item = copy.deepcopy(original)
    item.update(dataset_id="viz-bio-lightmycells-classic-tiff", name="真实犬细胞核图像：普通TIFF像素无损兼容样例",
                description="Light My Cells 已发布犬细胞核单张影像的普通 TIFF-6 兼容副本，1200×1200 uint16 全像素与原始 OME-BigTIFF 完全相同。只转换容器，不增强、不归一化、不裁剪、不重采样；不代表新的独立观测。原始 OME 文件随附，保留上游完整显微元数据。",
                plugin_id="tiff", entry_file="nucleus-classic-uint16.tiff",
                transformation="原始 OME-BigTIFF 经 Pillow 解码后，以未压缩 little-endian TIFF-6 单平面 uint16 写出；全部 1,440,000 像素逐项相同。派生文件不声明 OME 或物理标定；原始 OME 元数据仅在随附原件内保留。",
                test_steps=["打开 nucleus-classic-uint16.tiff，选择 TIFF 栅格插件，确认1200×1200灰度影像可见。", "用原始 OME-BigTIFF 与兼容文件读取数组比对，必须完整逐像素相等；屏幕亮度不代表原始数值。"])
    item["license_details"] = original["license_details"] + " 本补充数据集新增普通 TIFF-6 格式副本，许可与署名不变。"
    directory = output_root / item["dataset_id"]
    if directory.is_symlink():
        raise ValueError("Symlink dataset directory refused")
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        pixels = np.asarray(image)
        assert pixels.shape == (1200, 1200) and pixels.dtype == np.uint16
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, format="TIFF", compression="raw")
    converted = stream.getvalue()
    assert converted[:4] == b"II*\x00", "Must be classic little-endian TIFF, not BigTIFF"
    with Image.open(io.BytesIO(converted)) as image:
        image.load()
        np.testing.assert_array_equal(np.asarray(image), pixels)
        assert len(list(ImageSequence(image))) == 1
    source_name = "image_399_Nucleus.ome.tiff"
    entry = item["entry_file"]
    spec = encode({"kind": "scientific_format_conversion", "generator": GENERATOR,
                   "original": source_name, "original_sha256": SOURCE_SHA256,
                   "output": entry, "format": "TIFF-6", "compression": "none",
                   "dtype": "uint16", "shape_yx": [1200, 1200], "pixels_equal_original": True,
                   "resampled": False, "pixel_values_changed": False,
                   "physical_calibration_inferred": False, "ome_metadata": "preserved in original only"})
    for filename, data in [(source_name, raw), (entry, converted), ("GENERATION_SPEC.json", spec)]:
        put(directory / filename, data)
    original_file = copy.deepcopy(next(f for f in original["files"] if f["path"] == source_name))
    item["files"] = [original_file,
        {"path": entry, "size": len(converted), "sha256": digest(converted), "role": "data", "derived_from": [source_name]},
        {"path": "GENERATION_SPEC.json", "size": len(spec), "sha256": digest(spec), "role": "documentation",
         "origin": {"kind": "generated_fixture", "generator": GENERATOR,
                    "generator_sha256": digest(Path(__file__).read_bytes()),
                    "description": "普通 TIFF-6 无损容器转换参数；不是上游发布文件。"}}]
    put(descriptor_out, encode([item]))
    report = {"dataset_id": item["dataset_id"], "shape": list(pixels.shape), "dtype": str(pixels.dtype),
              "pixels_equal_original": True, "classic_tiff_header": converted[:4].hex(),
              "min": int(pixels.min()), "max": int(pixels.max()),
              "original_size": len(raw), "converted_size": len(converted), "converted_sha256": digest(converted)}
    put(descriptor_out.with_name("bio-classic-tiff-validation.json"), encode(report))
    print(json.dumps(report))


def ImageSequence(image):
    # Only metadata/frame counting; no active content exists in this raster format.
    from PIL.ImageSequence import Iterator
    return Iterator(image)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reviewed-descriptors", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--descriptor-out", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.reviewed_descriptors, args.output_root, args.descriptor_out)
