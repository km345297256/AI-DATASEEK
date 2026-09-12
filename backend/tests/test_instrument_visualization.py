import base64
import copy
import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

from app.application.services.instrument_visualization import (
    InstrumentPreviewError, validate_instrument_options, validate_instrument_payload,
)


@pytest.fixture(scope="module")
def independent():
    modules = []
    root = Path(__file__).resolve().parents[2] / "sandbox"
    for name, relative in [("test_instrument_range_reader", "app/services/instrument_window_reader.py"),
                           ("test_instrument_range_fixtures", "tests/instrument_window_fixtures.py")]:
        spec = importlib.util.spec_from_file_location(name, root / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    yield modules
    for module in modules:
        sys.modules.pop(module.__name__, None)


def payload(independent, fmt="edf", kind="image"):
    reader, fixture = independent
    source = fixture.MemorySource((fixture.edf_bytes if fmt == "edf" else fixture.spe_bytes)())
    options = {"frame": 1, "roi": [1, 1, 2, 2]} if kind == "image" else {}
    result = reader.instrument_window_preview(source.read, source.size, fmt, kind, options)
    return result, source, options


@pytest.mark.parametrize("fmt", ["edf", "spe"])
@pytest.mark.parametrize("kind", ["tree", "image"])
def test_real_isolated_reader_schema_and_request_binding(independent, fmt, kind):
    result, source, options = payload(independent, fmt, kind)
    assert validate_instrument_payload(result, size=source.size, kind=kind, options=options, format=fmt,
        read_bytes=sum(length for _, length in source.reads), read_requests=len(source.reads)) is result


@pytest.mark.parametrize("kind,options", [("tree", {}), ("image", {"frame": 999999, "roi": [99999, 99999, 1024, 1024]})])
def test_valid_option_boundaries(kind, options):
    assert validate_instrument_options(kind, options) is options


@pytest.mark.parametrize("kind,options", [("tree", {"frame": 0}), ("tree", None), ("image", {}), ([], {}),
    ("image", {"frame": 0, "roi": [0, 0, 1, 1], "path": "/private"}),
    ("image", {"frame": True, "roi": [0, 0, 1, 1]}), ("image", {"frame": 1000000, "roi": [0, 0, 1, 1]}),
    ("image", {"frame": 0, "roi": [0, 0, True, 1]}), ("image", {"frame": 0, "roi": [0, 0, 1025, 1]}),
    ("image", {"frame": 0, "roi": [-1, 0, 1, 1]}), ("image", {"frame": 0, "roi": [0.0, 0, 1, 1]}),
    ("image", {"frame": 0, "roi": [float("inf"), 0, 1, 1]})])
def test_invalid_options_always_safe_value_error(kind, options):
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_options(kind, options)


@pytest.mark.parametrize("key,value", [("contract_version", True), ("type", "edf"), ("reader", "edf"),
    ("kind", ["image"]), ("kind", "table"), ("media_type", "text/html"), ("sampled", 1),
    ("warnings", ["/private/secret"]), ("path", "/private/secret"), ("metadata", []),
    ("choices", {}), ("selected", []), ("url", "https://invalid.example")])
def test_inert_envelope_rejects_unknown_fields_and_wrong_types(independent, key, value):
    result, _, _ = payload(independent)
    result[key] = value
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


@pytest.mark.parametrize("key,value", [("format", ["esrf-edf"]), ("format", "edf"), ("dialect", "EDF+C"),
    ("format_version", 2.6), ("source_bytes", True), ("source_bytes", 8 * 1024**3 + 1),
    ("source_bytes", 1073), ("read_bytes", 1033), ("read_requests", True), ("read_requests", 2049),
    ("read_requests", 2), ("header_bytes", 1025), ("frame_count", 257), ("frame_count", True),
    ("frame_shape", [True, 4]), ("frame_shape", [3, 5]), ("dtype", ["uint16"]), ("dtype", "uint64"),
    ("byte_order", "auto"), ("input_mode", "whole"), ("header_text_hidden", False),
    ("calibration", "energy"), ("storage_layout", "column-major"), ("selection_mode", "auto"),
    ("patient", "PRIVATE"), ("normalization", "automatic scientific correction"),
    ("output_shape", [2, True]), ("invalid_pixels", 1), ("invalid_pixels", True),
    ("display_range", [0, float("inf")]), ("display_range", [1, 0]), ("display_range", [-1, 65536]),
    ("display_range", [105.5, 110]), ("roi_origin", "sensor bottom-right")])
def test_metadata_cannot_claim_unsupported_layout_calibration_or_budget(independent, key, value):
    result, _, _ = payload(independent)
    result["metadata"][key] = value
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


@pytest.mark.parametrize("mutation", [lambda r: r["choices"].update(frame_count=True),
    lambda r: r["choices"].update(max_roi_size=True), lambda r: r["choices"].update(path="/private"),
    lambda r: r["selected"].update(frame=2), lambda r: r["selected"].update(roi=[3, 2, 2, 2]),
    lambda r: r["selected"].update(roi=[1, 1, 1, 2]), lambda r: r.update(sampled=False)])
def test_choices_selection_shape_and_sampling_stay_consistent(independent, mutation):
    result, _, _ = payload(independent)
    mutation(result)
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


@pytest.mark.parametrize("kwargs", [{"size": 1073}, {"size": True}, {"read_bytes": 1000}, {"read_requests": 3},
    {"kind": "tree"}, {"format": "spe"}, {"format": "bdf"},
    {"options": {"frame": 0, "roi": [1, 1, 2, 2]}}, {"options": {"frame": 1, "roi": [0, 0, 2, 2]}}])
def test_valid_payload_for_different_request_is_rejected(independent, kwargs):
    result, _, _ = payload(independent)
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result, **kwargs)


@pytest.mark.parametrize("mutation", [lambda r: r.update(data_base64="AAAA"),
    lambda r: r["tree"][0]["attributes"].update(children_count=False),
    lambda r: r["tree"][0]["attributes"].update(value="PRIVATE"),
    lambda r: r["metadata"].update(read_bytes=1025), lambda r: r["metadata"].update(read_requests=3),
    lambda r: r["selected"].update(frame=1)])
def test_tree_is_header_only_exact_schema(independent, mutation):
    result, _, _ = payload(independent, kind="tree")
    mutation(result)
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


def chunk(name, content):
    return struct.pack(">I", len(content)) + name + content + struct.pack(">I", zlib.crc32(name + content))


def png(raw=b"\x00\x00\x33\x00\xcc\xff", *, compressed=None, width=2, height=2, color=0, extra=b""):
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw) if compressed is None else compressed) + extra + chunk(b"IEND", b""))


@pytest.mark.parametrize("encoded", ["<svg/>", "AAAA", "a" * (2 * 1024**2 + 1),
    base64.b64encode(png(width=3)).decode(), base64.b64encode(png(color=6)).decode(),
    base64.b64encode(png(raw=b"\x00" * 7)).decode(), base64.b64encode(png(raw=b"\x00" * 5)).decode(),
    base64.b64encode(png(raw=b"\x01\x00\x33\x00\xcc\xff")).decode(),
    base64.b64encode(png(compressed=zlib.compress(b"\x00" * 2000000))).decode(),
    base64.b64encode(png(compressed=zlib.compress(b"\x00" * 6) + b"trailing")).decode(),
    base64.b64encode(png(compressed=zlib.compress(b"\x00" * 6)[:-1])).decode(),
    base64.b64encode(png(extra=chunk(b"tEXt", b"secret"))).decode(),
    base64.b64encode(png() + b"trailing").decode()], ids=lambda value: f"encoded-length-{len(value)}")
def test_png_is_bounded_canonical_grayscale_without_chunks_bombs_or_trailing_data(independent, encoded):
    result, _, _ = payload(independent)
    result["data_base64"] = encoded
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


def test_png_crc_corruption_is_rejected(independent):
    result, _, _ = payload(independent)
    data = bytearray(base64.b64decode(result["data_base64"]))
    data[-1] ^= 1
    result["data_base64"] = base64.b64encode(data).decode()
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)


@pytest.mark.parametrize("field,value", [("dtype", "uint32"), ("byte_order", "big"), ("format_version", 3),
    ("format_version", float("inf")), ("format_version", True), ("header_bytes", 4096)])
def test_spe_schema_does_not_infer_other_versions_or_ambiguous_datatypes(independent, field, value):
    result, _, _ = payload(independent, fmt="spe")
    result["metadata"][field] = value
    with pytest.raises(InstrumentPreviewError):
        validate_instrument_payload(result)
