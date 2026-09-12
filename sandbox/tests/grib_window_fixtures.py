"""Only synthetic ECMWF ecCodes GRIB2 messages; never reads user data."""
import json
from functools import lru_cache
from pathlib import Path
import subprocess
import sys


@lru_cache(maxsize=64)
def message(*, ni=6, nj=4, scanning=0, missing=(4,), longitude=10.0, value_offset=0):
    """Independent upstream-Python oracle, never globally loaded in the reader.

    The child only generates a small synthetic message; it never reads a source
    file. Retaining the upstream implementation here cross-checks our C ABI.
    """
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--oracle", json.dumps({
        "ni": ni, "nj": nj, "scanning": scanning, "missing": missing,
        "longitude": longitude, "value_offset": value_offset})],
        capture_output=True, timeout=15, check=True)
    if not 20 <= len(result.stdout) <= 1048576 or result.stderr:
        raise RuntimeError("Synthetic GRIB oracle failed")
    return result.stdout


def _official_message(*, ni=6, nj=4, scanning=0, missing=(4,), longitude=10.0, value_offset=0):
    import eccodes as ec
    import numpy as np
    handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        positive_i, positive_j = not scanning & 128, bool(scanning & 64)
        first_lat = 47.0 if positive_j else 50.0
        last_lat = first_lat + (nj - 1) * (1 if positive_j else -1)
        first_lon = longitude if positive_i else longitude + ni - 1
        for key, value in {
            "Ni": ni, "Nj": nj, "scanningMode": scanning,
            "latitudeOfFirstGridPointInDegrees": first_lat,
            "latitudeOfLastGridPointInDegrees": last_lat,
            "longitudeOfFirstGridPointInDegrees": first_lon % 360,
            "longitudeOfLastGridPointInDegrees": (first_lon + (ni - 1) * (1 if positive_i else -1)) % 360,
            "iDirectionIncrementInDegrees": 1, "jDirectionIncrementInDegrees": 1,
            "dataDate": 20260910, "dataTime": 0, "forecastTime": 6,
            "shortName": "2t", "packingType": "grid_simple", "bitsPerValue": 16,
            "bitmapPresent": int(bool(missing)), "missingValue": 9999,
        }.items(): ec.codes_set(handle, key, value)
        values = np.arange(ni * nj, dtype="f8") + 280 + value_offset
        for index in missing: values[index] = 9999
        ec.codes_set_values(handle, values)
        return ec.codes_get_message(handle)
    finally:
        ec.codes_release(handle)


def sections(data):
    out, offset = {}, 16
    while offset < len(data) - 4:
        size = int.from_bytes(data[offset:offset + 4], "big")
        out[data[offset + 4]] = (offset, size)
        offset += size
    return out


def preview(data, kind="tree", options=None, fmt="grib2", limits=None):
    from app.services.grib_window_reader import grib_window_preview
    calls = []
    def read(offset, length):
        calls.append((offset, length))
        return data[offset:offset + length]
    result = grib_window_preview(read, len(data), fmt, kind, options, limits)
    assert result["metadata"]["read_bytes"] == sum(n for _, n in calls)
    assert result["metadata"]["read_requests"] == len(calls)
    return result, calls


def browser_payloads():
    data = message() + message(value_offset=20)
    tree, _ = preview(data)
    options = {"message": tree["choices"]["messages"][0]["id"], "roi": [1, 0, 4, 3]}
    image, _ = preview(data, "image", options)
    return {"provenance": "Synthetic ECMWF ecCodes 2.48.2 GRIB2 fixed regular_ll/simple packing messages; no user data", "tree": tree, "image": image}


def browser_paging_payloads():
    data = message() * 10
    first, _ = preview(data)
    second, _ = preview(data, options={"offset": first["metadata"]["next_offset"]})
    return {"first": first, "second": second}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--oracle":
        sys.stdout.buffer.write(_official_message(**json.loads(sys.argv[2])))
        raise SystemExit(0)
    import eccodes as ec
    data = message()
    parts = sections(data)
    prefix = data[:parts[7][0] + 5]
    handle = ec.codes_new_from_message(prefix, partial=True)
    try:
        metadata = {}
        for key in ["shortName", "name", "units", "Ni", "Nj", "packingType", "gridType", "typeOfLevel", "level"]:
            try: metadata[key] = ec.codes_get(handle, key)
            except Exception: metadata[key] = "NOT_FOUND"
        print(json.dumps({"size": len(data), "header_bytes": len(prefix), "sections": parts, "metadata": metadata}))
    finally:
        ec.codes_release(handle)
