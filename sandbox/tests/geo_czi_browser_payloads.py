"""Regenerate browser fixture replies from real readers and Zeiss synthetic CZI.

Run only in a test environment with the pinned CZI dependency. Does not read
dataset files; all source pixels and text are created in an owned temp directory.
"""
import hashlib
import json
import tempfile
from pathlib import Path

from app.services.czi_reader import czi_preview
from app.services.geo_format_readers import geoformat_preview


def generate():
    from pylibCZIrw import czi
    import numpy as np
    text = b"ncols 4 nrows 3 xllcorner 10 yllcorner 20 cellsize 1 nodata_value -9999\n0 1 2 3 4 -9999 6 7 8 9 10 11"
    grd = b"DSAA\n4 3\n10 13\n20 22\n0 11\n0 1 2 3 4 1.70141e38 6 7 8 9 10 11"
    kml = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Synthetic point</name><Point><coordinates>10,20</coordinates></Point></Placemark><Placemark><LineString><coordinates>10,20 11,21 12,20</coordinates></LineString></Placemark><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>12,20 13,20 13,21 12,20</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>'
    with tempfile.TemporaryDirectory(prefix="synthetic-czi-test-") as directory:
        path = Path(directory) / "synthetic.czi"
        with czi.create_czi(str(path)) as writer:
            for channel in range(2):
                for z in range(2):
                    pixels = np.arange(48, dtype=np.uint16).reshape(6, 8, 1) + channel * 100 + z * 1000
                    writer.write(pixels, plane={"C": channel, "Z": z, "T": 0}, scene=0)
        data = path.read_bytes()
        return {"provenance": {"generator": "sandbox/tests/geo_czi_browser_payloads.py", "czi_sha256": hashlib.sha256(data).hexdigest(), "czi_bytes": len(data), "source_pixels": "uint16 arange(48).reshape(6,8,1)+C*100+Z*1000"},
                "asc": geoformat_preview(text, "asc", {}), "asc_wgs84": geoformat_preview(text, "asc", {"crs": "EPSG:4326"}),
                "grd": geoformat_preview(grd, "grd", {}), "kml": geoformat_preview(kml, "kml", {}),
                "czi_tree": czi_preview(data, "tree", {}),
                "czi_image": czi_preview(data, "image", {"indices": [1, 1, 0], "roi": [2, 1, 3, 2]})}


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, allow_nan=False, separators=(",", ":")))
