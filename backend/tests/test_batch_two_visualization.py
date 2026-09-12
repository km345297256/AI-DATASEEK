"""Unified authorization/version/operation fences for the five new plugins."""
import copy
import json
from pathlib import Path

import pytest

from app.application.services import unified_visualization as service
from app.application.services.extended_visualization import validate_payload, validate_requested_selection
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.visualization_catalog import VisualizationDisabledError
from test_unified_visualization import environment, invoke
from test_archive_member_visualization import archive_reader, payload as archive_payload
from test_mca_visualization import payload as mca_payload

PLUGINS = [
    ("viz-edf-signals", "record.edf", 8 * 1024**3),
    ("viz-mca-spectrum", "spectrum.mca", 4 * 1024**2),
    ("viz-archive-members", "archive.zip", 64 * 1024**2),
    ("viz-geoformats", "map.asc", 16 * 1024**2),
    ("viz-czi", "microscope.czi", 64 * 1024**2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,maximum", PLUGINS)
@pytest.mark.parametrize("reason", ["foreign", "private", "disabled", "stale", "wrong-format", "oversize", "undeclared-bytes", "path-option"])
async def test_new_plugins_deny_before_storage_or_worker(environment, plugin, filename, maximum, reason):
    storage, catalog = environment
    storage.infos["file"].filename = filename
    kwargs, operation = {}, "preview"
    if reason == "private": storage.infos["file"].metadata = {"source": "tool_output_spill"}
    if reason == "disabled": await catalog.set_state("owner", plugin, False)
    if reason == "stale": kwargs["version"] = "0" * 64
    if reason == "wrong-format": storage.infos["file"].filename = "unrelated.xyz"
    if reason == "oversize": storage.infos["file"].size = maximum + 1
    if reason == "undeclared-bytes": operation = "bytes"
    if reason == "path-option": kwargs["options"] = {"path": "/private/forbidden"}
    with pytest.raises((FileNotFoundError, VisualizationDisabledError, PreviewVersionChanged, service.ScientificPreviewRejected)):
        await invoke(environment, plugin, operation, user="other" if reason == "foreign" else "owner", **kwargs)
    assert storage.reads == []


@pytest.mark.asyncio
async def test_archive_member_requires_explicit_current_version(environment):
    storage, _ = environment
    storage.infos["file"].filename = "archive.zip"
    with pytest.raises(service.ScientificPreviewRejected, match="版本"):
        await invoke(environment, "viz-archive-members", "preview", options={"member_id": "member-" + "a" * 64})
    assert storage.reads == []


@pytest.mark.asyncio
async def test_czi_pixels_require_directory_version(environment):
    storage, _ = environment
    storage.infos["file"].filename = "microscope.czi"
    with pytest.raises(service.ScientificPreviewRejected, match="版本"):
        await invoke(environment, "viz-czi", "preview", kind="image")
    assert storage.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,maximum", PLUGINS)
async def test_plugin_state_is_owner_scoped(environment, plugin, filename, maximum):
    storage, catalog = environment
    storage.infos["file"].filename = filename
    await catalog.set_state("other", plugin, False)
    assert (await catalog.require_enabled("owner", plugin)).id == plugin
    with pytest.raises(VisualizationDisabledError):
        await catalog.require_enabled("other", plugin)


@pytest.mark.parametrize("mode", ["directory", "text"])
@pytest.mark.parametrize("mismatch", ["format", "size", "offset", "resource"])
def test_valid_archive_schema_still_must_match_requested_resource(archive_reader, mode, mismatch):
    result = archive_payload(archive_reader, mode)
    validate_payload(result, "archive-member", "table", 1048576)
    options = {"member_id": result["metadata"]["member_id"]} if mode == "text" else {}
    fmt, size = "zip", result["metadata"]["source_bytes"]
    validate_requested_selection(result, "archive-member", "table", options, fmt, size)
    if mismatch == "format": fmt = "tar"
    elif mismatch == "size": size += 1
    elif mismatch == "offset": options["row_offset"] = 200
    elif mode == "text": options["member_id"] = "member-" + "0" * 64
    else: options["member_id"] = "member-" + "0" * 64
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "archive-member", "table", options, fmt, size)


def test_text_result_cannot_be_substituted_for_directory(archive_reader):
    result = archive_payload(archive_reader, "text")
    validate_payload(result, "archive-member", "table", 1048576)
    with pytest.raises(ValueError):
        validate_requested_selection(result, "archive-member", "table", {}, "zip", result["metadata"]["source_bytes"])


@pytest.mark.parametrize("calibrated", [False, True])
@pytest.mark.parametrize("mismatch", ["size", "format"])
def test_valid_mca_schema_cannot_be_substituted_for_other_input(calibrated, mismatch):
    result = mca_payload(calibrated)
    validate_payload(result, "mca", "series", 2097152)
    size, fmt = result["metadata"]["input_bytes"], "mca"
    validate_requested_selection(result, "mca", "series", {}, fmt, size)
    if mismatch == "size": size += 1
    else: fmt = "spe"
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "mca", "series", {}, fmt, size)


def geoscience_payload(name):
    # These small synthetic fixtures were generated by the actual bounded
    # readers, including pylibCZIrw 6.1.0. The API tests need no native parser.
    path = Path(__file__).resolve().parents[2] / "frontend/tests/browser/batch-two-geo-data.json"
    return json.loads(path.read_text())[name]


@pytest.mark.parametrize("name,fmt,options", [("asc", "asc", {}), ("asc_wgs84", "asc", {"crs": "EPSG:4326"}),
                                            ("grd", "grd", {}), ("kml", "kml", {})])
def test_real_geographic_result_is_bound_to_format(name, fmt, options):
    result = geoscience_payload(name)
    validate_payload(result, "geoformat", "map", 2097152)
    validate_requested_selection(result, "geoformat", "map", options, fmt, 100)
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "geoformat", "map", options, "kml" if fmt != "kml" else "asc", 100)


@pytest.mark.parametrize("name,fmt,request_options", [("asc", "asc", {"crs": "EPSG:4326"}),
    ("grd", "grd", {"crs": "EPSG:4326"}), ("asc_wgs84", "asc", {})])
def test_valid_crs_result_cannot_claim_unrequested_user_crs(name, fmt, request_options):
    result = geoscience_payload(name)
    validate_payload(result, "geoformat", "map", 2097152)
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "geoformat", "map", request_options, fmt, 100)


@pytest.mark.parametrize("name,kind", [("czi_tree", "tree"), ("czi_image", "image")])
def test_real_czi_result_must_match_input_size(name, kind):
    result = geoscience_payload(name)
    options = result["selected"] if kind == "image" else {}
    validate_payload(result, "czi", kind, 8388608)
    validate_requested_selection(result, "czi", kind, options, "czi", result["metadata"]["input_bytes"])
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "czi", kind, options, "czi", result["metadata"]["input_bytes"] + 1)


@pytest.mark.parametrize("field,value", [("indices", [0, 1, 0]), ("roi", [0, 0, 3, 2])])
def test_valid_czi_image_cannot_substitute_another_slice_or_roi(field, value):
    result = geoscience_payload("czi_image")
    validate_payload(result, "czi", "image", 8388608)
    options = copy.deepcopy(result["selected"])
    options[field] = value
    with pytest.raises(ValueError, match="request"):
        validate_requested_selection(result, "czi", "image", options, "czi", result["metadata"]["input_bytes"])
