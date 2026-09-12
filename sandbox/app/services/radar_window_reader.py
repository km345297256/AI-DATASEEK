"""Restricted ODIM_H5 2.4 polar SCAN windows, no polar-to-Earth projection.

HDF5 metadata read-ahead is counted, but only selected numerical chunks are
decoded. Fixed-size attrs only. Files, links, metadata strings, and filters do
not become application code, host paths, URLs or automatic calibration.
"""
from __future__ import annotations
import itertools
import math
import re
from .array_window_reader import RangeFile, _describe, _slice_plan, _verify_chunks
from .radar_window_payload import (ERROR, FORMATS, LIMITS, RANGE_SEMANTICS, UNITS,
    VALUE_SEMANTICS, WARNINGS, dtype, finite, require, validate_radar_window_options, validate_radar_window_payload)


class Metadata:
    def __init__(self): self.bytes = 0

    def attr(self, obj, name, text=False):
        import numpy as np
        require(name in obj.attrs)
        aid = obj.attrs.get_id(name)
        dt = aid.dtype
        require(aid.shape == () and dt.kind in ({"S"} if text else {"i", "u", "f"})
                and (not dt.metadata or text and set(dt.metadata) <= {"h5py_encoding"})
                and 0 < dt.itemsize <= (64 if text else 8) and (text or dt.itemsize in {1, 2, 4, 8}))
        require(self.bytes+dt.itemsize <= LIMITS["max_attribute_bytes"]); self.bytes += dt.itemsize
        value = obj.attrs[name]
        if isinstance(value, np.generic): value = value.item()
        if text:
            require(type(value) is bytes and b"\0" not in value)
            value = value.decode("ascii", errors="strict")
            require(0 < len(value) <= 64)
        else: require(finite(value))
        return value


class Directory:
    def __init__(self): self.seen = set()

    def child(self, group, name, cls):
        import h5py
        require(name in group and group.get(name, getlink=True, getclass=True) is h5py.HardLink)
        value = group[name]; require(isinstance(value, cls))
        address = h5py.h5o.get_info(value.id).addr
        require(address not in self.seen); self.seen.add(address)
        return value

    def numbered(self, group, prefix, maximum, extra):
        require(group.id.get_num_objs() <= maximum+len(extra))
        found = []
        for i in range(group.id.get_num_objs()):
            raw = group.id.get_objname_by_idx(i); require(len(raw) <= 64)
            name = raw.decode("ascii", errors="strict")
            if name in extra: continue
            match = re.fullmatch(prefix+r"([1-9][0-9]{0,2})", name)
            require(match is not None); found.append(int(match[1]))
        require(1 <= len(found) <= maximum and sorted(found) == list(range(1, len(found)+1)))
        return sorted(found)


def _catalog(handle, meta):
    import h5py
    directory = Directory()
    directory.seen.add(h5py.h5o.get_info(handle.id).addr)
    require(meta.attr(handle, "Conventions", True) == "ODIM_H5/V2_4")
    what = directory.child(handle, "what", h5py.Group)
    obj = meta.attr(what, "object", True); require(obj in {"PVOL", "SCAN"})
    require(meta.attr(what, "version", True) == "H5rad 2.4")
    sweep_ids = directory.numbered(handle, "dataset", 32, {"what", "where", "how"})
    require(obj != "SCAN" or len(sweep_ids) == 1)
    choices, datasets = [], {}
    for ident in sweep_ids:
        group = directory.child(handle, f"dataset{ident}", h5py.Group)
        sweep_what = directory.child(group, "what", h5py.Group)
        require(meta.attr(sweep_what, "product", True) == "SCAN")
        where = directory.child(group, "where", h5py.Group)
        # Reject contradictory sector/RHI geometry rather than inferring a map.
        require(not any(name in where.attrs for name in ("startaz", "stopaz", "az_angle", "azangle", "startel", "stopel")))
        s = {"id": ident, "elevation": meta.attr(where, "elangle"), "nrays": meta.attr(where, "nrays"),
             "nbins": meta.attr(where, "nbins"), "rstart_m": meta.attr(where, "rstart"),
             "rscale_m": meta.attr(where, "rscale"), "a1gate": meta.attr(where, "a1gate"), "quantities": []}
        ids = directory.numbered(group, "data", 16, {"what", "where", "how"})
        quantities = set()
        for data_id in ids:
            data_group = directory.child(group, f"data{data_id}", h5py.Group)
            # A deeper where could override sweep range geometry. Not supported.
            require("where" not in data_group)
            data_what = directory.child(data_group, "what", h5py.Group)
            quantity = meta.attr(data_what, "quantity", True)
            require(quantity in UNITS and quantity not in quantities); quantities.add(quantity)
            data = directory.child(data_group, "data", h5py.Dataset)
            require(data.ndim == 2 and list(data.shape) == [s["nrays"], s["nbins"]]
                    and dtype(data.dtype.str) and _describe(data)["selectable"])
            q = {"id": quantity, "dtype": data.dtype.str, "chunks": list(data.chunks) if data.chunks else None,
                 "gain": meta.attr(data_what, "gain"), "offset": meta.attr(data_what, "offset"),
                 "nodata": meta.attr(data_what, "nodata"), "undetect": meta.attr(data_what, "undetect"), "unit": UNITS[quantity]}
            # ODIM stores these flags as floating attrs, including integer data.
            for flag in ("nodata", "undetect"):
                require(q[flag] == int(q[flag])); q[flag] = int(q[flag])
            require(not any(name in data.attrs for name in ("scale_factor", "add_offset", "missing_value", "_FillValue", "units")))
            if "unit" in data_what.attrs: require(meta.attr(data_what, "unit", True) == UNITS[quantity])
            s["quantities"].append(q); datasets[(ident, quantity)] = data
            require(len(datasets) <= 128)
        choices.append(s)
    return obj, choices, datasets


def _read(dataset, options, size):
    start = (options["ray_start"], options["gate_start"])
    count = (options["ray_count"], options["gate_count"])
    selection = [{"start": a, "stop": a+b, "step": 1} for a, b in zip(start, count)]
    index, _, touched, decoded = _slice_plan(dataset, {"selection": selection})
    if dataset.chunks:
        coordinates = [range(a//c*c, (a+b-1)//c*c+1, c) for a, b, c in zip(start, count, dataset.chunks)]
        for point in itertools.product(*coordinates):
            info = dataset.id.get_chunk_info_by_coord(point)
            require(info.byte_offset is not None and info.size > 0)  # Never invent radar echoes from unallocated fill.
    else:
        offset = dataset.id.get_offset()
        require(type(offset) is int and offset >= 0 and offset+math.prod(dataset.shape)*dataset.dtype.itemsize <= size)
    _verify_chunks(dataset, selection, size)  # Checks actual compressed length before native decode.
    value = dataset[index]
    require(value.shape == count)
    return value.reshape(-1).tolist(), touched, decoded


def radar_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    options = validate_radar_window_options(kind, {} if options is None else options)
    require(type(fmt) is str and fmt in FORMATS)
    source = None
    try:
        source = RangeFile(read_range, size, limits)
        require(source.read(8) == b"\x89HDF\r\n\x1a\n"); source.seek(0)
        import h5py
        while h5py.h5pl.size(): h5py.h5pl.remove(0)
        meta = Metadata()
        with h5py.File(source, "r", driver="fileobj", rdcc_nbytes=4194304, rdcc_nslots=257, rdcc_w0=1) as handle:
            obj, sweeps, datasets = _catalog(handle, meta)
            result = {"contract_version": 2, "type": "radar-window", "reader": "radar-window", "kind": "tree", "media_type": "application/json",
                      "choices": {"sweeps": sweeps}, "selected": {}, "warnings": list(WARNINGS), "sampled": False,
                      "tree": [{"path": f"/sweeps/{s['id']}", "node_type": "array", "shape": [s["nrays"], s["nbins"]]} for s in sweeps],
                      "metadata": {"format": fmt, "odim_version": "ODIM_H5/V2_4", "object": obj, "input_mode": "window",
                          "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests, "attribute_bytes": meta.bytes,
                          "chunks_touched": 0, "decoded_chunk_bytes": 0, "numeric_bytes_read": 0, "value_semantics": VALUE_SEMANTICS,
                          "range_semantics": RANGE_SEMANTICS, "geometry": "ray index versus slant range; not georeferenced", "limits": dict(LIMITS)}}
            validate_radar_window_payload(result)  # Reject invalid calibration/geometry BEFORE touching numerical data.
            if kind == "image":
                require(options["sweep"] <= len(sweeps))
                sweep = sweeps[options["sweep"]-1]
                quantity = next((q for q in sweep["quantities"] if q["id"] == options["quantity"]), None)
                require(quantity is not None)
                values, touched, decoded = _read(datasets[(options["sweep"], options["quantity"])], options, size)
                del result["tree"]; result.update(kind="image", selected=options,
                    sampled=options["ray_count"] < sweep["nrays"] or options["gate_count"] < sweep["nbins"],
                    array={"shape": [options["ray_count"], options["gate_count"]], "dimensions": ["ray", "gate"], "dtype": quantity["dtype"], "values": values},
                    radar={"range_m": [sweep["rstart_m"]+(options["gate_start"]+i+.5)*sweep["rscale_m"] for i in range(options["gate_count"])],
                           "ray_indices": list(range(options["ray_start"], options["ray_start"]+options["ray_count"])),
                           "nodata_count": values.count(quantity["nodata"]), "undetect_count": values.count(quantity["undetect"])})
                result["metadata"].update(chunks_touched=touched, decoded_chunk_bytes=decoded, numeric_bytes_read=len(values)*int(quantity["dtype"][-1]))
        result["metadata"].update(read_bytes=source.read_bytes, read_requests=source.read_requests)
        return validate_radar_window_payload(result, kind=kind, options=options, fmt=fmt, source_bytes=size,
                                            read_bytes=source.read_bytes, read_requests=source.read_requests)
    except Exception: raise ValueError(ERROR) from None
    finally:
        if source is not None: source.close()
