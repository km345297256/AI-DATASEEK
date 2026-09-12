"""Emit real PNG/metadata fixtures from an actual 80 MB official-writer CZI.

Only generated, temporary test data. The reader receives a seek/read capability,
not file bytes. No production datasets or APIs. Run with locked pylibCZIrw 6.1.0.
"""
import importlib.metadata
import json
import tempfile
from pathlib import Path

import numpy as np
from pylibCZIrw import czi
from app.services.czi_window_reader import czi_window_preview


def generate():
    with tempfile.TemporaryDirectory(prefix="czi-range-browser-") as directory:
        path = Path(directory) / "synthetic.czi"
        data = np.arange(1000 * 40000, dtype=np.uint16).reshape(1000, 40000, 1)
        with czi.create_czi(str(path)) as writer:
            writer.write(data, plane={"C": 0, "Z": 0, "T": 0}, scene=0)
        del data
        reads = []

        def read_range(offset, length):
            reads.append([offset, length])
            with path.open("rb") as stream:
                stream.seek(offset)
                return stream.read(length)

        size = path.stat().st_size
        tree = czi_window_preview(read_range, size, "tree", {})
        tree_reads = reads[:]; reads.clear()
        image = czi_window_preview(read_range, size, "image", {"indices": [0, 0, 0], "roi": [1234, 50, 8, 4]})
        return {"provenance": {"generator": "pylibCZIrw " + importlib.metadata.version("pylibCZIrw"),
                "synthetic_only": True, "source_bytes": size, "tree_ranges": tree_reads, "image_ranges": reads},
                "tree": tree, "image": image}


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
