"""Real small ASCII/KML fixtures and hostile dialect/schema boundaries."""
import copy
import pytest
from app.services.geo_format_readers import GeoFormatError, geo_preview, validate_geo_payload


def asc(body="1 2 3 -9999", header="ncols 2\nnrows 2\nxllcorner 10\nyllcorner 20\ncellsize 1\n"):
    return (header + body).encode()


def kml(geometry, name="local"):
    return ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>' + name + '</name>' + geometry + '</Placemark></Document></kml>').encode()


def test_esri_corner_and_default_nodata_without_crs_does_not_guess():
    result = geo_preview(asc(), "asc", {})
    assert result["array"]["values"] == [1, 2, 3, None]
    assert result["metadata"]["extent"] == [10, 20, 12, 22]
    assert result["metadata"]["crs"] == "unknown"
    assert result["warnings"] and result["sampled"] is False
    assert validate_geo_payload(result) is result


def test_esri_center_origin_has_half_cell_correction_and_explicit_crs():
    result = geo_preview(asc(header="NCOLS 2 NROWS 2 XLLCENTER 10 YLLCENTER 20 CELLSIZE 2 NODATA_VALUE -9999\n"), "asc", {"crs": "EPSG:4326"})
    assert result["metadata"]["extent"] == [9, 19, 13, 23]
    assert result["metadata"]["crs_source"] == "user"


def test_dsaa_north_flip_and_node_registration_are_explicit():
    result = geo_preview(b"DSAA\n2 2\n10 12\n20 24\n1 3\n1 2 3 1.70141e38", "grd", {})
    assert result["array"]["values"] == [3, None, 1, 2]
    assert result["metadata"]["source_extent"] == [10, 20, 12, 24]
    assert result["metadata"]["extent"] == [9, 18, 13, 26]
    assert result["metadata"]["registration"] == "node"


def test_nearest_preview_is_bounded_and_not_aggregate():
    data = asc(" ".join(str(i) for i in range(130 * 129)), "ncols 130 nrows 129 xllcorner 0 yllcorner 0 cellsize 1\n")
    result = geo_preview(data, "asc", {})
    assert result["array"]["shape"] == [128, 128]
    assert len(result["array"]["values"]) == 16384
    assert result["array"]["values"][0] == 0
    assert result["array"]["values"][-1] == 130 * 129 - 1
    assert result["sampled"] is True


@pytest.mark.parametrize("data", [asc("1 2"), asc("1 2 3 4 5"), asc("nan 2 3 4"), asc("1e999 2 3 4"), asc("1D2 2 3 4"),
    asc(header="ncols 2 nrows 2 xllcorner 0 yllcenter 0 cellsize 1\n"),
    asc(header="ncols 2 nrows 2 xllcorner 0 yllcorner 0 cellsize -1\n"),
    asc(header="ncols 2048 nrows 2048 xllcorner 0 yllcorner 0 cellsize 1\n"),
    asc(header="ncols 2 nrows 2 nrows 2 xllcorner 0 yllcorner 0 cellsize 1\n"), b"\xff", b"\x00"])
def test_ascii_invalid_incomplete_ambiguous_or_oversize_fail_closed(data):
    with pytest.raises(GeoFormatError): geo_preview(data, "asc", {})


@pytest.mark.parametrize("options", [{"crs": "EPSG:32650"}, {"crs": True}, {"url": "https://example.com"}, {"crs": "EPSG:4326", "path": "/tmp/data"}])
def test_unknown_crs_or_option_cannot_reach_readers(options):
    with pytest.raises(GeoFormatError): geo_preview(asc(), "asc", options)


def test_known_crs_invalid_extent_rejected_but_unknown_coordinates_still_viewable():
    data = asc(header="ncols 2 nrows 2 xllcorner 500000 yllcorner 2000000 cellsize 30\n")
    assert geo_preview(data, "asc", {})["metadata"]["crs"] == "unknown"
    with pytest.raises(GeoFormatError): geo_preview(data, "asc", {"crs": "EPSG:4326"})


@pytest.mark.parametrize("data", [b"DSBBxx", b"DSRBxx", b"DSAA 1 1 0 1 0 1 0 1 2", b"DSAA 2 2 0 1 0 1 3 1 1 2 3 4"])
def test_grd_dialects_and_invalid_node_grid_refused(data):
    with pytest.raises(GeoFormatError): geo_preview(data, "grd", {})


def test_kml_basic_multi_geometry_flattened_into_inert_features():
    data = kml('<MultiGeometry><Point><coordinates>10,20,30</coordinates></Point><LineString><coordinates>10,20 11,21</coordinates></LineString><Polygon><outerBoundaryIs><LinearRing><coordinates>10,20 11,20 11,21 10,20</coordinates></LinearRing></outerBoundaryIs></Polygon></MultiGeometry>', "&lt;script&gt;text&lt;/script&gt;")
    result = geo_preview(data, "kml", {})
    assert len(result["geojson"]["features"]) == 3
    assert result["geojson"]["features"][0]["geometry"]["coordinates"] == [10, 20]
    assert result["geojson"]["features"][0]["properties"] == {"name": "<script>text</script>"}
    assert result["metadata"]["coordinate_count"] == 7
    assert result["metadata"]["crs"] == "EPSG:4326"
    assert "高度" in result["warnings"][0]


@pytest.mark.parametrize("data", [b'<!DOCTYPE kml [<!ENTITY x "text">]><kml xmlns="http://www.opengis.net/kml/2.2"/>',
    kml('<NetworkLink><Link><href>https://example.com/a</href></Link></NetworkLink>'),
    kml('<Point><coordinates>10,20</coordinates></Point><styleUrl>https://example.com/style</styleUrl>'),
    kml('<Point><coordinates>181,20</coordinates></Point>'), kml('<Point><coordinates>1,2 3,4</coordinates></Point>'),
    kml('<LineString><coordinates>170,20 -170,20</coordinates></LineString>'),
    kml('<Polygon><outerBoundaryIs><LinearRing><coordinates>1,2 2,2 2,3 1,3</coordinates></LinearRing></outerBoundaryIs></Polygon>'),
    b'<?handler a="b"?><kml xmlns="http://www.opengis.net/kml/2.2"/>',
    kml('<Point><coordinates>1,2</coordinates></Point>').replace(b'<Point>', b'<Point href="file:///tmp/x">'),
    b'<kml><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark></kml>'])
def test_kml_untrusted_resources_invalid_namespace_or_geometry_rejected(data):
    with pytest.raises(GeoFormatError): geo_preview(data, "kml", {})


def test_kml_fixed_crs_and_sanitized_label():
    data = kml('<Point><coordinates>10,20</coordinates></Point>', "/Users/person/data")
    assert geo_preview(data, "kml", {"crs": "EPSG:4326"})["geojson"]["features"][0]["properties"]["name"] == "[redacted]"
    with pytest.raises(GeoFormatError): geo_preview(data, "kml", {"crs": "EPSG:3857"})


def test_strict_result_validation_rejects_extensions_and_corrupt_geometry():
    result = geo_preview(asc(), "asc", {})
    broken = copy.deepcopy(result); broken["url"] = "https://example.com"
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)
    broken = copy.deepcopy(result); broken["array"]["values"][0] = True
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)
    result = geo_preview(kml('<Point><coordinates>10,20</coordinates></Point>'), "kml", {})
    broken = copy.deepcopy(result); broken["geojson"]["features"][0]["properties"]["href"] = "evil"
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)
    broken = copy.deepcopy(result); broken["metadata"]["coordinate_count"] = 2
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)


def test_private_geo_schema_rejects_float_version_and_boolean_shape():
    result = geo_preview(asc("2", "ncols 1 nrows 1 xllcorner 0 yllcorner 0 cellsize 1\n"), "asc", {})
    broken = copy.deepcopy(result); broken["contract_version"] = 2.0
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)
    broken = copy.deepcopy(result); broken["array"]["shape"] = [True, True]
    with pytest.raises(GeoFormatError): validate_geo_payload(broken)
