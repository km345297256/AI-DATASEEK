"""Narrow LevelDB v1 table guard: flat index, no filters, no compression.

Checks every block CRC32C/restart/handle and complete contiguous coverage before
allowing the native tool's old-format path (which has no table properties).
This is a physical point-record format, not a database or manifest reader.
"""
from .physical_database_payload import need


def _crc_entry(n):
    for _ in range(8): n = (n >> 1) ^ (0x82f63b78 if n & 1 else 0)
    return n


CRC_TABLE = tuple(_crc_entry(i) for i in range(256))


def crc32c(data):
    n = 0xffffffff
    for byte in data: n = CRC_TABLE[(n ^ byte) & 255] ^ (n >> 8)
    return n ^ 0xffffffff


def varint(data, pos):
    result = 0
    for i in range(10):
        need(pos < len(data))
        byte = data[pos]; pos += 1
        if i == 9: need(byte <= 1)
        result |= (byte & 127) << (7*i)
        if byte < 128:
            need(i == 0 or byte != 0)
            return result, pos
    need(False)


def handle(data, pos=0):
    offset, pos = varint(data, pos)
    size, pos = varint(data, pos)
    return (offset, size), pos


def block(data, descriptor):
    offset, size = descriptor
    need(size <= 1024**2 and 0 <= offset and offset+size+5 <= len(data)-48)
    content = data[offset:offset+size]
    need(data[offset+size] == 0)  # Compression is explicitly unsupported here.
    crc = crc32c(content+b"\0")
    masked = (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff
    need(int.from_bytes(data[offset+size+1:offset+size+5],"little") == masked)
    need(size >= 8)
    count = int.from_bytes(content[-4:],"little")
    need(1 <= count <= 4096 and (count+1)*4 <= size)
    end = size-(count+1)*4
    restarts = [int.from_bytes(content[end+i*4:end+i*4+4],"little") for i in range(count)]
    need(restarts[0] == 0 and restarts == sorted(set(restarts)) and all(x < max(1,end) for x in restarts))
    rows, start, previous, visited, decoded = [], 0, b"", set(), 0
    restart_set = set(restarts)
    encoded_entries = memoryview(content)[:end]
    while start < end:
        position = start
        shared, start = varint(encoded_entries,start)
        unshared, start = varint(encoded_entries,start)
        length, start = varint(encoded_entries,start)
        need(shared <= len(previous) and start+unshared+length <= end)
        decoded += shared+unshared+length
        need(decoded <= 4*1024**2)
        if position in restart_set: need(shared == 0); visited.add(position)
        key = previous[:shared]+content[start:start+unshared]; start += unshared
        value = content[start:start+length]; start += length
        rows.append((key,value)); previous = key
        need(len(rows) <= 1024)
    need(start == end and (visited == restart_set if end else restarts == [0]))
    return rows


def legacy_rows(data):
    need(48 <= len(data) <= 4*1024**2 and data[-8:] == bytes.fromhex("57fb808b247547db"))
    footer = data[-48:-8]
    meta, pos = handle(footer)
    index, pos = handle(footer,pos)
    need(not any(footer[pos:]) and meta[0]+meta[1]+5 == index[0] and index[0]+index[1]+5 == len(data)-48)
    need(block(data,meta) == [])  # No unknown metadata, filters, ranges or timestamps.
    indices = block(data,index)
    rows, expected_offset, previous = [], 0, None
    for _, raw_handle in indices:
        descriptor, end = handle(raw_handle)
        need(end == len(raw_handle) and descriptor[0] == expected_offset)
        for key, value in block(data,descriptor):
            need(len(key) >= 8)
            tag = int.from_bytes(key[-8:],"little"); user_key = key[:-8]
            typ, seq = tag & 255, tag >> 8
            need(typ in {0,1} and (typ != 0 or value == b""))
            order = (user_key, -seq, -typ)
            need(previous is None or previous < order); previous = order
            kb,vb = len(user_key),len(value); km,vm = kb>128,vb>512
            rows.append(["" if km else user_key.hex(),str(kb),str(seq),"deletion" if typ == 0 else "value",
                         "" if vm else value.hex(),str(vb),"both" if km and vm else "key" if km else "value" if vm else "none"])
            need(len(rows) <= 1024)
        expected_offset = descriptor[0]+descriptor[1]+5
    need(expected_offset == meta[0])
    return rows
