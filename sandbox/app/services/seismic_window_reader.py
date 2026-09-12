"""Bounded MiniSEED2 / SAC binary windows, implemented from public formats.

References: https://www.fdsn.org/pdf/SEEDManual_V2.4.pdf
https://ds.iris.edu/files/sac-manual/ and EarthScope/libmseed unpackdata.c.
No local paths, network, instrument response, calibration, trace merge or FFT.
"""
from __future__ import annotations
import calendar
import datetime as dt
from fractions import Fraction
import math
import re
import struct

from app.services.seismic_window_payload import (
    FORMATS, LIMITS, MAX_SOURCE, MAX_TOTAL, VALUE_SEMANTICS, TIME_AXIS, WARNINGS,
    SeismicWindowError, fail, finite, integer, validate_seismic_window_options,
    validate_seismic_window_payload,
)


class RangeSource:
    def __init__(self, read_range, size, limits=None):
        maxima = {"max_read_bytes": 1048576, "max_total_bytes": MAX_TOTAL, "max_reads": 128}
        if not callable(read_range) or not integer(size, MAX_SOURCE, 256): fail()
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(maxima) or any(not integer(v, maxima[k], 1) for k, v in limits.items()): fail()
            maxima = dict(limits)
        self.callback, self.size, self.limits = read_range, size, maxima
        self.read_bytes = self.read_requests = 0
        self.cache = {}

    def read(self, offset, length):
        if not integer(offset, self.size) or not integer(length, self.limits["max_read_bytes"], 1) or offset+length > self.size: fail()
        for (begin, end), data in self.cache.items():
            if begin <= offset and offset+length <= end: return data[offset-begin:offset-begin+length]
        if self.read_bytes+length > self.limits["max_total_bytes"] or self.read_requests >= self.limits["max_reads"]: fail()
        self.read_bytes += length; self.read_requests += 1
        data = self.callback(offset, length)
        if not isinstance(data, bytes) or len(data) != length: fail()
        self.cache[(offset, offset+length)] = data
        return data


def _date(year, day, hour, minute, second, microseconds=0):
    if not (1900 <= year <= 2200 and 1 <= day <= 365+calendar.isleap(year) and 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60): fail()
    return dt.datetime(year, 1, 1, hour, minute, second, tzinfo=dt.timezone.utc) + dt.timedelta(days=day-1, microseconds=microseconds)


def _utc(value):
    if value is None: return None
    if not 1900 <= value.year <= 2200: fail()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _label(parts):
    labels = []
    for data in parts:
        value = data.rstrip(b" \0").decode("ascii", errors="replace")
        labels.append(value if re.fullmatch(r"[A-Za-z0-9_-]{1,8}", value) and value != "-12345" else "?")
    return ".".join(labels)


def _choice(index, label, variant, encoding, endian, samples, interval, timestamp, **kwargs):
    return {"id": index, "label": label, "variant": variant, "encoding": encoding,
        "byte_order": "big" if endian == ">" else "little", "samples": samples, "sample_interval": float(interval),
        "unit": "unknown", "unit_source": "not supplied", "declared_scale": None, "start_time": _utc(timestamp),
        "begin_seconds": 0, "time_adjustment_seconds": 0, "timing_quality": None, "quality_flags": 0,
        "relation": "uncompared", "gap_seconds": None, **kwargs}


def _mseed_header(source, offset, index, record_bytes=None):
    header = source.read(offset, 48)
    if not re.fullmatch(rb"[0-9]{6}[DRQM] ", header[:8]): fail()
    candidates = []
    for endian in (">", "<"):
        year, day = struct.unpack_from(endian+"HH", header, 20)
        data_offset, block_offset = struct.unpack_from(endian+"HH", header, 44)
        if 1900 <= year <= 2200 and 1 <= day <= 365+calendar.isleap(year) and 48 <= block_offset < data_offset <= min(65535, source.size-offset) and (record_bytes is None or data_offset < record_bytes):
            candidates.append((endian, year, day, data_offset, block_offset))
    # Ambiguous byte orders are rejected, not probed by reading possible samples.
    if len(candidates) != 1: fail()
    endian, year, day, data_offset, block_offset = candidates[0]
    hour, minute, second, unused = header[24:28]
    fraction, samples, factor, multiplier = struct.unpack_from(endian+"HHhh", header, 28)
    if unused or fraction > 9999 or samples == 0 or header[36] & 48 or not 1 <= header[39] <= 3: fail()
    timestamp = _date(year, day, hour, minute, second, fraction*100)
    correction = struct.unpack_from(endian+"i", header, 40)[0]
    blocks = {}; block_end = 48
    # Before B1000 establishes a size, the minimum approved record is 256 bytes.
    # Never follow an untrusted header pointer into a later record and only then reject it.
    header_cap = record_bytes or 256
    while block_offset:
        if block_offset < block_end or block_offset+8 > min(data_offset, header_cap) or len(blocks) >= 3: fail()
        block = source.read(offset+block_offset, 8)
        typ, following = struct.unpack_from(endian+"HH", block)
        if typ not in (100, 1000, 1001) or typ in blocks: fail()
        length = 12 if typ == 100 else 8
        if block_offset+length > min(data_offset, header_cap): fail()
        if length > 8: block += source.read(offset+block_offset+8, length-8)
        if typ == 1000:
            code, order, exponent, reserved = block[4:8]
            if code not in (1, 3, 4, 5, 10, 11) or order not in (0, 1) or not 8 <= exponent <= 20 or reserved: fail()
            declared = 1 << exponent
            if record_bytes is not None and record_bytes != declared or offset+declared > source.size or data_offset >= declared or (order == 1) != (endian == ">") or block_offset+length > declared: fail()
            header_cap = declared
        blocks[typ] = block; block_end = block_offset+length; block_offset = following
    if len(blocks) != header[39] or 1000 not in blocks: fail()
    encoding, order, exponent, reserved = blocks[1000][4:8]
    if encoding not in (1, 3, 4, 5, 10, 11) or order not in (0, 1) or not 8 <= exponent <= 20 or reserved: fail()
    declared = 1 << exponent
    if record_bytes is not None and record_bytes != declared: fail()
    if offset+declared > source.size or data_offset >= declared or (order == 1) != (endian == ">"): fail()
    if 100 in blocks:
        rate = struct.unpack_from(endian+"f", blocks[100], 4)[0]
        if blocks[100][8:] != b"\0"*4 or not finite(rate, 1e-6, 1e7): fail()
        rate = Fraction(rate)
    else:
        if factor == 0 or multiplier == 0: fail()
        rate = (Fraction(factor) if factor > 0 else Fraction(-1, factor)) * (Fraction(multiplier) if multiplier > 0 else Fraction(-1, multiplier))
        if not 1e-6 <= rate <= 1e7: fail()
    microseconds = timing = frames = None
    if 1001 in blocks:
        timing, microseconds, unused, frames = struct.unpack_from("BbBB", blocks[1001], 4)
        if timing > 100 and timing != 255 or unused: fail()
        if timing == 255: timing = None
    adjustment_us = (0 if header[36] & 2 else correction*100) + (microseconds or 0)
    timestamp += dt.timedelta(microseconds=adjustment_us)
    fmt, width = {1: ("INT16", 2), 3: ("INT32", 4), 4: ("FLOAT32", 4), 5: ("FLOAT64", 8), 10: ("STEIM1", 4), 11: ("STEIM2", 4)}[encoding]
    if encoding in (10, 11):
        if data_offset % 64 or (declared-data_offset) % 64: fail()
        if frames and frames*64 > declared-data_offset: fail()
        body_length = (frames*64) if frames else declared-data_offset
        # At most 105 differences per frame, minus first-frame integration words.
        if samples > (body_length//64)*105: fail()
    else:
        if data_offset % width or samples*width > declared-data_offset: fail()
        body_length = samples*width
    choice = _choice(index, _label([header[18:20], header[8:13], header[13:15], header[15:18]]), "MiniSEED2", fmt, endian, samples, 1/rate, timestamp,
        time_adjustment_seconds=adjustment_us/1e6, timing_quality=timing, quality_flags=header[38])
    return {"choice": choice, "record_bytes": declared, "data_offset": offset+data_offset, "body_length": body_length, "endian": endian, "width": width,
        "timestamp": timestamp, "interval": 1/rate, "precision_us": 1 if 1001 in blocks else 100}


def _sac_header(source):
    header = source.read(0, 632)
    candidates = [order for order in (">", "<") if struct.unpack_from(order+"i", header, 304)[0] in (6, 7)]
    if len(candidates) != 1: fail()
    endian = candidates[0]; ints = struct.unpack_from(endian+"40i", header, 280)
    version, samples = ints[6], ints[9]
    if ints[15] != 1 or ints[35] != 1 or not integer(samples, 2**31-1, 1) or 632+samples*4+(176 if version == 7 else 0) != source.size: fail()
    floats = struct.unpack_from(endian+"70f", header)
    interval, begin = floats[0], floats[5]
    if version == 7:
        interval, begin, _ = struct.unpack(endian+"3d", source.read(632+samples*4, 24))
    if not finite(interval, 1e-7, 1e6) or not finite(begin) or begin == -12345: fail()
    timestamp = None
    if any(v != -12345 for v in ints[:6]):
        if any(v == -12345 for v in ints[:6]) or not 0 <= ints[5] <= 999: fail()
        timestamp = _date(*ints[:5], microseconds=ints[5]*1000) + dt.timedelta(seconds=begin)
    scale = floats[3]
    if scale == -12345: scale = None
    if scale is not None and not finite(scale): fail()
    if ints[16] not in (-12345, 5, 6, 7, 8, 50): fail()
    strings = [header[440+i*8:448+i*8] for i in range(24)]
    choice = _choice(0, _label([strings[21], strings[0], strings[3], strings[20]]), "SAC"+str(version), "FLOAT32", endian, samples, interval, timestamp,
        begin_seconds=begin, unit={6: "nm", 7: "nm/s", 8: "nm/s^2", 50: "V"}.get(ints[16], "unknown"), unit_source="SAC IDEP", declared_scale=scale)
    return {"choice": choice, "record_bytes": source.size, "data_offset": 632, "body_length": samples*4, "endian": endian, "width": 4,
        "timestamp": timestamp, "interval": Fraction(interval), "precision_us": 1000}


def _steim(data, count, encoding, endian):
    """Bounded integer integration; first inter-record difference is not used."""
    if not data or len(data) % 64 or not integer(count, 65535, 1): fail()
    result = []; seen_difference = False; reverse = None
    for frame_offset in range(0, len(data), 64):
        frame = data[frame_offset:frame_offset+64]; words = struct.unpack(endian+"16I", frame); control = words[0]
        if control >> 30 or frame_offset == 0 and (control >> 26) & 15: fail()
        if frame_offset == 0:
            first, reverse = struct.unpack_from(endian+"2i", frame, 4); result.append(first)
        for word_index in range(3 if frame_offset == 0 else 1, 16):
            code = (control >> (30-2*word_index)) & 3
            if code == 0: continue
            raw = frame[word_index*4:word_index*4+4]; word = words[word_index]
            if code == 1: differences = struct.unpack("4b", raw)
            elif encoding == "STEIM1": differences = struct.unpack(endian+("2h" if code == 2 else "i"), raw)
            else:
                nibble = word >> 30
                pair = ({1: (1, 30), 2: (2, 15), 3: (3, 10)} if code == 2 else {0: (5, 6), 1: (6, 5), 2: (7, 4)}).get(nibble)
                if pair is None: fail()
                n, width = pair; mask = (1 << width)-1
                differences = [((word >> ((n-i-1)*width)) & mask) for i in range(n)]
                differences = [v-(1 << width) if v & (1 << (width-1)) else v for v in differences]
            for difference in differences:
                if not seen_difference: seen_difference = True; continue
                if len(result) < count: result.append(((result[-1]+difference+2**31) % 2**32)-2**31)
        # Validate every control word in the last used frame before returning.
        # This safety subset is stricter than legacy libmseed's early-return path:
        # a reserved tail dnib within the used frame is not accepted as padding.
        if len(result) == count:
            if result[-1] != reverse: fail()
            return result
    fail()


def _relationship(records):
    previous = {}
    for record in records:
        choice = record["choice"]
        # Sanitized/missing channel identifiers cannot prove two records share a channel.
        if "?" in choice["label"]: continue
        before = previous.get(choice["label"])
        if before is not None and record["timestamp"] is not None and before["timestamp"] is not None:
            elapsed = record["timestamp"]-before["timestamp"]
            gap = Fraction(elapsed.days*86400+elapsed.seconds) + Fraction(elapsed.microseconds, 1000000) - before["choice"]["samples"]*before["interval"]
            tolerance = Fraction(max(record["precision_us"], before["precision_us"]), 2000000)
            choice["gap_seconds"] = float(gap)
            choice["relation"] = "rate-change" if record["interval"] != before["interval"] else "continuous" if abs(gap) <= tolerance else "gap" if gap > 0 else "overlap"
        previous[choice["label"]] = record


def seismic_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    selected = validate_seismic_window_options(kind, {} if options is None else options)
    if not isinstance(fmt, str) or fmt not in FORMATS: fail()
    source = RangeSource(read_range, size, limits)
    try:
        first = _sac_header(source) if fmt == "sac" else _mseed_header(source, 0, 0)
        record_bytes = first["record_bytes"]
        if size % record_bytes: fail()
        slots = size // record_bytes
        offset, limit = (selected.get("record_offset", 0), selected.get("record_limit", 16)) if kind == "tree" else (selected["record"], 1)
        if offset >= slots: fail()
        records = [first if i == 0 else _mseed_header(source, i*record_bytes, i, record_bytes) for i in range(offset, min(offset+limit, slots))]
        decoded = decoded_bytes = nonfinite = 0
        if kind == "tree": _relationship(records)
        else:
            record = records[0]; choice = record["choice"]; start, count = selected["start_sample"], selected["sample_count"]
            if start+count > choice["samples"]: fail()
            if choice["encoding"].startswith("STEIM"):
                decoded = choice["samples"]
                values = _steim(source.read(record["data_offset"], record["body_length"]), decoded, choice["encoding"], record["endian"])[start:start+count]
            else:
                decoded = count
                code = {"INT16": "h", "INT32": "i", "FLOAT32": "f", "FLOAT64": "d"}[choice["encoding"]]
                values = list(struct.unpack(record["endian"]+str(count)+code, source.read(record["data_offset"]+start*record["width"], count*record["width"])))
            decoded_bytes = decoded*record["width"]
            for index, value in enumerate(values):
                if not math.isfinite(value): values[index] = None; nonfinite += 1
        metadata = {"format": fmt, "input_mode": "window", "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests,
            "record_bytes": record_bytes, "record_slots": slots, "catalog_offset": offset, "catalog_count": len(records), "catalog_complete": offset == 0 and len(records) == slots,
            "decoded_samples": decoded, "decoded_bytes": decoded_bytes, "nonfinite_values": nonfinite, "value_semantics": VALUE_SEMANTICS, "time_axis": TIME_AXIS, "limits": dict(LIMITS)}
        choices = [record["choice"] for record in records]
        result = {"contract_version": 2, "type": "seismic-window", "reader": "seismic-window", "kind": kind, "media_type": "application/json", "choices": {"records": choices},
            "selected": selected, "metadata": metadata, "warnings": list(WARNINGS), "sampled": kind == "series" and (selected["sample_count"] != choices[0]["samples"] or slots > 1)}
        if kind == "tree": result["tree"] = [{"path": "/record-"+str(r["id"]), "node_type": "record", "attributes": {"label": r["label"], "encoding": r["encoding"]}} for r in choices]
        else: result["series"] = [{"record": choice["id"], "label": choice["label"], "unit": choice["unit"], "x": [(start+i)*choice["sample_interval"] for i in range(count)], "y": values}]
        return validate_seismic_window_payload(result, kind=kind, options=selected, fmt=fmt, source_bytes=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
    except SeismicWindowError: raise
    except Exception: fail()
    finally: source.cache.clear()
