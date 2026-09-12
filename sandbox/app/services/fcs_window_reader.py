"""Read-only, event-interleaved FCS 3.0/3.1 safe subset. Standard library only.

ISAC normative reference and whitews/FlowIO are documented in docs/fcs-window-formats.md.
No file paths, transformations, compensation, bit masking, metadata identity export or full-file fallback.
"""
from __future__ import annotations
import math
import re
import struct
from app.services.fcs_window_payload import (
    FcsWindowError, LIMITS, MAX_SOURCE, MAX_TOTAL, SEMANTICS, READ_SEMANTICS, WARNINGS,
    DECLARATIONS, fail, finite, integer, safe_text, validate_channel,
    validate_fcs_window_options, validate_fcs_window_payload,
)


class Source:
    def __init__(self, read_range, size, limits):
        caps = {"max_read_bytes": 1048576, "max_total_bytes": MAX_TOTAL, "max_reads": 128}
        if not callable(read_range) or not integer(size, MAX_SOURCE, 59): fail()
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(caps) or any(not integer(v, caps[k], 1) for k, v in limits.items()): fail()
            caps = dict(limits)
        self.callback, self.size, self.caps = read_range, size, caps
        self.read_bytes = self.read_requests = 0

    def ensure(self, length, requests=1):
        if self.read_bytes+length > self.caps["max_total_bytes"] or self.read_requests+requests > self.caps["max_reads"]: fail()

    def read(self, offset, length):
        if not integer(offset, self.size) or not integer(length, self.caps["max_read_bytes"], 1) or offset+length > self.size: fail()
        self.ensure(length)
        self.read_bytes += length; self.read_requests += 1
        result = self.callback(offset, length)
        if not isinstance(result, bytes) or len(result) != length: fail()
        return result


def _integer(text, maximum=MAX_SOURCE, minimum=0):
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{1,12}", text): fail()
    result = int(text)
    if not integer(result, maximum, minimum): fail()
    return result


def _float(text, minimum=-1e12, maximum=1e12):
    if not isinstance(text, str) or len(text) > 96 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text): fail()
    value = float(text)
    if not finite(value, minimum, maximum): fail()
    return value


def _offset(field, blank=False):
    if blank and field == b" "*8: return 0
    if not re.fullmatch(rb" *[0-9]{1,8}", field): fail()
    return int(field)


def _pairs(raw, version):
    if not raw or not 1 <= raw[0] <= 126 or raw[-1] != raw[0]: fail()
    delimiter = raw[0]; tokens = []; token = bytearray(); i = 1
    while i < len(raw):
        b = raw[i]
        if b == delimiter:
            if not token: fail()
            if i+1 < len(raw) and raw[i+1] == delimiter:
                token.append(b); i += 2; continue
            tokens.append(bytes(token)); token.clear(); i += 1
            if len(tokens) > 8192: fail()
        else:
            token.append(b); i += 1
        if len(token) > 196608: fail()
    if token or not tokens or len(tokens) % 2: fail()
    result = {}
    for k, v in zip(tokens[::2], tokens[1::2]):
        if not 1 <= len(k) <= 128 or any(c < 32 or c > 126 for c in k): fail()
        key = k.decode("ascii").upper()
        if key in result: fail()
        # FCS3.0's separate UNICODE scheme is not a UTF-8 guess/fallback.
        value = v.decode("utf-8" if version == "3.1" else "ascii", errors="strict")
        result[key] = value
    if "$UNICODE" in result: fail()
    return result


def _directory(source):
    header = source.read(0, 58)
    if header[:6] not in (b"FCS3.0", b"FCS3.1") or header[6:10] != b" "*4: fail()
    version = header[3:6].decode("ascii")
    tb, te, db, de = [_offset(header[i:i+8]) for i in (10, 18, 26, 34)]
    if _offset(header[42:50], True) or _offset(header[50:58], True): fail()
    if not 58 <= tb <= 4096 or not tb < te < min(99999999, source.size) or te-tb+1 > 262144: fail()
    if tb > 58 and source.read(58, tb-58) != b" "*(tb-58): fail()
    text_bytes = te-tb+1
    # If HEADER has a DATA span, reject a TEXT overlap before reading TEXT.
    if (db == 0) != (de == 0) or db and (db <= te or de < db or de >= source.size): fail()
    text = _pairs(source.read(tb, text_bytes), version)
    required = {"$BEGINANALYSIS", "$ENDANALYSIS", "$BEGINSTEXT", "$ENDSTEXT", "$BEGINDATA", "$ENDDATA", "$BYTEORD", "$DATATYPE", "$MODE", "$NEXTDATA", "$PAR", "$TOT"}
    if not required.issubset(text) or any(_integer(text[k]) != 0 for k in ("$BEGINANALYSIS", "$ENDANALYSIS", "$BEGINSTEXT", "$ENDSTEXT", "$NEXTDATA")): fail()
    if text["$MODE"] != "L" or text["$DATATYPE"] not in ("F", "D", "I") or text["$BYTEORD"] not in ("1,2,3,4", "4,3,2,1"): fail()
    begin, end = _integer(text["$BEGINDATA"]), _integer(text["$ENDDATA"])
    if begin <= te or end < begin or end+1 != source.size or begin-te-1 > 4096: fail()
    if (end <= 99999999 and (db, de) != (begin, end)) or (end > 99999999 and (db, de) != (0, 0)): fail()
    if begin > te+1 and source.read(te+1, begin-te-1) not in (b" "*(begin-te-1), b"\0"*(begin-te-1)): fail()
    channels_count, total = _integer(text["$PAR"], 128, 1), _integer(text["$TOT"], MAX_SOURCE, 1)
    datatype = text["$DATATYPE"]; channels = []
    # Recognized per-channel descriptors cannot escape the declared channel count.
    for key in text:
        match = re.fullmatch(r"\$P([0-9]+)(B|R|E|N|S|G|D|CALIBRATION)", key)
        if match and (not re.fullmatch(r"[1-9][0-9]{0,2}", match[1]) or int(match[1]) > channels_count): fail()
    for index in range(channels_count):
        prefix = "$P"+str(index+1)
        if any(prefix+k not in text for k in ("B", "R", "E", "N")): fail()
        e = text[prefix+"E"].split(",")
        if len(e) != 2: fail()
        channel = {"id": index, "name": text[prefix+"N"], "stain": text.get(prefix+"S"), "bits": _integer(text[prefix+"B"], 64, 1),
            "range": _integer(text[prefix+"R"], 2**32, 1) if datatype == "I" else _float(text[prefix+"R"], 1e-300, 1.7976931348623157e308),
            "exponent": [_float(v, 0, 1e12) for v in e], "gain": _float(text[prefix+"G"], 1e-300, 1e12) if prefix+"G" in text else None,
            "calibration": None, "display": None}
        if prefix+"CALIBRATION" in text:
            cal = text[prefix+"CALIBRATION"].split(",", 1)
            if len(cal) != 2: fail()
            channel["calibration"] = {"factor": _float(cal[0], 1e-300, 1e12), "unit": cal[1]}
        if prefix+"D" in text:
            display = text[prefix+"D"].split(",")
            if len(display) != 3: fail()
            channel["display"] = {"scale": display[0], "values": [_float(v) for v in display[1:]]}
        validate_channel(channel, index, datatype); channels.append(channel)
    if len({c["bits"] for c in channels}) != 1 or len({c["name"] for c in channels}) != len(channels): fail()
    width = channels[0]["bits"]//8; stride = width*channels_count
    if total*stride != end-begin+1: fail()
    compensation = {"declarations": [k for k in DECLARATIONS if k in text], "spillover": None}
    if "$SPILLOVER" in text:
        parts = text["$SPILLOVER"].split(","); n = _integer(parts[0], channels_count, 2)
        if len(parts) != 1+n+n*n or len(set(parts[1:1+n])) != n: fail()
        lookup = {c["name"]: c["id"] for c in channels}
        if any(name not in lookup for name in parts[1:1+n]): fail()
        coefficients = [_float(v, -1e6, 1e6) for v in parts[1+n:]]
        compensation["spillover"] = {"channels": [lookup[name] for name in parts[1:1+n]], "matrix": [coefficients[i*n:(i+1)*n] for i in range(n)]}
    timestep = _float(text["$TIMESTEP"], 1e-300, 1e12) if "$TIMESTEP" in text else None
    return {"channels": channels, "datatype": datatype, "version": version, "endian": "<" if text["$BYTEORD"] == "1,2,3,4" else ">",
        "total": total, "stride": stride, "width": width, "begin": begin, "text_bytes": text_bytes, "compensation": compensation, "timestep": timestep}


def fcs_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    selection = validate_fcs_window_options(kind, {} if options is None else options)
    if fmt != "fcs": fail()
    source = Source(read_range, size, limits)
    try:
        info = _directory(source); channels = info["channels"]; traces = []; nonfinite = 0; scanned = decoded = 0; plottable = 0
        if kind == "series":
            ids, offset, count = selection["channels"], selection["event_offset"], selection["event_count"]
            if offset+count > info["total"] or any(c >= len(channels) for c in ids): fail()
            scanned = count*info["stride"]; decoded = count*len(ids)
            rows_per_read = source.caps["max_read_bytes"]//info["stride"]
            if rows_per_read < 1: fail()
            source.ensure(scanned, (count+rows_per_read-1)//rows_per_read)
            traces = [{"channel": c, "label": channels[c]["name"], "x": list(range(offset, offset+count)), "y": []} for c in ids]
            code = {"F": "f", "D": "d"}.get(info["datatype"]) or {1: "B", 2: "H", 4: "I"}[info["width"]]
            unpack = struct.Struct(info["endian"]+code)
            for row_start in range(0, count, rows_per_read):
                rows = min(rows_per_read, count-row_start)
                chunk = source.read(info["begin"]+(offset+row_start)*info["stride"], rows*info["stride"])
                for row in range(rows):
                    for trace in traces:
                        value = unpack.unpack_from(chunk, row*info["stride"]+trace["channel"]*info["width"])[0]
                        if not math.isfinite(value): value = None; nonfinite += 1
                        trace["y"].append(value)
            plottable = sum(all(trace["y"][i] is not None for trace in traces) for i in range(count))
        metadata = {"format": "fcs", "fcs_version": info["version"], "datatype": info["datatype"], "byte_order": "little" if info["endian"] == "<" else "big", "input_mode": "window",
            "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests, "text_bytes": info["text_bytes"], "total_events": info["total"],
            "channel_count": len(channels), "event_bytes": info["stride"], "scanned_event_bytes": scanned, "decoded_values": decoded, "decoded_bytes": decoded*info["width"],
            "nonfinite_values": nonfinite, "plottable_events": plottable, "timestep": info["timestep"], "compensation": info["compensation"],
            "value_semantics": SEMANTICS, "read_semantics": READ_SEMANTICS, "limits": dict(LIMITS)}
        result = {"contract_version": 2, "type": "fcs-window", "reader": "fcs-window", "kind": kind, "media_type": "application/json", "choices": {"channels": channels}, "selected": selection,
            "metadata": metadata, "warnings": list(WARNINGS), "sampled": kind == "series" and selection["event_count"] < info["total"]}
        if kind == "tree": result["tree"] = [{"path": "/channel-"+str(c["id"]), "node_type": "channel", "attributes": {"name": c["name"], "bits": c["bits"]}} for c in channels]
        else: result["series"] = traces
        return validate_fcs_window_payload(result, kind=kind, options=selection, fmt=fmt, source_bytes=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
    except FcsWindowError: raise
    except Exception: fail()
