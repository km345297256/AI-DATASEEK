"""Offline one-shot alignment parser. Never resolves user reference/index paths."""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .alignment_browser_payload import ERROR, LIMITS, MAX_INPUT, READER, WARNING, need, safe_label, validate_options, validate_payload


def _md_mismatches(read, sequence, md):
    """Walk CIGAR/MD runs, never materialize intron/deletion-length tuples."""
    events = []; pos = read.reference_start; query = 0
    for code, length in read.cigartuples or []:
        if code in (0, 7, 8):
            events.append(("match", length, pos, query)); pos += length; query += length
        elif code == 2:
            events.append(("deletion", length, pos, query)); pos += length
        elif code == 3: pos += length  # N is absent from the MD stream.
        elif code in (1, 4): query += length
    index = offset = 0; mismatches = []
    tokens = re.findall(r"\d+|\^[A-Z]+|[A-Z]", md.upper())
    need("".join(tokens) == md.upper())
    for token in tokens:
        deletion = token.startswith("^"); mismatch = not deletion and not token.isdecimal()
        count = len(token)-1 if deletion else 1 if mismatch else int(token)
        while count:
            need(index < len(events))
            event, length, rpos, qpos = events[index]
            need(event == ("deletion" if deletion else "match"))
            size = min(count, length-offset)
            if mismatch:
                need(qpos+offset < len(sequence))
                # MD supplies the reference base; we do not infer reference
                # sequence from query letters or fetch a genome for it.
                if len(mismatches) <= 500:
                    mismatches.append({"position": rpos+offset, "query": sequence[qpos+offset].upper(), "reference": token})
            count -= size; offset += size
            if offset == length: index += 1; offset = 0
    need(index == len(events) and offset == 0)
    return mismatches


def _read_details(read):
    sequence = read.query_sequence or ""
    pos, query = read.reference_start, 0
    blocks, insertions, deletions, splices = [], [], [], []
    for code, length in read.cigartuples or []:
        if code in (0, 7, 8):
            blocks.append({"start": pos, "end": pos+length}); pos += length; query += length
        elif code == 1:
            insertions.append({"position": pos, "length": length, "sequence": sequence[query:query+min(length, 100)].upper()}); query += length
        elif code in (2, 3):
            (deletions if code == 2 else splices).append({"start": pos, "end": pos+length, "length": length}); pos += length
        elif code == 4:
            query += length
    md = read.get_tag("MD") if read.has_tag("MD") else None
    if md is not None and (not isinstance(md, str) or len(md) > 2048 or not re.fullmatch(r"[0-9ACGTNacgtn^]+", md)):
        raise ValueError(ERROR)
    mismatches = _md_mismatches(read, sequence, md) if md is not None and sequence else []
    return dict(name=safe_label(read.query_name or "unnamed"), start=read.reference_start, end=read.reference_end,
        reverse=read.is_reverse, mapq=read.mapping_quality, cigar=read.cigarstring, flag=read.flag,
        paired=read.is_paired, duplicate=read.is_duplicate, secondary=read.is_secondary,
        supplementary=read.is_supplementary, read1=read.is_read1, read2=read.is_read2,
        mate_start=read.next_reference_start if read.next_reference_start >= 0 else None,
        mate_reference=safe_label(read.next_reference_name) if read.next_reference_id >= 0 else None,
        template_length=read.template_length, read_group=safe_label(str(read.get_tag("RG"))) if read.has_tag("RG") else None,
        nm=read.get_tag("NM") if read.has_tag("NM") else None, md=md, blocks=blocks,
        mismatches=mismatches[:500], insertions=insertions[:100], deletions=deletions[:100], splices=splices[:100],
        mismatch_available=md is not None and bool(sequence),
        detail_truncated=len(mismatches)>500 or len(insertions)>100 or len(deletions)>100 or len(splices)>100)


def _region(handle, selected, refs):
    start, end = selected["start"], selected["end"]
    need(selected["reference"] < len(refs) and end <= refs[selected["reference"]]["length"])
    width = math.ceil((end-start)/selected["bins"])
    coverage = [{"start": left, "end": min(end, left+width), "covered_bases": 0, "depth": 0} for left in range(start, end, width)]
    reads = []; scanned = matched = query_bases = 0; complete = True
    for read in handle.fetch(until_eof=True):
        if scanned >= LIMITS["records"]:
            complete = False; break
        scanned += 1
        if read.is_unmapped or read.reference_id != selected["reference"]:
            continue
        left, right = read.reference_start, read.reference_end
        if right is None or right <= start or left >= end: continue
        cigar = read.cigartuples or []
        need(0 < len(cigar) <= 2048 and all(code in range(9) and 0 < n <= 2000000 for code, n in cigar))
        need(read.query_length <= 200000 and read.cigarstring is not None and len(read.cigarstring) <= 2048)
        consumed = sum(n for code, n in cigar if code in {0, 1, 4, 7, 8})
        if query_bases + consumed > LIMITS["query_bases"] or matched >= selected["max_reads"]*4:
            complete = False; break
        query_bases += consumed; matched += 1
        pos = left
        for code, n in cigar:
            if code in (0, 7, 8):
                lo, hi = max(start, pos), min(end, pos+n)
                if hi > lo:
                    for index in range((lo-start)//width, (hi-1-start)//width+1):
                        b = coverage[index]; b["covered_bases"] += min(hi, b["end"])-max(lo, b["start"])
                pos += n
            elif code in (2, 3): pos += n
        if len(reads) < selected["max_reads"]: reads.append(_read_details(read))
    for b in coverage: b["depth"] = b["covered_bases"]/(b["end"]-b["start"])
    return dict(reads=reads, coverage=coverage, records_scanned=scanned, matched_reads=matched,
                scan_complete=complete, reads_truncated=matched>len(reads))


def _offline_cram(path, directory, disabled_reference):
    """Strip native reference locators without decoding or altering records.

    HTSlib can fall back to @SQ UR after an explicit reference lacks a contig,
    despite the high-level reference_filename documentation. A fixed-header
    samtools reheader operation preserves SN/LN/M5, records and embedded refs;
    it neither evaluates a -c command nor round-trips alignments through SAM.
    """
    import pysam
    with pysam.AlignmentFile(str(path), "rc", check_sq=True, threads=1,
                             reference_filename=str(disabled_reference)) as initial:
        need(0 < initial.nreferences <= 256 and len(str(initial.header)) <= 1024**2)
        original = initial.header.to_dict()
    safe_header = {key: value for key, value in original.items() if key != "CO"}
    sequences = []
    for sequence in original.get("SQ", []):
        name = sequence.get("SN")
        # Native reference identifiers cannot double as local paths/URLs.
        need(isinstance(name, str) and 0 < len(name) <= 256 and not re.search(r"[\x00-\x20\x7f/\\]", name))
        md5 = sequence.get("M5")
        need(md5 is None or isinstance(md5, str) and re.fullmatch(r"[0-9a-fA-F]{32}", md5))
        sequences.append({k: v for k, v in sequence.items() if k != "UR"})
    safe_header["SQ"] = sequences
    header_path = Path(directory)/"offline-header.sam"
    safe_path = Path(directory)/"offline-source.cram"
    header_path.write_text(str(pysam.AlignmentHeader.from_dict(safe_header)), encoding="utf-8")
    # CRAM's native reheader writes directly to OS stdout in this pysam build;
    # dispatcher save_stdout does not reliably capture it. A fixed child
    # program + OS file descriptor redirection keeps binary data off JSONL.
    with safe_path.open("wb") as output:
        process = subprocess.run(
            [sys.executable, "-c", "import sys,pysam;pysam.reheader('-P',sys.argv[1],sys.argv[2],catch_stdout=False)", str(header_path), str(path)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            timeout=15, check=False)
    need(process.returncode == 0)
    need(safe_path.is_file() and 0 < safe_path.stat().st_size <= MAX_INPUT + 2*1024**2)
    return safe_path


def alignment_browser_preview(data, fmt, kind="tree", options=None):
    options = validate_options(kind, {} if options is None else options)
    need(isinstance(data, bytes) and 0 < len(data) <= MAX_INPUT and fmt in {"sam", "bam", "cram"})
    need(data.startswith(b"CRAM") if fmt == "cram" else data.startswith(b"\x1f\x8b") if fmt == "bam" else data.startswith(b"@"))
    import pysam
    try:
        with tempfile.TemporaryDirectory(prefix="alignment-") as directory:
            # All names are constants below the private tmpfs. No caller paths,
            # sidecar URLs, header URs, credential refs, or cache survive a call.
            path = Path(directory)/("source."+fmt); path.write_bytes(data)
            disabled_reference = Path(directory)/"reference-disabled.fa"
            disabled_reference.write_text(">__external_reference_disabled__\nN\n")
            previous = {key: os.environ.get(key) for key in ("REF_PATH", "REF_CACHE")}
            reference_root = Path(directory)/"empty-reference-cache"
            reference_root.mkdir()
            os.environ["REF_PATH"] = str(reference_root)+"/%s"
            os.environ["REF_CACHE"] = str(reference_root)+"/%s"
            try:
                if fmt == "cram":
                    path = _offline_cram(path, directory, disabled_reference)
                with pysam.AlignmentFile(str(path), {"sam": "r", "bam": "rb", "cram": "rc"}[fmt],
                        check_sq=True, threads=1, reference_filename=str(disabled_reference) if fmt == "cram" else None) as handle:
                    need(0 < handle.nreferences <= 256 and len(str(handle.header)) <= 1024**2)
                    refs = [{"id": i, "name": safe_label(name), "length": length} for i, (name, length) in enumerate(zip(handle.references, handle.lengths))]
                    header = handle.header.to_dict()
                    order = header.get("HD", {}).get("SO", "unknown")
                    choices = dict(references=refs, sort_order=order if order in {"coordinate", "queryname", "unsorted"} else "unknown", read_groups=len(header.get("RG", [])))
                    value = dict(contract_version=2, type=READER, reader=READER, kind=kind,
                        media_type="application/json", choices=choices, selected=options,
                        metadata=dict(format=fmt, source_bytes=len(data), input_mode="whole", coordinate_system="0-based-half-open", limits=dict(LIMITS)), warnings=[WARNING], sampled=False)
                    if kind == "tree":
                        value["tree"] = [{"path": f"/references/{r['id']}", "node_type": "reference", "attributes": {"label": r["name"], "length": r["length"]}} for r in refs]
                    else:
                        value["alignment"] = region = _region(handle, options, refs)
                        value["sampled"] = not region["scan_complete"] or region["reads_truncated"]
                    return validate_payload(value, kind=kind, options=options, format=fmt, source_bytes=len(data))
            finally:
                for key, old in previous.items():
                    if old is None: os.environ.pop(key, None)
                    else: os.environ[key] = old
    except (ValueError, OSError, KeyError, TypeError, OverflowError, subprocess.SubprocessError):
        raise ValueError(ERROR if fmt != "cram" else "CRAM 无法离线解码：请使用嵌入参考或无参考的 CRAM，或转为 BAM；不会访问外部参考。") from None
