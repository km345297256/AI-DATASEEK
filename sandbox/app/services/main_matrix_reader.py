"""Main's matrix workbench, adapted to a one-shot, networkless Cordis worker.

Only bytes enter this module. There is no preview session cache or host path.
Numeric values retain MATLAB reversed axes, complex components and sparse-bin
semantics. Existing generic array readers and budgets are intentionally unused.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import math
import os
import re
import struct
import warnings
import zipfile
import zlib

os.environ["HDF5_PLUGIN_PRELOAD"] = "::"

from .main_matrix_payload import (ERROR, LIMITS, WARNING, need, label,
                                  selection_plan, validate_main_matrix_options,
                                  validate_main_matrix_payload)

LIMIT = LIMITS["max_input_bytes"]


def _shape(shape, dtype):
    import numpy as np
    dtype = np.dtype(dtype)
    need(dtype.kind in "biufc" and not dtype.hasobject and not dtype.fields and not dtype.metadata)
    need(len(shape) <= 16 and all(type(n) is int and 0 < n <= 2**31 - 1 for n in shape) and math.prod(shape) <= 2**53 - 1)
    need(str(dtype) in {"bool", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "float16", "float32", "float64", "float128", "complex64", "complex128", "complex256"} or dtype.byteorder == ">")
    return str(dtype.newbyteorder("="))


def _header(stream, member_size, numeric=True):
    import numpy as np
    version = np.lib.format.read_magic(stream)
    need(version in {(1, 0), (2, 0)})
    shape, fortran, dtype = (np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0)(stream, max_header_size=10000)
    if numeric:
        _shape(shape, dtype)
    else:
        need(not dtype.hasobject and not dtype.fields and dtype.kind in "biuS" and dtype.itemsize <= 16 and len(shape) <= 2)
    nbytes = math.prod(shape) * dtype.itemsize
    need(nbytes <= LIMIT and stream.tell() + nbytes == member_size)
    return tuple(shape), dtype


def _zip(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        items = archive.infolist()
        need(1 <= len(items) <= 512 and sum(i.file_size for i in items) <= LIMIT)
        need(len({i.filename for i in items}) == len(items))
        for item in items:
            need(item.filename.endswith(".npy") and len(item.filename) <= 1024 and not item.is_dir() and not item.flag_bits & 1)
            need(not item.filename.startswith(("/", "\\")) and "\\" not in item.filename and not any(p in {".", "..", ""} for p in item.filename.split("/")))
            need(item.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} and ((item.external_attr >> 16) & 0o170000) in {0, 0o100000})
        return archive
    except Exception:
        archive.close()
        raise


def _mat_preflight(data):
    """Bound actual MAT v5 zlib expansion before SciPy can allocate from it."""
    if data.startswith(b"MATLAB 5.0 MAT-file"):
        need(len(data) >= 128 and data[126:128] in {b"IM", b"MI"})
        endian = "<" if data[126:128] == b"IM" else ">"
        pos, expanded = 128, 0
        while pos < len(data):
            need(pos + 8 <= len(data))
            kind, length = struct.unpack_from(endian + "II", data, pos)
            need(kind in {14, 15} and 0 < length <= LIMIT and pos + 8 + length <= len(data))
            if kind == 15:
                decoder = zlib.decompressobj()
                decoded = decoder.decompress(data[pos + 8:pos + 8 + length], LIMIT - expanded + 1)
                need(len(decoded) <= LIMIT - expanded and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail)
                expanded += len(decoded)
                pos += 8 + length  # miCOMPRESSED is not 8-byte padded.
            else:
                expanded += length
                need(expanded <= LIMIT)
                pos += 8 + ((length + 7) // 8) * 8
        need(pos == len(data))
        return
    # MAT v4 has no compression; validate each declared matrix before whosmat.
    pos = 0
    while pos < len(data):
        need(pos + 20 <= len(data))
        header = struct.unpack_from("<5i", data, pos)
        endian = "<"
        if not 0 <= header[0] <= 999:
            header, endian = struct.unpack_from(">5i", data, pos), ">"
        mopt, rows, cols, imag, namesize = header
        need(0 <= mopt <= 1999 and mopt // 1000 == (1 if endian == ">" else 0) and (mopt // 100) % 10 == 0 and mopt % 10 in {0, 1, 2})
        precision = (mopt // 10) % 10
        need(precision in range(6) and rows > 0 and cols > 0 and imag in {0, 1} and 1 <= namesize <= 1024)
        width = (8, 4, 4, 2, 2, 1)[precision]
        size = rows * cols * (imag + 1) * width
        need(size <= LIMIT and pos + 20 + namesize + size <= len(data))
        pos += 20 + namesize + size


def _hdf_signature(data):
    return any(data[o:o + 8] == b"\x89HDF\r\n\x1a\n" for o in [0] + [2**i for i in range(9, 21)])


def _hdf_check(dataset):
    import h5py
    p = dataset.id.get_create_plist()
    need(p.get_layout() != h5py.h5d.VIRTUAL and not p.get_external_count())
    _shape(dataset.shape, dataset.dtype)
    filters = [p.get_filter(i)[0] for i in range(p.get_nfilters())]
    need(filters in [[], [2], [1], [3], [2, 1], [2, 3], [1, 3], [2, 1, 3]])
    need(not dataset.chunks or math.prod(dataset.chunks) * dataset.dtype.itemsize <= LIMIT)


def _hdf_preflight(dataset, slices, source_bytes):
    """Validate actual selected compressed chunks, with a matrix-only 128MiB cap."""
    import itertools
    if not dataset.chunks:
        return
    chunk_bytes = math.prod(dataset.chunks) * dataset.dtype.itemsize
    coordinates = []
    for selected, chunk in zip(slices, dataset.chunks):
        indexes = [selected] if type(selected) is int else range(selected.start, selected.stop, selected.step)
        coordinates.append(sorted({i // chunk * chunk for i in indexes}))
    need(math.prod(map(len, coordinates)) * chunk_bytes <= LIMIT)
    p = dataset.id.get_create_plist()
    filters = [p.get_filter(i)[0] for i in range(p.get_nfilters())]
    for coord in itertools.product(*coordinates):
        info = dataset.id.get_chunk_info_by_coord(coord)
        if info.byte_offset is None and info.size == 0:
            continue
        need(type(info.byte_offset) is int and 0 <= info.byte_offset and 0 < info.size <= LIMIT and info.byte_offset + info.size <= source_bytes)
        mask, encoded = dataset.id.read_direct_chunk(coord)
        need(0 <= mask < 2**len(filters) and len(encoded) == info.size)
        active = [f for i, f in enumerate(filters) if not mask & (1 << i)]
        if 3 in active:
            need(len(encoded) >= 4)
            encoded = encoded[:-4]
        if 1 in active:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(encoded, chunk_bytes + 1)
            need(len(decoded) == chunk_bytes and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail)
        else:
            need(len(encoded) == chunk_bytes)


class MatrixSource:
    def __init__(self, data, fmt):
        self.data, self.fmt, self.arrays, self.names = data, fmt, [], {}
        self.is_hdf = fmt == "mat" and _hdf_signature(data)

    def add(self, name, shape, dtype, sparse=False):
        dtype_name = _shape(tuple(int(n) for n in shape), dtype)
        need(len(self.arrays) < 512)
        ident = f"array-{len(self.arrays)}"
        public_name = name if label(name) else "[redacted]"
        self.arrays.append({"id": ident, "name": public_name, "shape": [int(n) for n in shape], "dtype": dtype_name, "sparse": bool(sparse)})
        self.names[ident] = name

    def describe(self):
        import numpy as np
        from scipy import io as sio
        if self.fmt == "npy":
            shape, dtype = _header(io.BytesIO(self.data), len(self.data))
            self.add("array", shape, dtype)
        elif self.fmt == "npz":
            with _zip(self.data) as archive:
                files = set(archive.namelist())
                scipy_sparse = {"format.npy", "shape.npy", "data.npy"} <= files
                if scipy_sparse:
                    allowed = {"format.npy", "shape.npy", "data.npy", "indices.npy", "indptr.npy", "row.npy", "col.npy", "offsets.npy", "_is_array.npy"}
                    need(files <= allowed)
                    for item in archive.infolist():
                        with archive.open(item) as stream:
                            _header(stream, item.file_size, numeric=item.filename != "format.npy")
                    shape_raw = np.load(io.BytesIO(archive.read("shape.npy")), allow_pickle=False)
                    need(shape_raw.shape == (2,) and shape_raw.dtype.kind in "iu")
                    fmt = np.load(io.BytesIO(archive.read("format.npy")), allow_pickle=False)
                    need(fmt.shape == () and fmt.dtype.kind == "S" and fmt.item() in {b"csr", b"csc", b"coo", b"dia", b"bsr"})
                    with archive.open("data.npy") as stream:
                        _, dtype = _header(stream, archive.getinfo("data.npy").file_size)
                    self.add("matrix", shape_raw.tolist(), dtype, True)
                else:
                    for item in archive.infolist():
                        with archive.open(item) as stream:
                            try:
                                shape, dtype = _header(stream, item.file_size)
                            except ValueError:
                                continue  # Main ignored nonnumeric members in mixed NPZ files.
                        self.add(item.filename[:-4], shape, dtype)
        elif self.fmt == "mtx":
            rows, cols, entries, storage, field, symmetry = sio.mminfo(io.BytesIO(self.data))
            need(storage in {"coordinate", "array"} and field in {"real", "complex", "integer", "pattern"} and symmetry in {"general", "symmetric", "skew-symmetric", "hermitian"})
            need(entries <= 2_000_000 and (storage != "array" or rows * cols * 16 <= LIMIT))
            self.add("matrix", (rows, cols), "complex128" if field == "complex" else "int64" if field == "integer" else "float64", storage == "coordinate")
        elif self.is_hdf:
            import h5py
            while h5py.h5pl.size():
                h5py.h5pl.remove(0)
            with h5py.File(io.BytesIO(self.data), "r") as handle:
                count = handle.id.get_num_objs()
                need(count <= 512)
                for i in range(count):
                    raw_name = handle.id.get_objname_by_idx(i)
                    need(len(raw_name) <= 1024)
                    name = raw_name.decode("utf-8", "strict")
                    if name.startswith("#") or handle.get(name, getlink=True, getclass=True) is not h5py.HardLink:
                        continue
                    item = handle[name]
                    if not isinstance(item, h5py.Dataset) or item.dtype.kind not in "biufc":
                        continue
                    _hdf_check(item)
                    self.add(name, item.shape[::-1], item.dtype)
        else:
            _mat_preflight(self.data)
            entries = sio.whosmat(io.BytesIO(self.data))
            need(len(entries) <= 512 and len({e[0] for e in entries}) == len(entries))
            for name, shape, dtype in entries:
                if dtype in {"double", "single", "logical", "sparse", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"}:
                    self.add(name, shape, {"double": "float64", "single": "float32", "logical": "bool", "sparse": "float64"}.get(dtype, dtype), dtype == "sparse")
        need(self.arrays)
        return self.arrays

    @contextmanager
    def array(self, info):
        import numpy as np
        from scipy import io as sio, sparse
        name = self.names[info["id"]]
        if self.fmt == "npy":
            yield np.load(io.BytesIO(self.data), allow_pickle=False), False
        elif self.fmt == "npz":
            if info["sparse"]:
                value = sparse.load_npz(io.BytesIO(self.data))
                need(value.shape == tuple(info["shape"]) and value.dtype.kind in "biufc")
                if hasattr(value, "check_format"):
                    value.check_format(full_check=True)
                yield value.tocoo(), False
            else:
                with np.load(io.BytesIO(self.data), allow_pickle=False) as handle:
                    yield handle[name], False
        elif self.fmt == "mtx":
            # Worker CPU isolation and one parsing thread avoid spawning per-host CPU.
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):
                value = sio.mmread(io.BytesIO(self.data))
            yield value.tocoo() if sparse.issparse(value) else value, False
        elif self.is_hdf:
            import h5py
            with h5py.File(io.BytesIO(self.data), "r") as handle:
                yield handle[name], True
        else:
            need(info["sparse"] or math.prod(info["shape"]) * 16 <= LIMIT)
            value = sio.loadmat(io.BytesIO(self.data), variable_names=[name], verify_compressed_data_integrity=True)[name]
            yield value.tocoo() if sparse.issparse(value) else value, False


def _native(values):
    import numpy as np
    result = np.asarray(values)
    # Do not silently round a scientific integer beyond JavaScript's safe range.
    if result.dtype.kind in "iu":
        need(not result.size or np.all(np.abs(result.astype(np.longdouble)) <= 2**53 - 1))
    converted = np.asarray(result, dtype=float)
    need(not np.any(np.isfinite(result) & ~np.isfinite(converted)))
    return converted


def _render(source, selected):
    import numpy as np
    from scipy import sparse
    info = next((a for a in source.arrays if a["id"] == selected["variable"]), None)
    need(info is not None)
    row, col, row_step, col_step = selection_plan(info, selected)
    shape, axes, indices = info["shape"], selected["axes"], selected["indices"]
    slices = list(indices)
    if len(shape) >= 2:
        slices[axes[0]], slices[axes[1]] = slice(*row, row_step), slice(*col, col_step)
    elif shape:
        slices[0] = slice(*col, col_step)
    with source.array(info) as (array, reversed_axes):
        need(tuple(array.shape) == tuple(shape[::-1] if reversed_axes else shape))
        if sparse.issparse(array):
            array = array.tocoo(copy=True)
            array.sum_duplicates()
            mask = (array.row >= row[0]) & (array.row < row[1]) & (array.col >= col[0]) & (array.col < col[1])
            rr, cc, values = array.row[mask] - row[0], array.col[mask] - col[0], array.data[mask]
            nonzero = int(np.count_nonzero(values))
            mask = values != 0 if selected["structure"] else (rr % row_step == 0) & (cc % col_step == 0)
            plane = np.zeros((math.ceil((row[1] - row[0]) / row_step), math.ceil((col[1] - col[0]) / col_step)), dtype=float if selected["structure"] else array.dtype)
            np.add.at(plane, (rr[mask] // row_step, cc[mask] // col_step), np.ones(np.count_nonzero(mask)) if selected["structure"] else values[mask])
        else:
            hdf_slices = slices[::-1] if reversed_axes else slices
            if reversed_axes:
                _hdf_preflight(array, hdf_slices, len(source.data))
            plane = np.asarray(array[tuple(hdf_slices)])
            if reversed_axes:
                plane = plane.transpose()
            nonzero = int(np.count_nonzero(plane))
        if len(shape) >= 2 and axes[0] > axes[1]:
            plane = plane.T
        if selected["structure"]:
            plane = np.asarray(plane != 0, dtype=float)
        else:
            need(plane.dtype.kind not in "iu" or not plane.size or np.all(np.abs(plane.astype(np.longdouble)) <= 2**53 - 1))
            with np.errstate(over="ignore", invalid="ignore"):
                plane = {"real": np.real, "imaginary": np.imag, "magnitude": np.abs, "phase": np.angle}[selected["component"]](plane)
        plane = _native(plane).copy()
    plane = plane.reshape(1, -1) if plane.ndim < 2 else plane
    finite = plane[np.isfinite(plane)]
    # Scaling avoids overflow in mean/std/row means for valid large floats.
    scale = float(np.max(np.abs(finite))) if finite.size else 0
    scaled = plane / scale if scale else plane
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        row_profile = np.nanmean(np.where(np.isfinite(plane), scaled, np.nan), axis=1) * (scale or 1)
        col_profile = np.nanmean(np.where(np.isfinite(plane), scaled, np.nan), axis=0) * (scale or 1)
    result = {"values": [[float(v) if np.isfinite(v) else None for v in r] for r in plane],
              "rows": list(range(*row, row_step)), "columns": list(range(*col, col_step)), "shape": shape,
              "sampled": row_step > 1 or col_step > 1, "row_step": row_step, "column_step": col_step,
              "row_range": row, "column_range": col, "structure": selected["structure"], "nonzero": nonzero,
              "finite_count": int(finite.size), "count": int(plane.size), "non_finite": int(plane.size - finite.size),
              "minimum": float(finite.min()) if finite.size else None, "maximum": float(finite.max()) if finite.size else None,
              "mean": float(np.mean(finite / scale) * scale) if scale else 0.0 if finite.size else None,
              "standard_deviation": float(np.std(finite / scale) * scale) if scale else 0.0 if finite.size else None,
              "row_profile": [float(v) if np.isfinite(v) else None for v in row_profile],
              "column_profile": [float(v) if np.isfinite(v) else None for v in col_profile]}
    return result


def _curves(source):
    import numpy as np
    from scipy import sparse
    curves = []
    labels = {"S": "奇异值谱", "singular_values": "奇异值谱", "residual_history": "迭代残差", "eigenvalues": "特征值分布"}
    for info in source.arrays:
        name = source.names[info["id"]]
        if name not in labels or math.prod(info["shape"]) > 4096:
            continue
        with source.array(info) as (array, reverse):
            if reverse:
                slices = [slice(0, n, 1) for n in array.shape]
                _hdf_preflight(array, slices, len(source.data))
                values = np.asarray(array[tuple(slices)]).transpose().reshape(-1)
            elif sparse.issparse(array):
                values = array.toarray().reshape(-1)
            else:
                values = np.asarray(array).reshape(-1)
            if not np.all(np.isfinite(values)):
                continue
            real, imag = _native(values.real), _native(values.imag)
        curves.append({"variable": name, "title": labels[name], "kind": "scatter" if name == "eigenvalues" else "line",
                       "x": real.tolist() if name == "eigenvalues" else list(range(len(real))),
                       "y": imag.tolist() if name == "eigenvalues" else real.tolist()})
        if name in {"S", "singular_values"}:
            scale = float(np.max(np.abs(real)))
            if scale:
                energy = np.cumsum((real / scale)**2)
                curves.append({"variable": name, "title": "已保存奇异值的累计能量比例", "kind": "line", "x": list(range(len(real))), "y": (energy / energy[-1]).tolist()})
    return curves


def main_matrix_preview(data, fmt, kind="tree", options=None):
    try:
        selected = validate_main_matrix_options(kind, {} if options is None else options)
        need(isinstance(data, bytes) and 0 < len(data) <= LIMIT and fmt in {"npy", "npz", "mat", "mtx"})
        source = MatrixSource(data, fmt)
        arrays = source.describe()
        plane = _render(source, selected) if kind == "image" else None
        result = {"contract_version": 2, "type": "matrix-workbench", "reader": "matrix-workbench", "kind": kind,
                  "media_type": "application/json", "selected": selected,
                  "matrix": {"arrays": arrays, "plane": plane, "curves": _curves(source) if kind == "series" else []},
                  "metadata": {"format": fmt, "input_mode": "whole", "source_bytes": len(data), "limits": dict(LIMITS),
                               "matrix_semantics": "selected component; sampled statistics; sparse structure bins nonzero entries"},
                  "warnings": [WARNING], "sampled": plane["sampled"] if plane else False}
        return validate_main_matrix_payload(result, kind=kind, options=selected, fmt=fmt, size=len(data))
    except Exception:
        raise ValueError(ERROR) from None
