"""Restricted GRIB2 preflight + ECMWF ecCodes decoding over a range capability.

Only GDT 3.0 / PDT 4.0 / DRT 5.0, one field per message, no local sections.
We check lengths, counts and packing before any native handle is constructed;
native ecCodes, not a hand-written bit decoder, produces all displayed values.
"""
from __future__ import annotations

import datetime
import math
import struct

from .grib_window_payload import (
    FORMATS, MAX_MESSAGES, MAX_POINTS, MAX_READ, MAX_READS, MAX_SOURCE, MAX_TOTAL,
    SEMANTICS, WARNING, GribWindowError, fail, integer, label, message_offset,
    validate_grib_window_options, validate_grib_window_payload,
)


class _Unsupported(ValueError):
    pass


def require(condition):
    if not condition: raise _Unsupported()


def uint(data):
    return int.from_bytes(data, "big")


def signed(data):
    value, sign = uint(data), 1 << (len(data) * 8 - 1)
    return -(value & (sign - 1)) if value & sign else value


class _Source:
    """Exact ranges only: no read-ahead into section 7 during directory scans."""
    def __init__(self, callback, size, limits):
        if not callable(callback) or not integer(size, MAX_SOURCE, 20): fail()
        maximum = {"max_read_bytes": MAX_READ, "max_total_bytes": MAX_TOTAL, "max_reads": MAX_READS}
        if limits is not None and (not isinstance(limits, dict) or set(limits) != set(maximum)
            or any(not integer(v, maximum[k], 1) for k, v in limits.items())): fail()
        self.callback, self.size, self.limits = callback, size, limits or maximum
        self.read_bytes = self.read_requests = 0

    def read(self, offset, length):
        if (not integer(offset, self.size - 1) or not integer(length, self.limits["max_read_bytes"], 1)
            or offset + length > self.size or self.read_bytes + length > self.limits["max_total_bytes"]
            or self.read_requests >= self.limits["max_reads"]): fail()
        self.read_bytes += length
        self.read_requests += 1
        value = self.callback(offset, length)
        if not isinstance(value, bytes) or len(value) != length: fail()
        return value


def _sections(source, offset):
    header = source.read(offset, 16)
    if header[:4] != b"GRIB" or header[4:6] not in {b"\0\0", b"\xff\xff"} or header[7] != 2: fail()
    size = uint(header[8:16])
    if size < 20 or offset + size > source.size or source.read(offset + size - 4, 4) != b"7777": fail()
    pos, sections, prefix = offset + 16, {}, [header]
    sequence = []
    while pos < offset + size - 4:
        if len(sections) >= 7 or offset + size - 4 - pos < 5: fail()
        raw = source.read(pos, 5)
        length, number = uint(raw[:4]), raw[4]
        if length < 5 or pos + length > offset + size - 4 or number in sections or not 1 <= number <= 7: fail()
        # Only bounded non-data sections may be fetched before format preflight.
        body = source.read(pos + 5, length - 5) if number != 7 and 5 < length <= 4096 else b""
        sections[number] = (pos, length, raw + body)
        sequence.append(number)
        prefix.append(raw + body)
        pos += length
    if sequence not in ([1, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7]) or pos != offset + size - 4: fail()
    return header, size, sections, b"".join(prefix)


def _preflight(header, size, parts):
    require(size <= MAX_READ and 2 not in parts)
    s1, s3, s4, s5, s6 = (parts[n][2] for n in (1, 3, 4, 5, 6))
    require([parts[n][1] for n in (1, 3, 4, 5)] == [21, 72, 34, 21])
    require(1 <= s1[9] < 255 and s1[10] == 0)  # Standard WMO tables, not local parameter definitions.
    require(s3[5] == 0 and s3[10:14] == b"\0\0\0\0" and s3[14] <= 9)
    ni, nj, points = uint(s3[30:34]), uint(s3[34:38]), uint(s3[6:10])
    require(0 < ni <= MAX_POINTS and 0 < nj <= MAX_POINTS and ni * nj == points <= MAX_POINTS)
    require(uint(s3[38:42]) == 0 and uint(s3[42:46]) == 2**32 - 1)  # The standard microdegree convention.
    scan = s3[71]
    require(not scan & 31 and s3[54] & 48 == 48)
    lon, lat = signed(s3[50:54]) / 1e6, signed(s3[46:50]) / 1e6
    last_lon, last_lat = signed(s3[59:63]) / 1e6, signed(s3[55:59]) / 1e6
    dx, dy = uint(s3[63:67]) / 1e6, uint(s3[67:71]) / 1e6
    require(0 < dx <= 360 and 0 < dy <= 180 and -360 <= lon <= 360 and -360 <= last_lon <= 360 and abs(lat) <= 90 and abs(last_lat) <= 90)
    dx *= -1 if scan & 128 else 1
    dy *= 1 if scan & 64 else -1
    require(abs((ni - 1) * dx) < 360 and abs(lat + (nj - 1) * dy - last_lat) <= 1e-6)
    require(abs(((lon + (ni - 1) * dx - last_lon + 180) % 360) - 180) <= 1e-6)
    require(s4[5:9] == b"\0\0\0\0" and s4[28] == 255)  # Instantaneous surface/level, not a layer or ensemble/interval.
    require(s4[9] < 192 and s4[10] < 192 and s4[17] in {0, 1, 2, 10, 11, 12, 13} and signed(s4[18:22]) >= 0)
    reference = datetime.datetime(uint(s1[12:14]), *s1[14:19]).isoformat() + "Z"
    require(uint(s5[9:11]) == 0 and s5[19] <= 32 and s5[20] in {0, 1})
    require(abs(signed(s5[15:17])) <= 100 and abs(signed(s5[17:19])) <= 100 and math.isfinite(struct.unpack(">f", s5[11:15])[0]))
    bitmap_kind = s6[5] if len(s6) >= 6 else -1
    require(bitmap_kind in {0, 255})  # Never reuse bitmap 254 or a predefined external bitmap.
    require(parts[6][1] == (6 + (points + 7) // 8 if bitmap_kind == 0 else 6))
    bitmap = [bool(s6[6 + n // 8] & (128 >> (n % 8))) for n in range(points)] if bitmap_kind == 0 else [True] * points
    if bitmap_kind == 0 and points % 8: require(s6[-1] & ((1 << (8 - points % 8)) - 1) == 0)
    count, bits = uint(s5[5:9]), s5[19]
    require(count == sum(bitmap) and parts[7][1] == 5 + (count * bits + 7) // 8)
    return {"shape": [nj, ni], "first": [lon % 360, lat], "step": [dx, dy], "scanning_mode": scan, "earth_shape": s3[14],
        "bitmap": bitmap_kind == 0, "missing_count": points - count, "packing_bits": bits,
        "discipline": header[6], "parameter_category": s4[9], "parameter_number": s4[10], "reference_time": reference,
        "forecast_time": signed(s4[18:22]), "forecast_unit": s4[17], "surface_type": s4[22],
        "surface_scale": None if s4[23] == 255 else signed(s4[23:24]),
        "surface_value": None if uint(s4[24:28]) == 2**32 - 1 else uint(s4[24:28])}, bitmap


def _native_labels(prefix):
    from .grib_eccodes_runtime import get_eccodes_runtime
    ec = get_eccodes_runtime()
    # Partial handles are an official ecCodes API. We deliberately never ask
    # them for bitmap, coordinates, sizes or values; only three bounded labels.
    handle = ec.codes_new_from_message(prefix, partial=True)
    try:
        output = {name: ec.codes_get(handle, key) for name, key in (("label", "name"), ("short_name", "shortName"), ("unit", "units"))}
        require(all(label(value) for value in output.values()))
        return output
    finally:
        ec.codes_release(handle)


def _decode(message, item, bitmap, roi):
    from .grib_eccodes_runtime import get_eccodes_runtime
    ec = get_eccodes_runtime()
    import numpy as np
    handle = ec.codes_new_from_message(message)
    try:
        ny, nx = item["shape"]
        require(ec.codes_get(handle, "gridType") == "regular_ll" and ec.codes_get(handle, "packingType") == "grid_simple")
        require(all(ec.codes_get_size(handle, name) == nx * ny for name in ("values", "latitudes", "longitudes")))
        raw = ec.codes_get_values(handle)
        latitude = ec.codes_get_array(handle, "latitudes")
        longitude = ec.codes_get_array(handle, "longitudes")
        require(len(raw) == len(latitude) == len(longitude) == nx * ny)
        def plane(values):
            return np.asarray(values).reshape((nx, ny)).T if item["scanning_mode"] & 32 else np.asarray(values).reshape((ny, nx))
        values, lat, lon, present = map(plane, (raw, latitude, longitude, bitmap))
        expected_lat = item["first"][1] + np.arange(ny) * item["step"][1]
        expected_lon = (item["first"][0] + np.arange(nx) * item["step"][0]) % 360
        require(np.allclose(lat, expected_lat[:, None], rtol=0, atol=1e-6) and np.allclose(np.mod(lon, 360), expected_lon[None, :], rtol=0, atol=1e-6))
        x, y, width, height = roi
        require(x + width <= nx and y + height <= ny)
        xvalues, yvalues = expected_lon[x:x + width].tolist(), expected_lat[y:y + height].tolist()
        # Do not sort coordinates or silently bridge the 0/360 seam.
        require(all(math.isclose(b - a, item["step"][0], rel_tol=0, abs_tol=1e-6) for a, b in zip(xvalues, xvalues[1:])))
        output = []
        for value, valid in zip(values[y:y + height, x:x + width].reshape(-1), present[y:y + height, x:x + width].reshape(-1)):
            require(not valid or math.isfinite(value))
            output.append(float(value) if valid else None)
        return {"shape": [height, width], "dimensions": ["latitude", "longitude"], "values": output}, [
            {"name": "latitude", "unit": "degrees_north", "values": yvalues},
            {"name": "longitude", "unit": "degrees_east", "values": xvalues}]
    finally:
        ec.codes_release(handle)


def grib_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    try:
        options = validate_grib_window_options(kind, {} if options is None else options)
        if not isinstance(fmt, str) or fmt not in FORMATS: fail()
        source = _Source(read_range, size, limits)
        offset = options["offset"] if kind == "tree" else message_offset(options["message"])
        page_offset, scanned, skipped, choices, selected_data = offset, 0, 0, [], None
        while offset < size and scanned < (MAX_MESSAGES if kind == "tree" else 1):
            header, length, sections, prefix = _sections(source, offset)
            scanned += 1
            try:
                item, bitmap = _preflight(header, length, sections)
                item = {"id": f"g-{offset:016x}", "byte_length": length, **_native_labels(prefix), **item}
                choices.append(item)
                if kind == "image":
                    x, y, width, height = options["roi"]
                    require(x + width <= item["shape"][1] and y + height <= item["shape"][0])
                    start, body_size = sections[7][0] + 5, sections[7][1] - 5
                    packed = source.read(start, body_size) if body_size else b""
                    count = math.prod(item["shape"]) - item["missing_count"]
                    unused = (-count * item["packing_bits"]) % 8
                    require(not packed or not unused or packed[-1] & ((1 << unused) - 1) == 0)
                    selected_data = _decode(prefix + packed + b"7777", item, bitmap, options["roi"])
            except _Unsupported:
                if kind == "image": fail()
                skipped += 1
            offset += length
        if not scanned: fail()
        output = {"contract_version": 2, "type": "grib-window", "reader": "grib-window", "kind": kind, "media_type": "application/json",
            "choices": {"messages": choices}, "selected": options, "warnings": [WARNING], "sampled": False,
            "metadata": {"format": fmt, "edition": 2, "grid_type": "regular_ll", "packing": "grid_simple", "decoder": "ecCodes", "input_mode": "window", "value_semantics": SEMANTICS,
                "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests, "page_offset": page_offset,
                "next_offset": offset if offset < size else None, "scanned_messages": scanned, "skipped_messages": skipped,
                "decoded_points": 0, "output_values": 0, "missing_values": 0}}
        if kind == "tree": output["tree"] = [{"path": "/" + item["id"], "node_type": "array", "attributes": {"label": item["short_name"]}} for item in choices]
        else:
            if selected_data is None: fail()
            output["array"], output["axes"] = selected_data
            output["metadata"].update(decoded_points=math.prod(choices[0]["shape"]), output_values=len(output["array"]["values"]), missing_values=sum(v is None for v in output["array"]["values"]))
        return validate_grib_window_payload(output, kind=kind, options=options, fmt=fmt, source_bytes=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
    except GribWindowError:
        raise
    except Exception:
        fail()
