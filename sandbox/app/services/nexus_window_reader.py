"""NXdata semantic preview over the approved HDF5 range VFD.

Modern group@signal / group@axes; fixed-size, bounded attributes only.
One default signal per group, 1D or 2D, numeric axis centres and standard
FIELDNAME_errors. No vlen attributes, external/soft links, VDS or plugins.
https://manual.nexusformat.org/classes/base_classes/NXdata.html
"""
from __future__ import annotations

import hashlib
import math
import re

from .array_window_reader import RangeFile, _describe, _slice_plan, _verify_chunks
from .nexus_window_payload import (
    FORMATS, MAX_ATTRIBUTE_BYTES, MAX_CHUNK_BYTES, MAX_DECODED_BYTES, MAX_ELEMENTS,
    MAX_SIGNALS, NexusWindowError, VALUE_SEMANTICS, WARNING, fail,
    validate_nexus_window_options, validate_nexus_window_payload,
)


class _Unsupported(ValueError):
    pass


def _unsupported():
    raise _Unsupported()


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 128 or value in {".", ".."} or re.search(r"[/\\\x00-\x1f\x7f]", value):
        _unsupported()
    return value


def _clean(value):
    from .nexus_window_payload import label
    return value if label(value) else "[redacted]"


class _Metadata:
    def __init__(self):
        self.bytes = 0

    def attr(self, obj, name, *, numeric=False, vector=False):
        """No vlen read: a small heap reference can point to unbounded memory."""
        import numpy as np
        if name not in obj.attrs:
            return None
        aid = obj.attrs.get_id(name)
        shape, dtype = aid.shape, aid.dtype
        if shape not in ({(), (1,), (2,)} if vector else {(), (1,)}) or dtype.kind not in ({"i", "u"} if numeric else {"S"}):
            _unsupported()
        if dtype.metadata and not (dtype.kind == "S" and set(dtype.metadata) <= {"h5py_encoding"}):
            _unsupported()
        amount = math.prod(shape or (1,)) * dtype.itemsize
        if not 0 < dtype.itemsize <= 128 or amount > 256 or self.bytes + amount > MAX_ATTRIBUTE_BYTES:
            _unsupported()
        self.bytes += amount
        raw = obj.attrs[name]
        if isinstance(raw, np.ndarray):
            values = raw.reshape(-1).tolist()
        else:
            values = [raw.item() if hasattr(raw, "item") else raw]
        if numeric:
            return values if vector else values[0]
        output = []
        for value in values:
            if not isinstance(value, bytes):
                _unsupported()
            # No hidden text past embedded NUL, no arbitrary replacement decode.
            text = value.decode("utf-8", errors="strict")
            if "\0" in text:
                _unsupported()
            output.append(text)
        return output if vector else output[0]


def _dataset(group, name):
    import h5py
    _name(name)
    if name not in group or group.get(name, getlink=True, getclass=True) is not h5py.HardLink:
        _unsupported()
    item = group[name]
    if (not isinstance(item, h5py.Dataset) or not _describe(item)["selectable"]
        or not re.fullmatch(r"(?:\|[iu]1|[<>][iu][248]|[<>]f[48])", item.dtype.str)):
        _unsupported()
    return item


def _info(dataset):
    return {"dtype": dataset.dtype.str, "chunks": list(dataset.chunks) if dataset.chunks else None}


def _unit(metadata, dataset):
    value = metadata.attr(dataset, "units")
    return _clean(value) if value is not None else None


def _signal(group, path, metadata):
    signal_name = metadata.attr(group, "signal")
    if signal_name is None:
        _unsupported()  # Do not guess an old per-dataset signal=1 convention.
    signal = _dataset(group, _name(signal_name))
    if not 1 <= signal.ndim <= 2:
        _unsupported()
    axes_names = metadata.attr(group, "axes", vector=True)
    if "axes" in signal.attrs:
        _unsupported()  # Mixed legacy axes can contradict the modern declaration.
    if axes_names is None:
        axes_names = ["."] * signal.ndim
    if len(axes_names) != signal.ndim or len([a for a in axes_names if a != "."]) != len(set(a for a in axes_names if a != ".")):
        _unsupported()
    axis_datasets, axes = [], []
    for dimension, name in enumerate(axes_names):
        if name == ".":
            axis_datasets.append(None)
            axes.append({"label": f"index_{dimension}", "unit": None, "source": "index", "dtype": None, "chunks": None})
            continue
        coordinate = _dataset(group, _name(name))
        if coordinate.shape != (signal.shape[dimension],):
            _unsupported()  # Bin edges and multidimensional coordinates need a distinct adapter.
        indices = metadata.attr(group, name + "_indices", numeric=True, vector=True)
        if indices is not None and indices != [dimension]:
            _unsupported()
        axis_datasets.append(coordinate)
        axes.append({"label": _clean(name), "unit": _unit(metadata, coordinate), "source": "dataset", **_info(coordinate)})
    # Current NXdata uses FIELDNAME_errors, not a model-generated uncertainty.
    error_name = signal_name + "_errors"
    errors = _dataset(group, error_name) if error_name in group else None
    if errors is not None and errors.shape != signal.shape:
        _unsupported()
    if "uncertainties" in signal.attrs or "errors" in group and error_name != "errors":
        _unsupported()  # Ambiguous/deprecated error declarations are not silently ignored.
    if errors is not None:
        error_unit, signal_unit = metadata.attr(errors, "units"), metadata.attr(signal, "units")
        if error_unit is not None and error_unit != signal_unit:
            _unsupported()
    choice = {"id": "n-" + hashlib.sha256(path.encode()).hexdigest()[:32], "label": _clean(path.rsplit("/", 1)[-1]),
              "signal": _clean(signal_name), "shape": list(signal.shape), **_info(signal), "unit": _unit(metadata, signal),
              "axes": axes, "errors": _info(errors) if errors is not None else None}
    return choice, (signal, axis_datasets, errors)


def _catalog(handle, metadata):
    import h5py
    seen = {h5py.h5o.get_info(handle.id).addr}
    choices, objects = [], {}
    count = skipped = 0
    truncated = False

    def visit(group, prefix, depth):
        nonlocal count, skipped, truncated
        n = group.id.get_num_objs()
        for index in range(min(n, 128)):
            if count >= 128:
                truncated = True
                return
            count += 1
            raw = group.id.get_objname_by_idx(index)
            if len(raw) > 1024:
                truncated = True
                continue
            name = raw.decode("utf-8", errors="strict")
            # Never instantiate a link target or return its filename to the host.
            if group.get(name, getlink=True, getclass=True) is not h5py.HardLink:
                fail()
            obj = group[name]
            if not isinstance(obj, h5py.Group):
                continue
            address = h5py.h5o.get_info(obj.id).addr
            if address in seen:
                continue
            seen.add(address)
            path = prefix + "/" + name
            try:
                nxclass = metadata.attr(obj, "NX_class")
                if nxclass == "NXdata":
                    if len(choices) >= MAX_SIGNALS:
                        truncated = True
                    else:
                        choice, datasets = _signal(obj, path, metadata)
                        choices.append(choice)
                        objects[choice["id"]] = datasets
            except _Unsupported:
                skipped += 1
            if depth < 7:
                visit(obj, path, depth + 1)
            elif obj.id.get_num_objs():
                truncated = True
        if n > 128:
            truncated = True

    visit(handle, "", 0)
    return choices, objects, skipped, truncated


def _read(dataset, selection, source_size):
    import numpy as np
    index, _, chunks, decoded = _slice_plan(dataset, {"selection": selection})
    _verify_chunks(dataset, selection, source_size)
    values = dataset[index]
    if values.size > MAX_ELEMENTS or values.dtype.kind not in {"i", "u", "f"}:
        fail()
    if values.dtype.kind in {"i", "u"} and (np.any(values > 2**53 - 1) or np.any(values < -(2**53 - 1))):
        fail()
    return [v if math.isfinite(v) else None for v in values.reshape(-1).tolist()], chunks, decoded


def nexus_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    options = validate_nexus_window_options(kind, {} if options is None else options)
    if not isinstance(fmt, str) or fmt not in FORMATS:
        fail()
    source = None
    try:
        source = RangeFile(read_range, size, limits)
        import h5py
        while h5py.h5pl.size():
            h5py.h5pl.remove(0)
        found = False
        for offset in [0] + [2**i for i in range(9, 21)]:
            if offset + 8 > size:
                break
            source.seek(offset)
            if source.read(8) == b"\x89HDF\r\n\x1a\n":
                found = True
                break
        if not found:
            fail()
        source.seek(0)
        attrs = _Metadata()
        with h5py.File(source, "r", driver="fileobj", rdcc_nbytes=MAX_CHUNK_BYTES, rdcc_nslots=257, rdcc_w0=1) as handle:
            choices, datasets, skipped, truncated = _catalog(handle, attrs)
            output = {"contract_version": 2, "type": "nexus-window", "reader": "nexus-window", "kind": kind, "media_type": "application/json",
                      "choices": {"signals": choices}, "selected": options, "warnings": [WARNING], "sampled": False}
            chunks = decoded = 0
            all_values = []
            if kind == "tree":
                output["tree"] = [{"path": "/" + item["id"], "node_type": "array", "attributes": {"label": item["label"]}} for item in choices]
            else:
                choice = next((v for v in choices if v["id"] == options["nxdata"]), None)
                if choice is None or len(choice["shape"]) != len(options["selection"]):
                    fail()
                signal, coordinates, errors = datasets[choice["id"]]
                parts = options["selection"]
                plans = [(signal, parts)] + [(axis, [part]) for axis, part in zip(coordinates, parts) if axis is not None]
                if errors is not None:
                    plans.append((errors, parts))
                indices = [list(range(p["start"], p["stop"], p["step"])) for p in parts]
                element_count = math.prod(map(len, indices)) * (2 if errors is not None else 1) + sum(map(len, indices))
                if element_count > MAX_ELEMENTS:
                    fail()
                # Preflight the aggregate of signal + coordinates + uncertainty
                # before the first pixel read, not separate per-dataset budgets.
                for dataset, selection in plans:
                    _, _, a, b = _slice_plan(dataset, {"selection": selection})
                    chunks += a
                    decoded += b
                if chunks > 128 or decoded > MAX_DECODED_BYTES:
                    fail()
                values, _, _ = _read(signal, parts, size)
                all_values.extend(values)
                output["array"] = {"shape": [len(v) for v in indices], "dimensions": [a["label"] for a in choice["axes"]], "values": values}
                output["axes"] = []
                for dimension, (axis, desc, part) in enumerate(zip(coordinates, choice["axes"], parts)):
                    values = indices[dimension] if axis is None else _read(axis, [part], size)[0]
                    if any(v is None for v in values) or len(values) > 1 and not (all(a < b for a, b in zip(values, values[1:])) or all(a > b for a, b in zip(values, values[1:]))):
                        fail()
                    all_values.extend(values)
                    output["axes"].append({"dimension": dimension, "indices": indices[dimension], "values": values, **{k: desc[k] for k in ("label", "unit", "source")}})
                output["errors"] = _read(errors, parts, size)[0] if errors is not None else None
                if output["errors"] is not None:
                    if any(v is not None and v < 0 for v in output["errors"]):
                        fail()
                    all_values.extend(output["errors"])
            output["metadata"] = {"format": fmt, "container": "HDF5", "standard": "NXdata", "input_mode": "window", "value_semantics": VALUE_SEMANTICS,
                "source_bytes": size, "read_bytes": 0, "read_requests": 0, "catalog_truncated": truncated, "skipped_nxdata": skipped,
                "attribute_bytes": attrs.bytes, "chunks_touched": chunks, "decoded_chunk_bytes": decoded,
                "nonfinite_values": sum(v is None for v in all_values), "output_values": len(all_values)}
        output["metadata"].update(read_bytes=source.read_bytes, read_requests=source.read_requests)
        return validate_nexus_window_payload(output, kind=kind, options=options, fmt=fmt, source_bytes=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
    except NexusWindowError:
        raise
    except Exception:
        fail()
    finally:
        if source is not None:
            source.close()
