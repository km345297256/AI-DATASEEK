"""Original synthetic fixtures using the published EDF/SPE field layouts.

SparseSource never allocates its declared source size. These fixtures contain
no real experiment, patient, credentials or filesystem-backed data.
"""
from __future__ import annotations

import struct


def edf_header(width=4, height=3, *, datatype="UnsignedShort", sample_bytes=2,
               byte_order="LowByteFirst", frame=0, header_size=512, fields=None):
    entries = {
        "HeaderID": "EH:000001:000000:000000", "Image": str(frame),
        "ByteOrder": byte_order, "DataType": datatype,
        "Dim_1": str(width), "Dim_2": str(height), "Size": str(width * height * sample_bytes),
        "EDF_HeaderSize": str(header_size), "Comment": "SYNTHETIC PRIVATE /private/not-a-real-path",
    }
    if fields:
        entries.update(fields)
    text = "{\n" + "".join(f"{key} = {value} ;\n" for key, value in entries.items())
    encoded = text.encode("ascii")
    if len(encoded) + 2 > header_size:
        raise ValueError("Synthetic header too small")
    return encoded + b" " * (header_size - len(encoded) - 2) + b"}\n"


def edf_bytes(width=4, height=3, *, frames=2, dtype="H", datatype="UnsignedShort", order="<", values=None, fields=None):
    output = bytearray()
    for frame in range(frames):
        values_for_frame = values if values is not None else [frame * 100 + i for i in range(width * height)]
        output.extend(edf_header(width, height, datatype=datatype, sample_bytes=struct.calcsize(dtype),
                                 byte_order="LowByteFirst" if order == "<" else "HighByteFirst", frame=frame, fields=fields))
        output.extend(struct.pack(order + dtype * (width * height), *values_for_frame))
    return bytes(output)


def spe_header(width=4, height=3, *, frames=2, datatype=3, version=2.6, fields=None):
    header = bytearray(4100)
    comment = b"SYNTHETIC PRIVATE /private/not-a-real-path"
    header[200:200 + len(comment)] = comment
    # Datatype, x/y output dimensions, frames and version are the core
    # fixed header entries in ImageIO Spec.basic, at their published offsets.
    values = {42: ("H", width), 656: ("H", height), 108: ("h", datatype),
              1446: ("i", frames), 1992: ("f", version), 678: ("Q", 0), 1510: ("h", 0)}
    if fields:
        values.update(fields)
    for offset, (code, value) in values.items():
        struct.pack_into("<" + code, header, offset, value)
    return bytes(header)


def spe_bytes(width=4, height=3, *, frames=2, datatype=3, version=2.6, values=None, fields=None):
    code = {0: "f", 1: "i", 2: "h", 3: "H"}[datatype]
    values = values if values is not None else [frame * 100 + i for frame in range(frames) for i in range(width * height)]
    return spe_header(width, height, frames=frames, datatype=datatype, version=version, fields=fields) + struct.pack("<" + code * len(values), *values)


class MemorySource:
    def __init__(self, data):
        self.data, self.size, self.reads, self.after_read = data, len(data), [], None

    def read(self, offset, length):
        self.reads.append((offset, length))
        data = self.data[offset:offset + length]
        if self.after_read:
            self.after_read(offset, length)
        return data


class SparseSource:
    """Read-only logical zero source with small explicitly supplied segments."""
    def __init__(self, size, segments):
        self.size, self.segments, self.reads = size, segments, []

    def read(self, offset, length):
        assert type(offset) is int and type(length) is int
        assert 0 <= offset < self.size and 0 < length <= 1024**2 and offset + length <= self.size
        self.reads.append((offset, length))
        data = bytearray(length)
        for start, segment in self.segments:
            left, right = max(offset, start), min(offset + length, start + len(segment))
            if left < right:
                data[left - offset:right - offset] = segment[left - start:right - start]
        return bytes(data)
