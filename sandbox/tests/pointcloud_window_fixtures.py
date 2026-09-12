"""Original synthetic ASPRS fixed-record LAS fixtures; no network or native SDK."""
import struct

SIZES = {0: 20, 1: 28, 2: 26, 3: 34, 6: 30, 7: 36, 8: 38}


def las_bytes(point_format=7, version="1.4", count=64, *, extra_bytes=0, vlrs=(), evlrs=(),
              coordinates=None, scales=(.25, .5, .125), offsets=(600000., 4500000., 100.)):
    """Return small complete bytes. VLR entries are (user_id, record_id, body)."""
    pf = point_format
    coordinates = coordinates or [(i % 8 * 10, i // 8 * 10, (i % 8 + i // 8) * 2) for i in range(count)]
    count = len(coordinates)
    records = bytearray()
    for index, xyz in enumerate(coordinates):
        p = bytearray(SIZES[pf] + extra_bytes)
        struct.pack_into("<3iH", p, 0, *xyz, index * 997 % 65536)
        p[14] = 0x11 if pf >= 6 else 0x09
        if pf >= 6:
            p[15] = index % 16 | (index % 4) << 4
            p[16] = (index + 64) % 256
            struct.pack_into("<d", p, 22, 12345. + index)
        else:
            p[15] = (index + 2) % 32 | (index % 8) << 5
            if pf in (1, 3): struct.pack_into("<d", p, 20, 12345. + index)
        if pf in (2, 3, 7, 8):
            pos = 20 if pf == 2 else 28 if pf == 3 else 30
            struct.pack_into("<3H", p, pos, index * 1024 % 65536, 65535 - index * 512 % 65536, index * 256 % 65536)
        if pf == 8: struct.pack_into("<H", p, 36, index * 123 % 65536)
        records += p
    header_size = 227 if version == "1.2" else 375
    v = b"".join(struct.pack("<H16sHH32s", 0, user.encode(), identifier, len(body), b"private metadata hidden") + body for user, identifier, body in vlrs)
    e = b"".join(struct.pack("<H16sHQ32s", 0, user.encode(), identifier, len(body), b"private metadata hidden") + body for user, identifier, body in evlrs)
    h = bytearray(header_size)
    h[:4] = b"LASF"
    h[24:26] = bytes((1, int(version[-1])))
    h[26:58] = b"/private/fixture-source".ljust(32, b"\0")
    struct.pack_into("<HII BHI", h, 94, header_size, header_size + len(v), len(vlrs), pf, SIZES[pf] + extra_bytes,
                     count if version == "1.2" else 0)
    struct.pack_into("<3d3d", h, 131, *scales, *offsets)
    for d in range(3):
        lower = min((p[d] for p in coordinates), default=0) * scales[d] + offsets[d]
        upper = max((p[d] for p in coordinates), default=0) * scales[d] + offsets[d]
        struct.pack_into("<2d", h, 179 + d * 16, upper, lower)
    if version == "1.4":
        struct.pack_into("<QI Q", h, 235, header_size + len(v) + len(records) if evlrs else 0, len(evlrs), count)
    return bytes(h) + v + bytes(records) + e


def browser_payloads():
    from app.services.pointcloud_window_reader import pointcloud_window_preview
    samples = {"modern": las_bytes(), "legacy": las_bytes(3, "1.2"),
               "precision": las_bytes(count=3, coordinates=[(2147483000 + i, -2147483000 + i, i) for i in range(3)],
                                      scales=(.001, .002, .003), offsets=(1e12, -1e12, 1e12))}
    output = {}
    for name, data in samples.items():
        read = lambda offset, length: data[offset:offset + length]
        selection = {"point_offset": 8, "point_count": 32} if name != "precision" else {"point_offset": 0, "point_count": 3}
        output[name + "_tree"] = pointcloud_window_preview(read, len(data), "las")
        output[name + "_geometry"] = pointcloud_window_preview(read, len(data), "las", "geometry", selection)
    return output


if __name__ == "__main__":
    import json
    print(json.dumps(browser_payloads(), ensure_ascii=False, allow_nan=False))
