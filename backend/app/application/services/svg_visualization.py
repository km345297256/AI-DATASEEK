"""Validate the isolated renderer's PNG envelope; never parse SVG on the API host.

Original SVG remains an attachment-only download. Only bounded, structurally
checked PNG bytes cross the preview boundary, so scripts/external SVG resources
cannot reach an inline document or image URL through a forged worker payload.
"""
import base64
import json
import struct
import zlib


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected PNG preview")
    offset, dimensions, saw_data, saw_end = 8, None, False, False
    compressed = bytearray()
    channels = 0
    while offset < len(data):
        if len(data) - offset < 12 or saw_end:
            raise ValueError("Incomplete PNG preview")
        length = int.from_bytes(data[offset:offset + 4], "big")
        name = data[offset + 4:offset + 8]
        if length > len(data) - offset - 12 or name not in {b"IHDR", b"pHYs", b"IDAT", b"IEND"}:
            raise ValueError("Unsupported PNG preview chunk")
        body = data[offset + 8:offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length:offset + 12 + length], "big")
        if zlib.crc32(name + body) & 0xffffffff != crc:
            raise ValueError("Corrupt PNG preview")
        if name == b"IHDR":
            if dimensions is not None or offset != 8 or length != 13:
                raise ValueError("Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if not (1 <= width <= 2048 and 1 <= height <= 2048 and depth == 8 and color in {2, 6}
                    and compression == filtering == interlace == 0):
                raise ValueError("PNG preview dimensions or encoding exceeds limits")
            dimensions = (width, height)
            channels = 4 if color == 6 else 3
        elif dimensions is None:
            raise ValueError("PNG header missing")
        elif name == b"pHYs":
            if length != 9 or saw_data:
                raise ValueError("Invalid PNG resolution")
        elif name == b"IDAT":
            if not length:
                raise ValueError("Empty PNG data")
            saw_data = True
            compressed.extend(body)
        elif name == b"IEND":
            if length or not saw_data:
                raise ValueError("Incomplete PNG preview")
            saw_end = True
        offset += 12 + length
    if not dimensions or not saw_end:
        raise ValueError("Incomplete PNG preview")
    width, height = dimensions
    row_bytes = width * channels + 1
    expected = row_bytes * height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
    except zlib.error:
        raise ValueError("Corrupt PNG pixels") from None
    if (len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or any(pixels[index] > 4 for index in range(0, expected, row_bytes))):
        raise ValueError("Invalid PNG pixel stream")
    return dimensions


def validate_svg_preview_payload(payload: object, *, kind: str, limit: int) -> dict:
    fields = {"contract_version", "type", "reader", "kind", "media_type", "data_base64",
              "metadata", "warnings", "sampled"}
    if (not isinstance(payload, dict) or set(payload) != fields or kind != "image"
            or type(payload["contract_version"]) is not int or payload["contract_version"] != 2
            or payload["type"] != "svg" or payload["reader"] != "svg" or payload["kind"] != "image"
            or payload["media_type"] != "image/png" or payload["sampled"] is not False
            or payload["warnings"] != []):
        raise ValueError("Invalid SVG preview envelope")
    metadata = payload["metadata"]
    if (not isinstance(metadata, dict) or set(metadata) != {"source_format", "width", "height", "rasterized"}
            or metadata["source_format"] != "SVG" or metadata["rasterized"] is not True
            or any(type(metadata[key]) is not int or not 1 <= metadata[key] <= 2048 for key in ("width", "height"))):
        raise ValueError("Invalid SVG preview metadata")
    encoded = payload["data_base64"]
    if not isinstance(encoded, str) or not encoded or len(encoded) > min(limit, 8 * 1024 * 1024):
        raise ValueError("SVG preview exceeds output limit")
    data = base64.b64decode(encoded, validate=True)
    if _png_dimensions(data) != (metadata["width"], metadata["height"]):
        raise ValueError("SVG preview dimensions do not match")
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > min(limit, 8 * 1024 * 1024):
        raise ValueError("SVG preview exceeds output limit")
    return payload
