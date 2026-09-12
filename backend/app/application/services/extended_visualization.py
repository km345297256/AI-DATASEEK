"""Contract v2: owner- and capability-scoped, bounded offline previews.

Deliberately independent of AgentLoop and SSE. Native parsers only run in a
one-shot container with no dataset mounts, credentials or network access.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import _is_private_spill
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.infrastructure.external.sandbox.extended_visualization_worker import run_extended_visualization_worker
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError
from app.application.services import main_migration_visualization as migration

MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
_SLOTS = asyncio.Semaphore(2)
_KINDS = {
    "sql-dump": {"tree", "table"}, "pg-dump": {"tree"},
    "bson": {"tree", "table"}, "redis-rdb": {"tree", "table"},
    "database-table": {"tree", "table"},
    "sqlite-table": {"tree", "table"},
    "gro-trajectory": {"tree", "geometry"}, "simulation-mesh": {"tree", "geometry"},
    "mass-spectrum": {"tree", "series"}, "diffraction": {"tree", "series"},
    "phylogeny": {"tree"},
    "scientific-graph": {"graph"},
    "tabular": {"table", "series", "heatmap"}, "hdf5": {"tree", "series", "heatmap"},
    "excel": {"table"}, "rdkit": {"image"}, "metpy": {"image"},
    "office": {"pdf"}, "fastqc": {"report"},
    "root": {"series", "heatmap"}, "jcamp": {"series"},
    "structure": {"tree"}, "archive": {"table"}, "archive-member": {"table"},
    "geoformat": {"map"}, "czi": {"tree", "image"}, "mca": {"series"},
}
_OPTIONS = {
    "sql-dump": {"dialect", "table", "columns", "row_offset", "row_limit"}, "pg-dump": set(),
    "bson": {"group_id", "offset", "limit"}, "redis-rdb": {"group_id", "offset", "limit"},
    "database-table": {"table", "columns", "row_offset", "row_limit"},
    "sqlite-table": {"table", "columns", "row_offset", "row_limit"},
    "gro-trajectory": {"frame"}, "simulation-mesh": {"field", "component"},
    "mass-spectrum": {"offset", "spectrum"}, "diffraction": {"scan"},
    "phylogeny": set(),
    "scientific-graph": set(),
    "tabular": {"variable", "row_offset", "column_offset", "x_column", "y_columns", "indices"},
    "hdf5": {"path", "indices"}, "excel": {"sheet", "row_offset", "column_offset"},
    "rdkit": {"molecule"}, "metpy": {"pressure_column", "temperature_column", "dewpoint_column", "pressure_unit", "temperature_unit", "dewpoint_unit"},
    "office": set(), "fastqc": {"confirm"}, "binary": set(),
    "root": {"path"}, "jcamp": set(),
    "structure": set(), "archive": {"row_offset"}, "archive-member": {"row_offset", "member_id"},
    "geoformat": {"crs"}, "czi": {"indices", "roi"}, "mca": set(),
}
_DEFAULT_KIND = {"tabular": "table", "hdf5": "tree", "excel": "table", "rdkit": "image", "metpy": "image", "office": "pdf", "fastqc": "report", "root": "series", "jcamp": "series", "structure": "tree", "archive": "table", "archive-member": "table"}
_DEFAULT_KIND.update({"geoformat": "map", "czi": "tree", "mca": "series", "scientific-graph": "graph"})
_DEFAULT_KIND["phylogeny"] = "tree"
_DEFAULT_KIND["sqlite-table"] = "tree"
_DEFAULT_KIND["database-table"] = "tree"
_DEFAULT_KIND.update({reader: "tree" for reader in ("sql-dump", "pg-dump", "bson", "redis-rdb")})
_DEFAULT_KIND.update({"mass-spectrum": "tree", "diffraction": "tree"})
_DEFAULT_KIND.update({"gro-trajectory": "tree", "simulation-mesh": "tree"})
_KINDS.update(migration.KINDS)
_DEFAULT_KIND.update({reader: "tree" for reader in migration.KINDS})


class ExtendedPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["table", "series", "heatmap", "tree", "image", "pdf", "report", "map", "graph", "geometry"] | None = None
    options: dict[str, Any] = Field(default_factory=dict, max_length=10)


def validate_options(reader: str, options: dict) -> None:
    if reader in {"sql-dump", "pg-dump", "bson", "redis-rdb"}:
        return  # Exact private contract is checked before source lookup below.
    if reader in migration.KINDS:
        return  # Explicit reader/kind validator runs before source IO below.
    if reader in {"mass-spectrum", "diffraction", "gro-trajectory", "simulation-mesh", "sqlite-table", "database-table"}:
        return  # Kind-specific validation below, before file lookup or IO.
    if reader == "phylogeny":
        from app.application.services.phylogeny_visualization import validate_phylogeny_options
        try:
            validate_phylogeny_options("tree", options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("系统发育树不接受额外执行参数。") from None
        return
    if reader == "scientific-graph":
        from app.application.services.scientific_graph_visualization import validate_scientific_graph_options
        try:
            validate_scientific_graph_options("graph", options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("关系网络不接受额外读取或执行参数。") from None
        return
    if reader in {"geoformat", "mca"}:
        try:
            if reader == "geoformat":
                from app.application.services.geo_visualization import validate_geo_options
                validate_geo_options(options)
            else:
                from app.application.services.mca_visualization import validate_mca_options
                validate_mca_options(options)
        except ValueError:
            raise ScientificPreviewRejected("此读取器不接受这些参数。") from None
        return
    if reader == "czi":
        # The kind-specific C/Z/T and ROI contract is checked before file I/O.
        return
    if reader == "archive-member":
        from app.application.services.archive_member_visualization import validate_archive_member_options
        validate_archive_member_options(options)
        return
    if set(options) - _OPTIONS.get(reader, set()):
        raise ScientificPreviewRejected("此读取器不接受这些参数。")
    if reader == "archive" and "row_offset" in options and (type(options["row_offset"]) is not int or not 0 <= options["row_offset"] <= 4095):
        raise ScientificPreviewRejected("目录分页索引超出范围。")
    for key, value in options.items():
        if key in {"row_offset", "column_offset", "x_column", "molecule"}:
            maximum = {"row_offset": 100000, "column_offset": 16383, "x_column": 99, "molecule": 99}[key]
            if type(value) is not int or not 0 <= value <= maximum:
                raise ScientificPreviewRejected("预览索引超出范围。")
        elif key in {"indices", "y_columns"}:
            if not isinstance(value, list) or len(value) > (6 if key == "indices" else 8) or any(type(v) is not int or v < 0 or v > (2**31 - 1 if key == "indices" else 99) for v in value):
                raise ScientificPreviewRejected("预览切片无效。")
        elif key == "confirm":
            if value is not True:
                raise ScientificPreviewRejected("质控任务需要明确确认。")
        elif not isinstance(value, str) or not value or len(value) > (512 if key == "path" else 256) or any(ord(c) < 32 for c in value) or "\\" in value:
            raise ScientificPreviewRejected("预览参数无效。")
        elif key == "path":
            if (reader == "hdf5" and not value.startswith("/")) or ".." in value.split("/"):
                raise ScientificPreviewRejected("仅支持 HDF5 内部节点路径。")
        elif "/" in value:
            raise ScientificPreviewRejected("预览参数不能包含文件路径。")
    if reader == "metpy":
        if set(options) != _OPTIONS[reader] or options["pressure_unit"] not in {"Pa", "hPa"} or any(options[key] not in {"K", "degC"} for key in ("temperature_unit", "dewpoint_unit")):
            raise ScientificPreviewRejected("请明确选择气压、温度、露点列及其单位。")
    if reader == "fastqc" and options.get("confirm") is not True:
        raise ScientificPreviewRejected("质控任务需要明确确认。")


def validate_payload(payload: Any, reader: str, kind: str, limit: int) -> dict:
    if reader in {"sql-dump", "pg-dump", "bson", "redis-rdb"}:
        from app.application.services.database_file_visualization import validate_payload as validate_database_file
        return validate_database_file(payload, reader, kind, limit=limit)
    if reader == "database-table":
        from app.application.services.database_table_visualization import validate_database_table_payload
        return validate_database_table_payload(payload, kind=kind, limit=limit)
    if reader in migration.KINDS:
        # Dedicated exact schemas bound nested planes, sequences and tracks;
        # none of the generic legacy limits are widened for these payloads.
        return migration.validate_payload(payload, reader, kind, limit=limit)
    if not isinstance(payload, dict) or type(payload.get("contract_version")) is not int or payload.get("contract_version") != 2 or payload.get("type") != reader or payload.get("reader") != reader or payload.get("kind") != kind:
        raise ValueError("Invalid preview envelope")
    allowed = {"contract_version", "type", "reader", "kind", "media_type", "data_base64", "table", "array", "tree", "choices", "selected", "metadata", "warnings", "sampled", "sections"}
    if reader == "geoformat":
        allowed.add("geojson")
    if reader == "scientific-graph":
        allowed.add("graph")
    if reader == "phylogeny":
        allowed.add("phylogeny")
    if reader == "diffraction":
        allowed.add("series")
    if reader == "gro-trajectory":
        allowed.add("trajectory")
    if reader == "simulation-mesh":
        allowed.add("mesh")
    if set(payload) - allowed:
        raise ValueError("Unknown payload fields")
    if (type(payload.get("sampled")) is not bool or not isinstance(payload.get("metadata"), dict)
        or not isinstance(payload.get("warnings"), list) or len(payload["warnings"]) > 32
        or any(not isinstance(value, str) for value in payload["warnings"])):
        raise ValueError("Invalid preview diagnostics")
    count = 0
    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > (200000 if reader == "gro-trajectory" else 100000):
            raise ValueError("Preview complexity exceeded")
        if isinstance(value, str):
            if len(value) > 2048 or re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|[A-Za-z]:\\)", value):
                raise ValueError("Unsafe preview label")
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Nonfinite preview")
        elif value is not None and type(value) not in {bool, int}:
            raise ValueError("Unsupported preview value")
    visit({key: value for key, value in payload.items() if key != "data_base64"})
    if "data_base64" in payload:
        encoded = payload["data_base64"]
        if not isinstance(encoded, str) or len(encoded) > limit:
            raise ValueError("Invalid binary preview")
        decoded = base64.b64decode(encoded, validate=True)
        signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "application/pdf": b"%PDF-"}
        signature = signatures.get(payload.get("media_type"))
        if not signature or not decoded.startswith(signature):
            raise ValueError("Unsupported preview media")
    if "table" in payload:
        table = payload["table"]
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or len(table["columns"]) > 100 or not isinstance(table.get("rows"), list) or len(table["rows"]) > 200 or any(not isinstance(row, list) or len(row) > 100 for row in table["rows"]):
            raise ValueError("Invalid table window")
    if "array" in payload:
        array = payload["array"]
        # The approved mass spectrum is N explicit m/z-intensity pairs. Do not
        # widen any legacy numeric array budget.
        scalar_limit = 32768 if reader == "mass-spectrum" else 16384
        if not isinstance(array, dict) or not isinstance(array.get("shape"), list) or len(array["shape"]) > 8 or not isinstance(array.get("values"), list) or len(array["values"]) > scalar_limit or any(type(v) is not int or v < 0 for v in array["shape"]) or math.prod(array["shape"]) != len(array["values"]):
            raise ValueError("Invalid numeric array")
        if any(value is not None and type(value) not in {int, float} for value in array["values"]):
            raise ValueError("Array values must be numeric or missing")
    if "tree" in payload and (not isinstance(payload["tree"], list) or len(payload["tree"]) > 256):
        raise ValueError("Invalid hierarchy")
    if reader in {"structure", "archive"}:
        field = "tree" if reader == "structure" else "table"
        if set(payload) != {"contract_version", "type", "reader", "kind", "media_type", field, "metadata", "warnings", "sampled"} or payload["media_type"] != "application/json":
            raise ValueError("Invalid bounded reader schema")
        if reader == "structure":
            paths = set()
            if not payload["tree"]:
                raise ValueError("Empty structure tree")
            for node in payload["tree"]:
                if not isinstance(node, dict) or set(node) != {"path", "node_type", "attributes"}:
                    raise ValueError("Invalid structure node")
                path, attrs = node["path"], node["attributes"]
                if (not isinstance(path, str) or not re.fullmatch(r"/0(?:/[0-9]{1,4}){0,7}", path) or path in paths
                    or (path != "/0" and path.rsplit("/", 1)[0] not in paths)
                    or node["node_type"] not in {"object", "array", "element", "attribute", "text", "string", "number", "null", "boolean"}
                    or not isinstance(attrs, dict) or set(attrs) - {"label", "value", "children_count", "numeric_representation"}
                    or not isinstance(attrs.get("label"), str) or len(attrs["label"]) > 512
                    or type(attrs.get("children_count")) is not int or not 0 <= attrs["children_count"] <= 4096):
                    raise ValueError("Invalid structure hierarchy")
                value = attrs.get("value")
                if value is not None and type(value) not in {str, bool}:
                    raise ValueError("Structure values must remain inert and exact")
                if isinstance(value, str) and len(value) > 512:
                    raise ValueError("Structure scalar is too large")
                if node["node_type"] == "number" and (not isinstance(value, str) or attrs.get("numeric_representation") != "source lexeme"):
                    raise ValueError("Numeric structure values must retain source lexemes")
                paths.add(path)
        else:
            table = payload["table"]
            if (set(table) != {"columns", "rows", "row_offset", "column_offset", "total_rows", "total_columns"}
                or table["columns"] != ["成员", "类型", "声明大小（字节）", "压缩大小（字节）", "嵌套压缩包"]
                or type(table["row_offset"]) is not int or not 0 <= table["row_offset"] <= 4095
                or type(table["total_rows"]) is not int or not 0 <= table["total_rows"] <= 4096
                or type(table["column_offset"]) is not int or table["column_offset"] != 0
                or type(table["total_columns"]) is not int or table["total_columns"] != 5
                or table["row_offset"] + len(table["rows"]) > table["total_rows"]):
                raise ValueError("Invalid archive page")
            for row in table["rows"]:
                if (len(row) != 5 or not isinstance(row[0], str) or len(row[0]) > 512 or row[1] not in {"file", "directory", "gzip header"}
                    or any(v is not None and (type(v) is not int or not 0 <= v <= 512 * 1024 * 1024) for v in row[2:4]) or type(row[4]) is not bool):
                    raise ValueError("Invalid archive member")
    if "sections" in payload:
        if reader != "fastqc" or not isinstance(payload["sections"], list) or len(payload["sections"]) > 32:
            raise ValueError("Invalid QC sections")
        for section in payload["sections"]:
            if (not isinstance(section, dict) or set(section) != {"name", "status", "columns", "rows"}
                or not isinstance(section["name"], str) or section["status"] not in {"pass", "warn", "fail"}
                or not isinstance(section["columns"], list) or len(section["columns"]) > 20
                or any(not isinstance(value, str) for value in section["columns"])
                or not isinstance(section["rows"], list) or len(section["rows"]) > 1000
                or any(not isinstance(row, list) or len(row) > 20 or any(not isinstance(value, str) for value in row) for row in section["rows"])):
                raise ValueError("Invalid QC section table")
    if reader == "sqlite-table":
        from app.application.services.sqlite_table_visualization import validate_sqlite_table_payload
        validate_sqlite_table_payload(payload, limit=limit)
    if reader == "archive-member":
        from app.application.services.archive_member_visualization import validate_archive_member_payload
        validate_archive_member_payload(payload)
    if reader == "mca":
        from app.application.services.mca_visualization import validate_mca_payload
        validate_mca_payload(payload)
    if reader == "scientific-graph":
        from app.application.services.scientific_graph_visualization import validate_scientific_graph_payload
        validate_scientific_graph_payload(payload, limit=limit)
    if reader == "phylogeny":
        from app.application.services.phylogeny_visualization import validate_phylogeny_payload
        validate_phylogeny_payload(payload, limit=limit)
    if reader == "mass-spectrum":
        from app.application.services.mass_spectrum_visualization import validate_mass_spectrum_payload
        validate_mass_spectrum_payload(payload, limit=limit)
    if reader == "gro-trajectory":
        from app.application.services.gro_trajectory_visualization import validate_gro_trajectory_payload
        validate_gro_trajectory_payload(payload, limit=limit)
    if reader == "simulation-mesh":
        from app.application.services.simulation_mesh_visualization import validate_simulation_mesh_payload
        validate_simulation_mesh_payload(payload, limit=limit)
    if reader == "diffraction":
        from app.application.services.diffraction_visualization import validate_diffraction_payload
        validate_diffraction_payload(payload, limit=limit)
    if reader == "geoformat":
        from app.application.services.geo_visualization import validate_geo_payload
        validate_geo_payload(payload)
    if reader == "czi":
        from app.application.services.czi_visualization import validate_czi_payload
        validate_czi_payload(payload)
    if len(json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()) > limit:
        raise ValueError("Preview output limit exceeded")
    return payload


def validate_requested_selection(result: dict, reader: str, kind: str, options: dict, format: str, size: int):
    """A well-formed result must still belong to the requested resource/window."""
    meta = result["metadata"]
    if reader in {"sql-dump", "pg-dump", "bson", "redis-rdb"}:
        from app.application.services.database_file_visualization import validate_payload as validate_database_file
        validate_database_file(result, reader, kind, options=options, fmt=format, size=size)
        return
    if reader == "database-table":
        from app.application.services.database_table_visualization import validate_database_table_payload
        validate_database_table_payload(result, kind=kind, options=options, fmt=format, size=size)
        return
    if reader in migration.KINDS:
        migration.validate_payload(result, reader, kind, options=options, format=format, source_bytes=size)
        return
    if reader == "sqlite-table":
        from app.application.services.sqlite_table_visualization import validate_sqlite_table_payload
        validate_sqlite_table_payload(result, kind=kind, options=options, fmt=format, size=size)
    if reader == "gro-trajectory":
        from app.application.services.gro_trajectory_visualization import validate_gro_trajectory_payload
        validate_gro_trajectory_payload(result, kind=kind, options=options, fmt=format, size=size)
    if reader == "simulation-mesh":
        from app.application.services.simulation_mesh_visualization import validate_simulation_mesh_payload
        validate_simulation_mesh_payload(result, kind=kind, options=options, fmt=format, size=size)
    if reader == "mass-spectrum":
        from app.application.services.mass_spectrum_visualization import validate_mass_spectrum_payload
        validate_mass_spectrum_payload(result, kind=kind, options=options, fmt=format, size=size)
    if reader == "diffraction":
        from app.application.services.diffraction_visualization import validate_diffraction_payload
        validate_diffraction_payload(result, kind=kind, options=options, fmt=format, size=size)
    if reader == "phylogeny":
        from app.application.services.phylogeny_visualization import validate_phylogeny_payload
        validate_phylogeny_payload(result, size=size, fmt=format)
        if options or kind != "tree":
            raise ValueError("Phylogeny result does not match request")
    if reader == "scientific-graph":
        from app.application.services.scientific_graph_visualization import validate_scientific_graph_payload
        validate_scientific_graph_payload(result, size=size)
        if meta["format"] != format or options or kind != "graph":
            raise ValueError("Graph result does not match request")
    if reader == "archive-member":
        selected = options.get("member_id")
        if (meta["format"] != format or meta["source_bytes"] != size
                or meta["mode"] != ("text" if selected is not None else "directory")
                or result["table"]["row_offset"] != options.get("row_offset", 0)
                or (selected is not None and meta.get("member_id") != selected)):
            raise ValueError("Archive result does not match request")
    elif reader == "czi":
        if meta["input_bytes"] != size or (kind == "image" and result["selected"] != options):
            raise ValueError("CZI result does not match request")
    elif reader == "geoformat":
        formats = {"asc": "ESRI ASCII", "grd": "Surfer DSAA", "kml": "KML 2.2"}
        expected_crs = "EPSG:4326" if format == "kml" else options.get("crs", "unknown")
        expected_source = "format" if format == "kml" else "user" if "crs" in options else "unspecified"
        if meta["format"] != formats.get(format) or meta["crs"] != expected_crs or meta["crs_source"] != expected_source:
            raise ValueError("Geographic result does not match request")
    elif reader == "mca" and (meta["format"] != format or meta["input_bytes"] != size):
        raise ValueError("Spectrum result does not match request")


async def extended_visualization(file_service, catalog, image: str | None, file_id: str,
                                 user_id: str, request: ExtendedPreviewRequest, *, binary=False, allow_job=False,
                                 worker=run_extended_visualization_worker):
    plugin = await catalog.require_enabled(user_id, request.plugin_id)
    revision = (await catalog.list_for_user(user_id)).revision
    operation = "bytes" if binary else "job" if allow_job else "preview"
    if operation not in plugin.capabilities.operations or binary != (plugin.reader == "binary"):
        raise ScientificPreviewRejected("此插件不支持该读取接口。")
    reader = plugin.reader
    if reader == "fastqc" and not allow_job:
        raise ScientificPreviewRejected("完整质控必须通过明确启动的 AnalysisJob 执行。")
    validate_options(reader, request.options)
    kind = request.kind or _DEFAULT_KIND.get(reader)
    if reader in {"sql-dump", "pg-dump", "bson", "redis-rdb"}:
        from app.application.services.database_file_visualization import validate_options as validate_database_file_options
        try:
            validate_database_file_options(reader, kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("数据库文件视图、方言或分页选择无效。") from None
        if kind == "table" and request.version is None:
            raise ScientificPreviewRejected("请先读取目录，再携带同一文件版本选择分页。")
    if reader == "database-table":
        from app.application.services.database_table_visualization import validate_database_table_options
        try:
            validate_database_table_options(kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("数据库表格选择无效。") from None
        if kind == "table" and request.version is None:
            raise ScientificPreviewRejected("请先读取目录，再携带文件版本选择数据表。")
    if reader in migration.KINDS:
        try:
            migration.validate_options(reader, kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("工作台数据选择或操作参数无效。") from None
        if kind != "tree" and request.version is None:
            raise ScientificPreviewRejected("请先读取目录，再携带同一文件版本明确选择工作台数据。")
    if reader == "sqlite-table":
        from app.application.services.sqlite_table_visualization import validate_sqlite_table_options
        try:
            validate_sqlite_table_options(kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("SQLite 表格选择无效。") from None
        if kind == "table" and request.version is None:
            raise ScientificPreviewRejected("请先读取目录，再携带文件版本选择数据表。")
    if reader in {"gro-trajectory", "simulation-mesh"}:
        try:
            if reader == "gro-trajectory":
                from app.application.services.gro_trajectory_visualization import validate_gro_trajectory_options
                validate_gro_trajectory_options(kind, request.options)
            else:
                from app.application.services.simulation_mesh_visualization import validate_simulation_mesh_options
                validate_simulation_mesh_options(kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("几何数据选择无效。") from None
        if kind == "geometry" and request.version is None:
            raise ScientificPreviewRejected("请先读取目录，再携带文件版本选择几何数据。")
    if reader in {"mass-spectrum", "diffraction"}:
        try:
            if reader == "mass-spectrum":
                from app.application.services.mass_spectrum_visualization import validate_mass_spectrum_options
                validate_mass_spectrum_options(kind, request.options)
            else:
                from app.application.services.diffraction_visualization import validate_diffraction_options
                validate_diffraction_options(kind, request.options)
        except (ValueError, TypeError):
            raise ScientificPreviewRejected("谱图或扫描选择无效。") from None
        if request.version is None and (kind == "series" or reader == "mass-spectrum" and request.options.get("offset", 0) != 0):
            raise ScientificPreviewRejected("请先读取目录，再携带同一文件版本选择谱图或分页。")
    if reader == "czi":
        from app.application.services.czi_visualization import validate_czi_options
        try:
            validate_czi_options(kind, request.options)
        except ValueError:
            raise ScientificPreviewRejected("请使用有效的 C/Z/T 索引与有界像素区域。") from None
        if kind == "image" and not request.version:
            raise ScientificPreviewRejected("请先读取显微图像目录，再以同一版本选择像素区域。")
    if not binary and kind not in _KINDS.get(reader, set()):
        raise ScientificPreviewRejected("此读取器不支持该视图。")
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    if not plugin.matches_filename(info.filename or ""):
        raise ScientificPreviewRejected("此插件不支持当前格式。")
    reader_limit = {"gro-trajectory": 16 * 1024 * 1024, "simulation-mesh": 16 * 1024 * 1024, "mass-spectrum": 16 * 1024 * 1024, "diffraction": 16 * 1024 * 1024, "structure": 4 * 1024 * 1024, "phylogeny": 4 * 1024 * 1024, "mca": 4 * 1024 * 1024, "scientific-graph": 4 * 1024 * 1024, "geoformat": 16 * 1024 * 1024}.get(reader, MAX_INPUT)
    if reader in {"sqlite-table", "database-table", "sql-dump", "pg-dump", "bson", "redis-rdb"}:
        reader_limit = 16 * 1024 * 1024
    if reader in migration.INPUT_LIMITS:
        reader_limit = migration.INPUT_LIMITS[reader]
    if type(info.size) is not int or not 0 < info.size <= min(reader_limit, plugin.limits.max_input_bytes):
        raise ScientificPreviewRejected("文件超过此交互式插件的安全上限，请使用分析工具处理。")
    version = preview_version(info)
    if request.version and request.version != version:
        raise PreviewVersionChanged()
    if not binary and not image:
        raise VisualizationWorkerError("未配置隔离读取器镜像。")
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=5)
    except TimeoutError:
        raise VisualizationWorkerError("预览繁忙，请稍后重试。") from None
    cancel = threading.Event()
    release_here = True
    async def owned(operation):
        nonlocal release_here
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            release_here = False
            def completed(done):
                _SLOTS.release()
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(completed)
            raise
    try:
        read = getattr(file_service._file_storage, "download_file_range", None)
        if not callable(read):
            raise NotImplementedError("Bounded storage required")
        # Matrix alone may have a 128 MiB whole-source budget. Each storage
        # operation remains <=64 MiB, preserving dataset FD/allowlist guards.
        # Recheck ownership/version/capability between every bounded segment.
        chunks = []
        for offset in range(0, info.size, MAX_INPUT):
            await catalog.require_enabled(user_id, request.plugin_id)
            length = min(MAX_INPUT, info.size-offset)
            chunk, ranged = await owned(read(file_id, user_id, offset=offset, length=length))
            if _is_private_spill(ranged):
                raise FileNotFoundError("File not found")
            if preview_version(ranged) != version:
                raise PreviewVersionChanged()
            if len(chunk) != length:
                raise ScientificPreviewRejected("范围读取不完整。")
            chunks.append(chunk)
        data = chunks[0] if len(chunks) == 1 else b"".join(chunks)
        del chunks
        if len(data) != info.size:
            raise ScientificPreviewRejected("范围读取不完整。")
        await catalog.require_enabled(user_id, request.plugin_id)
        if not binary:
            format = (info.filename or "").lower().rsplit(".", 1)[-1]
            if reader == "astronomy-workbench" and (info.filename or "").lower().endswith(".fits.gz"):
                format = "fits.gz"
            if not re.fullmatch(r"[a-z0-9]{1,12}", format) and not (reader == "astronomy-workbench" and format == "fits.gz"):
                raise ScientificPreviewRejected("无法识别文件格式。")
            response = await owned(asyncio.to_thread(worker, image, data, reader=reader, kind=kind,
                options=request.options, format=format, truncated=False, cancelled=cancel))
            if response.get("ok") is not True:
                # Parser messages can contain paths/data. Never forward them.
                raise ScientificPreviewRejected("此文件无法安全预览；请检查格式、参数及读取器依赖。")
            try:
                result = validate_payload(response.get("data"), reader, kind, min(MAX_OUTPUT, plugin.limits.max_output_bytes))
                validate_requested_selection(result, reader, kind, request.options, format, info.size)
            except (ValueError, TypeError, OverflowError):
                raise VisualizationWorkerError("读取结果未通过 v2 插件协议校验。") from None
        current = await catalog.require_enabled(user_id, request.plugin_id)
        if current != plugin or (await catalog.list_for_user(user_id)).revision != revision:
            raise PreviewVersionChanged()
        latest = await file_service.get_file_info(file_id, user_id)
        if latest is None:
            raise FileNotFoundError("File not found")
        if preview_version(latest) != version:
            raise PreviewVersionChanged()
        # File providers may await I/O. Fence a disable occurring during that
        # final storage check before handing any bytes back to the browser.
        final_revision = (await catalog.list_for_user(user_id)).revision
        final_plugin = await catalog.require_enabled(user_id, request.plugin_id)
        if final_plugin != plugin or final_revision != revision:
            raise PreviewVersionChanged()
        if binary:
            return data, version
        return {**result, "version": version, "plugin_id": plugin.id, "revision": revision}
    finally:
        cancel.set()
        if release_here:
            _SLOTS.release()
