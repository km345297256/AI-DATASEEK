"""HDF5/NetCDF4 numeric windows using only an authorized synchronous byte reader.

No filename, URL, filesystem callback, xarray eager load or core driver is used.
HDF5 is closed before its RawIOBase wrapper, as required by h5py fileobj VFD.
References: https://docs.h5py.org/en/stable/high/file.html#python-file-like-objects
https://docs.h5py.org/en/stable/high/group.html#dict-interface-and-links
https://api.h5py.org/h5p.html#h5py.h5p.PropDCID.get_external_count
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import os
import re
import zlib
from collections import OrderedDict

# Must precede the first native HDF5 import in the disposable worker. The host
# also sets this before process start; clear paths below covers reused test VMs.
os.environ["HDF5_PLUGIN_PRELOAD"] = "::"

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 8 * 1024**2
MAX_READS = 128
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_ELEMENTS = 16384
MAX_NODES = 128
MAX_RANK = 8
MAX_DIM = 2**31 - 1
MAX_CHUNK_BYTES = 4 * 1024**2
MAX_ENCODED_CHUNK_BYTES = MAX_CHUNK_BYTES + 65536
MAX_DECODED_BYTES = 16 * 1024**2
MAX_CHUNKS = 128
VALUE_SEMANTICS = "raw storage values; no CF scale/add_offset or finite fill masking"
WARNING = "仅显示显式选择的原始存储值；未应用 CF 缩放、偏移或有限填充值掩膜。非有限值显示为空。"
REASONS = {"", "link", "alias", "depth", "type", "shape", "storage", "filter", "chunk"}
_IDENTIFIER = re.compile(r"v-[0-9a-f]{32}\Z")
_SENSITIVE = re.compile(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)


class ArrayWindowError(ValueError):
    pass


def _fail():
    raise ArrayWindowError("数组窗口、文件结构或读取预算无效；请缩小切片或使用其他已启用预览。")


def _integer(value, maximum=MAX_DIM, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def validate_array_window_options(kind, options):
    if not isinstance(kind, str) or kind not in {"tree", "series", "image"} or not isinstance(options, dict):
        _fail()
    if kind == "tree":
        if options:
            _fail()
        return {}
    if set(options) != {"variable", "selection", "decode"} or options["decode"] != "raw" or not isinstance(options["variable"], str) or not _IDENTIFIER.fullmatch(options["variable"]):
        _fail()
    selection = options["selection"]
    if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_RANK:
        _fail()
    slices, count = 0, 1
    for value in selection:
        if _integer(value):
            continue
        if not isinstance(value, dict) or set(value) != {"start", "stop", "step"} or not all(_integer(v) for v in value.values()) or not 0 <= value["start"] < value["stop"] or value["step"] < 1:
            _fail()
        slices += 1
        count *= (value["stop"] - value["start"] + value["step"] - 1) // value["step"]
    if slices != (1 if kind == "series" else 2) or count > MAX_ELEMENTS:
        _fail()
    return json.loads(json.dumps(options))


class RangeFile(io.RawIOBase):
    """Bounded fileobj VFD. Small metadata reads share a 16 KiB page cache.

    Cache bytes count against the same source budget, including read-ahead.
    Large HDF5 reads split into capability-sized requests, never a full-file
    fallback. Native allocation/decompression additionally has process limits.
    """
    def __init__(self, read_range, size, limits=None):
        super().__init__()
        maxima = {"max_read_bytes": MAX_READ_BYTES, "max_total_bytes": MAX_TOTAL_BYTES, "max_reads": MAX_READS}
        if not callable(read_range) or not _integer(size, MAX_SOURCE_BYTES, 256):
            _fail()
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(maxima) or any(not _integer(v, maxima[k], 1) for k, v in limits.items()):
                _fail()
            maxima = dict(limits)
        self.size, self._read_range, self.limits = size, read_range, maxima
        self.position = self.read_bytes = self.read_requests = 0
        self._pages = OrderedDict()
        self._blocks, self._block_bytes = OrderedDict(), 0
        self._page_size = min(16384, maxima["max_read_bytes"], maxima["max_total_bytes"])

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if type(offset) is not int or whence not in {0, 1, 2}:
            _fail()
        target = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if not 0 <= target <= self.size:
            _fail()
        self.position = target
        return target

    def write(self, _):
        _fail()

    def truncate(self, size=None):
        _fail()

    def _fetch(self, offset, length):
        if (not _integer(offset, self.size) or not _integer(length, self.limits["max_read_bytes"], 1)
            or offset + length > self.size or self.read_requests >= self.limits["max_reads"]
            or self.read_bytes + length > self.limits["max_total_bytes"]):
            _fail()
        self.read_bytes += length
        self.read_requests += 1
        data = self._read_range(offset, length)
        if not isinstance(data, bytes) or len(data) != length:
            _fail()
        return data

    def read(self, length=-1):
        if self.closed or type(length) is not int:
            _fail()
        if length == -1:
            length = self.size - self.position
        if not 0 <= length <= MAX_ENCODED_CHUNK_BYTES:
            _fail()
        length = min(length, self.size - self.position)
        if not length:
            return b""
        start, end = self.position, self.position + length
        # Raw-chunk preflight and HDF5 decoding often request the same compressed
        # bytes. Keep at most one maximal chunk (or several smaller ones) so the
        # safety pass does not routinely double network/storage work.
        for bounds, cached in self._blocks.items():
            if bounds[0] <= start and end <= bounds[1]:
                self._blocks.move_to_end(bounds)
                self.position = end
                return cached[start - bounds[0]:end - bounds[0]]
        origin = start
        output = bytearray()
        while start < end:
            page_start = start // self._page_size * self._page_size
            page = self._pages.get(page_start)
            if page is None and end - start > self._page_size:
                count = min(end - start, self.limits["max_read_bytes"])
                output.extend(self._fetch(start, count))
                start += count
                continue
            if page is None:
                page = self._fetch(page_start, min(self._page_size, self.size - page_start))
                self._pages[page_start] = page
                if len(self._pages) > 64:
                    self._pages.popitem(last=False)
            else:
                self._pages.move_to_end(page_start)
            count = min(end - start, len(page) - (start - page_start))
            output.extend(page[start - page_start:start - page_start + count])
            start += count
        self.position = end
        value = bytes(output)
        if length > self._page_size:
            self._blocks[(origin, end)] = value
            self._block_bytes += len(value)
            while self._block_bytes > MAX_ENCODED_CHUNK_BYTES:
                _, discarded = self._blocks.popitem(last=False)
                self._block_bytes -= len(discarded)
        return value

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def close(self):
        self._pages.clear()
        self._blocks.clear()
        self._block_bytes = 0
        super().close()


def _label(value):
    if not isinstance(value, str) or _SENSITIVE.search(value):
        return "[redacted]"
    return re.sub(r"[<>\x00-\x1f\x7f]", "_", value)[:128]


def _id(path):
    return "v-" + hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()[:32]


def _numeric_dtype(dtype):
    return dtype.kind in {"i", "u", "f", "b"} and dtype.itemsize in {1, 2, 4, 8} and not dtype.metadata


def _describe(dataset):
    """Inspect only metadata before any dataset read; never dereference VDS."""
    import h5py
    props = dataset.id.get_create_plist()
    shape = list(dataset.shape) if dataset.shape is not None else []
    dtype = dataset.dtype
    chunks = list(dataset.chunks) if dataset.chunks else None
    reason = ""
    if props.get_layout() == h5py.h5d.VIRTUAL or props.get_external_count():
        reason = "storage"
    elif not _numeric_dtype(dtype):
        reason = "type"
    elif not 1 <= len(shape) <= MAX_RANK or any(not _integer(v, MAX_DIM, 1) for v in shape) or math.prod(shape) > 2**53 - 1:
        reason = "shape"
    elif props.get_nfilters() > 3 or [props.get_filter(i)[0] for i in range(props.get_nfilters())] not in [[], [2], [1], [3], [2, 1], [2, 3], [1, 3], [2, 1, 3]]:
        reason = "filter"
    elif chunks and math.prod(chunks) * dtype.itemsize > MAX_CHUNK_BYTES:
        reason = "chunk"
    # Unsupported datatype representations may contain arbitrary field names.
    return {"shape": shape[:MAX_RANK] if len(shape) <= MAX_RANK and all(_integer(v) for v in shape) else [],
            "dtype": dtype.str if _numeric_dtype(dtype) else "unsupported", "chunks": chunks if chunks and len(chunks) <= MAX_RANK and all(_integer(v, MAX_DIM, 1) for v in chunks) else None,
            "selectable": not reason, "reason": reason}


def _catalog(handle):
    import h5py
    tree, variables, paths = [], [], {}
    seen = {h5py.h5o.get_info(handle.id).addr}
    truncated = False

    def visit(group, prefix, depth):
        nonlocal truncated
        # get_objname_by_idx avoids materializing Group.keys() for huge groups.
        count = group.id.get_num_objs()
        for index in range(min(count, MAX_NODES)):
            if len(tree) >= MAX_NODES:
                truncated = True
                return
            name_bytes = group.id.get_objname_by_idx(index)
            if len(name_bytes) > 1024:
                truncated = True
                continue
            name = name_bytes.decode("utf-8", errors="strict")
            path = prefix + "/" + name
            ident = _id(path)
            kind, reason = "object", ""
            # Never instantiate ExternalLink (which exposes target paths).
            link_class = group.get(name, getlink=True, getclass=True)
            if link_class is not h5py.HardLink:
                reason = "link"
            else:
                item = group[name]
                address = h5py.h5o.get_info(item.id).addr
                if address in seen:
                    reason = "alias"
                else:
                    seen.add(address)
                    if isinstance(item, h5py.Dataset):
                        kind = "array"
                        description = _describe(item)
                        reason = description["reason"]
                        variables.append({"id": ident, "label": _label(name), **description})
                        if description["selectable"]:
                            paths[ident] = path
                    elif isinstance(item, h5py.Group):
                        if depth >= MAX_RANK:
                            reason, truncated = "depth", True
                    else:
                        reason = "type"
            tree.append({"path": "/" + ident, "node_type": kind, "attributes": {"label": _label(name), "depth": depth, "reason": reason}})
            if not reason and kind == "object" and isinstance(item, h5py.Group):
                visit(item, path, depth + 1)
        if count > MAX_NODES:
            truncated = True

    visit(handle, "", 0)
    return tree, variables, paths, truncated


def _attributes(dataset):
    """Read only tiny fixed-size whitelisted scalar attributes, never vlen."""
    import numpy as np
    output = {}
    for name in ("units", "scale_factor", "add_offset", "_FillValue", "missing_value"):
        if name not in dataset.attrs:
            continue
        aid = dataset.attrs.get_id(name)
        dtype = aid.dtype
        if aid.shape not in {(), (1,)} or dtype.itemsize > 128 or dtype.kind not in {"i", "u", "f", "S"} or dtype.metadata and not (dtype.kind == "S" and set(dtype.metadata) <= {"h5py_encoding"}):
            continue
        value = dataset.attrs[name]
        if isinstance(value, np.ndarray):
            value = value.reshape(-1)[0]
        if dtype.kind == "S" and name == "units":
            output[name] = _label(bytes(value).decode("utf-8", errors="replace"))
        elif dtype.kind in {"i", "u", "f"} and name != "units":
            number = value.item() if hasattr(value, "item") else value
            if math.isfinite(number) and (not isinstance(number, int) or abs(number) <= 2**53 - 1):
                output[name] = number
    return output


def _slice_plan(dataset, options):
    shape, selection = dataset.shape, options["selection"]
    if len(shape) != len(selection):
        _fail()
    index, axes, chunk_counts = [], [], []
    for axis, (size, value) in enumerate(zip(shape, selection)):
        if type(value) is int:
            if value >= size:
                _fail()
            index.append(value)
            chunk_counts.append(1)
        else:
            if value["stop"] > size:
                _fail()
            indices = range(value["start"], value["stop"], value["step"])
            index.append(slice(value["start"], value["stop"], value["step"]))
            axes.append({"dimension": axis, "indices": list(indices)})
            chunk_counts.append(len({v // dataset.chunks[axis] for v in indices}) if dataset.chunks else 1)
    touched = math.prod(chunk_counts) if dataset.chunks else 0
    decoded = touched * math.prod(dataset.chunks) * dataset.dtype.itemsize if dataset.chunks else 0
    if touched > MAX_CHUNKS or decoded > MAX_DECODED_BYTES:
        _fail()
    return tuple(index), axes, touched, decoded


def _verify_chunks(dataset, selection, source_size):
    """Prevent a lying compressed stream from inflating inside native HDF5.

    Dataset shape/chunks only describe the *claimed* decoded length. Check the
    actual selected compressed streams with zlib's max_length before allowing
    native decoding. Only standard shuffle -> deflate -> fletcher32 pipelines
    are approved. Native HDF5 still verifies Fletcher32 and performs byte-order
    and shuffle conversion; this preflight does not invent numerical values.
    All raw chunk reads pass through the same fileobj and byte/count budget.
    """
    if dataset.chunks is None:
        return
    size = math.prod(dataset.chunks) * dataset.dtype.itemsize
    props = dataset.id.get_create_plist()
    filters = [props.get_filter(i)[0] for i in range(props.get_nfilters())]
    coordinates = []
    for axis, selected in enumerate(selection):
        indices = [selected] if type(selected) is int else range(selected["start"], selected["stop"], selected["step"])
        coordinates.append(sorted({index // dataset.chunks[axis] * dataset.chunks[axis] for index in indices}))
    for coordinate in itertools.product(*coordinates):
        info = dataset.id.get_chunk_info_by_coord(coordinate)
        if info.byte_offset is None and info.size == 0:
            continue  # Unallocated chunks use HDF5's bounded numeric fill value.
        if (type(info.byte_offset) is not int or info.byte_offset < 0 or not 0 < info.size <= MAX_ENCODED_CHUNK_BYTES
            or info.byte_offset + info.size > source_size):
            _fail()
        mask, encoded = dataset.id.read_direct_chunk(coordinate)
        if mask < 0 or mask >= 2**len(filters) or len(encoded) != info.size:
            _fail()
        active = [filter_id for index, filter_id in enumerate(filters) if not mask & (1 << index)]
        if 3 in active:
            if len(encoded) < 4:
                _fail()
            encoded = encoded[:-4]  # Native HDF5 subsequently validates checksum.
        if 1 in active:
            inflater = zlib.decompressobj()
            decoded = inflater.decompress(encoded, size + 1)
            if len(decoded) != size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                _fail()
        elif len(encoded) != size:
            _fail()


def array_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    """Private v2 result. Call only inside the isolated window worker."""
    options = validate_array_window_options(kind, {} if options is None else options)
    if not isinstance(fmt, str) or fmt not in {"h5", "hdf5", "hdf", "nc", "nc4", "netcdf", "mat"}:
        _fail()
    source = RangeFile(read_range, size, limits)
    try:
        import h5py
        import numpy as np
        # Builtin codecs remain usable. Dynamic codecs/vol/vfd are never needed.
        while h5py.h5pl.size():
            h5py.h5pl.remove(0)
        signature = False
        for offset in [0] + [2**i for i in range(9, 21)]:
            if offset + 8 > size:
                break
            source.seek(offset)
            if source.read(8) == b"\x89HDF\r\n\x1a\n":
                signature = True
                break
        if not signature:
            _fail()
        source.seek(0)
        with h5py.File(source, "r", driver="fileobj", rdcc_nbytes=MAX_CHUNK_BYTES, rdcc_nslots=257, rdcc_w0=1) as handle:
            tree, variables, paths, truncated = _catalog(handle)
            payload = {"contract_version": 2, "type": "array-window", "reader": "array-window", "kind": kind,
                       "media_type": "application/json", "choices": {"variables": variables}, "selected": options,
                       "warnings": [WARNING], "sampled": False}
            attributes, chunks, decoded, nonfinite = {}, 0, 0, 0
            if kind == "tree":
                payload["tree"] = tree
            else:
                if options["variable"] not in paths:
                    _fail()
                dataset = handle[paths[options["variable"]]]
                if not _describe(dataset)["selectable"]:
                    _fail()
                index, axes, chunks, decoded = _slice_plan(dataset, options)
                _verify_chunks(dataset, options["selection"], size)
                attributes = _attributes(dataset)
                values = dataset[index]
                if values.size > MAX_ELEMENTS or values.ndim != (1 if kind == "series" else 2):
                    _fail()
                if values.dtype.kind in {"i", "u"} and (np.any(values > 2**53 - 1) or np.any(values < -(2**53 - 1))):
                    _fail()  # Never silently lose integer precision in JSON/JS.
                flat = [int(v) if isinstance(v, bool) else v for v in values.reshape(-1).tolist()]
                nonfinite = sum(not math.isfinite(v) for v in flat)
                payload["array"] = {"shape": list(values.shape), "dimensions": ["index_" + str(a["dimension"]) for a in axes],
                                    "values": [v if math.isfinite(v) else None for v in flat]}
                payload["axes"] = axes
            payload["metadata"] = {"format": fmt, "container": "HDF5", "input_mode": "window", "value_semantics": VALUE_SEMANTICS,
                "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests,
                "chunks_touched": chunks, "decoded_chunk_bytes": decoded, "catalog_truncated": truncated,
                "attributes": attributes, "nonfinite_values": nonfinite, "coordinates": "zero-based dimension indices; not geospatial coordinates",
                "limits": {"max_elements": MAX_ELEMENTS, "max_chunk_bytes": MAX_CHUNK_BYTES, "max_decoded_bytes": MAX_DECODED_BYTES, "max_nodes": MAX_NODES}}
        # Closing the HDF5 handle must precede final accounting and source close.
        payload["metadata"]["read_bytes"] = source.read_bytes
        payload["metadata"]["read_requests"] = source.read_requests
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > MAX_OUTPUT_BYTES - 256:
            _fail()
        return payload
    except ArrayWindowError:
        raise
    except Exception:
        _fail()
    finally:
        source.close()
