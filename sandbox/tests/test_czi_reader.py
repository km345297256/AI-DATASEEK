"""Real Zeiss-written synthetic files; no user or downloaded specimen data."""
import base64
import io
import struct
import pytest
from app.services.czi_payload import CziPreviewError, validate_czi_payload
from app.services.czi_reader import _preflight, czi_preview


@pytest.fixture
def czi_lib():
    return pytest.importorskip("pylibCZIrw.czi", reason="locked CZI dependency must be installed to exercise native integration")


def create(czi_lib, tmp_path, *, color=False, scene_count=1, float_values=None):
    import numpy as np
    path = tmp_path / "synthetic.czi"
    with czi_lib.create_czi(str(path)) as writer:
        for scene in range(scene_count):
            for channel in range(2):
                for z in range(2):
                    data = np.arange(48, dtype=np.uint16).reshape(6, 8, 1) + channel * 100 + z * 1000
                    if color:
                        data = np.zeros((6, 8, 3), dtype=np.uint8); data[:, :, 0] = 12; data[:, :, 1] = 24; data[:, :, 2] = 96
                    if float_values is not None:
                        data = np.full((6, 8, 1), float_values, dtype=np.float32)
                    writer.write(data, plane={"C": channel, "Z": z, "T": 0}, scene=scene, location=(scene * 20, 0))
    return path.read_bytes()


def test_actual_czi_inspection_is_metadata_only_and_pin_is_visible(czi_lib, tmp_path, monkeypatch):
    data = create(czi_lib, tmp_path)
    def forbidden(*args, **kwargs): pytest.fail("inspect must not decode a plane")
    monkeypatch.setattr(czi_lib.CziReader, "read", forbidden)
    result = czi_preview(data, "tree", {})
    assert result["metadata"]["engine"] == "pylibCZIrw 6.1.0"
    assert result["metadata"]["scene_shape"] == [6, 8]
    assert result["metadata"]["dimension_sizes"] == {"C": 2, "Z": 2, "T": 1}
    assert result["choices"] == {"channels": [0, 1], "z_count": 2, "time_count": 1, "max_roi_size": 1024}
    assert result["selected"] == {"indices": [0, 0, 0], "roi": [0, 0, 8, 6]}
    assert "data_base64" not in result and "raw_metadata" not in str(result)
    assert validate_czi_payload(result) is result


def test_actual_czi_selected_channel_z_and_roi_have_exact_intensity_range(czi_lib, tmp_path):
    from PIL import Image
    data = create(czi_lib, tmp_path)
    result = czi_preview(data, "image", {"indices": [1, 1, 0], "roi": [2, 1, 3, 2]})
    assert result["metadata"]["display_range"] == [1110, 1120]
    image = Image.open(io.BytesIO(base64.b64decode(result["data_base64"])))
    assert image.size == (3, 2) and image.mode == "RGBA"
    assert image.getpixel((0, 0)) == (0, 0, 0, 255)
    assert image.getpixel((2, 1)) == (255, 255, 255, 255)
    assert result["sampled"] is True


def test_actual_czi_rgb_order_is_not_silently_swapped(czi_lib, tmp_path):
    from PIL import Image
    data = create(czi_lib, tmp_path, color=True)
    result = czi_preview(data, "image", {"indices": [0, 0, 0], "roi": [0, 0, 2, 2]})
    image = Image.open(io.BytesIO(base64.b64decode(result["data_base64"])))
    assert image.getpixel((0, 0)) == (96, 24, 12)
    assert result["metadata"]["normalization"] == "native uint8 BGR to RGB; no scaling"


def test_multi_scene_actual_czi_is_refused_before_decode(czi_lib, tmp_path):
    with pytest.raises(CziPreviewError, match="scene"):
        czi_preview(create(czi_lib, tmp_path, scene_count=2), "tree", {})


def test_all_nonfinite_actual_czi_roi_is_refused(czi_lib, tmp_path):
    with pytest.raises(CziPreviewError, match="有限"):
        czi_preview(create(czi_lib, tmp_path, float_values=float("nan")), "image", {"indices": [0, 0, 0], "roi": [0, 0, 2, 2]})


@pytest.mark.parametrize("options", [{}, {"indices": [0, 0], "roi": [0, 0, 1, 1]}, {"indices": [0, 0, 0], "roi": [0, 0, 1025, 1]}, {"indices": [0, 0, 0], "roi": [-1, 0, 1, 1]}, {"indices": [2, 0, 0], "roi": [0, 0, 1, 1]}, {"indices": [False, 0, 0], "roi": [0, 0, 1, 1]}, {"indices": [0, 0, 0], "roi": [7, 0, 2, 1]}, {"indices": [0, 0, 0], "roi": [0, 0, 1, 1], "url": "evil"}])
def test_plane_requires_explicit_valid_selection(czi_lib, tmp_path, options):
    with pytest.raises(CziPreviewError): czi_preview(create(czi_lib, tmp_path), "image", options)


@pytest.mark.parametrize("data", [b"", b"not-czi", b"ZISRAWFILE" + b"\0" * 104, b"ZISRAWFILE" + b"\0" * 104 + b"trailing"])
def test_segment_preflight_fails_without_native_import(data):
    with pytest.raises(CziPreviewError): _preflight(data)


def test_real_czi_multipart_and_corrupt_segment_lengths_refused(czi_lib, tmp_path):
    data = bytearray(create(czi_lib, tmp_path))
    struct.pack_into("<I", data, 80, 1)
    with pytest.raises(CziPreviewError, match="分卷"): _preflight(bytes(data))
    struct.pack_into("<I", data, 80, 0); struct.pack_into("<q", data, 16, len(data) * 10)
    with pytest.raises(CziPreviewError, match="分段"): _preflight(bytes(data))


def test_synthetic_png_shape_and_extra_payload_are_rejected(czi_lib, tmp_path):
    result = czi_preview(create(czi_lib, tmp_path), "image", {"indices": [0, 0, 0], "roi": [0, 0, 2, 2]})
    result["metadata"]["output_shape"] = [100000, 100000]
    with pytest.raises(CziPreviewError): validate_czi_payload(result)


def test_private_czi_schema_requires_strict_integer_fields(czi_lib, tmp_path):
    import copy
    result = czi_preview(create(czi_lib, tmp_path), "image", {"indices": [0, 0, 0], "roi": [0, 0, 1, 1]})
    mutations = [lambda r: r.update(contract_version=2.0), lambda r: r["metadata"].update(scene=False),
                 lambda r: r["metadata"].update(output_shape=[True, True]), lambda r: r["choices"].update(time_count=True),
                 lambda r: r["choices"].update(channels=[False, 1])]
    for mutate in mutations:
        broken = copy.deepcopy(result); mutate(broken)
        with pytest.raises(CziPreviewError): validate_czi_payload(broken)
