"""Bounded, networkless readers for main's sequence / genome / BLAST viewers.

The UI never downloads source text. Whole means the complete *bounded* source is
parsed on each request, not a claim of indexed random access. No header may load
another file, reference genome, track, program, stylesheet or remote resource.
"""
from __future__ import annotations
import math
import re
from decimal import Decimal
from .sequence_browser_payload import (
    ERROR, FORMATS, LIMITS, MAX_COORD, MAX_INPUT, UNSAFE, WARNINGS, EVALUE,
    need, number, overlap, validate_options, validate_payload,
)

INT = re.compile(r"[0-9]{1,10}\Z")
NUM = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?\Z")
BASES = re.compile(r"[A-Za-z*.\-]+\Z")
BLAST_COLUMNS = ["query id", "subject id", "% identity", "alignment length", "mismatches", "gap opens", "q. start", "q. end", "s. start", "s. end", "evalue", "bit score"]


def _integer(text, low=0, high=MAX_COORD):
    need(isinstance(text, str) and INT.fullmatch(text))
    value = int(text)
    need(low <= value <= high)
    return value


def _number(text, low=-1e12, high=1e12):
    need(isinstance(text, str) and len(text) <= 64 and NUM.fullmatch(text))
    exact = Decimal(text)
    value = float(exact)
    need(number(value, low, high) and (exact == 0 or value != 0))
    return value


class _Labels:
    def __init__(self):
        self.redacted = 0

    def text(self, value, fallback, limit=128):
        if not value or len(value) > limit or UNSAFE.search(value):
            self.redacted += 1
            # Safe text is shortened; unsafe text is never partially exposed.
            return value[:limit - 1] + "…" if value and not UNSAFE.search(value) else fallback
        return value


def _sequences(text, fmt, labels):
    lines = text.splitlines()
    need(len(lines) <= 1000000)
    records = []
    total = 0

    def append(name, chunks, quality=None):
        nonlocal total
        sequence = "".join(chunks).upper()
        need(sequence and BASES.fullmatch(sequence))
        total += len(sequence)
        need(total <= 8000000 and len(records) < 256)
        n = len(sequence)
        bins = min(120, n)
        gc_bins = []
        for i in range(bins):
            piece = sequence[i * n // bins:(i + 1) * n // bins]
            gc_bins.append(piece.count("G") + piece.count("C"))
        desc = {"id": len(records), "name": labels.text(name, f"Sequence {len(records) + 1}"), "length": n,
                "gc_count": sum(gc_bins), "n_count": sequence.count("N"), "gc_bins": gc_bins, "qualities": quality is not None}
        records.append((desc, sequence, quality))

    if fmt in {"fq", "fastq"}:
        i = 0
        while i < len(lines):
            need(lines[i].startswith("@") and len(lines[i]) > 1)
            name = lines[i][1:]
            i += 1
            chunks = []
            while i < len(lines) and not lines[i].startswith("+"):
                need(BASES.fullmatch(lines[i]))
                chunks.append(lines[i]); i += 1
            need(i < len(lines) and chunks)
            second = lines[i][1:]
            need(not second or second.split()[0] == name.split()[0])
            i += 1
            length = sum(map(len, chunks))
            need(length <= 8000000)
            qs = []; qlen = 0
            while i < len(lines) and qlen < length:
                row = lines[i]
                need(row and all(33 <= ord(c) <= 126 for c in row))
                qlen += len(row); qs.append(row); i += 1
            need(qlen == length)
            append(name, chunks, "".join(qs))
    else:
        name = None; chunks = []
        for line in lines:
            if not line.strip():
                continue
            if line.startswith(">"):
                if name is not None:
                    append(name, chunks)
                name = line[1:].strip(); chunks = []
                need(name)
            else:
                need(name is not None)
                line = re.sub(r"[ \t]", "", line)
                need(BASES.fullmatch(line)); chunks.append(line)
        if name is not None:
            append(name, chunks)
    need(records)
    return records, total


def _sequence_result(records, selected):
    need(selected["record"] < len(records))
    desc, sequence, quality = records[selected["record"]]
    start = selected["start"] - 1; count = selected["count"]
    need(start < len(sequence))
    encoding = selected["quality_encoding"]
    if quality is None:
        need(encoding is None); qs = stats = None
    else:
        need(encoding in {"phred33", "phred64"})
        zero = 33 if encoding == "phred33" else 64
        need(all(ord(c) >= zero for c in quality))
        stats = {"sum": sum(ord(c) - zero for c in quality), "low_count": sum(ord(c) - zero < 20 for c in quality)}
        qs = [ord(c) - zero for c in quality[start:start + count]]
    motif = selected["motif"]; positions = []; total = 0; offset = 0
    if motif:
        while True:
            hit = sequence.find(motif, offset)
            if hit < 0:
                break
            total += 1
            if len(positions) < 10000:
                positions.append(hit + 1)
            offset = hit + 1
    return {"bases": sequence[start:start + count], "qualities": qs, "quality_stats": stats,
            "search": {"positions": positions, "total": total, "truncated": total > 10000}}


def _tracks(text, fmt, labels):
    chromosomes = []; by_name = {}; features = []
    wig = None; bed_columns = None
    for raw in text.splitlines():
        need(len(raw) <= 8192)
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("track ") or line.startswith("browser "):
            continue
        if fmt == "wig" and (line.startswith("fixedStep ") or line.startswith("variableStep ")):
            parts = line.split(); mode = parts[0]; attrs = {}
            for item in parts[1:]:
                need(item.count("=") == 1)
                key, value = item.split("="); need(key not in attrs); attrs[key] = value
            need(set(attrs) <= ({"chrom", "span", "start", "step"} if mode == "fixedStep" else {"chrom", "span"}) and "chrom" in attrs)
            if mode == "fixedStep":
                need({"start", "step"} <= set(attrs))
            wig = {"mode": mode, "chrom": attrs["chrom"], "span": _integer(attrs.get("span", "1"), 1),
                   "position": _integer(attrs.get("start", "1"), 1), "step": _integer(attrs.get("step", "1"), 1), "last": 0}
            continue
        fields = line.split("\t")
        strand = "."; value = None
        if fmt in {"bed", "bedgraph"}:
            fields = line.split()
            need(len(fields) == 4 if fmt == "bedgraph" else 3 <= len(fields) <= 12)
            if bed_columns is None:
                bed_columns = len(fields)
            need(len(fields) == bed_columns)
            chrom = fields[0]; start = _integer(fields[1]); end = _integer(fields[2], start)
            if fmt == "bedgraph":
                need(end > start); value = _number(fields[3]); track = "signal"; name = "Signal"
            else:
                track = "interval"; name = fields[3] if len(fields) >= 4 else "Interval"
                if len(fields) >= 5 and fields[4] != ".":
                    value = _number(fields[4], 0, 1000)
                if len(fields) >= 6:
                    strand = fields[5]; need(strand in {"+", "-", "."})
                # BED extra fields are retained as inert detail, not inferred exon blocks.
        elif fmt in {"gff", "gff3", "gtf"}:
            need(len(fields) == 9)
            chrom = fields[0]; start = _integer(fields[3], 1) - 1; end = _integer(fields[4], start + 1)
            name = fields[2]; track = "annotation"; strand = fields[6]
            need(strand in {"+", "-", ".", "?"} and fields[7] in {".", "0", "1", "2"})
            if fields[5] != ".":
                value = _number(fields[5])
        elif fmt == "vcf":
            need(len(fields) >= 8 and re.fullmatch(r"[ACGTNacgtn]+", fields[3]))
            chrom = fields[0]; start = _integer(fields[1], 1) - 1; end = start + len(fields[3]); track = "variant"
            need(fields[4] and not any(c in fields[4] for c in "[]"))
            endpoints = [part[4:] for part in fields[7].split(";") if part.startswith("END=")]
            need(len(endpoints) <= 1)
            if endpoints:
                end = _integer(endpoints[0], start + 1)
            if fields[5] != ".":
                value = _number(fields[5], 0)
            # Display-only delimiters preserve allele meaning while remaining
            # inert under the common no-angle-brackets label contract.
            shown_alt = fields[4].replace("<", "〈").replace(">", "〉")
            name = fields[2] if fields[2] != "." else fields[3] + "→" + shown_alt
            fields = fields[:8]  # Sample identifiers / genotype columns are not needed for a variant track.
            fields[4] = shown_alt
        else:
            need(fmt == "wig" and wig is not None)
            values = line.split()
            if wig["mode"] == "fixedStep":
                need(len(values) == 1); position = wig["position"]; wig["position"] += wig["step"]
            else:
                need(len(values) == 2); position = _integer(values[0], 1)
            need(position > wig["last"]); wig["last"] = position
            chrom = wig["chrom"]; start = position - 1; end = start + wig["span"]
            value = _number(values[-1]); track = "signal"; name = "Signal"
        need(chrom and chrom != "." and end <= MAX_COORD)
        if chrom not in by_name:
            need(len(chromosomes) < 256)
            ordinal = len(chromosomes); by_name[chrom] = ordinal
            shown = labels.text(chrom, f"Chromosome {ordinal + 1}")
            # Redaction must not merge different reference identifiers.
            if any(c["name"] == shown for c in chromosomes):
                shown = f"Chromosome {ordinal + 1}"
            chromosomes.append({"id": ordinal, "name": shown, "start": start, "end": end, "records": 0})
        ordinal = by_name[chrom]; c = chromosomes[ordinal]
        c["start"] = min(start, c["start"]); c["end"] = max(end, c["end"]); c["records"] += 1
        need(len(features) < 100000)
        detail = labels.text(" | ".join(fields), "Fields omitted", 256)
        features.append({"id": len(features), "chromosome": ordinal, "start": start, "end": end,
                         "label": labels.text(name, f"Feature {len(features) + 1}"), "track": track,
                         "strand": strand, "value": value, "detail": detail})
    need(features)
    return chromosomes, features


def _blast(text, labels):
    queries = []; by_name = {}; hits = []; declared_columns = None; width = None
    for raw in text.splitlines():
        need(len(raw) <= 8192)
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("# Fields:"):
                cols = [x.strip() for x in line[len("# Fields:"):].split(",")]
                need(cols in (BLAST_COLUMNS, BLAST_COLUMNS + ["query length"]))
                need(declared_columns is None or declared_columns == len(cols))
                declared_columns = len(cols)
            continue
        values = line.split()
        need(len(values) in {12, 13} and (len(values) == 12 or declared_columns == 13) and (declared_columns is None or declared_columns == len(values)))
        if width is None:
            width = len(values)
        need(width == len(values))
        query, subject = values[:2]
        identity = _number(values[2], 0, 100); length = _integer(values[3], 1)
        mismatches = _integer(values[4], 0, length); gaps = _integer(values[5], 0, length)
        qs, qe, ss, se = [_integer(x, 1) for x in values[6:10]]
        need(len(values[10]) <= 64 and EVALUE.fullmatch(values[10]))
        bitscore = _number(values[11], 0)
        qlen = _integer(values[12], max(qs, qe)) if len(values) == 13 else None
        if query not in by_name:
            need(len(queries) < 256)
            ordinal = len(queries); by_name[query] = ordinal
            shown = labels.text(query, f"Query {ordinal + 1}")
            if any(q["name"] == shown for q in queries):
                shown = f"Query {ordinal + 1}"
            queries.append({"id": ordinal, "name": shown, "length": qlen, "extent": max(qs, qe), "hits": 0})
        ordinal = by_name[query]; q = queries[ordinal]
        need(q["length"] == qlen)
        q["extent"] = max(q["extent"], qs, qe); q["hits"] += 1
        need(len(hits) < 20000)
        hits.append({"id": len(hits), "query": ordinal, "subject": labels.text(subject, f"Subject {len(hits) + 1}"),
                     "identity": identity, "alignment_length": length, "mismatches": mismatches, "gap_opens": gaps,
                     "qstart": qs, "qend": qe, "sstart": ss, "send": se, "evalue": values[10], "bitscore": bitscore,
                     "coverage": None if qlen is None else (abs(qe - qs) + 1) * 100 / qlen})
    need(hits)
    return queries, hits


def sequence_browser_preview(data, reader, fmt, kind="tree", options=None):
    """Return a validated private v2 result from a bounded authorized byte string."""
    try:
        selected = validate_options(reader, kind, options if options is not None else {})
        need(fmt in FORMATS[reader] and isinstance(data, bytes) and 1 <= len(data) <= MAX_INPUT)
        text = data.decode("utf-8-sig", "strict")
        need(not any(ord(c) < 32 and c not in "\t\r\n" for c in text) and "\x7f" not in text)
        labels = _Labels()
        result = {"contract_version": 2, "type": reader, "reader": reader, "kind": kind,
                  "media_type": "application/json", "selected": selected, "warnings": [WARNINGS[reader]], "sampled": False}
        if reader == "sequence-browser":
            records, units = _sequences(text, fmt, labels)
            choices = [r[0] for r in records]; result["choices"] = {"records": choices}
            count = len(records); path = "records"
            if kind != "tree":
                result["sequence"] = _sequence_result(records, selected)
        elif reader == "genome-tracks":
            choices, records = _tracks(text, fmt, labels)
            count = units = len(records); result["choices"] = {"chromosomes": choices}; path = "chromosomes"
            if kind != "tree":
                need(selected["chromosome"] < len(choices))
                selected_rows = [r for r in records if r["chromosome"] == selected["chromosome"] and overlap(r["start"], r["end"], selected["start"], selected["end"])]
                need(len(selected_rows) <= 3000); result["tracks"] = selected_rows
        else:
            choices, records = _blast(text, labels)
            count = units = len(records); result["choices"] = {"queries": choices}; path = "queries"
            if kind != "tree":
                need(selected["query"] is None or selected["query"] < len(choices))
                eligible = [h for h in records if (selected["query"] is None or h["query"] == selected["query"]) and h["identity"] >= selected["min_identity"]]
                unknown = sum(h["coverage"] is None for h in eligible)
                filtered = [h for h in eligible if selected["min_coverage"] == 0 or h["coverage"] is not None and h["coverage"] >= selected["min_coverage"]]
                need(selected["offset"] <= len(filtered))
                result["hits"] = {"rows": filtered[selected["offset"]:selected["offset"] + selected["count"]], "total": len(filtered), "unknown_coverage": unknown}
        if kind == "tree":
            result["tree"] = [{"path": f"/{path}/{c['id']}", "node_type": "array", "attributes": {"label": c["name"]}} for c in choices]
        result["metadata"] = {"format": fmt, "input_mode": "whole", "source_bytes": len(data), "records": count,
                              "total_units": units, "labels_redacted": labels.redacted, "limits": LIMITS[reader]}
        return validate_payload(result, reader=reader, kind=kind, options=selected, format=fmt, source_bytes=len(data))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError, ArithmeticError):
        raise ValueError(ERROR) from None
