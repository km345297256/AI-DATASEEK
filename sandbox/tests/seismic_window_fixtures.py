"""Synthetic format fixtures independent of the production reader."""
import io
import struct


def sac_bytes(values=(1.25, -2.5, 0.0, 4.75), *, endian="<", version=6, interval=0.25, begin=0.125, scale=2.0, idep=7, sample_count=None):
    count = len(values) if sample_count is None else sample_count
    floats, ints = [-12345.0]*70, [-12345]*40
    floats[0], floats[3], floats[5], floats[6] = interval, scale, begin, begin+(count-1)*interval
    ints[:7] = [2024, 60, 12, 30, 10, 250, version]; ints[9], ints[15], ints[16], ints[35] = count, 1, idep, 1
    texts = [b"-12345  "]*24
    for i, value in [(0, b"TEST"), (3, b"00"), (20, b"BHZ"), (21, b"XX")]: texts[i] = value.ljust(8)
    header = struct.pack(endian+"70f40i", *floats, *ints)+b"".join(texts)
    if sample_count is not None: return header, 632+4*count+(176 if version == 7 else 0)
    footer = struct.pack(endian+"22d", interval, begin, begin+(count-1)*interval, *([-12345.0]*19)) if version == 7 else b""
    return header + struct.pack(endian+str(count)+"f", *values) + footer


def mseed_bytes(values=(1, -2, 3, 4), *, endian=">", encoding=3, record_bytes=512, second=0, fraction=0, factor=4, multiplier=1, block100_rate=None, correction=0, activity=0, microseconds=None, station=b"TEST", channel=b"BHZ"):
    codes = {1: "h", 3: "i", 4: "f", 5: "d"}; block_types = [1000] + ([100] if block100_rate is not None else []) + ([1001] if microseconds is not None else [])
    data_offset = 48 + sum(12 if typ == 100 else 8 for typ in block_types)
    data_offset = ((data_offset+7)//8)*8
    header = bytearray(data_offset)
    header[:20] = b"000001D "+station.ljust(5)+b"00"+channel.ljust(3)+b"XX"
    struct.pack_into(endian+"HHBBBBHHhhBBBBiHH", header, 20, 2024, 60, 0, 0, second, 0, fraction, len(values), factor, multiplier, activity, 0, 0, len(block_types), correction, data_offset, 48)
    offset = 48
    for index, typ in enumerate(block_types):
        width = 12 if typ == 100 else 8; next_offset = offset+width if index+1 < len(block_types) else 0
        struct.pack_into(endian+"HH", header, offset, typ, next_offset)
        if typ == 1000: struct.pack_into("4B", header, offset+4, encoding, 1 if endian == ">" else 0, record_bytes.bit_length()-1, 0)
        elif typ == 100: struct.pack_into(endian+"f4B", header, offset+4, block100_rate, 0, 0, 0, 0)
        else: struct.pack_into("BbBB", header, offset+4, 95, microseconds, 0, 0)
        offset += width
    body = struct.pack(endian+str(len(values))+codes[encoding], *values)
    if len(header)+len(body) > record_bytes: raise ValueError("synthetic record too small")
    return bytes(header)+body+b"\0"*(record_bytes-len(header)-len(body))


def official_mseed(values, encoding="STEIM2", endian=">", *, rate=20, record_bytes=4096):
    import numpy as np
    import obspy
    dtype = "float64" if encoding == "FLOAT64" else "float32" if encoding == "FLOAT32" else "int16" if encoding == "INT16" else "int32"
    trace = obspy.Trace(np.array(values, dtype=dtype), header={"station": "TEST", "network": "XX", "location": "00", "channel": "BHZ", "sampling_rate": rate, "starttime": obspy.UTCDateTime("2024-02-29T00:00:00.123456")})
    output = io.BytesIO(); trace.write(output, format="MSEED", encoding=encoding, reclen=record_bytes, byteorder=endian)
    return output.getvalue()
