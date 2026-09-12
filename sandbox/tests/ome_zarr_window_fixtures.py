"""Actual Zarr v2 MemoryStore NGFF 0.4 fixtures; no disk or user data."""
from __future__ import annotations

import json

import numpy as np
import zarr
from numcodecs import Blosc, GZip, Zlib


def codec(name):
    return {"none": lambda: None, "zlib": lambda: Zlib(level=1), "gzip": lambda: GZip(level=1),
            "blosc": lambda: Blosc(cname="lz4", clevel=1, shuffle=Blosc.SHUFFLE)}[name]()


def memory_store(*, compression="none", shape=(2, 2, 3, 7, 9), chunks=(1, 1, 2, 3, 4),
                 dtype="<u2", separator=".", multilevel=True, values=None, fill_value=0):
    store = zarr.storage.MemoryStore()
    root = zarr.group(store=store)
    names = {2: ["y", "x"], 3: ["z", "y", "x"], 4: ["c", "z", "y", "x"], 5: ["t", "c", "z", "y", "x"]}[len(shape)]
    axes = [{"name": name, "type": "time" if name == "t" else "channel" if name == "c" else "space", **({"unit": "second"} if name == "t" else {"unit": "micrometer"} if name in "zyx" else {})} for name in names]
    if values is None:
        data = np.arange(np.prod(shape), dtype=np.dtype(dtype)).reshape(shape)
    else:
        data = np.asarray(values, dtype=np.dtype(dtype)).reshape(shape)
    arrays, datasets = [data], []
    if multilevel:
        arrays.append(data[..., ::2, ::2].copy())
    for index, array in enumerate(arrays):
        root.create_dataset(str(index), data=array, chunks=chunks, compressor=codec(compression),
                            dimension_separator=separator, fill_value=fill_value, overwrite=True)
        scale = [2.0 if name == "t" else 1.0 if name == "c" else 3.0 if name == "z" else 0.5 * 2**index for name in names]
        translation = [10.0 if name == "t" else 0.0 if name == "c" else -2.0 if name == "z" else 100.0 if name == "y" else -50.0 for name in names]
        datasets.append({"path": str(index), "coordinateTransformations": [{"type": "scale", "scale": scale}, {"type": "translation", "translation": translation}]})
    root.attrs.update({"multiscales": [{"version": "0.4", "name": "SYNTHETIC PRIVATE /private/no-real-data", "axes": axes, "datasets": datasets}],
                       "omero": {"name": "PRIVATE", "channels": [{"label": "PRIVATE"}]}})
    return store, arrays


def change_json(store, key, mutate):
    value = json.loads(bytes(store[key]))
    mutate(value)
    store[key] = json.dumps(value, separators=(",", ":"), allow_nan=True).encode()


class ObjectSource:
    def __init__(self, store):
        self.data = {key: bytes(store[key]) for key in sorted(store.keys())}
        self.resources, self.reads, self.after_read = [], [], None
        offset = 0
        for key, data in self.data.items():
            self.resources.append({"key": key, "offset": offset, "size": len(data)})
            offset += len(data)
        self.size = offset

    def read(self, offset, length):
        for row in self.resources:
            if row["offset"] <= offset and offset + length <= row["offset"] + row["size"]:
                assert length <= 1024**2
                self.reads.append((row["key"], offset - row["offset"], length))
                result = self.data[row["key"]][offset - row["offset"]:offset - row["offset"] + length]
                if self.after_read:
                    self.after_read(row["key"])
                return result
        raise AssertionError("Cross-object or out-of-scope range")


def browser_payloads():
    from app.services.ome_zarr_reader import ome_zarr_preview
    store, _ = memory_store(compression="zlib")
    source = ObjectSource(store)
    tree = ome_zarr_preview(source.read, source.size, source.resources)
    options = {"level": 1, "indices": [1, 1, 2], "roi": [1, 1, 3, 2]}
    source = ObjectSource(store)
    image = ome_zarr_preview(source.read, source.size, source.resources, "image", options)
    return {"provenance": f"Synthetic actual Zarr {zarr.__version__} MemoryStore NGFF0.4; zlib; TCZYX; no real user data", "tree": tree, "image": image}


if __name__ == "__main__":
    print(json.dumps(browser_payloads(), ensure_ascii=False, indent=2, allow_nan=False))
