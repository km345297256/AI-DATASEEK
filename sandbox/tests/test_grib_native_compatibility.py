"""Same-process GRIB/CZI native compatibility, independent SDK oracle bytes."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.grib_eccodes_runtime import _ReadOnlyEcCodes, get_eccodes_runtime
from app.services.grib_window_payload import GribWindowError
from grib_window_fixtures import message, preview


@pytest.mark.parametrize("order", ["grib-first", "czi-first"])
def test_ecmwf_core_and_old_czi_zstd_coexist_in_both_import_orders(order):
    # Separate process only to guarantee each requested initial import order;
    # inside each process both actual native libraries perform work together.
    code = r'''
import ctypes, importlib.metadata, json, os, pathlib, sys, tempfile
before_env, before_cdll = dict(os.environ), ctypes.CDLL
versions = {name: importlib.metadata.version(name) for name in (
  'pylibCZIrw','numpy','rasterio','netCDF4','h5py','numcodecs','eccodes','eccodeslib','eckitlib')}
from app.services.grib_eccodes_runtime import get_eccodes_runtime
from grib_window_fixtures import message, preview
import numpy as np
def czi_roundtrip():
    from pylibCZIrw import czi
    with tempfile.TemporaryDirectory() as temp:
        path = str(pathlib.Path(temp) / 'synthetic.czi')
        data = np.arange(48, dtype=np.uint16).reshape(6,8,1)
        with czi.create_czi(path, compression_options='zstd0:ExplicitLevel=1') as writer:
            writer.write(data, plane={'C':0,'Z':0,'T':0})
        with czi.open_czi(path) as reader:
            actual = reader.read(roi=(0,0,8,6), plane={'C':0,'Z':0,'T':0}, zoom=1)
            assert np.array_equal(actual, data)
def grib_roundtrip():
    runtime = get_eccodes_runtime()
    assert runtime.codes_get_api_version() == '2.48.2'
    data = message()  # upstream Python ecCodes oracle in another process
    result, _ = preview(data, 'image', {'message':'g-0000000000000000','roi':[1,0,4,3]})
    assert result['array']['values'] == [281,282,283,None,287,288,289,290,293,294,295,296]
    assert result['axes'][0]['values'] == [50,49,48]
    assert result['axes'][1]['values'] == [11,12,13,14]
if sys.argv[1] == 'grib-first':
    grib_roundtrip(); czi_roundtrip(); grib_roundtrip()
else:
    czi_roundtrip(); grib_roundtrip(); czi_roundtrip()
assert before_env == dict(os.environ) and ctypes.CDLL is before_cdll
assert 'eccodes' not in sys.modules and 'gribapi.bindings' not in sys.modules and 'findlibs' not in sys.modules
assert versions == {name:importlib.metadata.version(name) for name in versions}
print(json.dumps({'order':sys.argv[1], 'grib_values':True,'czi_zstd_roundtrip':True,'versions':versions}))
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)))
    result = subprocess.run([sys.executable, "-c", code, order], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    out = json.loads(result.stdout)
    assert out["order"] == order and out["czi_zstd_roundtrip"] and out["grib_values"]
    assert out["versions"]["eccodeslib"] == "2.48.2.27"
    assert out["versions"]["eckitlib"] == "2.2.0.27"
    assert out["versions"]["pylibCZIrw"] == "6.1.0"


def test_package_version_mismatch_fails_before_native_load(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "wrong")
    with pytest.raises(GribWindowError, match="受控边界"):
        _ReadOnlyEcCodes()


def test_native_adapter_exposes_no_file_sample_or_write_api():
    runtime = get_eccodes_runtime()
    assert not any(name.startswith(("codes_set", "codes_grib_new", "codes_write")) for name in dir(runtime))
    assert not hasattr(runtime, "codes_new_from_file")
    with pytest.raises(GribWindowError): runtime.codes_new_from_message(b"x" * 1048577)
    with pytest.raises(GribWindowError): runtime.codes_get(None, "source_filename")
    with pytest.raises(GribWindowError): runtime.codes_get_size(None, "unbounded_key")


def test_native_failure_still_releases_handle_and_does_not_leak_error(monkeypatch):
    data, runtime, released = message(), get_eccodes_runtime(), []
    release = runtime.codes_release
    def tracked(handle):
        released.append(handle)
        release(handle)
    def fail(*_):
        raise RuntimeError("native /private/source/secret.grib")
    monkeypatch.setattr(runtime, "codes_release", tracked)
    monkeypatch.setattr(runtime, "codes_get", fail)
    with pytest.raises(GribWindowError) as result:
        preview(data)
    assert len(released) == 1 and "secret" not in str(result.value)
