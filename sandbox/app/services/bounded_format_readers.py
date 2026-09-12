"""Read-only, bounded structure and archive directory adapters.

Only bytes and validated options enter this module. No member is extracted,
no user constructor is called, and no archive content or checksum is claimed
to have been verified. The caller runs these parsers in the v2 isolated worker.
"""
from __future__ import annotations

import io
import json
import re
import stat
import struct
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from xml.parsers import expat

STRUCTURE_INPUT_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_NODES = 4096
MAX_SOURCE_DEPTH = 32
MAX_SCALAR_CHARS = 65536
MAX_NUMBER_CHARS = 256
MAX_DISPLAY_NODES = 256
MAX_DISPLAY_DEPTH = 8
MAX_MEMBERS = 4096
MAX_DECLARED_BYTES = 512 * 1024 * 1024
MAX_RATIO = 1000
MAX_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 512
PAGE_ROWS = 200
MAX_TAR_METADATA_BYTES = 65536
MAX_GZIP_HEADER_BYTES = 8192
_NESTED = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst")


class BoundedFormatError(ValueError):
    """All messages are fixed and safe to return to the client."""


@dataclass
class _Node:
    kind: str
    value: object = None
    children: list = field(default_factory=list)


@dataclass
class _Number:
    lexeme: str


def _base(reader, kind, fmt):
    return {"contract_version": 2, "type": reader, "reader": reader, "kind": kind,
            "media_type": "application/json", "metadata": {"format": fmt},
            "warnings": [], "sampled": False}


def _label(value, limit=512):
    value = str(value)
    # Sanitize before truncation, including escaped/control-obfuscated paths.
    value = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf", "Cs"})
    if re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", value, re.I):
        return "[redacted]"
    return value[:limit]


def _input(data, maximum):
    if type(data) is not bytes or not 0 < len(data) <= maximum:
        raise BoundedFormatError("文件为空或超过此预览的输入预算。")


def _text(data):
    _input(data, STRUCTURE_INPUT_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise BoundedFormatError("结构树仅支持 UTF-8 文本。") from None
    if "\x00" in text:
        raise BoundedFormatError("结构文本包含不支持的空字符。")
    return text


def _number(text):
    if len(text) > MAX_NUMBER_CHARS:
        raise BoundedFormatError("数值字面量超出安全长度。")
    return _Number(text)


def _json_preflight(text):
    """Bound nesting and token allocations before the standard JSON decoder."""
    depth = tokens = chars = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            chars += 1
            if chars > MAX_SCALAR_CHARS:
                raise BoundedFormatError("结构标量长度超过安全预算。")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted, chars = True, 0
            tokens += 1
        elif char in "[{":
            depth += 1
            tokens += 1
            if depth > MAX_SOURCE_DEPTH:
                raise BoundedFormatError("结构深度超过安全预算。")
        elif char in "]}":
            depth -= 1
        elif char == ",":
            tokens += 1
        if tokens > MAX_SOURCE_NODES * 2:
            raise BoundedFormatError("结构节点数量超过安全预算。")


def _json_tree(text):
    _json_preflight(text)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BoundedFormatError("JSON 不允许重复对象键。")
            result[key] = value
        return result

    def constant(_):
        raise BoundedFormatError("JSON 不允许非有限数值常量。")

    try:
        value = json.loads(text, parse_int=_number, parse_float=_number,
                           parse_constant=constant, object_pairs_hook=pairs)
    except (json.JSONDecodeError, RecursionError):
        raise BoundedFormatError("无法解析此 JSON 结构。") from None
    count = 0

    def walk(value, depth):
        nonlocal count
        count += 1
        if depth > MAX_SOURCE_DEPTH or count > MAX_SOURCE_NODES:
            raise BoundedFormatError("结构深度或节点数量超过安全预算。")
        if isinstance(value, dict):
            return _Node("object", children=[(k, walk(v, depth + 1)) for k, v in value.items()])
        if isinstance(value, list):
            return _Node("array", children=[(str(i), walk(v, depth + 1)) for i, v in enumerate(value)])
        if isinstance(value, _Number):
            return _Node("number", value.lexeme)
        if value is None:
            return _Node("null")
        if isinstance(value, bool):
            return _Node("boolean", value)
        return _Node("string", value)

    return walk(value, 1), count


def _yaml_tree(text):
    import yaml

    class BudgetLoader(yaml.SafeLoader):
        count = depth = 0

        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise BoundedFormatError("YAML 结构预览不允许别名或锚点。")
            event = self.peek_event()
            if getattr(event, "anchor", None) is not None:
                raise BoundedFormatError("YAML 结构预览不允许别名或锚点。")
            self.count += 1
            self.depth += 1
            if self.count > MAX_SOURCE_NODES or self.depth > MAX_SOURCE_DEPTH:
                raise BoundedFormatError("结构深度或节点数量超过安全预算。")
            if len(getattr(event, "value", "")) > MAX_SCALAR_CHARS:
                raise BoundedFormatError("结构标量长度超过安全预算。")
            try:
                node = super().compose_node(parent, index)
            finally:
                self.depth -= 1
            if node.tag not in {"tag:yaml.org,2002:" + tag for tag in
                                ("map", "seq", "str", "int", "float", "bool", "null", "timestamp")}:
                raise BoundedFormatError("YAML 包含不受支持的构造标签或合并键。")
            return node

    loader = BudgetLoader(text)
    try:
        root = loader.get_single_node()  # Compose only: never construct Python objects.
        count = loader.count
    except yaml.YAMLError:
        raise BoundedFormatError("无法解析此 YAML 单文档结构。") from None
    finally:
        loader.dispose()
    if root is None:
        raise BoundedFormatError("YAML 文档为空。")

    def walk(node):
        if isinstance(node, yaml.MappingNode):
            seen, children = set(), []
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                    raise BoundedFormatError("YAML 对象键必须是唯一的字符串。")
                if key.value in seen:
                    raise BoundedFormatError("YAML 不允许重复对象键。")
                seen.add(key.value)
                children.append((key.value, walk(value)))
            return _Node("object", children=children)
        if isinstance(node, yaml.SequenceNode):
            return _Node("array", children=[(str(i), walk(v)) for i, v in enumerate(node.value)])
        tag = node.tag.rsplit(":", 1)[-1]
        if tag in {"int", "float"}:
            value = node.value
            if len(value) > MAX_NUMBER_CHARS or value.lower().lstrip("+-") in {".inf", ".nan"}:
                raise BoundedFormatError("YAML 数值超出安全表示范围。")
            patterns = [pattern for items in yaml.SafeLoader.yaml_implicit_resolvers.values()
                        for resolved, pattern in items if resolved == node.tag]
            if not any(pattern.fullmatch(value) for pattern in patterns):
                raise BoundedFormatError("YAML 数值标签与字面量不一致。")
            return _Node("number", value)
        if tag == "null":
            if node.value not in {"", "~", "null", "Null", "NULL"}:
                raise BoundedFormatError("YAML 空值标签与字面量不一致。")
            return _Node("null")
        if tag == "bool":
            if node.value.lower() not in {"yes", "no", "true", "false", "on", "off"}:
                raise BoundedFormatError("YAML 布尔标签与字面量不一致。")
            return _Node("boolean", node.value.lower() in {"yes", "true", "on"})
        return _Node("string", node.value)

    return walk(root), count


def _xml_tree(text):
    parser = expat.ParserCreate(namespace_separator="|")
    parser.buffer_text = True
    stack, roots = [], []
    count = 0

    def blocked(*_):
        raise BoundedFormatError("XML 不允许 DTD、实体声明、外部资源或处理指令。")

    def budget(extra=1):
        nonlocal count
        count += extra
        if count > MAX_SOURCE_NODES or len(stack) >= MAX_SOURCE_DEPTH:
            raise BoundedFormatError("结构深度或节点数量超过安全预算。")

    def start(name, attrs):
        budget(1 + len(attrs))
        if len(name) > MAX_SCALAR_CHARS or any(len(k) > MAX_SCALAR_CHARS or len(v) > MAX_SCALAR_CHARS for k, v in attrs.items()):
            raise BoundedFormatError("结构标量长度超过安全预算。")
        node = _Node("element", children=[("@" + k, _Node("attribute", v)) for k, v in attrs.items()])
        (stack[-1].children if stack else roots).append((name, node))
        stack.append(node)

    def characters(value):
        if not stack or not value:
            return
        children = stack[-1].children
        if children and children[-1][1].kind == "text":
            node = children[-1][1]
            if len(node.value) + len(value) > MAX_SCALAR_CHARS:
                raise BoundedFormatError("结构标量长度超过安全预算。")
            node.value += value
        else:
            budget()
            if len(value) > MAX_SCALAR_CHARS:
                raise BoundedFormatError("结构标量长度超过安全预算。")
            children.append(("#text", _Node("text", value)))

    def declaration(_version, encoding, _standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise BoundedFormatError("结构树仅支持 UTF-8 文本。")

    parser.StartElementHandler, parser.EndElementHandler = start, lambda _: stack.pop()
    parser.CharacterDataHandler, parser.XmlDeclHandler = characters, declaration
    parser.StartDoctypeDeclHandler = blocked
    parser.EntityDeclHandler = blocked
    parser.ExternalEntityRefHandler = blocked
    parser.ProcessingInstructionHandler = blocked
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        raise BoundedFormatError("无法解析此 XML 结构。") from None
    if len(roots) != 1:
        raise BoundedFormatError("XML 必须有一个根元素。")
    return roots[0][1], count, roots[0][0]


def structure_preview(data, fmt, options=None):
    if options not in (None, {}):
        raise BoundedFormatError("结构树视图不接受执行或路径参数。")
    text = _text(data)
    root_label = "root"
    if fmt == "json":
        root, count = _json_tree(text)
    elif fmt in {"yaml", "yml"}:
        root, count = _yaml_tree(text)
    elif fmt == "xml":
        root, count, root_label = _xml_tree(text)
    else:
        raise BoundedFormatError("未注册的结构格式。")
    result = _base("structure", "tree", fmt)
    tree = []

    def display(node, path, label, depth):
        if len(tree) >= MAX_DISPLAY_NODES or depth > MAX_DISPLAY_DEPTH:
            result["sampled"] = True
            return
        attrs = {"label": _label(label), "children_count": len(node.children)}
        if len(str(label)) > 512:
            result["sampled"] = True
        if node.kind not in {"object", "array", "element"}:
            attrs["value"] = _label(node.value) if isinstance(node.value, str) else node.value
            if isinstance(node.value, str) and len(node.value) > 512:
                result["sampled"] = True
            if node.kind == "number":
                attrs["numeric_representation"] = "source lexeme"
        tree.append({"path": path, "node_type": node.kind, "attributes": attrs})
        for i, (key, child) in enumerate(node.children):
            display(child, f"{path}/{i}", key, depth + 1)

    display(root, "/0", root_label, 1)
    result["tree"] = tree
    result["metadata"].update(source_nodes=count, shown_nodes=len(tree), numeric_representation="source lexeme",
                              content_policy="inert values; no constructors, expressions or external resources",
                              display_depth_limit=MAX_DISPLAY_DEPTH, display_node_limit=MAX_DISPLAY_NODES)
    if fmt in {"yaml", "yml"}:
        result["metadata"]["yaml_schema"] = "SafeLoader YAML 1.1 scalar resolution; compose only; no constructors"
    result["warnings"].append("数值以原始字面量文本显示，避免浏览器整数或小数精度损失；不执行公式或构造器。")
    if result["sampled"]:
        result["warnings"].append("结构树或长值已截断：最多 256 个节点、8 层、每值 512 字符；可切换原文文本视图查看，当前不是完整检索。")
    return result


def _member_path(value):
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise BoundedFormatError("压缩包成员名称超出安全预算。")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value) or "\\" in value or ":" in value:
        raise BoundedFormatError("压缩包包含不安全的成员路径。")
    normalized = unicodedata.normalize("NFC", value).rstrip("/")
    parts = normalized.split("/")
    if not normalized or any(not p or p in {".", ".."} or p[-1] in {".", " "} for p in parts):
        raise BoundedFormatError("压缩包包含绝对路径、路径穿越或歧义路径。")
    return normalized


def _validate_members(members):
    if len(members) > MAX_MEMBERS:
        raise BoundedFormatError("压缩包成员数量超过 4096 项预算。")
    paths, total = {}, 0
    for member in members:
        member["path"] = _member_path(member["path"])
        canonical = member["path"].casefold()
        if canonical in paths:
            raise BoundedFormatError("压缩包包含重复或大小写冲突的成员路径。")
        paths[canonical] = member["type"]
        size, packed = member["size"], member["packed"]
        if size < 0 or size > MAX_DECLARED_BYTES or (packed is not None and (packed < 0 or size > max(1, packed) * MAX_RATIO)):
            raise BoundedFormatError("压缩包声明大小或压缩比超过安全预算。")
        total += size
        if total > MAX_DECLARED_BYTES:
            raise BoundedFormatError("压缩包累计声明展开量超过 512 MiB。")
    for path in paths:
        parts = path.split("/")
        if any(paths.get("/".join(parts[:i])) == "file" for i in range(1, len(parts))):
            raise BoundedFormatError("压缩包的文件与目录路径发生冲突。")
    return total


def _zip_members(data):
    # Bound the central directory before ZipFile allocates every ZipInfo.
    eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if eocd < 0 or eocd + 22 > len(data):
        raise BoundedFormatError("ZIP 目录不完整。")
    disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from("<4H2IH", data, eocd + 4)
    if (disk or cd_disk or count != disk_count or count > MAX_MEMBERS or count == 65535
            or cd_size > MAX_DIRECTORY_BYTES or cd_offset + cd_size != eocd or eocd + 22 + comment != len(data)
            or (count and not data.startswith(b"PK\x03\x04"))
            or (not count and (cd_offset or cd_size))):
        raise BoundedFormatError("ZIP 目录超出预算，或为不支持的分卷、ZIP64、自解压结构。")
    position, scanned = cd_offset, 0
    while position < eocd:
        if position + 46 > eocd or data[position:position + 4] != b"PK\x01\x02":
            raise BoundedFormatError("ZIP 中央目录记录不完整。")
        packed, unpacked = struct.unpack_from("<II", data, position + 20)
        name_len, extra_len, comment_len, member_disk = struct.unpack_from("<4H", data, position + 28)
        if member_disk or packed == 0xFFFFFFFF or unpacked == 0xFFFFFFFF:
            raise BoundedFormatError("ZIP64 或分卷成员不在目录预览支持范围。")
        position += 46 + name_len + extra_len + comment_len
        scanned += 1
        if scanned > MAX_MEMBERS or scanned > count or position > eocd:
            raise BoundedFormatError("ZIP 中央目录数量或长度超过安全预算。")
    if scanned != count:
        raise BoundedFormatError("ZIP 成员数与目录声明不一致。")
    members, ranges = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) != count:
                raise BoundedFormatError("ZIP 成员数与目录声明不一致。")
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if (info.flag_bits & 0x41 or info.compress_type not in {0, 8, 12, 14}
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise BoundedFormatError("ZIP 含加密、符号链接、特殊文件或不支持的压缩方法。")
                start = info.header_offset
                if start < 0 or start + 30 > cd_offset or data[start:start + 4] != b"PK\x03\x04":
                    raise BoundedFormatError("ZIP 本地文件头不完整。")
                flags, method = struct.unpack_from("<HH", data, start + 6)
                name_len, extra_len = struct.unpack_from("<HH", data, start + 26)
                end = start + 30 + name_len + extra_len + info.compress_size
                name = data[start + 30:start + 30 + name_len].decode("utf-8" if flags & 0x800 else "cp437")
                if name != info.orig_filename or name != info.filename or flags != info.flag_bits or method != info.compress_type or end > cd_offset:
                    raise BoundedFormatError("ZIP 本地文件头与目录不一致。")
                if info.is_dir() and info.file_size:
                    raise BoundedFormatError("ZIP 目录成员包含异常数据。")
                if stat.S_IFMT(mode) == stat.S_IFDIR and not info.is_dir():
                    raise BoundedFormatError("ZIP 目录类型与成员名称不一致。")
                ranges.append((start, end))
                members.append({"path": info.filename, "type": "directory" if info.is_dir() else "file",
                                "size": info.file_size, "packed": info.compress_size})
    except (zipfile.BadZipFile, UnicodeError, struct.error, NotImplementedError):
        raise BoundedFormatError("无法安全读取此 ZIP 目录。") from None
    ranges.sort()
    if any(current[0] < previous[1] for previous, current in zip(ranges, ranges[1:])):
        raise BoundedFormatError("ZIP 成员数据范围发生重叠。")
    return members


def _pax(payload):
    fields, offset = {}, 0
    while offset < len(payload):
        space = payload.find(b" ", offset, min(len(payload), offset + 12))
        if space < 0 or not payload[offset:space].isdigit():
            raise BoundedFormatError("TAR 扩展元信息不完整。")
        length = int(payload[offset:space])
        end = offset + length
        if length <= space - offset + 2 or end > len(payload) or payload[end - 1:end] != b"\n":
            raise BoundedFormatError("TAR 扩展元信息长度无效。")
        try:
            key, value = payload[space + 1:end - 1].decode("utf-8").split("=", 1)
        except (UnicodeError, ValueError):
            raise BoundedFormatError("TAR 扩展元信息编码无效。") from None
        if key not in {"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname", "charset", "comment"} or key in fields:
            raise BoundedFormatError("TAR 包含不支持或重复的扩展元信息。")
        fields[key] = value
        offset = end
    return fields


def _tar_members(data):
    members, offset, headers, pending = [], 0, 0, {}
    ended = False
    while offset + 512 <= len(data):
        header = data[offset:offset + 512]
        if header == b"\x00" * 512:
            if pending or offset + 1024 > len(data) or any(data[offset:]):
                raise BoundedFormatError("TAR 结束记录无效或包含未扫描的尾随数据。")
            ended = True
            break
        headers += 1
        if headers > MAX_MEMBERS * 2:
            raise BoundedFormatError("TAR 头部扫描数量超过安全预算。")
        try:
            info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.HeaderError, UnicodeError, ValueError):
            raise BoundedFormatError("TAR 文件头不完整或校验失败。") from None
        size = info.size
        offset += 512
        if info.type in {tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME}:
            if pending or not 0 <= size <= MAX_TAR_METADATA_BYTES or offset + size > len(data):
                raise BoundedFormatError("TAR 扩展元信息超过安全预算。")
            payload = data[offset:offset + size]
            if info.type == tarfile.XHDTYPE:
                pending = _pax(payload)
            else:
                try:
                    pending = {"path": payload.rstrip(b"\x00").decode("utf-8")}
                except UnicodeError:
                    raise BoundedFormatError("TAR 长文件名编码无效。") from None
            offset += (size + 511) // 512 * 512
            continue
        if info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE} or info.linkname:
            raise BoundedFormatError("TAR 不允许链接、稀疏文件、设备或全局扩展记录。")
        if "size" in pending:
            value = pending["size"]
            if not re.fullmatch(r"[0-9]{1,12}", value):
                raise BoundedFormatError("TAR 声明大小无效。")
            size = int(value)
        if size < 0 or size > MAX_DECLARED_BYTES or offset + size > len(data) or (info.isdir() and size):
            raise BoundedFormatError("TAR 成员大小超过预算或数据已截断。")
        members.append({"path": pending.get("path", info.name), "type": "directory" if info.isdir() else "file", "size": size, "packed": None})
        if len(members) > MAX_MEMBERS:
            raise BoundedFormatError("压缩包成员数量超过 4096 项预算。")
        pending = {}
        offset += (size + 511) // 512 * 512
    if not ended:
        raise BoundedFormatError("TAR 缺少完整结束记录。")
    return members


def _gzip_header(data):
    if len(data) < 18 or data[:3] != b"\x1f\x8b\x08" or data[3] & 0xE0:
        raise BoundedFormatError("GZIP 文件头无效或不受支持。")
    flags, offset, name = data[3], 10, "gzip stream"
    if flags & 4:
        if offset + 2 > len(data):
            raise BoundedFormatError("GZIP 扩展头部不完整。")
        length = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + length
    if offset > MAX_GZIP_HEADER_BYTES or offset > len(data) - 8:
        raise BoundedFormatError("GZIP 头部超过安全预算。")
    for flag in (8, 16):
        if flags & flag:
            end = data.find(b"\x00", offset, min(len(data) - 8, MAX_GZIP_HEADER_BYTES))
            if end < 0:
                raise BoundedFormatError("GZIP 名称或注释头部不完整。")
            if flag == 8:
                name = _member_path(data[offset:end].decode("latin-1"))
            offset = end + 1
    if flags & 2:
        offset += 2
    if offset > MAX_GZIP_HEADER_BYTES or offset >= len(data) - 8:
        raise BoundedFormatError("GZIP 头部或负载不完整。")
    # Do not use the final ISIZE as a size/ratio: it is modulo 2**32 and a
    # concatenated file's final trailer may belong to another stream.
    return name, offset


def archive_preview(data, fmt, options=None):
    _input(data, MAX_INPUT_BYTES)
    options = {} if options is None else options
    if (not isinstance(options, dict) or set(options) - {"row_offset"}
            or type(options.get("row_offset", 0)) is not int or not 0 <= options.get("row_offset", 0) < MAX_MEMBERS):
        raise BoundedFormatError("压缩包目录分页参数无效。")
    row_offset = options.get("row_offset", 0)
    result = _base("archive", "table", fmt)
    if fmt == "zip":
        members = _zip_members(data)
    elif fmt == "tar":
        members = _tar_members(data)
    elif fmt in {"gz", "gzip", "tgz", "tar.gz"}:
        name, header_bytes = _gzip_header(data)
        if row_offset:
            raise BoundedFormatError("GZIP 元信息没有后续分页。")
        members = []
        rows = [[_label(name), "gzip header", None, len(data), name.lower().endswith(_NESTED) or fmt in {"tgz", "tar.gz"}]]
        total, declared = 1, None
        result["metadata"].update(header_bytes=header_bytes, stream_count=None, declared_uncompressed_bytes=None,
                                  compression_ratio=None, gzip_header_only=True)
        result["warnings"].append("GZIP 仅检查首个流头部，不解压、不验证 CRC、展开大小或流数量；拼接流及内部 TAR/科学文件不能据此确认，也不提供成员提取。")
    else:
        raise BoundedFormatError("未注册的压缩包格式。")
    if fmt in {"zip", "tar"}:
        declared = _validate_members(members)
        total = len(members)
        if row_offset >= max(1, total):
            raise BoundedFormatError("压缩包目录分页超出范围。")
        rows = [[_label(m["path"]), m["type"], m["size"], m["packed"], m["path"].lower().endswith(_NESTED)]
                for m in members[row_offset:row_offset + PAGE_ROWS]]
        result["metadata"].update(members_scanned=total, declared_uncompressed_bytes=declared,
                                  nested_policy="name hint only; no recursive inspection or extraction")
        result["warnings"].append("仅列目录；大小来自压缩包声明，未验证成员负载或校验和。嵌套压缩包只按名称标记，不展开。")
    result["table"] = {"columns": ["成员", "类型", "声明大小（字节）", "压缩大小（字节）", "嵌套压缩包"],
                       "rows": rows, "row_offset": row_offset, "column_offset": 0,
                       "total_rows": total, "total_columns": 5}
    result["sampled"] = row_offset > 0 or row_offset + len(rows) < total
    result["metadata"].update(listing_only=True, contents_verified=False, input_bytes=len(data),
                              member_limit=MAX_MEMBERS, max_declared_bytes=MAX_DECLARED_BYTES,
                              compression_ratio_limit=MAX_RATIO, scan_bytes_limit=MAX_INPUT_BYTES)
    if result["sampled"]:
        result["warnings"].append("目录已分页，每页最多 200 项；安全检查覆盖本次有界目录的全部成员。")
    return result
