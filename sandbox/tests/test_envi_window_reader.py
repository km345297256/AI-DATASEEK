import copy
import importlib.util
import json
from pathlib import Path
import numpy as np
import pytest
from app.services.envi_window_reader import envi_window_preview, parse_header
from app.services.envi_window_payload import validate_envi_window_payload, validate_envi_window_options, validate_envi_resources
from envi_window_fixtures import fixture, preview, browser_payloads

@pytest.mark.parametrize("layout", ["bsq", "bil", "bip"])
@pytest.mark.parametrize("dtype", ["u1", "<i2", ">i2", "<i4", ">i4", "<f4", ">f4", "<f8", ">f8", "<u2", ">u2", "<u4", ">u4"])
def test_layout_endian_dtype_matches_independent_numpy_slices(layout, dtype):
    h, d, expected = fixture(layout, dtype)
    tree, calls = preview(h, d)
    assert calls == [(0, len(h))] and "array" not in tree
    image, _ = preview(h, d, "image", {"band": 2, "x": 1, "y": 1, "width": 3, "height": 2})
    assert image["array"]["values"] == expected[2, 1:3, 1:4].flatten().tolist()
    series, _ = preview(h, d, "series", {"x": 2, "y": 1, "band_start": 1, "band_count": 3})
    assert series["array"]["values"] == expected[1:4, 1, 2].tolist()
    assert series["axes"][0]["values"] == [500, 600, 700]

@pytest.mark.parametrize("interleave", ["BSQ", "BIL", "BIP"])
@pytest.mark.parametrize("dtype", ["int16", "float32", "uint32"])
def test_gdal_generated_real_envi_pair(tmp_path, interleave, dtype):
    import rasterio
    values = np.arange(60, dtype=dtype).reshape(5, 3, 4)
    path = tmp_path / "oracle.bin"
    with rasterio.open(path, "w", driver="ENVI", width=4, height=3, count=5, dtype=dtype, interleave=interleave) as out:
        out.write(values)
    result, _ = preview(path.with_suffix(".hdr").read_bytes(), path.read_bytes(), "series", {"x": 3, "y": 2, "band_start": 0, "band_count": 5})
    assert result["array"]["values"] == values[:, 2, 3].tolist()
    assert result["choices"]["cube"]["interleave"] == interleave.lower()

@pytest.mark.parametrize("before,after", [(b"samples = 4", b"samples = 0"), (b"bands = 5", b"bands = 9999999999"),
    (b"data type = 4", b"data type = 6"), (b"data type = 4", b"data type = 14"), (b"byte order = 0", b"byte order = 2"),
    (b"interleave = bsq", b"interleave = gzip"), (b"header offset = 8", b"header offset = 7"),
    (b"file type = ENVI Standard", b"file type = ENVI Classification"), (b"400, 500", b"nan, 500"),
    (b"Nanometers", b"<script>"), (b"bands = 5", b"bands = -5"), (b"samples = 4", b"samples = 4\nsamples = 4")])
def test_malformed_or_unsupported_header_rejected(before, after):
    h, d, _ = fixture()
    with pytest.raises(ValueError): preview(h.replace(before, after), d)

@pytest.mark.parametrize("extra", [b"data file = /Users/private/secret.bin\n", b"data file = https://example.invalid/x\n", b"broken\n", b"wavelength = {1,2\n", b"nested = {{x}}\n", b"\x00", b"a"*65536])
def test_paths_malformed_fields_and_budget_never_reach_data(extra):
    h, d, _ = fixture()
    with pytest.raises(ValueError): preview(h+extra, d)

def test_missing_nonfinite_and_ignore_values_are_explicit_nulls():
    h, d, _ = fixture()
    h += b"data ignore value = -999\n"
    values = np.frombuffer(d[8:], dtype="<f4").copy(); values[:4] = [np.nan, np.inf, -999, 0]
    result, _ = preview(h, d[:8]+values.tobytes(), "image", {"band": 0, "x": 0, "y": 0, "width": 4, "height": 1})
    assert result["array"]["values"] == [None, None, None, 0] and result["metadata"]["null_values"] == 3

def test_float32_ignore_is_compared_in_stored_dtype():
    h,d,_=fixture(); h+=b"data ignore value = -9999.1\n"
    values=np.frombuffer(d[8:],dtype="<f4").copy(); values[0]=-9999.1
    out,_=preview(h,d[:8]+values.tobytes(),"image",{"band":0,"x":0,"y":0,"width":1,"height":1})
    assert out["array"]["values"] == [None] and out["metadata"]["null_values"] == 1

@pytest.mark.parametrize("ignore", [b"1e-999",b"1e-99",b"1e300"])
def test_ignore_underflow_or_overflow_never_masks_zero(ignore):
    h,d,_=fixture()
    with pytest.raises(ValueError): preview(h+b"data ignore value = "+ignore+b"\n",d)

@pytest.mark.parametrize("unit", [b"GHz",b"MHz",b"Wavenumber"])
def test_spectral_axis_never_mislabels_frequency_as_wavelength(unit):
    h,d,_=fixture()
    out,_=preview(h.replace(b"Nanometers",unit),d,"series",{"x":0,"y":0,"band_start":0,"band_count":2})
    assert out["axes"][0]["label"] == "光谱坐标" and out["axes"][0]["unit"] == unit.decode()

def test_large_virtual_cube_metadata_and_roi_only_fetch_selected_spans():
    h = b"ENVI\nsamples = 1000000\nlines = 500\nbands = 1\nfile type = ENVI Standard\ndata type = 5\ninterleave = bsq\nbyte order = 0\n"
    size = len(h)+4_000_000_000
    resources = [{"key":"header", "offset":0,"size":len(h)}, {"key":"data","offset":len(h),"size":4_000_000_000}]
    calls=[]
    def read(offset, length):
        calls.append((offset,length)); return h if offset == 0 else b"\0"*length
    tree = envi_window_preview(read,size,resources)
    assert calls == [(0,len(h))] and tree["metadata"]["source_bytes"] == size
    calls.clear()
    image = envi_window_preview(read,size,resources,"image",{"band":0,"x":999990,"y":490,"width":4,"height":3})
    assert image["array"]["values"] == [0]*12 and image["metadata"]["read_bytes"] == len(h)+96 and len(calls)==4

@pytest.mark.parametrize("kind,options", [("tree",{"url":"x"}), ("image",{"band":0,"x":0,"y":0,"width":129,"height":1}),
    ("series",{"x":True,"y":0,"band_start":0,"band_count":1}), ("series",{"x":0,"y":0,"band_start":0,"band_count":129}),
    ("series",{"x":0,"y":0,"band_start":0,"band_count":1,"sql":"x"})])
def test_invalid_options_before_any_io(kind, options):
    with pytest.raises(ValueError): envi_window_preview(lambda *_: pytest.fail("unexpected IO"),2,[],kind,options)

def test_budget_and_truncation_reject():
    h,d,_=fixture()
    with pytest.raises(ValueError): preview(h,d,limits={"max_total_bytes":1,"max_read_bytes":1,"max_reads":1})
    with pytest.raises(ValueError): preview(h,d[:-1])
    with pytest.raises(ValueError): preview(h,d,"series",{"x":4,"y":0,"band_start":0,"band_count":1})

def test_backend_payload_copy_and_request_binding():
    path=Path(__file__).resolve().parents[2]/"backend/app/application/services/envi_window_visualization.py"
    spec=importlib.util.spec_from_file_location("envi_backend",path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for kind,value in browser_payloads().items():
        assert module.validate_envi_window_payload(value,kind=kind,options=value["selected"],fmt="hdr",source_bytes=value["metadata"]["source_bytes"]) == value
        with pytest.raises(ValueError): module.validate_envi_window_payload(value,read_bytes=value["metadata"]["read_bytes"]+1)
    source=Path(__file__).resolve().parents[1]/"app/services/envi_window_payload.py"
    assert path.read_text() == source.read_text()

@pytest.mark.parametrize("key", ["format","source_bytes","read_bytes","null_values","no_calibration"])
def test_strict_metadata_forgery(key):
    result=browser_payloads()["image"]; result["metadata"][key] = None
    with pytest.raises(ValueError): validate_envi_window_payload(result)

def test_scope_resources_cannot_be_arbitrary_paths_or_overlap():
    for resources in ([{"key":"/data/raw","offset":0,"size":1},{"key":"data","offset":1,"size":1}],
        [{"key":"header","offset":0,"size":1},{"key":"data","offset":0,"size":1}]):
        with pytest.raises(ValueError): validate_envi_resources(resources,2)
