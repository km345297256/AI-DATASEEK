"""Small, read-only format evidence, never an image/scientific data decoder.

Only ``probe_visualization_file`` is an authorization boundary. The lower-level
callback API deliberately knows neither filenames nor paths and must only receive
an already authorized range reader. A magic/tag match is routing evidence, not a
promise that the eventual scientific reader supports every encoding or axis.
"""
from __future__ import annotations

import asyncio
import re
import struct
from collections.abc import Awaitable, Callable
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

MAX_PROBE_BYTES = 64 * 1024
MAX_PROBE_READS = 64
MAX_HDF5_USERBLOCK = 1024 * 1024
MAX_IFDS = 8
MAX_IFD_ENTRIES = 256
MAX_TOTAL_ENTRIES = 512
MAX_DESCRIPTION_BYTES = 16 * 1024
PROBE_BLOCK_BYTES = 4096
_PROBE_SLOTS = asyncio.Semaphore(2)
_HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
_PROBE_ADAPTERS = {"h5web", "plotly", "tiff", "openlayers", "viv", "scientific-map", "scientific-series"}

Evidence = Literal[
    "netcdf-cdf1-magic", "netcdf-cdf2-magic", "netcdf-cdf5-magic",
    "hdf5-signature", "hdf5-userblock-signature", "matlab-level5-header",
    "matlab-v7.3-header", "tiff-header", "bigtiff-header", "tiff-geokeys",
    "tiff-geotransform", "tiff-ome-xml",
]


class ContentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    profile_version: Literal[1] = 1
    container: Literal["unknown", "netcdf", "hdf5", "mat", "tiff"] = "unknown"
    dialect: Literal[
        "unknown", "netcdf-classic", "netcdf-64bit-offset", "netcdf-cdf5",
        "hdf5", "matlab-level5", "matlab-v7.3", "tiff", "bigtiff",
    ] = "unknown"
    traits: list[Literal["numeric-container", "geotiff", "ome"]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    bytes_read: int = Field(default=0, ge=0, le=MAX_PROBE_BYTES)
    truncated: bool = False


class AuthorizedContentProfile(ContentProfile):
    plugin_id: str
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisualizationProbeRejected(ValueError):
    """Malformed metadata or unsupported probe operation; all messages are safe."""


class _BudgetExceeded(Exception):
    pass


class _Ranges:
    def __init__(self, read: Callable[[int, int], Awaitable[bytes]], size: int, max_bytes: int):
        self.read = read
        self.size = size
        self.max_bytes = max_bytes
        self.bytes_read = 0
        self.reads = 0
        self.cache: list[tuple[int, bytes]] = []

    def span(self, offset: int, length: int):
        if (type(offset) is not int or type(length) is not int or offset < 0
                or length < 0 or offset > self.size or length > self.size - offset):
            raise VisualizationProbeRejected("文件元数据包含越界偏移或截断的结构。")

    async def get(self, offset: int, length: int) -> bytes:
        self.span(offset, length)
        if not length:
            return b""
        for start, data in self.cache:
            if start <= offset and offset + length <= start + len(data):
                return data[offset - start:offset - start + length]
        if self.bytes_read + length > self.max_bytes or self.reads >= MAX_PROBE_READS:
            raise _BudgetExceeded()
        # Dataset storage may start a safe native helper for every range. Read
        # a small nearby block once so common TIFF tags / a 512-byte HDF5 user
        # block cost one request, not one helper per tag. Never exceed the total
        # metadata budget merely to fill a block; exact reads remain a fallback.
        start = offset // PROBE_BLOCK_BYTES * PROBE_BLOCK_BYTES
        end = min(self.size, max(start + PROBE_BLOCK_BYTES, offset + length))
        if self.bytes_read + end - start > self.max_bytes:
            start, end = offset, offset + length
        fetched = end - start
        self.reads += 1
        self.bytes_read += fetched
        data = await self.read(start, fetched)
        if not isinstance(data, bytes) or len(data) != fetched:
            raise VisualizationProbeRejected("文件元数据范围读取不完整。")
        self.cache.append((start, data))
        return data[offset - start:offset - start + length]


def _ome_description(raw: bytes) -> bool:
    """Bounded XML metadata only; do not return descriptions or follow links."""
    raw = raw.rstrip(b"\0")
    if not raw or b"\0" in raw:
        return False
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise VisualizationProbeRejected("图像描述不允许 XML 实体或文档类型声明。")
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError):
        return False
    namespace = r"\{https?://www\.openmicroscopy\.org/Schemas/OME/\d{4}-\d{2}\}"
    if not re.fullmatch(namespace + "OME", root.tag):
        return False
    nodes = list(root.iter())
    if len(nodes) > 512:
        raise VisualizationProbeRejected("图像描述的 XML 节点数量超过探测上限。")
    return any(re.fullmatch(namespace + "Pixels", node.tag) for node in nodes)


async def _tiff_profile(ranges: _Ranges, header: bytes, profile: ContentProfile) -> ContentProfile:
    endian = "<" if header[:2] == b"II" else ">"
    if len(header) < 8:
        raise VisualizationProbeRejected("TIFF 文件头不完整。")
    version = struct.unpack_from(endian + "H", header, 2)[0]
    if version == 42:
        header_size, count_size, entry_size, inline_size = 8, 2, 12, 4
        count_code, pointer_code = "H", "I"
        offset = struct.unpack_from(endian + "I", header, 4)[0]
        dialect, evidence = "tiff", "tiff-header"
    elif version == 43:
        if len(header) < 16 or struct.unpack_from(endian + "HH", header, 4) != (8, 0):
            raise VisualizationProbeRejected("BigTIFF 文件头或偏移宽度无效。")
        header_size, count_size, entry_size, inline_size = 16, 8, 20, 8
        count_code = pointer_code = "Q"
        offset = struct.unpack_from(endian + "Q", header, 8)[0]
        dialect, evidence = "bigtiff", "bigtiff-header"
    else:
        return profile
    profile = profile.model_copy(update={"container": "tiff", "dialect": dialect, "evidence": [evidence]})
    queue = [offset] if offset else []
    seen: set[int] = set()
    total_entries = 0
    # Standard TIFF value widths; unknown future types are never dereferenced.
    widths = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
              10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
    traits, evidence_items = set(profile.traits), list(profile.evidence)
    truncated = False
    try:
        while queue:
            current = queue.pop(0)
            if current in seen:
                raise VisualizationProbeRejected("TIFF 目录包含循环或重复偏移。")
            if current < header_size:
                raise VisualizationProbeRejected("TIFF 目录偏移落在文件头内。")
            ranges.span(current, count_size)
            if len(seen) >= MAX_IFDS:
                truncated = True
                break
            seen.add(current)
            count = struct.unpack(endian + count_code, await ranges.get(current, count_size))[0]
            # Validate even a refused directory's declared span without reading it.
            ranges.span(current + count_size, count * entry_size + inline_size)
            if count > MAX_IFD_ENTRIES or total_entries + count > MAX_TOTAL_ENTRIES:
                truncated = True
                break
            total_entries += count
            block = await ranges.get(current + count_size, count * entry_size + inline_size)
            tags: dict[int, tuple[int, int, bytes, int]] = {}
            for index in range(count):
                start = index * entry_size
                tag, dtype = struct.unpack_from(endian + "HH", block, start)
                number = struct.unpack_from(endian + pointer_code, block, start + 4)[0]
                value_start = start + (12 if version == 43 else 8)
                inline = block[value_start:value_start + inline_size]
                if tag in tags:
                    raise VisualizationProbeRejected("TIFF 目录包含重复标签。")
                width = widths.get(dtype)
                if width is None:
                    truncated = True
                    continue
                length = number * width
                value_offset = struct.unpack(endian + pointer_code, inline)[0] if length > inline_size else -1
                if value_offset >= 0:
                    ranges.span(value_offset, length)
                    if value_offset < header_size:
                        raise VisualizationProbeRejected("TIFF 标签数据偏移落在文件头内。")
                tags[tag] = (dtype, number, inline[:length], value_offset)

            async def value(tag: int, maximum: int) -> bytes | None:
                nonlocal truncated
                dtype, number, inline, location = tags[tag]
                length = number * widths[dtype]
                if length > maximum:
                    truncated = True
                    return None
                return await ranges.get(location, length) if location >= 0 else inline

            if 270 in tags and tags[270][0] == 2:
                description = await value(270, MAX_DESCRIPTION_BYTES)
                if description is not None and _ome_description(description):
                    traits.add("ome")
                    evidence_items.append("tiff-ome-xml")
            if 34735 in tags and tags[34735][0] == 3:
                keys = await value(34735, 4096)
                if keys is not None:
                    if len(keys) < 8 or len(keys) % 2:
                        raise VisualizationProbeRejected("GeoTIFF 键目录不完整。")
                    revision, major, minor, key_count = struct.unpack_from(endian + "4H", keys)
                    if revision != 1 or major != 1 or minor not in {0, 1} or 8 + key_count * 8 > len(keys):
                        raise VisualizationProbeRejected("GeoTIFF 键目录版本或长度无效。")
                    if key_count:
                        traits.add("geotiff")
                        evidence_items.append("tiff-geokeys")
            # These signatures identify georeferencing metadata, not a supported
            # CRS. The actual map reader still validates finite values and CRS.
            if ((33550 in tags and tags[33550][:2] == (12, 3)
                 and 33922 in tags and tags[33922][0] == 12 and tags[33922][1] >= 6 and tags[33922][1] % 6 == 0)
                    or (34264 in tags and tags[34264][:2] == (12, 16))):
                traits.add("geotiff")
                evidence_items.append("tiff-geotransform")
            following = struct.unpack_from(endian + pointer_code, block, count * entry_size)[0]
            children = []
            if 330 in tags:
                dtype, number, _, _ = tags[330]
                if dtype not in {4, 13, 16, 18}:
                    raise VisualizationProbeRejected("TIFF 子目录偏移类型无效。")
                data = await value(330, MAX_IFDS * 8)
                if data is not None:
                    code = "I" if widths[dtype] == 4 else "Q"
                    children = list(struct.unpack(endian + str(number) + code, data))
            for target in [following, *children]:
                if target:
                    if target in seen or target in queue:
                        raise VisualizationProbeRejected("TIFF 目录包含循环或重复偏移。")
                    ranges.span(target, count_size)
                    queue.append(target)
    except _BudgetExceeded:
        truncated = True
    return profile.model_copy(update={"traits": sorted(traits), "evidence": list(dict.fromkeys(evidence_items)), "truncated": truncated})


async def probe_visualization_content(read_range: Callable[[int, int], Awaitable[bytes]], size: int,
                                      *, max_bytes: int = MAX_PROBE_BYTES) -> ContentProfile:
    """Probe trusted range access. No filename inference, arbitrary paths or codecs.

    HDF5 containers include NetCDF4, but magic alone cannot prove that semantic
    variant. ``dialect=hdf5`` intentionally leaves it for the isolated reader.
    ``truncated`` means missing evidence is inconclusive, never a negative match.
    """
    if type(size) is not int or not 0 <= size <= 2**63 - 1:
        raise VisualizationProbeRejected("文件大小不适合有界内容探测。")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PROBE_BYTES:
        raise VisualizationProbeRejected("内容探测读取预算无效。")
    ranges = _Ranges(read_range, size, max_bytes)
    result = ContentProfile()
    try:
        header = await ranges.get(0, min(size, 128))
        cdf = {b"CDF\x01": ("netcdf-classic", "netcdf-cdf1-magic"),
               b"CDF\x02": ("netcdf-64bit-offset", "netcdf-cdf2-magic"),
               b"CDF\x05": ("netcdf-cdf5", "netcdf-cdf5-magic")}
        if header[:4] in cdf:
            dialect, evidence = cdf[header[:4]]
            result = ContentProfile(container="netcdf", dialect=dialect, traits=["numeric-container"], evidence=[evidence])
        elif header[:2] in {b"II", b"MM"}:
            result = await _tiff_profile(ranges, header, result)
        else:
            matlab73 = header.startswith(b"MATLAB 7.3 MAT-file")
            matlab5 = header.startswith(b"MATLAB 5.0 MAT-file")
            if matlab5 or matlab73:
                if len(header) < 128 or header[126:128] not in {b"IM", b"MI"}:
                    raise VisualizationProbeRejected("MATLAB 文件头不完整或字节序标识无效。")
                endian = "<" if header[126:128] == b"IM" else ">"
                expected = 0x0200 if matlab73 else 0x0100
                if struct.unpack_from(endian + "H", header, 124)[0] != expected:
                    raise VisualizationProbeRejected("MATLAB 文件头版本无效。")
            if matlab5:
                result = ContentProfile(container="mat", dialect="matlab-level5", traits=["numeric-container"], evidence=["matlab-level5-header"])
            else:
                offsets = [0] + [2**power for power in range(9, 21)]
                signature = None
                for offset in offsets:
                    if offset > MAX_HDF5_USERBLOCK or offset + 8 > size:
                        break
                    if await ranges.get(offset, 8) == _HDF5_MAGIC:
                        signature = offset
                        break
                if signature is not None:
                    evidence = "hdf5-userblock-signature" if signature else "hdf5-signature"
                    is_mat73 = matlab73 and signature >= 512
                    result = ContentProfile(container="hdf5", dialect="matlab-v7.3" if is_mat73 else "hdf5",
                        traits=["numeric-container"], evidence=(["matlab-v7.3-header"] if is_mat73 else []) + [evidence])
                elif matlab73:
                    # Never misroute a declared 7.3 file to scipy's level-5 path.
                    result = ContentProfile(evidence=["matlab-v7.3-header"], truncated=True)
                elif size > MAX_HDF5_USERBLOCK + 8:
                    result = ContentProfile(truncated=True)
    except _BudgetExceeded:
        result = result.model_copy(update={"truncated": True})
    return result.model_copy(update={"bytes_read": ranges.bytes_read})


async def probe_visualization_file(file_service, catalog, file_id: str, user_id: str,
                                   plugin_id: str, *, version: str | None = None) -> AuthorizedContentProfile:
    """Owner, explicit enabled capability and revision fences before *any* bytes.

    No stateful result cache or ordinary download fallback. Cancellation holds
    admission until a possibly native storage read has actually completed.
    The caller owns the public unified VisualizationResult envelope.
    """
    from app.application.services.file_preview import PreviewVersionChanged, preview_version
    from app.application.services.file_service import _is_private_spill
    from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError

    plugin = await catalog.require_enabled(user_id, plugin_id)
    if plugin.adapter not in _PROBE_ADAPTERS or "prepare" not in plugin.capabilities.operations:
        raise VisualizationProbeRejected("此插件未声明高频科学格式内容探测能力。")
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    if not plugin.matches_filename(info.filename or ""):
        raise VisualizationProbeRejected("此插件不支持当前文件的内容探测。")
    pinned = preview_version(info)
    if version is not None and version != pinned:
        raise PreviewVersionChanged()
    snapshot = await catalog.list_for_user(user_id)
    revision = snapshot.revision

    async def fence():
        current = await file_service.get_file_info(file_id, user_id)
        if current is None or _is_private_spill(current):
            raise FileNotFoundError("File not found")
        if preview_version(current) != pinned:
            raise PreviewVersionChanged()
        current_snapshot = await catalog.list_for_user(user_id)
        active = next((item for item in current_snapshot.plugins if item.id == plugin_id), None)
        if active is None:
            raise VisualizationNotFoundError("Visualization plugin not found")
        if not active.enabled:
            raise VisualizationDisabledError("Visualization plugin is disabled")
        if current_snapshot.revision != revision or active.model_dump(exclude={"enabled"}) != plugin.model_dump():
            raise PreviewVersionChanged()

    await fence()
    read = getattr(file_service._file_storage, "download_file_range", None)
    if not callable(read):
        raise NotImplementedError("Bounded storage required")
    try:
        await asyncio.wait_for(_PROBE_SLOTS.acquire(), timeout=5)
    except TimeoutError:
        raise VisualizationProbeRejected("内容探测繁忙，请稍后重试。") from None
    release_here = True

    async def authorized_range(offset: int, length: int) -> bytes:
        nonlocal release_here
        await fence()
        operation = asyncio.create_task(read(file_id, user_id, offset=offset, length=length))
        try:
            data, ranged = await asyncio.shield(operation)
        except asyncio.CancelledError:
            release_here = False
            def complete(task):
                _PROBE_SLOTS.release()
                if not task.cancelled():
                    task.exception()
            operation.add_done_callback(complete)
            raise
        if _is_private_spill(ranged):
            raise FileNotFoundError("File not found")
        if preview_version(ranged) != pinned:
            raise PreviewVersionChanged()
        await fence()
        return data

    try:
        async with asyncio.timeout(20):
            result = await probe_visualization_content(authorized_range, info.size,
                max_bytes=min(MAX_PROBE_BYTES, plugin.limits.max_input_bytes))
            await fence()
        return AuthorizedContentProfile(**result.model_dump(), plugin_id=plugin_id, version=pinned, revision=revision)
    except TimeoutError:
        raise VisualizationProbeRejected("内容探测已超过读取时限，请稍后重试。") from None
    finally:
        if release_here:
            _PROBE_SLOTS.release()
