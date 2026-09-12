"""Actual locked Zeiss writer fixtures and adversarial true-range checks."""
import ast
import base64
import copy
import io
import struct
from pathlib import Path

import pytest

from app.services.czi_window_payload import CziWindowError, validate_czi_window_options, validate_czi_window_payload
from app.services.czi_window_reader import CziRangeSource, czi_window_preview


def write_native(tmp_path, *, color=False, dtype="uint16", scenes=1, tiles=None, compression=None, large=False):
    from pylibCZIrw import czi
    import numpy as np
    path = tmp_path / "native-uncompressed.czi"
    with czi.create_czi(str(path), compression_options=compression) as writer:
        for scene in range(scenes):
            for channel in range(2 if not large else 1):
                for z in range(2 if not large else 1):
                    positions = tiles or [(0, 0)]
                    for x, y in positions:
                        h, w = (1000, 40000) if large else (6, 8)
                        data = (np.arange(h * w, dtype=np.dtype(dtype)).reshape(h, w, 1) + channel * 100 + z * 1000).astype(dtype)
                        if color:
                            data = np.full((h, w, 3), (12, 24, 96), dtype=np.uint8)
                        writer.write(data, plane={"C": channel, "Z": z, "T": 0}, scene=scene, location=(x, y))
    return path


class FileRanges:
    def __init__(self, path):
        self.path = path
        self.size = path.stat().st_size
        self.requests = []

    def __call__(self, offset, length):
        self.requests.append((offset, length))
        with self.path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)


def entries(data):
    position = struct.unpack_from("<q", data, 84)[0]
    count = struct.unpack_from("<i", data, position + 32)[0]
    current = position + 160
    result = []
    for _ in range(count):
        n = struct.unpack_from("<i", data, current + 28)[0]
        block = struct.unpack_from("<q", data, current + 6)[0]
        result.append((current, block, n))
        current += 32 + n * 20
    return position, result


def image(source, **kwargs):
    return czi_window_preview(source, source.size, "image", kwargs or {"indices": [1, 1, 0], "roi": [2, 1, 3, 2]})


def test_actual_zeiss_directory_reads_no_subblock_pixels_or_xml(tmp_path):
    path = write_native(tmp_path)
    source = FileRanges(path)
    result = czi_window_preview(source, source.size, "tree", {})
    directory = struct.unpack_from("<q", path.read_bytes(), 84)[0]
    assert source.requests[0] == (0, 544)
    assert all(offset >= directory for offset, _ in source.requests[1:])
    assert result["metadata"]["dimension_sizes"] == {"C": 2, "Z": 2, "T": 1}
    assert result["metadata"]["scene_shape"] == [6, 8]
    assert result["metadata"]["coverage"] == "not decoded"
    assert result["metadata"]["read_bytes"] == sum(n for _, n in source.requests)
    assert result["metadata"]["read_requests"] == len(source.requests)
    assert "data_base64" not in result


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "float32"])
def test_native_selected_window_matches_official_decoder(tmp_path, dtype):
    import numpy as np
    from PIL import Image
    from pylibCZIrw import czi
    path = write_native(tmp_path, dtype=dtype)
    source = FileRanges(path)
    result = image(source)
    with czi.open_czi(str(path)) as reader:
        actual = reader.read(roi=(2, 1, 3, 2), plane={"C": 1, "Z": 1, "T": 0}, scene=0, zoom=1)
    assert result["metadata"]["display_range"] == [float(actual.min()), float(actual.max())]
    displayed = np.array(Image.open(io.BytesIO(base64.b64decode(result["data_base64"]))))
    expected = np.rint((actual[:, :, 0].astype(float) - actual.min()) / (actual.max() - actual.min()) * 255).astype("uint8")
    assert np.array_equal(displayed[:, :, 0], expected)
    assert (displayed[:, :, 3] == 255).all()
    assert result["metadata"]["coverage"] == "complete and non-overlapping"
    assert source.requests[-2:][0][1] == source.requests[-1][1] == 3 * np.dtype(dtype).itemsize


def test_native_bgr_bytes_are_displayed_in_rgb_order(tmp_path):
    from PIL import Image
    result = image(FileRanges(write_native(tmp_path, color=True)))
    pixels = Image.open(io.BytesIO(base64.b64decode(result["data_base64"])))
    assert pixels.getpixel((0, 0)) == (96, 24, 12)
    assert result["metadata"]["display_range"] == [0, 255]


def test_true_ranges_from_actual_80mb_native_file_never_materialize_whole_source(tmp_path):
    source = FileRanges(write_native(tmp_path, large=True))
    assert source.size > 64 * 1024**2
    result = image(source, indices=[0, 0, 0], roi=[1234, 50, 8, 4])
    assert result["metadata"]["source_bytes"] == source.size
    assert result["metadata"]["read_bytes"] < 4096
    assert max(length for _, length in source.requests) <= 1024**2
    assert len(source.requests) < 16
    assert result["selected"]["roi"] == [1234, 50, 8, 4]


def test_native_disjoint_tiles_with_exact_cover_are_assembled_without_resampling(tmp_path):
    result = image(FileRanges(write_native(tmp_path, tiles=[(-8, 3), (0, 3)])), indices=[0, 0, 0], roi=[6, 2, 4, 2])
    assert result["metadata"]["scene_shape"] == [6, 16]
    assert result["metadata"]["display_range"] == [16, 31]
    assert result["metadata"]["output_shape"] == [2, 4]


@pytest.mark.parametrize("tiles,reason", [([(0, 0), (10, 0)], "未覆盖"), ([(0, 0), (4, 0)], "重叠")])
def test_native_gap_and_overlap_refuse_before_any_pixels(tmp_path, tiles, reason):
    path = write_native(tmp_path, tiles=tiles)
    source = FileRanges(path)
    with pytest.raises(CziWindowError, match=reason):
        image(source, indices=[0, 0, 0], roi=[2, 0, 10, 6])
    # Only file, directory and fixed subblock headers; never pixel rows.
    assert all(length in (544, 160, 288) or offset == struct.unpack_from("<q", path.read_bytes(), 84)[0] + 160 for offset, length in source.requests)


def test_multiscene_official_file_refused(tmp_path):
    source = FileRanges(write_native(tmp_path, scenes=2))
    with pytest.raises(CziWindowError, match="scene"):
        czi_window_preview(source, source.size, "tree", {})


def test_official_zstd_compressed_file_never_falls_back_to_whole_decode(tmp_path):
    source = FileRanges(write_native(tmp_path, compression="zstd0:ExplicitLevel=1"))
    with pytest.raises(CziWindowError, match="未压缩"):
        czi_window_preview(source, source.size, "tree", {})


def test_short_ranges_fail_without_padding_source(tmp_path):
    data = write_native(tmp_path).read_bytes()
    with pytest.raises(CziWindowError, match="不完整"):
        czi_window_preview(lambda offset, count: data[offset:offset + count - 1], len(data), "tree", {})


def test_source_and_budget_native_integer_limits_reject_before_read():
    for size in (True, 543, 8 * 1024**3 + 1):
        with pytest.raises(CziWindowError): CziRangeSource(size, lambda *_: pytest.fail("No read"))
    for limits in ({"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2 + 1, "max_reads": 4096}, {"max_read_bytes": True, "max_total_bytes": 1, "max_reads": 1}):
        with pytest.raises(CziWindowError): CziRangeSource(1024, lambda *_: pytest.fail("No read"), limits)


@pytest.mark.parametrize("mutation", ["compression", "pyramid", "DE", "part", "duplicate", "bad_offset", "pending", "version", "dimension_count", "different_header", "mask", "data_length", "directory_tail", "truncated"])
def test_adversarial_headers_refuse_safely(tmp_path, mutation):
    data = bytearray(write_native(tmp_path).read_bytes())
    directory, blocks = entries(data); first, block, count = blocks[0]
    if mutation == "compression": struct.pack_into("<i", data, first + 18, 4)
    elif mutation == "pyramid": struct.pack_into("<i", data, first + 32 + 16, 4)
    elif mutation == "DE": data[first:first + 2] = b"DE"
    elif mutation == "part": struct.pack_into("<i", data, 80, 1)
    elif mutation == "duplicate": struct.pack_into("<q", data, blocks[1][0] + 6, block)
    elif mutation == "bad_offset": struct.pack_into("<q", data, first + 6, 545)
    elif mutation == "pending": struct.pack_into("<i", data, 100, 1)
    elif mutation == "version": struct.pack_into("<i", data, 36, 99)
    elif mutation == "dimension_count": struct.pack_into("<i", data, first + 28, 1000000000)
    elif mutation == "different_header": struct.pack_into("<i", data, block + 48 + 2, 0)
    elif mutation == "mask": struct.pack_into("<i", data, block + 36, 1)
    elif mutation == "data_length": struct.pack_into("<q", data, block + 40, 1)
    elif mutation == "directory_tail": struct.pack_into("<q", data, directory + 24, struct.unpack_from("<q", data, directory + 24)[0] + 1)
    elif mutation == "truncated": data = data[:-100]
    with pytest.raises(CziWindowError):
        czi_window_preview(lambda o, n: bytes(data[o:o + n]), len(data), "image", {"indices": [0, 0, 0], "roi": [0, 0, 1, 1]})


@pytest.mark.parametrize("options", [{}, {"indices": [True, 0, 0], "roi": [0, 0, 1, 1]}, {"indices": [0, 0, 0], "roi": [0, 0, 1025, 1]}, {"indices": [0, 0, 0], "roi": [-1, 0, 1, 1]}, {"indices": [0, 0, 0], "roi": [0, 0, 1, 1], "scene": 0}, {"indices": [0, 0, 0], "roi": [0, 0, True, 1]}])
def test_invalid_selections_make_zero_reads(options):
    reads = []
    with pytest.raises(CziWindowError):
        czi_window_preview(lambda *args: reads.append(args), 1024, "image", options)
    assert not reads


def test_budget_checked_before_callback_and_actual_total_checked(tmp_path):
    called = []
    source = CziRangeSource(8 * 1024**3, lambda *args: called.append(args))
    with pytest.raises(CziWindowError): source.read(0, 1024**2 + 1)
    assert not called
    path = write_native(tmp_path); ranges = FileRanges(path)
    with pytest.raises(CziWindowError):
        czi_window_preview(ranges, ranges.size, "image", {"indices": [0, 0, 0], "roi": [0, 0, 8, 6]}, {"max_read_bytes": 1024**2, "max_total_bytes": 600, "max_reads": 4096})
    assert ranges.requests == [(0, 544)]


def test_strict_payload_statistics_selection_and_no_metadata_leak(tmp_path):
    result = image(FileRanges(write_native(tmp_path)))
    assert validate_czi_window_payload(result, size=result["metadata"]["source_bytes"], read_bytes=result["metadata"]["read_bytes"], read_requests=result["metadata"]["read_requests"]) is result
    for mutate in [lambda r: r.update(contract_version=2.0), lambda r: r["metadata"].update(scene=False), lambda r: r["metadata"].update(coverage="filled"), lambda r: r["metadata"].update(path="/Users/private"), lambda r: r["metadata"]["limits"].update(max_reads=True), lambda r: r["selected"]["indices"].__setitem__(0, True), lambda r: r["choices"]["channels"].__setitem__(0, False)]:
        bad = copy.deepcopy(result); mutate(bad)
        with pytest.raises(CziWindowError): validate_czi_window_payload(bad)
    with pytest.raises(CziWindowError): validate_czi_window_payload(result, read_bytes=1)
    with pytest.raises(CziWindowError): validate_czi_window_payload(result, size=1)


def test_host_and_sandbox_schema_remain_exact_stdlib_copies():
    sandbox = Path(__file__).resolve().parents[1] / "app/services/czi_window_payload.py"
    backend = sandbox.parents[3] / "backend/app/application/services/czi_window_visualization.py"
    if not backend.exists(): pytest.skip("Cross-tree parity requires repository mount")
    assert ast.dump(ast.parse(sandbox.read_text())) == ast.dump(ast.parse(backend.read_text()))
