"""Strict data-only schema for archive-scoped, explicit text member previews."""
import re
import json
import unicodedata

from app.application.services.scientific_visualization import ScientificPreviewRejected

MEMBER = re.compile(r"member-[0-9a-f]{64}")
MAX_MEMBER = 256 * 1024
DIRECTORY_COLUMNS = ["成员", "类型", "声明大小（字节）", "可预览", "预览标识"]
TEXT_COLUMNS = ["行号", "文本"]
DIRECTORY_WARNING = "先验证整个有界目录；仅用户选择的小型文本成员会读取内容。ZIP 成员预览仅支持 stored/deflate。"
TEXT_WARNING = "仅显示纯文本，不解析其中的 HTML、脚本、XML 实体或外链；每页最多 200 行，每行最多 1024 字符。"
TAR_WARNING = "TAR 只验证头部与长度，不提供成员内容校验和。"


def validate_archive_member_options(options):
    if (not isinstance(options, dict) or set(options) - {"row_offset", "member_id"}
            or type(options.get("row_offset", 0)) is not int or not 0 <= options.get("row_offset", 0) <= MAX_MEMBER
            or ("member_id" in options and (not isinstance(options["member_id"], str) or not MEMBER.fullmatch(options["member_id"])))):
        raise ScientificPreviewRejected("成员预览只接受当前目录给出的不透明标识与有界分页索引。")


def validate_archive_member_payload(value):
    def integer(v, maximum):
        return type(v) is int and 0 <= v <= maximum

    def label(v, maximum=512):
        return (isinstance(v, str) and len(v) <= maximum
            and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in v)
            and not re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", v, re.I))

    def name(v):
        return (label(v) and bool(v) and ":" not in v and "\\" not in v
                and all(part and part not in {".", ".."} and part[-1] not in {".", " "} for part in v.split("/")))

    common = {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled", "table"}
    if (not isinstance(value, dict) or set(value) != common or type(value["contract_version"]) is not int
            or value["contract_version"] != 2 or value["type"] != "archive-member" or value["reader"] != "archive-member"
            or value["kind"] != "table" or value["media_type"] != "application/json"
            or type(value["sampled"]) is not bool or not isinstance(value["warnings"], list)):
        raise ValueError("Invalid archive member envelope")
    meta, table = value["metadata"], value["table"]
    base = {"format", "source_bytes", "member_limit_bytes", "writes_source", "recursive", "mode"}
    if (not isinstance(meta, dict) or not base <= set(meta) or not isinstance(meta["format"], str) or meta["format"] not in {"zip", "tar"}
            or not integer(meta["source_bytes"], 64 * 1024 * 1024) or not meta["source_bytes"]
            or type(meta["member_limit_bytes"]) is not int or meta["member_limit_bytes"] != MAX_MEMBER
            or meta["writes_source"] is not False or meta["recursive"] is not False):
        raise ValueError("Invalid archive scope")
    if (not isinstance(table, dict) or set(table) != {"columns", "rows", "row_offset", "column_offset", "total_rows", "total_columns"}
            or not isinstance(table["rows"], list) or len(table["rows"]) > 200
            or not integer(table["row_offset"], MAX_MEMBER) or not integer(table["total_rows"], MAX_MEMBER + 1)
            or type(table["column_offset"]) is not int or table["column_offset"] != 0
            or type(table["total_columns"]) is not int
            or table["row_offset"] >= max(1, table["total_rows"])
            or len(table["rows"]) != min(200, table["total_rows"] - table["row_offset"])):
        raise ValueError("Invalid archive member page")
    if meta["mode"] == "directory":
        if (set(meta) != base | {"contents_verified"} or meta["contents_verified"] is not False
                or table["columns"] != DIRECTORY_COLUMNS or table["total_columns"] != 5 or table["total_rows"] > 4096
                or table["row_offset"] > 4095 or value["warnings"] != [DIRECTORY_WARNING]
                or value["sampled"] != (table["row_offset"] > 0 or len(table["rows"]) < table["total_rows"])):
            raise ValueError("Invalid archive directory")
        ids = set()
        for row in table["rows"]:
            if (not isinstance(row, list) or len(row) != 5 or not name(row[0]) or not isinstance(row[1], str) or row[1] not in {"file", "directory"}
                    or not integer(row[2], 512 * 1024 * 1024) or type(row[3]) is not bool):
                raise ValueError("Invalid archive resource")
            if row[1] == "directory" and row[2] != 0:
                raise ValueError("Archive directory must not contain data")
            if row[3]:
                if row[1] != "file" or row[2] > MAX_MEMBER or not isinstance(row[4], str) or not MEMBER.fullmatch(row[4]) or row[4] in ids:
                    raise ValueError("Invalid member reference")
                ids.add(row[4])
            elif row[4] is not None:
                raise ValueError("Ineligible archive resource")
    elif meta["mode"] == "text":
        if (set(meta) != base | {"member_id", "member_name", "member_bytes", "checksum_verified", "encoding", "line_char_limit", "member_text_only"}
                or not isinstance(meta["member_id"], str) or not MEMBER.fullmatch(meta["member_id"])
                or not name(meta["member_name"]) or not integer(meta["member_bytes"], MAX_MEMBER)
                or type(meta["checksum_verified"]) is not bool or meta["checksum_verified"] != (meta["format"] == "zip")
                or meta["encoding"] != "utf-8" or type(meta["line_char_limit"]) is not int or meta["line_char_limit"] != 1024 or meta["member_text_only"] is not True
                or table["columns"] != TEXT_COLUMNS or table["total_columns"] != 2 or not table["total_rows"]
                or value["warnings"] != [TEXT_WARNING] + ([TAR_WARNING] if meta["format"] == "tar" else [])
                or ((table["row_offset"] > 0 or len(table["rows"]) < table["total_rows"]) and not value["sampled"])):
            raise ValueError("Invalid archive text preview")
        for i, row in enumerate(table["rows"]):
            if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or row[0] != table["row_offset"] + i + 1 or not label(row[1], 1024):
                raise ValueError("Invalid archive text line")
    else:
        raise ValueError("Unknown archive member mode")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024**2:
        raise ValueError("Archive member output budget exceeded")
    return value
