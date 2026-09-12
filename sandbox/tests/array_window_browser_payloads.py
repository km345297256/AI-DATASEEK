"""Real h5py results for browser smoke, using only a disposable sparse source.

The 1.2 GB file is sparse and never read in full, hashed in full, uploaded or
mounted into a production worker. Each preview uses the real range adapter.
"""
import json
import tempfile
from pathlib import Path

from app.services.array_window_reader import array_window_preview


def generate():
    import h5py
    with tempfile.TemporaryDirectory(prefix="synthetic-array-window-") as directory:
        path = Path(directory) / "synthetic.h5"
        with h5py.File(path, "w") as handle:
            space = h5py.h5s.create_simple((30000, 5000))
            dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            dcpl.set_fill_time(h5py.h5d.FILL_TIME_NEVER)
            dataset = h5py.Dataset(h5py.h5d.create(handle.id, b"temperature", h5py.h5t.NATIVE_DOUBLE, space, dcpl=dcpl))
            dataset[0, :4] = [1.0000000000000002, -9999, 3, 4]
            dataset[1, :4] = [5, float("nan"), 7, 8]
            dataset.attrs["scale_factor"] = 0.1
            dataset.attrs["add_offset"] = 273.15
            dataset.attrs["_FillValue"] = -9999.0
        size = path.stat().st_size
        with path.open("rb") as stream:
            def read(offset, length):
                stream.seek(offset)
                return stream.read(length)
            tree = array_window_preview(read, size, "h5", "tree", {})
            ident = tree["choices"]["variables"][0]["id"]
            series = array_window_preview(read, size, "h5", "series", {"variable": ident, "selection": [0, {"start": 0, "stop": 4, "step": 1}], "decode": "raw"})
            image = array_window_preview(read, size, "h5", "image", {"variable": ident, "selection": [{"start": 0, "stop": 2, "step": 1}, {"start": 0, "stop": 3, "step": 1}], "decode": "raw"})
        return {"provenance": {"generator": "sandbox/tests/array_window_browser_payloads.py", "engine": "h5py " + h5py.__version__, "source_bytes": size, "logical_shape": [30000, 5000], "synthetic_sparse": True}, "tree": tree, "series": series, "image": image}


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, allow_nan=False, separators=(",", ":")))
