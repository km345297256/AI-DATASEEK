"""Explicit, archive-scoped UTF-8 member previews; never extract to disk.

The existing directory-only reader is unchanged. An opaque member reference is
derived from the complete archive bytes and its validated index, never accepted
as a filesystem path. The caller independently pins owner/plugin/file version.
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import tarfile
import unicodedata
import zipfile
import zlib

from app.services.bounded_format_readers import (
    BoundedFormatError, MAX_INPUT_BYTES, MAX_MEMBERS, PAGE_ROWS,
    _input, _label, _member_path, _tar_members, _validate_members, _zip_members,
)

MAX_MEMBER_BYTES = 256 * 1024
MAX_COMPRESSED_MEMBER_BYTES = MAX_MEMBER_BYTES + 65536
MAX_LINE_CHARS = 1024
TEXT_SUFFIXES = {"txt", "csv", "tsv", "json", "xml", "yaml", "yml", "md", "log", "ini", "cfg", "toml"}
DIRECTORY_COLUMNS = ["成员", "类型", "声明大小（字节）", "可预览", "预览标识"]
TEXT_COLUMNS = ["行号", "文本"]


def _eligible(member, method=None):
    name = member["path"].rsplit("/", 1)[-1].lower()
    return (member["type"] == "file" and member["size"] <= MAX_MEMBER_BYTES
            and (member["packed"] is None or member["packed"] <= MAX_COMPRESSED_MEMBER_BYTES)
            and "." in name and name.rsplit(".", 1)[1] in TEXT_SUFFIXES
            and method in {None, zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


def _reference(digest, index, path):
    return "member-" + hashlib.sha256(b"dataseek-archive-member-v1\0" + digest
        + index.to_bytes(4, "big") + path.encode("utf-8")).hexdigest()


def _zip_details(data):
    """Cross-check all local sizes/CRCs, not just the currently selected page."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        details = archive.infolist()
    for item in details:
        crc, packed, size = struct.unpack_from("<III", data, item.header_offset + 14)
        expected = (item.CRC, item.compress_size, item.file_size)
        values = (crc, packed, size)
        # Bit 3 allows local placeholders for a data descriptor. A nonzero
        # local declaration still must agree with the bounded central index.
        if (item.flag_bits & 8 and any(v not in {0, e} for v, e in zip(values, expected))) or (not item.flag_bits & 8 and values != expected):
            raise BoundedFormatError("ZIP 本地校验和或长度与中央目录不一致。")
    return details


def _zip_text_bytes(data, info):
    """Decode one independently bounded stream, never trust ZipExtFile's _left.

    ZipExtFile truncates returned data to the declared file_size. Checking its
    returned length/CRC alone cannot prove that a forged stream does not expand
    further. Validate actual deflate EOF, length, trailing data and CRC here.
    """
    name_size, extra_size = struct.unpack_from("<HH", data, info.header_offset + 26)
    start = info.header_offset + 30 + name_size + extra_size
    packed = data[start:start + info.compress_size]
    if len(packed) != info.compress_size or len(packed) > MAX_COMPRESSED_MEMBER_BYTES:
        raise BoundedFormatError("ZIP 成员压缩内容超过预算或被截断。")
    if info.compress_type == zipfile.ZIP_STORED:
        content = packed
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        inflater = zlib.decompressobj(-15)
        content = inflater.decompress(packed, MAX_MEMBER_BYTES + 1)
        if not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
            raise BoundedFormatError("ZIP 成员压缩流不完整、超预算或有尾随数据。")
    else:
        raise BoundedFormatError("ZIP 文本预览仅支持 stored/deflate。")
    if len(content) != info.file_size or len(content) > MAX_MEMBER_BYTES or zlib.crc32(content) != info.CRC:
        raise BoundedFormatError("ZIP 成员实际大小或 CRC 校验失败。")
    return content


def archive_member_preview(data, fmt, options=None):
    _input(data, MAX_INPUT_BYTES)
    options = {} if options is None else options
    if (not isinstance(options, dict) or set(options) - {"row_offset", "member_id"}
            or type(options.get("row_offset", 0)) is not int or not 0 <= options.get("row_offset", 0) <= MAX_MEMBER_BYTES):
        raise BoundedFormatError("成员预览分页参数无效。")
    member_id = options.get("member_id")
    if "member_id" in options and (not isinstance(member_id, str) or not re.fullmatch(r"member-[0-9a-f]{64}", member_id)):
        raise BoundedFormatError("需要当前压缩包的有效成员标识，不能传入路径。")
    if fmt not in {"zip", "tar"}:
        raise BoundedFormatError("成员预览只支持受限 ZIP 和未压缩 TAR。")
    members = _zip_members(data) if fmt == "zip" else _tar_members(data)
    _validate_members(members)  # Complete bounded index, including unseen pages.
    digest = hashlib.sha256(data).digest()
    references = [_reference(digest, i, item["path"]) for i, item in enumerate(members)]
    methods = [None] * len(members)
    details = None
    if fmt == "zip":
        details = _zip_details(data)
        methods = [item.compress_type for item in details]
    offset = options.get("row_offset", 0)
    result = {"contract_version": 2, "type": "archive-member", "reader": "archive-member", "kind": "table",
              "media_type": "application/json", "metadata": {"format": fmt, "source_bytes": len(data),
                  "member_limit_bytes": MAX_MEMBER_BYTES, "writes_source": False, "recursive": False},
              "warnings": [], "sampled": False}
    if member_id is None:
        if offset >= max(1, len(members)):
            raise BoundedFormatError("压缩包目录分页超出范围。")
        rows = []
        for i in range(offset, min(offset + PAGE_ROWS, len(members))):
            item = members[i]
            eligible = _eligible(item, methods[i])
            rows.append([_label(item["path"]), item["type"], item["size"], eligible, references[i] if eligible else None])
        result["table"] = {"columns": DIRECTORY_COLUMNS, "rows": rows, "row_offset": offset,
                           "column_offset": 0, "total_rows": len(members), "total_columns": 5}
        result["metadata"].update(mode="directory", contents_verified=False)
        result["warnings"].append("先验证整个有界目录；仅用户选择的小型文本成员会读取内容。ZIP 成员预览仅支持 stored/deflate。")
        result["sampled"] = offset > 0 or offset + len(rows) < len(members)
        return result
    try:
        index = references.index(member_id)
    except ValueError:
        raise BoundedFormatError("成员标识不属于当前压缩包版本。") from None
    member = members[index]
    if not _eligible(member, methods[index]):
        raise BoundedFormatError("此成员不是预算内可预览的文本。")
    try:
        if fmt == "zip":
            content = _zip_text_bytes(data, details[index])
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:", encoding="utf-8", errors="strict") as archive:
                items = archive.getmembers()
                if len(items) != len(members):
                    raise BoundedFormatError("TAR 成员索引不一致。")
                info = items[index]
                if not info.isfile() or info.size != member["size"] or _member_path(info.name) != member["path"]:
                    raise BoundedFormatError("TAR 成员索引不一致。")
                if info.offset_data < 0 or info.offset_data + info.size > len(data):
                    raise BoundedFormatError("TAR 成员内容位置无效。")
                content = data[info.offset_data:info.offset_data + info.size]
                if len(content) != member["size"]:
                    raise BoundedFormatError("成员实际大小与声明不一致。")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError, zlib.error) as error:
        if isinstance(error, BoundedFormatError):
            raise
        raise BoundedFormatError("此成员的内容或完整性校验失败。") from None
    if len(content) > MAX_MEMBER_BYTES:
        raise BoundedFormatError("成员展开内容超过 256 KiB。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise BoundedFormatError("此成员不是有效 UTF-8 文本。") from None
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\r\n\t" for c in text):
        raise BoundedFormatError("此成员包含二进制或不安全文本控制字符。")
    lines = text.splitlines() or [""]
    if offset >= len(lines):
        raise BoundedFormatError("文本分页超出范围。")
    page = [line.replace("\t", "    ") for line in lines[offset:offset + PAGE_ROWS]]
    clipped = any(len(line) > MAX_LINE_CHARS for line in page)
    # Labels and text are plain strings; redact potential host paths, never HTML.
    rows = [[i + offset + 1, _label(line, MAX_LINE_CHARS)] for i, line in enumerate(page)]
    result["table"] = {"columns": TEXT_COLUMNS, "rows": rows, "row_offset": offset,
                       "column_offset": 0, "total_rows": len(lines), "total_columns": 2}
    result["metadata"].update(mode="text", member_id=member_id, member_name=_label(member["path"]),
        member_bytes=len(content), checksum_verified=fmt == "zip", encoding="utf-8",
        line_char_limit=MAX_LINE_CHARS, member_text_only=True)
    result["sampled"] = offset > 0 or offset + len(rows) < len(lines) or clipped
    result["warnings"].append("仅显示纯文本，不解析其中的 HTML、脚本、XML 实体或外链；每页最多 200 行，每行最多 1024 字符。")
    if fmt == "tar":
        result["warnings"].append("TAR 只验证头部与长度，不提供成员内容校验和。")
    return result
