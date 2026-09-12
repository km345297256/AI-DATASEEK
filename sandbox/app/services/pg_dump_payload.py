"""Pure, exact contract for PostgreSQL archive directories, never restoration."""
from __future__ import annotations

import json
import re
import unicodedata

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
FORMATS = frozenset({"pgdump", "dump", "backup", "tar"})
VERSIONS = frozenset({"1.14.0", "1.15.0", "1.16.0"})
CONTAINERS = frozenset({"PostgreSQL custom archive", "PostgreSQL tar archive"})
LIMITS = {
    "max_input_bytes": MAX_INPUT,
    "max_toc_bytes": 8 * 1024**2,
    "max_objects": 1024,
    "max_field_bytes": 1024**2,
    "max_dependencies_per_object": 256,
    "max_dependencies_total": 16384,
    "max_tar_members": 4096,
    "max_label_chars": 128,
    "max_label_bytes": 512,
}
WARNING = "仅完成归档目录读取；未解压、校验或恢复数据段。"
ERROR = "PostgreSQL 归档目录、编码、版本或读取预算不符合受限只读协议。"
OBJECT_TYPES = frozenset({
    "ACL", "ACL LANGUAGE", "AGGREGATE", "BLOB", "BLOB COMMENTS", "BLOB METADATA",
    "BLOBS", "CAST", "CHECK CONSTRAINT", "COLLATION", "COMMENT", "CONSTRAINT",
    "CONVERSION", "DATABASE", "DATABASE PROPERTIES", "DEFAULT", "DEFAULT ACL",
    "DOMAIN", "DOMAIN CONSTRAINT", "ENCODING", "EVENT TRIGGER", "EXTENSION",
    "FK CONSTRAINT", "FOREIGN DATA WRAPPER", "FOREIGN SERVER", "FOREIGN TABLE",
    "FUNCTION", "INDEX", "INDEX ATTACH", "MATERIALIZED VIEW",
    "MATERIALIZED VIEW DATA", "OPERATOR", "OPERATOR CLASS", "OPERATOR FAMILY",
    "POLICY", "PROCEDURAL LANGUAGE", "PROCEDURE", "PUBLICATION",
    "PUBLICATION TABLE", "PUBLICATION TABLES IN SCHEMA", "ROW SECURITY", "RULE",
    "SCHEMA", "SEARCHPATH", "SECURITY LABEL", "SEQUENCE", "SEQUENCE OWNED BY",
    "SEQUENCE SET", "SHELL TYPE", "STATISTICS", "STATISTICS DATA", "STDSTRINGS",
    "SUBSCRIPTION", "SUBSCRIPTION TABLE", "TABLE", "TABLE ATTACH", "TABLE DATA",
    "TABLE DATA STATISTICS", "TABLESPACE", "TEXT SEARCH CONFIGURATION",
    "TEXT SEARCH DICTIONARY", "TEXT SEARCH PARSER", "TEXT SEARCH TEMPLATE",
    "TRANSFORM", "TRIGGER", "TYPE", "USER MAPPING", "VIEW", "OTHER",
})
# Generic tags can encode database names, role names or foreign-server details.
# Only these known object kinds have identifiers we intentionally render.
PUBLIC_NAME_TYPES = frozenset({
    "ENCODING", "STDSTRINGS", "SEARCHPATH", "SCHEMA", "TABLE", "TABLE DATA",
    "TABLE ATTACH", "VIEW", "MATERIALIZED VIEW", "MATERIALIZED VIEW DATA",
    "INDEX", "INDEX ATTACH", "CONSTRAINT", "CHECK CONSTRAINT", "FK CONSTRAINT",
    "DEFAULT", "FUNCTION", "PROCEDURE", "AGGREGATE", "TRIGGER", "RULE",
    "TYPE", "SHELL TYPE", "DOMAIN", "DOMAIN CONSTRAINT", "SEQUENCE",
    "SEQUENCE SET", "SEQUENCE OWNED BY", "STATISTICS", "STATISTICS DATA",
    "TABLE DATA STATISTICS", "COLLATION", "CONVERSION", "OPERATOR",
    "OPERATOR CLASS", "OPERATOR FAMILY", "TEXT SEARCH CONFIGURATION",
    "TEXT SEARCH DICTIONARY", "TEXT SEARCH PARSER", "TEXT SEARCH TEMPLATE",
})
_LABEL = re.compile(r"[\w .()@,+-]+\Z", re.UNICODE)


class PGDumpError(ValueError):
    pass


def need(condition):
    if not condition:
        raise PGDumpError(ERROR)


def keys(value, names):
    return type(value) is dict and set(value) == set(names.split())


def integer(value, minimum=0, maximum=MAX_INPUT):
    return type(value) is int and minimum <= value <= maximum


def safe_label(value):
    if type(value) is not str or not 1 <= len(value) <= 128:
        return False
    if value != value.strip() or _LABEL.fullmatch(value) is None:
        return False
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 512
    except UnicodeError:
        return False


def display_label(value, *, schema=False):
    if schema and value in {None, ""}:
        return "无命名空间"
    if safe_label(value):
        return value
    return "命名空间已隐藏" if schema else "名称已隐藏"


def validate_pg_dump_options(kind, options):
    need(type(kind) is str and kind == "tree" and keys(options, ""))
    return {}


def validate_pg_dump_payload(value, *, kind=None, options=None, fmt=None,
                             size=None, limit=MAX_OUTPUT):
    try:
        need(keys(value, "type reader kind contract_version media_type tree metadata warnings sampled choices selected"))
        need(value["type"] == value["reader"] == "pg-dump")
        need(type(value["contract_version"]) is int and value["contract_version"] == 2)
        need(value["media_type"] == "application/json")
        validate_pg_dump_options(value["kind"], value["selected"])
        need(kind is None or kind == "tree")
        if options is not None:
            validate_pg_dump_options("tree", options)
        need(keys(value["choices"], "") and value["sampled"] is False)
        need(type(value["warnings"]) is list and value["warnings"] == [WARNING])
        metadata = value["metadata"]
        need(keys(metadata, "engine format container archive_version source_bytes input_mode objects_returned objects_total data_verified limits"))
        need(metadata["engine"] == "pg-dump" and metadata["input_mode"] == "whole")
        need(type(metadata["format"]) is str and metadata["format"] in FORMATS)
        need(fmt is None or type(fmt) is str and metadata["format"] == fmt)
        need(type(metadata["container"]) is str and metadata["container"] in CONTAINERS)
        need(type(metadata["archive_version"]) is str and metadata["archive_version"] in VERSIONS)
        need(integer(metadata["source_bytes"], 64) and (size is None or type(size) is int and metadata["source_bytes"] == size))
        need(metadata["data_verified"] is False)
        need(keys(metadata["limits"], " ".join(LIMITS)) and metadata["limits"] == LIMITS)
        need(all(type(v) is int for v in metadata["limits"].values()))
        tree = value["tree"]
        need(type(tree) is list and 1 <= len(tree) <= LIMITS["max_objects"])
        need(integer(metadata["objects_total"], len(tree), len(tree)))
        need(integer(metadata["objects_returned"], len(tree), len(tree)))
        paths = set()
        for node in tree:
            need(keys(node, "path node_type attributes") and node["node_type"] == "group")
            path = node["path"]
            need(type(path) is str and re.fullmatch(r"/o-[0-9a-f]{24}", path) is not None and path not in paths)
            paths.add(path)
            attrs = node["attributes"]
            need(keys(attrs, "object_type schema name"))
            need(type(attrs["object_type"]) is str and attrs["object_type"] in OBJECT_TYPES)
            need(safe_label(attrs["schema"]) and safe_label(attrs["name"]))
            if attrs["object_type"] not in PUBLIC_NAME_TYPES:
                need(attrs["name"] == "名称已隐藏" and attrs["schema"] == "无命名空间")
        need(integer(limit, 1, MAX_OUTPUT))
        need(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")) <= limit)
        return value
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, AttributeError):
        raise PGDumpError(ERROR) from None
