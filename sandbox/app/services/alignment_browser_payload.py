"""Data-only SAM/BAM/CRAM workbench contract. No native imports or file IO."""
from __future__ import annotations

import json
import math
import re

READER = "alignment-browser"
MAX_INPUT = 64 * 1024**2
MAX_OUTPUT = 4 * 1024**2
ERROR = "比对数据不符合受限读取、区域选择或输出预算。"
WARNING = "坐标为 0 基半开区间；覆盖度是已扫描记录的 M/=/X 碱基数除以分箱长度，不过滤重复或次级比对。无索引顺序扫描；达到预算时不是全区域覆盖度。CRAM 不读取外部参考序列。"
LIMITS = {"references": 256, "records": 100000, "region_width": 2000000, "reads": 2000, "bins": 1000, "query_bases": 4000000}


def need(condition):
    if not condition:
        raise ValueError(ERROR)


def integer(value, lo=0, hi=2**31-1):
    return type(value) is int and lo <= value <= hi


def exact(value, names):
    return isinstance(value, dict) and set(value) == set(names.split())


def label(value, maximum=256):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not re.search(r"[\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|[A-Za-z]:\\|https?://|file://)", value))


def safe_label(value, maximum=256):
    return value if label(value, maximum) else "[omitted unsafe label]"


def validate_options(kind, options):
    need(kind in {"tree", "table"} and isinstance(options, dict))
    if kind == "tree":
        need(not options)
    else:
        need(exact(options, "reference start end max_reads bins"))
        need(integer(options["reference"], 0, 255) and integer(options["start"]) and integer(options["end"], 1))
        need(0 < options["end"] - options["start"] <= LIMITS["region_width"])
        need(integer(options["max_reads"], 1, 2000) and integer(options["bins"], 20, 1000))
    return dict(options)


def _span(value, fields="start end", lo=0, hi=2**31-1):
    need(exact(value, fields) and integer(value["start"], lo, hi) and integer(value["end"], value["start"]+1, hi))


def covered_bins(reads, start, end, width):
    """Linear in CIGAR blocks + bins, not reads × bins × CIGAR length."""
    count = math.ceil((end-start)/width); partial = [0]*count; delta = [0]*(count+1)
    for read in reads:
        for block in read["blocks"]:
            lo, hi = max(start,block["start"]), min(end,block["end"])
            if hi <= lo: continue
            first, last = (lo-start)//width, (hi-1-start)//width
            if first == last: partial[first] += hi-lo
            else:
                partial[first] += start+(first+1)*width-lo
                partial[last] += hi-(start+last*width)
                delta[first+1] += 1; delta[last] -= 1
    depth = 0
    for i in range(count):
        depth += delta[i]; partial[i] += depth*(min(end,start+(i+1)*width)-(start+i*width))
    return partial


def validate_payload(payload, *, kind=None, options=None, format=None, source_bytes=None, limit=MAX_OUTPUT):
    try:
        need(isinstance(payload, dict)); k = payload.get("kind")
        selected = validate_options(k, payload.get("selected"))
        need(kind is None or kind == k)
        need(options is None or selected == validate_options(k, options))
        need(exact(payload, "contract_version type reader kind media_type choices selected metadata warnings sampled " + ("tree" if k == "tree" else "alignment")))
        need(type(payload["contract_version"]) is int and payload["contract_version"] == 2)
        need(payload["reader"] == payload["type"] == READER and payload["media_type"] == "application/json")
        need(payload["warnings"] == [WARNING] and type(payload["sampled"]) is bool)
        meta = payload["metadata"]
        need(exact(meta, "format source_bytes input_mode coordinate_system limits"))
        need(meta["format"] in {"sam", "bam", "cram"} and (format is None or meta["format"] == format))
        need(integer(meta["source_bytes"], 1, MAX_INPUT) and (source_bytes is None or meta["source_bytes"] == source_bytes))
        need(meta["input_mode"] == "whole" and meta["coordinate_system"] == "0-based-half-open")
        need(meta["limits"] == LIMITS and all(type(v) is int for v in meta["limits"].values()))
        choices = payload["choices"]
        need(exact(choices, "references sort_order read_groups"))
        refs = choices["references"]
        need(isinstance(refs, list) and 1 <= len(refs) <= 256)
        for index, ref in enumerate(refs):
            need(exact(ref, "id name length") and integer(ref["id"], index, index) and label(ref["name"]) and integer(ref["length"], 1))
        need(len({r["name"] for r in refs}) == len(refs))
        need(choices["sort_order"] in {"unknown", "unsorted", "queryname", "coordinate"} and integer(choices["read_groups"], 0, 10000))
        if k == "tree":
            need(payload["sampled"] is False)
            need(payload["tree"] == [{"path": f"/references/{r['id']}", "node_type": "reference", "attributes": {"label": r["name"], "length": r["length"]}} for r in refs])
        else:
            need(selected["reference"] < len(refs) and selected["end"] <= refs[selected["reference"]]["length"])
            a = payload["alignment"]
            need(exact(a, "reads coverage records_scanned matched_reads scan_complete reads_truncated"))
            need(integer(a["records_scanned"], 0, LIMITS["records"]) and integer(a["matched_reads"], 0, a["records_scanned"]))
            need(type(a["scan_complete"]) is bool and type(a["reads_truncated"]) is bool)
            need(payload["sampled"] == (not a["scan_complete"] or a["reads_truncated"]))
            reads = a["reads"]
            need(isinstance(reads, list) and len(reads) == min(a["matched_reads"], selected["max_reads"]))
            need(a["reads_truncated"] == (a["matched_reads"] > len(reads)))
            for read in reads:
                need(exact(read, "name start end reverse mapq cigar flag paired duplicate secondary supplementary read1 read2 mate_start mate_reference template_length read_group nm md blocks mismatches insertions deletions splices mismatch_available detail_truncated"))
                need(label(read["name"]) and integer(read["start"]) and integer(read["end"], read["start"]+1))
                need(read["end"] > selected["start"] and read["start"] < selected["end"] and read["end"] <= refs[selected["reference"]]["length"])
                need(integer(read["flag"], 0, 65535) and integer(read["mapq"], 0, 255))
                need(isinstance(read["cigar"], str) and len(read["cigar"]) <= 2048 and re.fullmatch(r"(?:[1-9][0-9]{0,8}[MIDNSHP=X])+", read["cigar"]))
                for field, bit in {"reverse": 16, "paired": 1, "duplicate": 1024, "secondary": 256, "supplementary": 2048, "read1": 64, "read2": 128}.items():
                    need(type(read[field]) is bool and read[field] == bool(read["flag"] & bit))
                need(read["mate_start"] is None or integer(read["mate_start"]))
                need(read["mate_reference"] is None or label(read["mate_reference"]))
                need(integer(read["template_length"], -(2**31), 2**31-1))
                need(read["read_group"] is None or label(read["read_group"]))
                need(read["nm"] is None or integer(read["nm"]))
                need(read["md"] is None or isinstance(read["md"], str) and len(read["md"]) <= 2048 and re.fullmatch(r"[0-9ACGTNacgtn^]+", read["md"]))
                need(type(read["mismatch_available"]) is bool and type(read["detail_truncated"]) is bool)
                need(not read["mismatch_available"] or read["md"] is not None)
                cigar_blocks, cigar_insertions, cigar_deletions, cigar_splices = [], [], [], []
                pos = read["start"]
                for n, code in re.findall(r"([1-9][0-9]{0,8})([MIDNSHP=X])", read["cigar"]):
                    n = int(n)
                    if code in "M=X":
                        cigar_blocks.append({"start":pos,"end":pos+n});pos += n
                    elif code == "I": cigar_insertions.append({"position":pos,"length":n})
                    elif code in "DN":
                        (cigar_deletions if code == "D" else cigar_splices).append({"start":pos,"end":pos+n,"length":n});pos += n
                need(pos == read["end"] and cigar_blocks == read["blocks"] and cigar_deletions[:100] == read["deletions"] and cigar_splices[:100] == read["splices"])
                need(isinstance(read["insertions"],list) and [{"position":i.get("position"),"length":i.get("length")} for i in read["insertions"]] == cigar_insertions[:100])
                for field in ("blocks", "deletions", "splices"):
                    need(isinstance(read[field], list) and len(read[field]) <= (2048 if field == "blocks" else 100))
                    for span in read[field]:
                        _span(span, "start end" if field == "blocks" else "start end length", read["start"], read["end"])
                        if field != "blocks": need(integer(span["length"], span["end"]-span["start"], span["end"]-span["start"]))
                need(isinstance(read["insertions"], list) and len(read["insertions"]) <= 100)
                for insertion in read["insertions"]:
                    need(exact(insertion, "position length sequence") and integer(insertion["position"], read["start"], read["end"]) and integer(insertion["length"], 1, 200000))
                    need(isinstance(insertion["sequence"], str) and len(insertion["sequence"]) <= min(insertion["length"], 100) and re.fullmatch(r"[A-Z=]*", insertion["sequence"]))
                need(isinstance(read["mismatches"], list) and len(read["mismatches"]) <= 500 and (read["mismatch_available"] or not read["mismatches"]))
                for mismatch in read["mismatches"]:
                    need(exact(mismatch, "position query reference") and integer(mismatch["position"], read["start"], read["end"]-1) and re.fullmatch(r"[A-Z=]", mismatch["query"]) and re.fullmatch(r"[A-Z=]", mismatch["reference"]))
                    need(any(b["start"] <= mismatch["position"] < b["end"] for b in cigar_blocks))
            coverage = a["coverage"]
            width = math.ceil((selected["end"]-selected["start"])/selected["bins"])
            need(isinstance(coverage, list) and len(coverage) == math.ceil((selected["end"]-selected["start"])/width))
            shown_coverage = covered_bins(reads,selected["start"],selected["end"],width)
            for index, point in enumerate(coverage):
                left = selected["start"]+index*width; right = min(left+width, selected["end"])
                need(exact(point, "start end covered_bases depth") and integer(point["start"], left, left) and integer(point["end"], right, right))
                need(integer(point["covered_bases"], 0, LIMITS["query_bases"]) and type(point["depth"]) in {int, float} and math.isfinite(point["depth"]))
                need(abs(point["depth"]-point["covered_bases"]/(right-left)) < 1e-8)
                need(point["covered_bases"] <= a["matched_reads"] * (right-left))
                visible_covered = shown_coverage[index]
                need(point["covered_bases"] >= visible_covered)
                if not a["reads_truncated"]: need(point["covered_bases"] == visible_covered)
            need(sum(b["covered_bases"] for b in coverage) <= LIMITS["query_bases"])
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) <= limit)
        return payload
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        raise ValueError(ERROR) from None
