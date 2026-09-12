"""Synthetic standards-based fixed-string NXdata in an in-memory HDF5 file."""
import io
import json

import h5py
import numpy as np


def nxdata(handle, name="spectrum", shape=(12,), dtype="<f8", axes=True, errors=True, chunks=True):
    group = handle.create_group("entry/" + name)
    group.attrs["NX_class"] = np.bytes_("NXdata")
    group.attrs["signal"] = np.bytes_("counts")
    chunk_shape = tuple(min(size, 4) for size in shape) if chunks else None
    dataset = group.create_dataset("counts", data=np.arange(np.prod(shape), dtype=dtype).reshape(shape),
                                   chunks=chunk_shape, compression="gzip" if chunks else None)
    dataset.attrs["units"] = np.bytes_("counts")
    if axes:
        names = ["energy"] if len(shape) == 1 else ["y", "x"]
        group.attrs["axes"] = np.asarray(names, dtype="S16")
        for dimension, (axis, size) in enumerate(zip(names, shape)):
            value = group.create_dataset(axis, data=100 + dimension * 100 + np.arange(size) * .5)
            value.attrs["units"] = np.bytes_("eV" if len(shape) == 1 else "mm")
            group.attrs[axis + "_indices"] = np.int32(dimension)
    if errors:
        err = group.create_dataset("counts_errors", data=np.ones(shape), chunks=chunk_shape,
                                   compression="gzip" if chunks else None)
        err.attrs["units"] = np.bytes_("counts")
    return group


def fixture(build=None):
    stream = io.BytesIO()
    with h5py.File(stream, "w") as handle:
        if build:
            build(handle)
        else:
            nxdata(handle)
            nxdata(handle, "detector", (4, 6), "<i2")
    return stream.getvalue()


def preview(data, kind="tree", options=None, fmt="nxs", limits=None):
    from app.services.nexus_window_reader import nexus_window_preview
    reads = []
    def read(offset, length):
        assert 0 <= offset < len(data) and 0 < length <= 1024**2 and offset + length <= len(data)
        reads.append((offset, length))
        return data[offset:offset + length]
    result = nexus_window_preview(read, len(data), fmt, kind, options, limits)
    assert result["metadata"]["read_requests"] == len(reads)
    assert result["metadata"]["read_bytes"] == sum(length for _, length in reads)
    return result, reads


def sl(start, stop, step=1):
    return {"start": start, "stop": stop, "step": step}


def selected(tree, label, selection):
    return {"nxdata": next(v["id"] for v in tree["choices"]["signals"] if v["label"] == label), "selection": selection}


def browser_payloads():
    data = fixture()
    tree, _ = preview(data)
    series, _ = preview(data, "series", selected(tree, "spectrum", [sl(2, 10, 2)]))
    image, _ = preview(data, "image", selected(tree, "detector", [sl(1, 3), sl(2, 5)]))
    return {"provenance": "Synthetic in-memory HDF5 NXdata, modern fixed-size attributes; no user data", "tree": tree, "series": series, "image": image}


if __name__ == "__main__":
    print(json.dumps(browser_payloads(), ensure_ascii=False, allow_nan=False, indent=2))
