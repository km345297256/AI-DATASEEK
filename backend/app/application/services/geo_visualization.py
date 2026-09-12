"""Strict host-side geoscience schema; no XML/native parser imports."""
import json
import math

MAX_CELLS = 1024 * 1024
MAX_POINTS = 16384
MAX_FEATURES = 512
CRS = {"EPSG:4326", "EPSG:3857"}

class GeoFormatError(ValueError):
    pass

def _fail(message="地理文件结构、数值或坐标不符合受限格式规范。"):
    raise GeoFormatError(message)

def _extent(value, crs):
    if not isinstance(value, list) or len(value) != 4 or any(type(v) not in {int, float} or not math.isfinite(v) for v in value):
        _fail()
    if value[0] >= value[2] or value[1] >= value[3] or not math.isfinite(value[2] - value[0]) or not math.isfinite(value[3] - value[1]):
        _fail()
    if crs == "EPSG:4326" and not (-180 <= value[0] < value[2] <= 180 and -90 <= value[1] < value[3] <= 90):
        _fail("栅格范围不符合所选 EPSG:4326；请检查数据说明，系统不推测坐标系。")
    if crs == "EPSG:3857" and any(abs(v) > 20037508.34279 for v in value):
        _fail("栅格范围不符合受支持的 EPSG:3857 范围。")


def validate_geo_options(options):
    if not isinstance(options, dict) or set(options) - {"crs"} or ("crs" in options and (not isinstance(options["crs"], str) or options["crs"] not in CRS)):
        _fail("不支持的显式坐标系参数。")
    return options


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
