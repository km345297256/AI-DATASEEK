"""Small read-only ecCodes C ABI adapter with local native symbol visibility.

ecCodes-python 2.48's findlibs dependency discovery preloads every eckit library
with RTLD_GLOBAL. That conflicts with the existing libCZI ZSTD writer. This
adapter uses the official ecCodes C core, not a replacement GRIB decoder: only
the five required eckit libraries and libeccodes are loaded, all RTLD_LOCAL.
No environment, ctypes, findlibs, or third-party module is monkeypatched.
Sample creation and setters deliberately live only in subprocess test fixtures.
"""
from __future__ import annotations

import ctypes
import importlib.metadata
from pathlib import Path
import sys
import threading

from .grib_window_payload import GribWindowError

_LOCK = threading.Lock()
_RUNTIME = None
_VERSIONS = {"eccodes": "2.48.0", "eccodeslib": "2.48.2.27", "eckitlib": "2.2.0.27"}
_CDEF = """
typedef struct grib_handle codes_handle;
typedef struct grib_context codes_context;
codes_handle* codes_handle_new_from_message_copy(codes_context*, const void*, size_t);
codes_handle* grib_handle_new_from_partial_message_copy(codes_context*, const void*, size_t);
int codes_handle_delete(codes_handle*);
int codes_get_string(const codes_handle*, const char*, char*, size_t*);
int codes_get_size(const codes_handle*, const char*, size_t*);
int codes_get_double_array(const codes_handle*, const char*, double*, size_t*);
long codes_get_api_version(void);
char* codes_samples_path(const codes_context*);
char* codes_definition_path(const codes_context*);
"""


def _fail():
    raise GribWindowError("GRIB 原生读取器不可用或超出受控边界")


def _package_root(name):
    dist = importlib.metadata.distribution(name)
    if dist.version != _VERSIONS[name]:
        _fail()
    # Only installed wheel records, never a dataset-provided path, environment
    # variable, home-directory config or system-wide find_library fallback.
    record = f"{name}/__init__.py"
    if not any(str(item) == record for item in dist.files or ()):
        _fail()
    return Path(dist.locate_file(record)).resolve(strict=True).parent


def _library(root, name):
    path = (root / "lib64" / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        _fail()
    return str(path)


class _ReadOnlyEcCodes:
    def __init__(self):
        if sys.platform != "linux":
            _fail()  # Production is the locked Linux sandbox, not host libs.
        import cffi
        for name, expected in _VERSIONS.items():
            if importlib.metadata.version(name) != expected:
                _fail()
        eckit, eccodes = _package_root("eckitlib"), _package_root("eccodeslib")
        # Wheel DT_NEEDED dependencies, not an unbounded directory glob. Keep
        # handles alive so another local library can bind their SONAMEs safely.
        self._dependencies = tuple(ctypes.CDLL(_library(eckit, name), mode=ctypes.RTLD_LOCAL) for name in (
            "libeckit.so", "libeckit_spec.so", "libeckit_maths.so",
            "libeckit_codec.so", "libeckit_geo.so"))
        self._ffi = cffi.FFI()
        self._ffi.cdef(_CDEF)
        self._lib = self._ffi.dlopen(_library(eccodes, "libeccodes.so"), self._ffi.RTLD_LOCAL | self._ffi.RTLD_NOW)
        if self._lib.codes_get_api_version() != 24802:
            _fail()
        if self.codes_definition_path() != "/MEMFS/definitions" or self.codes_samples_path() != "/MEMFS/samples":
            _fail()

    def codes_get_api_version(self):
        return "2.48.2"

    def _path(self, function):
        value = function(self._ffi.NULL)
        if value == self._ffi.NULL:
            _fail()
        return self._ffi.string(value, 128).decode("ascii")

    def codes_definition_path(self):
        return self._path(self._lib.codes_definition_path)

    def codes_samples_path(self):
        return self._path(self._lib.codes_samples_path)

    def codes_new_from_message(self, message, partial=False):
        if type(message) is not bytes or not 20 <= len(message) <= 1048576 or type(partial) is not bool:
            _fail()
        function = (self._lib.grib_handle_new_from_partial_message_copy if partial
                    else self._lib.codes_handle_new_from_message_copy)
        handle = function(self._ffi.NULL, message, len(message))
        if handle == self._ffi.NULL:
            _fail()
        return handle

    def codes_release(self, handle):
        if self._lib.codes_handle_delete(handle) != 0:
            _fail()

    def codes_get(self, handle, key):
        if key not in {"name", "shortName", "units", "gridType", "packingType"}:
            _fail()
        value, length = self._ffi.new("char[129]"), self._ffi.new("size_t*", 129)
        if self._lib.codes_get_string(handle, key.encode("ascii"), value, length) != 0 or not 1 <= length[0] <= 129:
            _fail()
        result = self._ffi.string(value, 129)
        if not 1 <= len(result) <= 128:
            _fail()
        return result.decode("ascii")

    def codes_get_size(self, handle, key):
        if key not in {"values", "latitudes", "longitudes"}:
            _fail()
        length = self._ffi.new("size_t*")
        if self._lib.codes_get_size(handle, key.encode("ascii"), length) != 0 or not 1 <= length[0] <= 16384:
            _fail()
        return int(length[0])

    def codes_get_array(self, handle, key):
        count = self.codes_get_size(handle, key)
        values, length = self._ffi.new("double[]", count), self._ffi.new("size_t*", count)
        if self._lib.codes_get_double_array(handle, key.encode("ascii"), values, length) != 0 or length[0] != count:
            _fail()
        return list(self._ffi.unpack(values, count))

    def codes_get_values(self, handle):
        return self.codes_get_array(handle, "values")


def get_eccodes_runtime():
    """Lazy singleton, initialized without process-global loading side effects."""
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None:
            try:
                _RUNTIME = _ReadOnlyEcCodes()
            except Exception:
                _fail()
        return _RUNTIME
