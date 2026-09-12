"""Inert result schemas for migrated sequence, annotation and BLAST viewers.

No filesystem access, parser dispatch, imports from scientific libraries or URLs.
Coordinates in genome tracks are always zero-based, half-open; sequence and BLAST
positions remain one-based. Schemas are mirrored at the FastAPI trust boundary.
"""
from __future__ import annotations
import json
import math
import re
from decimal import Decimal, InvalidOperation

ERROR = "生物数据格式、明确选择或预览预算无效；请缩小文件或区域并检查格式。"
MAX_INPUT = 16 * 1024 ** 2
MAX_OUTPUT = 2 * 1024 ** 2
FORMATS = {
    "sequence-browser": {"fa", "fasta", "fna", "ffn", "frn", "faa", "fastq", "fq"},
    "genome-tracks": {"vcf", "gff", "gff3", "gtf", "bed", "bedgraph", "wig"},
    "blast-hits": {"blast", "blast6", "m8", "blasttab", "tab"},
}
KINDS = {"sequence-browser": "table", "genome-tracks": "map", "blast-hits": "table"}
WARNINGS = {
    "sequence-browser": "仅有界读取未压缩 FASTA / FASTQ；序列以大写显示，GC/N 是字符占比。FASTQ 质量按用户明确选择的编码换算，不自动猜测；模体为字面匹配。",
    "genome-tracks": "仅绘制当前文件的已存储坐标与信号；内部坐标为 0 基半开区间，界面显示 1 基位置。参考基因组未指定，不获取外部轨道、不插值信号；详情有界截取并移除路径。",
    "blast-hits": "只读取标准 12 列 BLAST tabular，或明确声明附加 qlen 的 13 列。未声明 Query 全长时覆盖度未知，图示范围仅为观测命中；不以命中跨度猜测全长。",
}
LIMITS = {
    "sequence-browser": {"max_records": 256, "max_bases": 8000000, "max_window": 1000, "max_matches": 10000},
    "genome-tracks": {"max_chromosomes": 256, "max_records": 100000, "max_features": 3000},
    "blast-hits": {"max_queries": 256, "max_hits": 20000, "max_page": 500},
}
MAX_COORD = 2147483647
UNSAFE = re.compile(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)
EVALUE = re.compile(r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?\Z")


def need(ok):
    if not ok:
        raise ValueError(ERROR)


def keys(v, names):
    return isinstance(v, dict) and set(v) == set(names.split())


def integer(v, low, high):
    return type(v) is int and low <= v <= high


def number(v, low=-1e12, high=1e12):
    return type(v) in {int, float} and low <= v <= high and math.isfinite(v)


def label(v, maximum=128, empty=False):
    return isinstance(v, str) and (empty or bool(v)) and len(v) <= maximum and not UNSAFE.search(v)


def validate_options(reader, kind, options):
    need(reader in FORMATS and kind in {"tree", KINDS[reader]} and isinstance(options, dict))
    if kind == "tree":
        need(not options)
    elif reader == "sequence-browser":
        need(keys(options, "record start count motif quality_encoding"))
        need(integer(options["record"], 0, 255) and integer(options["start"], 1, 8000000) and integer(options["count"], 1, 1000))
        need(isinstance(options["motif"], str) and re.fullmatch(r"[A-Z*.\-]{0,64}", options["motif"]))
        need(options["quality_encoding"] is None or isinstance(options["quality_encoding"], str) and options["quality_encoding"] in {"phred33", "phred64"})
    elif reader == "genome-tracks":
        need(keys(options, "chromosome start end") and integer(options["chromosome"], 0, 255))
        need(integer(options["start"], 0, MAX_COORD - 1) and integer(options["end"], options["start"] + 1, MAX_COORD))
    else:
        need(keys(options, "query min_identity min_coverage offset count"))
        need(options["query"] is None or integer(options["query"], 0, 255))
        need(number(options["min_identity"], 0, 100) and number(options["min_coverage"], 0, 100))
        need(integer(options["offset"], 0, 20000) and integer(options["count"], 1, 500))
    return dict(options)


def overlap(start, end, left, right):
    return left <= start < right if start == end else start < right and end > left


def _sequence(v, kind, choices, metadata, selected):
    records = choices.get("records")
    need(keys(choices, "records") and isinstance(records, list) and 1 <= len(records) <= 256)
    total = 0
    for i, r in enumerate(records):
        need(keys(r, "id name length gc_count n_count gc_bins qualities") and integer(r["id"], i, i) and label(r["name"]))
        n = r["length"]
        need(integer(n, 1, 8000000) and integer(r["gc_count"], 0, n) and integer(r["n_count"], 0, n - r["gc_count"]) and type(r["qualities"]) is bool)
        need(r["qualities"] == (metadata["format"] in {"fq", "fastq"}))
        bins = r["gc_bins"]
        need(isinstance(bins, list) and len(bins) == min(n, 120) and all(integer(b, 0, n) for b in bins))
        need(sum(bins) == r["gc_count"] and all(b <= (j + 1) * n // len(bins) - j * n // len(bins) for j, b in enumerate(bins)))
        total += n
    need(metadata["records"] == len(records) and metadata["total_units"] == total and total <= 8000000)
    if kind == "tree":
        need(v["tree"] == [{"path": f"/records/{i}", "node_type": "array", "attributes": {"label": r["name"]}} for i, r in enumerate(records)])
        return
    need(selected["record"] < len(records))
    r = records[selected["record"]]
    need(selected["start"] <= r["length"] and (selected["quality_encoding"] in {"phred33", "phred64"} if r["qualities"] else selected["quality_encoding"] is None))
    s = v["sequence"]
    need(keys(s, "bases qualities quality_stats search") and isinstance(s["bases"], str) and re.fullmatch(r"[A-Z*.\-]+", s["bases"]))
    n = min(selected["count"], r["length"] - selected["start"] + 1)
    need(len(s["bases"]) == n)
    if r["qualities"]:
        hi = 93 if selected["quality_encoding"] == "phred33" else 62
        need(isinstance(s["qualities"], list) and len(s["qualities"]) == n and all(integer(q, 0, hi) for q in s["qualities"]))
        qs = s["quality_stats"]
        need(keys(qs, "sum low_count") and integer(qs["sum"], 0, hi * r["length"]) and integer(qs["low_count"], 0, r["length"]))
        if n == r["length"]:
            need(qs["sum"] == sum(s["qualities"]) and qs["low_count"] == sum(q < 20 for q in s["qualities"]))
    else:
        need(s["qualities"] is None and s["quality_stats"] is None)
    search = s["search"]
    need(keys(search, "positions total truncated") and isinstance(search["positions"], list) and len(search["positions"]) <= 10000)
    pos = search["positions"]
    need(all(integer(p, 1, r["length"] - len(selected["motif"]) + 1) for p in pos) and all(a < b for a, b in zip(pos, pos[1:])))
    need(integer(search["total"], len(pos), r["length"]) and len(pos) == min(10000, search["total"]) and type(search["truncated"]) is bool and search["truncated"] == (search["total"] > 10000))
    if not selected["motif"]:
        need(not pos and search["total"] == 0)
    for p in pos:
        a = p - selected["start"]
        if 0 <= a <= n - len(selected["motif"]):
            need(s["bases"][a:a + len(selected["motif"])] == selected["motif"])


def _tracks(v, kind, choices, metadata, selected):
    chromosomes = choices.get("chromosomes")
    need(keys(choices, "chromosomes") and isinstance(chromosomes, list) and 1 <= len(chromosomes) <= 256)
    for i, c in enumerate(chromosomes):
        need(keys(c, "id name start end records") and integer(c["id"], i, i) and label(c["name"]))
        need(integer(c["start"], 0, MAX_COORD) and integer(c["end"], c["start"], MAX_COORD) and integer(c["records"], 1, 100000))
    need(len({c["name"] for c in chromosomes}) == len(chromosomes) and sum(c["records"] for c in chromosomes) == metadata["records"] and metadata["total_units"] == metadata["records"])
    if kind == "tree":
        need(v["tree"] == [{"path": f"/chromosomes/{i}", "node_type": "array", "attributes": {"label": c["name"]}} for i, c in enumerate(chromosomes)])
        return
    need(selected["chromosome"] < len(chromosomes))
    c = chromosomes[selected["chromosome"]]
    records = v["tracks"]
    need(isinstance(records, list) and len(records) <= min(c["records"], 3000))
    ids = set()
    for f in records:
        need(keys(f, "id chromosome start end label track strand value detail") and integer(f["id"], 0, metadata["records"] - 1) and f["id"] not in ids)
        ids.add(f["id"])
        need(integer(f["chromosome"], c["id"], c["id"]) and integer(f["start"], c["start"], c["end"]) and integer(f["end"], f["start"], c["end"]))
        need(overlap(f["start"], f["end"], selected["start"], selected["end"]))
        need(label(f["label"]) and label(f["detail"], 256, True) and f["track"] in {"variant", "annotation", "interval", "signal"} and f["strand"] in {"+", "-", ".", "?"})
        need(f["value"] is None or number(f["value"]))
        expected_track = "signal" if metadata["format"] in {"bedgraph", "wig"} else "variant" if metadata["format"] == "vcf" else "interval" if metadata["format"] == "bed" else "annotation"
        need(f["track"] == expected_track)
        need(f["value"] is not None if f["track"] == "signal" else True)


def _blast(v, kind, choices, metadata, selected):
    queries = choices.get("queries")
    need(keys(choices, "queries") and isinstance(queries, list) and 1 <= len(queries) <= 256)
    for i, q in enumerate(queries):
        need(keys(q, "id name length extent hits") and integer(q["id"], i, i) and label(q["name"]))
        need(integer(q["extent"], 1, MAX_COORD) and (q["length"] is None or integer(q["length"], q["extent"], MAX_COORD)) and integer(q["hits"], 1, 20000))
    need(len({q["name"] for q in queries}) == len(queries) and sum(q["hits"] for q in queries) == metadata["records"] and metadata["records"] == metadata["total_units"] and metadata["records"] <= 20000)
    if kind == "tree":
        need(v["tree"] == [{"path": f"/queries/{i}", "node_type": "array", "attributes": {"label": q["name"]}} for i, q in enumerate(queries)])
        return
    need(selected["query"] is None or selected["query"] < len(queries))
    b = v["hits"]
    need(keys(b, "rows total unknown_coverage") and isinstance(b["rows"], list) and integer(b["total"], 0, metadata["records"]) and integer(b["unknown_coverage"], 0, metadata["records"]))
    need(selected["offset"] <= b["total"] and len(b["rows"]) == min(selected["count"], b["total"] - selected["offset"]))
    ids = set()
    for h in b["rows"]:
        need(keys(h, "id query subject identity alignment_length mismatches gap_opens qstart qend sstart send evalue bitscore coverage") and integer(h["id"], 0, metadata["records"] - 1) and h["id"] not in ids)
        ids.add(h["id"])
        need(integer(h["query"], 0, len(queries) - 1) and (selected["query"] is None or h["query"] == selected["query"]) and label(h["subject"]))
        need(number(h["identity"], selected["min_identity"], 100) and integer(h["alignment_length"], 1, MAX_COORD) and integer(h["mismatches"], 0, h["alignment_length"]) and integer(h["gap_opens"], 0, h["alignment_length"]))
        q = queries[h["query"]]
        need(all(integer(h[k], 1, q["extent"] if k.startswith("q") else MAX_COORD) for k in ("qstart", "qend", "sstart", "send")))
        expected = None if q["length"] is None else abs(h["qend"] - h["qstart"]) * 100 / q["length"] + 100 / q["length"]
        need(h["coverage"] is None if expected is None else number(h["coverage"], 0, 100) and abs(h["coverage"] - expected) < 1e-10)
        need(selected["min_coverage"] == 0 or h["coverage"] is not None and h["coverage"] >= selected["min_coverage"])
        need(number(h["bitscore"], 0) and isinstance(h["evalue"], str) and len(h["evalue"]) <= 64 and EVALUE.fullmatch(h["evalue"]))
        need(Decimal(h["evalue"]).is_finite() and Decimal(h["evalue"]) >= 0)


def validate_payload(payload, *, reader, kind=None, options=None, format=None, source_bytes=None, limit=MAX_OUTPUT):
    try:
        need(reader in FORMATS and isinstance(payload, dict))
        k = payload.get("kind")
        selected = validate_options(reader, k, payload.get("selected"))
        need(kind is None or kind == k)
        need(options is None or selected == validate_options(reader, k, options))
        field = "tree" if k == "tree" else {"sequence-browser": "sequence", "genome-tracks": "tracks", "blast-hits": "hits"}[reader]
        need(keys(payload, "contract_version type reader kind media_type choices selected metadata warnings sampled " + field))
        need(type(payload["contract_version"]) is int and payload["contract_version"] == 2 and payload["type"] == payload["reader"] == reader and payload["media_type"] == "application/json" and payload["warnings"] == [WARNINGS[reader]] and payload["sampled"] is False)
        m = payload["metadata"]
        need(keys(m, "format input_mode source_bytes records total_units labels_redacted limits"))
        need(m["format"] in FORMATS[reader] and (format is None or m["format"] == format) and m["input_mode"] == "whole" and integer(m["source_bytes"], 1, MAX_INPUT) and (source_bytes is None or type(source_bytes) is int and source_bytes == m["source_bytes"]))
        need(integer(m["records"], 1, 100000) and integer(m["total_units"], 1, 8000000) and integer(m["labels_redacted"], 0, 1000000) and m["limits"] == LIMITS[reader] and all(type(x) is int for x in m["limits"].values()))
        {"sequence-browser": _sequence, "genome-tracks": _tracks, "blast-hits": _blast}[reader](payload, k, payload["choices"], m, selected)
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) <= limit)
        return payload
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, InvalidOperation):
        raise ValueError(ERROR) from None
