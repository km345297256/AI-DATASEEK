"""Bounded, offline ESRI ASCII / Surfer DSAA / basic KML readers.

Only approved bytes enter this module, inside the networkless v2 worker. No
sidecar, CRS inference, XML resource, style, HTML or external file is opened.
Specs: Esri ASCII raster format; Golden Software Surfer 6 Text Grid Format;
OGC KML 2.2 (Google's official KML Reference).
"""
from __future__ import annotations

import json
import math
import re
from xml.parsers import expat

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CELLS = 1024 * 1024
MAX_POINTS = 16384
MAX_FEATURES = 512
CRS = {"EPSG:4326", "EPSG:3857"}


class GeoFormatError(ValueError):
    """Fixed, non-sensitive diagnostics only."""


def _fail(message="地理文件结构、数值或坐标不符合受限格式规范。"):
    raise GeoFormatError(message)


def _number(value):
    if len(value) > 128 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        _fail()
    number = float(value)
    if not math.isfinite(number):
        _fail()
    return number


def _integer(value):
    if not re.fullmatch(r"[1-9][0-9]{0,6}", value):
        _fail()
    return int(value)


def _tokens(text):
    for token in re.finditer(r"\S+", text):
        if token.end() - token.start() > 128:
            _fail()
        yield token.group()


def _extent(value, crs):
    if not isinstance(value, list) or len(value) != 4 or any(type(v) not in {int, float} or not math.isfinite(v) for v in value):
        _fail()
    if value[0] >= value[2] or value[1] >= value[3] or not math.isfinite(value[2] - value[0]) or not math.isfinite(value[3] - value[1]):
        _fail()
    if crs == "EPSG:4326" and not (-180 <= value[0] < value[2] <= 180 and -90 <= value[1] < value[3] <= 90):
        _fail("栅格范围不符合所选 EPSG:4326；请检查数据说明，系统不推测坐标系。")
    if crs == "EPSG:3857" and any(abs(v) > 20037508.34279 for v in value):
        _fail("栅格范围不符合受支持的 EPSG:3857 范围。")


def _raster(text, fmt, crs):
    tokens = iter(_tokens(text))
    try:
        if fmt == "asc":
            header = {}
            valid = {"ncols", "nrows", "xllcorner", "xllcenter", "yllcorner", "yllcenter", "cellsize", "nodata_value"}
            token = next(tokens)
            while token.lower() in valid:
                key = token.lower()
                if key in header or len(header) >= 6:
                    _fail("ESRI ASCII 栅格头重复或不受支持。")
                header[key] = next(tokens)
                token = next(tokens)
            if not {"ncols", "nrows", "cellsize"} <= header.keys():
                _fail("文件不是受支持的 ESRI ASCII 栅格。")
            if ("xllcorner" in header, "yllcorner" in header) == (True, True) and not {"xllcenter", "yllcenter"} & header.keys():
                registration = "cell-corner"
                x, y = _number(header["xllcorner"]), _number(header["yllcorner"])
            elif ("xllcenter" in header, "yllcenter" in header) == (True, True) and not {"xllcorner", "yllcorner"} & header.keys():
                registration = "cell-center"
                x, y = _number(header["xllcenter"]), _number(header["yllcenter"])
            else:
                _fail("ESRI ASCII 需要配对且唯一的 corner 或 center 原点。")
            cols, rows, step = _integer(header["ncols"]), _integer(header["nrows"]), _number(header["cellsize"])
            if step <= 0:
                _fail()
            if registration == "cell-center":
                x, y = x - step / 2, y - step / 2
            extent = [x, y, x + cols * step, y + rows * step]
            nodata = _number(header.get("nodata_value", "-9999"))
            first = token
            source_extent = extent[:]
        else:
            if next(tokens) != "DSAA":
                _fail("GRD 首期仅支持 Surfer ASCII DSAA，不支持二进制或其他 GRD 方言。")
            cols, rows = _integer(next(tokens)), _integer(next(tokens))
            if cols < 2 or rows < 2:
                _fail("DSAA 节点网格每个维度至少需要两个节点。")
            x0, x1, y0, y1 = [_number(next(tokens)) for _ in range(4)]
            z0, z1 = [_number(next(tokens)) for _ in range(2)]
            if x0 >= x1 or y0 >= y1 or z0 > z1:
                _fail()
            dx, dy = (x1 - x0) / (cols - 1), (y1 - y0) / (rows - 1)
            source_extent = [x0, y0, x1, y1]
            extent = [x0 - dx / 2, y0 - dy / 2, x1 + dx / 2, y1 + dy / 2]
            nodata, registration, first = 1.70141e38, "node", next(tokens)
        if cols * rows > MAX_CELLS:
            _fail("栅格源网格超过 1,048,576 点预算。")
        _extent(extent, crs)
        out_w, out_h = min(cols, 128), min(rows, 128)
        xs = {min(cols - 1, math.floor((i + 0.5) * cols / out_w)): i for i in range(out_w)}
        ys = {min(rows - 1, math.floor((i + 0.5) * rows / out_h)): i for i in range(out_h)}
        values = [None] * (out_w * out_h)
        count = 0
        from itertools import chain
        for token in chain([first], tokens):
            if count >= cols * rows:
                _fail("栅格值数量与头部尺寸不一致。")
            value = _number(token)
            row, col = divmod(count, cols)
            north_row = rows - row - 1 if fmt == "grd" else row
            if north_row in ys and col in xs:
                missing = value >= nodata if fmt == "grd" else value == nodata
                values[ys[north_row] * out_w + xs[col]] = None if missing else value
            count += 1
        if count != cols * rows:
            _fail("栅格值数量与头部尺寸不一致。")
    except StopIteration:
        _fail("栅格文件不完整。")
    return {"array": {"shape": [out_h, out_w], "dimensions": ["y", "x"], "values": values},
            "metadata": {"format": "ESRI ASCII" if fmt == "asc" else "Surfer DSAA", "crs": crs or "unknown",
                         "extent": extent, "source_extent": source_extent, "source_shape": [rows, cols],
                         "nodata": nodata, "registration": registration, "row_order": "north-to-south",
                         "sampling": "nearest cell-centre sample; no aggregation", "crs_source": "user" if crs else "unspecified"},
            "sampled": out_w != cols or out_h != rows,
            "warnings": [] if crs else ["文件本身未声明坐标系；当前按原始坐标显示纯数值热图，不推测地理位置。"]}


def _kml(text, crs):
    if crs and crs != "EPSG:4326":
        _fail("KML 的坐标固定为 WGS84 经度、纬度，不接受其他坐标系覆盖。")
    parser = expat.ParserCreate(namespace_separator="|")
    parser.buffer_text = True
    stack, root, count = [], None, 0
    allowed = {"kml", "Document", "Folder", "Placemark", "name", "description", "Point", "LineString", "LinearRing", "Polygon", "MultiGeometry", "outerBoundaryIs", "innerBoundaryIs", "coordinates", "altitudeMode", "extrude", "tessellate", "visibility", "open", "Style", "LineStyle", "PolyStyle", "color", "colorMode", "width", "fill", "outline", "styleUrl"}

    def blocked(*_):
        _fail("KML 不允许外部资源、NetworkLink、DTD、实体或处理指令。")

    def start(name, attrs):
        nonlocal count, root
        parts = name.split("|")
        if len(parts) != 2 or parts[0] != "http://www.opengis.net/kml/2.2" or parts[1] not in allowed:
            _fail("KML 首期仅支持 2.2 命名空间中的基本点、线、面；不接受网络或扩展元素。")
        if set(attrs) - {"id"} or any(len(v) > 128 for v in attrs.values()):
            blocked()
        count += 1
        if count > 4096 or len(stack) >= 16:
            _fail("KML 节点或深度超过预算。")
        node = {"tag": parts[1], "text": "", "children": []}
        if stack:
            stack[-1]["children"].append(node)
        elif root is None:
            root = node
        else:
            _fail()
        stack.append(node)

    def chars(value):
        if stack:
            node = stack[-1]
            if len(node["text"]) + len(value) > 1024 * 1024:
                _fail("KML 坐标或文本长度超过预算。")
            node["text"] += value

    parser.StartElementHandler, parser.EndElementHandler = start, lambda _: stack.pop()
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = parser.ExternalEntityRefHandler = parser.ProcessingInstructionHandler = blocked
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        _fail("无法解析受限 KML 文档。")
    if root is None or root["tag"] != "kml":
        _fail()
    points = 0

    def children(node, tag):
        return [child for child in node["children"] if child["tag"] == tag]

    def coordinates(node, minimum, ring=False):
        nonlocal points
        records = children(node, "coordinates")
        if len(records) != 1:
            _fail()
        values = []
        for token in re.finditer(r"\S+", records[0]["text"]):
            parts = token.group().split(",")
            if len(parts) not in {2, 3}:
                _fail()
            coords = [_number(value) for value in parts]
            if not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90:
                _fail("KML 经度或纬度越界。")
            points += 1
            if points > MAX_POINTS:
                _fail("KML 坐标点超过 16,384 点预算。")
            if values and abs(coords[0] - values[-1][0]) > 180:
                _fail("首期不支持跨越日期变更线的 KML 几何。")
            values.append(coords[:2])
        if len(values) < minimum or (ring and values[0] != values[-1]):
            _fail("KML 几何点数不足或多边形环未闭合。")
        return values

    def geometry(node, depth=0):
        if depth > 8:
            _fail()
        tag = node["tag"]
        if tag == "Point":
            data = coordinates(node, 1)
            if len(data) != 1:
                _fail()
            return [{"type": "Point", "coordinates": data[0]}]
        if tag == "LineString":
            return [{"type": "LineString", "coordinates": coordinates(node, 2)}]
        if tag == "Polygon":
            outer = children(node, "outerBoundaryIs")
            if len(outer) != 1:
                _fail()
            rings = []
            for boundary in outer + children(node, "innerBoundaryIs"):
                ring = children(boundary, "LinearRing")
                if len(ring) != 1:
                    _fail()
                rings.append(coordinates(ring[0], 4, True))
            return [{"type": "Polygon", "coordinates": rings}]
        if tag == "MultiGeometry":
            output = []
            for child in node["children"]:
                output.extend(geometry(child, depth + 1))
                if len(output) > MAX_FEATURES:
                    _fail()
            if not output:
                _fail()
            return output
        _fail()

    features = []

    def visit(node):
        if node["tag"] == "styleUrl" and not re.fullmatch(r"\s*#[A-Za-z0-9_.-]{1,128}\s*", node["text"]):
            blocked()
        if node["tag"] == "Placemark":
            geometries = [child for child in node["children"] if child["tag"] in {"Point", "LineString", "Polygon", "MultiGeometry"}]
            if len(geometries) != 1:
                _fail("每个 Placemark 必须包含一个受支持几何或 MultiGeometry。")
            names = children(node, "name")
            label = (names[0]["text"].strip() if names else "")[:256]
            label = "".join(c for c in label if ord(c) >= 32)
            if re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", label, re.I):
                label = "[redacted]"
            for geom in geometry(geometries[0]):
                features.append({"type": "Feature", "properties": {"name": label}, "geometry": geom})
                if len(features) > MAX_FEATURES:
                    _fail("KML 要素超过 512 项预算。")
        for child in node["children"]:
            visit(child)

    visit(root)
    if not features:
        _fail("KML 未包含受支持的点、线、面要素。")
    return {"geojson": {"type": "FeatureCollection", "features": features},
            "metadata": {"format": "KML 2.2", "crs": "EPSG:4326", "crs_source": "format", "feature_count": len(features), "coordinate_count": points},
            "warnings": ["二维几何预览；高度、地形、样式、说明 HTML 和动态行为不渲染；无在线底图。"], "sampled": False}


def validate_geo_options(options):
    if not isinstance(options, dict) or set(options) - {"crs"} or ("crs" in options and (not isinstance(options["crs"], str) or options["crs"] not in CRS)):
        _fail("不支持的显式坐标系参数。")
    return options


def geo_preview(data, fmt, options):
    if type(data) is not bytes or not 0 < len(data) <= MAX_INPUT_BYTES:
        _fail("地理文本为空或超过 16 MiB 输入预算。")
    validate_geo_options(options)
    if fmt not in {"asc", "grd", "kml"}:
        _fail("不支持的地理格式或显式坐标系参数。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        _fail("地理文本首期仅支持 UTF-8 / ASCII。")
    if "\x00" in text:
        _fail()
    result = {"contract_version": 2, "type": "geoformat", "reader": "geoformat", "kind": "map", "media_type": "application/json"}
    result.update(_kml(text, options.get("crs")) if fmt == "kml" else _raster(text, fmt, options.get("crs")))
    validate_geo_payload(result)
    return result


# Stable worker entry point; the shorter name remains convenient in unit tests.
geoformat_preview = geo_preview


def validate_geo_payload(result):
    """Host-reusable strict schema check; no parser or third-party dependency."""
    common = {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled"}
    if not isinstance(result, dict) or set(result) not in (common | {"array"}, common | {"geojson"}):
        _fail()
    if type(result["contract_version"]) is not int or result["contract_version"] != 2 or result["type"] != "geoformat" or result["reader"] != "geoformat" or result["kind"] != "map" or result["media_type"] != "application/json":
        _fail()
    if type(result["sampled"]) is not bool or not isinstance(result["warnings"], list) or len(result["warnings"]) > 8 or any(not isinstance(w, str) or len(w) > 512 for w in result["warnings"]):
        _fail()
    metadata = result["metadata"]
    if not isinstance(metadata, dict):
        _fail()
    if "array" in result:
        if set(metadata) != {"format", "crs", "extent", "source_extent", "source_shape", "nodata", "registration", "row_order", "sampling", "crs_source"}:
            _fail()
        if metadata["format"] not in {"ESRI ASCII", "Surfer DSAA"} or metadata["crs"] not in CRS | {"unknown"} or metadata["registration"] not in {"cell-center", "cell-corner", "node"} or metadata["row_order"] != "north-to-south" or metadata["sampling"] != "nearest cell-centre sample; no aggregation" or metadata["crs_source"] not in {"user", "unspecified"}:
            _fail()
        _extent(metadata["extent"], metadata["crs"])
        _extent(metadata["source_extent"], None)
        shape = metadata["source_shape"]
        if not isinstance(shape, list) or len(shape) != 2 or any(type(v) is not int or v < 1 for v in shape) or math.prod(shape) > MAX_CELLS:
            _fail()
        if type(metadata["nodata"]) not in {int, float} or not math.isfinite(metadata["nodata"]):
            _fail()
        array = result["array"]
        if not isinstance(array, dict) or set(array) != {"shape", "dimensions", "values"} or array["dimensions"] != ["y", "x"] or not isinstance(array["shape"], list) or any(type(v) is not int for v in array["shape"]) or array["shape"] != [min(v, 128) for v in shape]:
            _fail()
        if not isinstance(array["values"], list) or len(array["values"]) != math.prod(array["shape"]) or any(v is not None and (type(v) not in {int, float} or not math.isfinite(v)) for v in array["values"]):
            _fail()
    else:
        if set(metadata) != {"format", "crs", "crs_source", "feature_count", "coordinate_count"} or metadata["format"] != "KML 2.2" or metadata["crs"] != "EPSG:4326" or metadata["crs_source"] != "format":
            _fail()
        data = result["geojson"]
        if not isinstance(data, dict) or set(data) != {"type", "features"} or data["type"] != "FeatureCollection" or not isinstance(data["features"], list) or not 1 <= len(data["features"]) <= MAX_FEATURES:
            _fail()
        points = 0
        for feature in data["features"]:
            if not isinstance(feature, dict) or set(feature) != {"type", "properties", "geometry"} or feature["type"] != "Feature" or not isinstance(feature["properties"], dict) or set(feature["properties"]) != {"name"} or not isinstance(feature["properties"]["name"], str) or len(feature["properties"]["name"]) > 256:
                _fail()
            geom = feature["geometry"]
            if not isinstance(geom, dict) or set(geom) != {"type", "coordinates"} or geom["type"] not in {"Point", "LineString", "Polygon"}:
                _fail()
            values = geom["coordinates"]
            lines = [[values]] if geom["type"] == "Point" else [values] if geom["type"] == "LineString" else values
            if not isinstance(lines, list) or not lines:
                _fail()
            for line in lines:
                if not isinstance(line, list) or len(line) < {"Point": 1, "LineString": 2, "Polygon": 4}[geom["type"]]:
                    _fail()
                if geom["type"] == "Polygon" and line[0] != line[-1]:
                    _fail()
                for point in line:
                    if not isinstance(point, list) or len(point) != 2 or any(type(v) not in {int, float} or not math.isfinite(v) for v in point) or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
                        _fail()
                    points += 1
        if type(metadata["coordinate_count"]) is not int or points != metadata["coordinate_count"] or points > MAX_POINTS or type(metadata["feature_count"]) is not int or metadata["feature_count"] != len(data["features"]):
            _fail()
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024 * 1024:
        _fail("地理预览输出超过 2 MiB 预算。")
    return result
