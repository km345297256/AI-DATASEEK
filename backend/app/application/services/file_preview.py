"""Bounded, read-only previews over the existing object-store range contract."""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.models.file import FileInfo

TEXT_PAGE_BYTES = 64 * 1024
CSV_PAGE_BYTES = 128 * 1024
CSV_PAGE_ROWS = 100
CSV_PAGE_COLUMNS = 50


class PreviewVersionChanged(ValueError):
    pass


class FilePreviewPage(BaseModel):
    version: str
    offset: int
    next_offset: int | None = None
    total_bytes: int
    text: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    columns_truncated: bool = False
    delimiter: Literal[",", "\t"] | None = None
    header_pending: bool = False
    bytes_read: int


def preview_version(info: FileInfo) -> str:
    # File IDs identify immutable uploads. Include recorded revision markers to
    # reject a changed object instead of combining pages from two revisions.
    metadata = info.metadata or {}
    identity = [info.file_id, info.size, str(info.upload_date), metadata.get("sha256"), metadata.get("content_sha256")]
    if metadata.get("dataset_file_version"):
        identity.append(metadata["dataset_file_version"])
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _record_ends(data: bytes, *, eof: bool, delimiter: Literal[",", "\t"]) -> list[int]:
    """CSV records, including quoted newlines and escaped double quotes."""
    ends: list[int] = []
    quoted = False
    field_start = True
    delimiter_byte = ord(delimiter)
    index = 0
    while index < len(data):
        char = data[index]
        if quoted:
            if char == 34 and index + 1 < len(data) and data[index + 1] == 34:
                index += 2
                continue
            if char == 34:
                quoted = False
        elif char == 34 and field_start:
            quoted = True
            field_start = False
        elif char == delimiter_byte:
            field_start = True
        elif char in (10, 13):
            if char == 13:
                if index + 1 == len(data) and not eof:
                    break  # Do not split CRLF across pages.
                if index + 1 < len(data) and data[index + 1] == 10:
                    index += 1
            ends.append(index + 1)
            field_start = True
        else:
            field_start = False
        index += 1
    if eof and (not ends or ends[-1] < len(data)):
        if quoted:
            raise ValueError("CSV contains an incomplete quoted record")
        ends.append(len(data))
    return ends


def _guess_delimiter(record: bytes) -> Literal[",", "\t"]:
    counts = {9: 0, 44: 0}
    quoted = False
    field_start = True
    index = 0
    while index < len(record):
        char = record[index]
        if quoted:
            if char == 34 and index + 1 < len(record) and record[index + 1] == 34:
                index += 2
                continue
            if char == 34:
                quoted = False
        elif char == 34 and field_start:
            quoted = True
            field_start = False
        elif char in counts:
            counts[char] += 1
            field_start = True
        elif char in (10, 13):
            if any(counts.values()):
                break
            field_start = True
        else:
            field_start = False
        index += 1
    return "\t" if counts[9] > counts[44] else ","


def parse_preview_page(data: bytes, info: FileInfo, *, offset: int, mode: Literal["text", "csv"], delimiter: Literal[",", "\t"] | None = None, header_pending: bool = False) -> FilePreviewPage:
    total = info.size or 0
    eof = offset + len(data) >= total
    page = FilePreviewPage(version=preview_version(info), offset=offset, total_bytes=total, bytes_read=len(data))
    if mode == "text":
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            page.text = decoder.decode(data, final=eof)
        except UnicodeDecodeError as error:
            raise ValueError("Preview supports UTF-8 text; download this file to view another encoding") from error
        pending, _ = decoder.getstate()
        consumed = len(data) - len(pending)
        if offset == 0:
            page.text = page.text.removeprefix("\ufeff")
    else:
        needs_header = offset == 0 or header_pending
        if delimiter is None:
            sample = data.removeprefix(codecs.BOM_UTF8) if offset == 0 else data
            delimiter = "\t" if str(info.filename or "").lower().endswith(".tsv") else _guess_delimiter(sample)
        page.delimiter = delimiter
        # Ignore a leading BOM for CSV grammar while retaining byte offsets.
        scan_data = data
        bom_bytes = 0
        if offset == 0 and data.startswith(codecs.BOM_UTF8):
            scan_data = data[len(codecs.BOM_UTF8):]
            bom_bytes = len(codecs.BOM_UTF8)
        ends = [end + bom_bytes for end in _record_ends(scan_data, eof=eof, delimiter=delimiter)]
        if not ends and data:
            raise ValueError("A CSV record exceeds the 128 KiB preview limit; download the file to inspect it")
        # Parse only complete records. Even one quoted record cannot cause an
        # unbounded read, and the next request starts at a record boundary.
        consumed = 0
        records: list[list[str]] = []
        for end in ends:
            try:
                text = data[consumed:end].decode("utf-8-sig" if offset + consumed == 0 else "utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("Preview supports UTF-8 CSV; download this file to view another encoding") from error
            try:
                row = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True), [])
            except csv.Error as error:
                raise ValueError("CSV record cannot be previewed safely; download the file to inspect it") from error
            consumed = end
            if not any(row):
                continue
            page.columns_truncated |= len(row) > CSV_PAGE_COLUMNS
            records.append(row[:CSV_PAGE_COLUMNS])
            if len(records) >= CSV_PAGE_ROWS + (1 if needs_header else 0):
                break
        if needs_header and records:
            page.headers = records.pop(0)
            needs_header = False
        page.header_pending = needs_header
        page.rows = records
    if offset + consumed < total:
        if consumed == 0:
            raise ValueError("Preview cannot advance within the bounded page")
        page.next_offset = offset + consumed
    return page
