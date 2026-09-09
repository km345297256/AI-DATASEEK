"""Real scientific-reader coverage using synthetic, disposable files only."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from netCDF4 import Dataset

from app.services import visualization_worker as worker


def netcdf_bytes(tmp_path, build, *, format="NETCDF4"):
    path = tmp_path / "synthetic.nc"
    with Dataset(path, "w", format=format) as dataset:
        build(dataset)
    return path.read_bytes()


def fits_bytes(*hdus):
    output = io.BytesIO()
    fits.HDUList(list(hdus)).writeto(output)
    return output.getvalue()


def fastq_record(sequence=b"ACGT", quality=b"IIII", identifier=b"sample"):
    return b"@" + identifier + b"\n" + sequence + b"\n+\n" + quality + b"\n"


def worker_process(data, *, header=None, raw_header=None, source=None):
    if header is None:
        header = dict(contract_version=1, reader="fastq", kind="quality", size=len(data))
    prefix = raw_header if raw_header is not None else json.dumps(header).encode() + b"\n"
    command = [sys.executable, "-c", source] if source else [sys.executable, "-m", "app.services.visualization_worker"]
    process = subprocess.run(command, input=prefix + data, capture_output=True, check=True, timeout=20)
    assert len(process.stdout) <= worker.MAX_OUTPUT_BYTES
    return json.loads(process.stdout), process


@pytest.mark.parametrize("format", ["NETCDF3_CLASSIC", "NETCDF3_64BIT_OFFSET", "NETCDF4"])
def test_netcdf_series_uses_selected_slice_without_aggregation(tmp_path, format):
    def build(dataset):
        dataset.createDimension("iteration", 4)
        dataset.createDimension("model", 2)
        x = dataset.createVariable("iteration", "f8", ("iteration",))
        x[:] = [0, 0.25, 1, 5]
        x.units = "simulation step"
        values = dataset.createVariable("loss", "f8", ("iteration", "model"))
        values[:] = [[10, 91], [20, 81], [30, 71], [40, 61]]
    data = netcdf_bytes(tmp_path, build, format=format)
    result = worker.preview_bytes(data, "netcdf", "series", {"variable": "loss", "x_dimension": "iteration", "indices": {"model": 1}})
    assert result["x"] == [0, 0.25, 1, 5]
    assert result["y"] == [91, 81, 71, 61]
    assert result["metadata"]["indices"] == {"model": 1}
    assert result["metadata"]["x_dimension"] == "iteration"
    assert result["sampled"] is False


def test_netcdf_mask_and_scale_are_applied_before_json(tmp_path):
    def build(dataset):
        dataset.createDimension("step", 4)
        variable = dataset.createVariable("packed", "i2", ("step",), fill_value=-9999)
        variable.scale_factor = 0.5
        variable.add_offset = 10
        variable.set_auto_maskandscale(False)
        variable[:] = [0, 2, -9999, 6]
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "series")
    assert result["x"] == [0, 1, 2, 3]
    assert result["y"] == [10, 11, None, 13]
    assert "索引" in result["x_label"]


@pytest.mark.parametrize("format", ["NETCDF3_CLASSIC", "NETCDF4"])
def test_netcdf_map_reorders_reverse_latitude_and_wrapped_longitude(tmp_path, format):
    def build(dataset):
        for name, size in [("time", 2), ("lat", 3), ("lon", 4)]:
            dataset.createDimension(name, size)
        lat = dataset.createVariable("lat", "f8", ("lat",)); lat[:] = [10, 20, 30]; lat.units = "degrees_north"
        lon = dataset.createVariable("lon", "f8", ("lon",)); lon[:] = [0, 90, 180, 270]; lon.units = "degrees_east"
        values = dataset.createVariable("temperature", "f8", ("time", "lon", "lat"))
        values[:] = np.arange(24).reshape(2, 4, 3)
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build, format=format), "netcdf", "map", {"variable": "temperature", "indices": {"time": 1}})
    expected = np.arange(24).reshape(2, 4, 3)[1, [2, 3, 0, 1], :][:, [2, 1, 0]].T
    assert result["x"] == [-180, -90, 0, 90]
    assert result["y"] == [30, 20, 10]
    assert result["width"] == 4 and result["height"] == 3
    assert result["values"] == expected.ravel().tolist()
    assert result["extent"] == [-180, 10, 90, 30]
    assert result["metadata"]["indices"] == {"time": 1}
    assert result["metadata"]["spatial_dimensions"] == ["lat", "lon"]


@pytest.mark.parametrize("lat,lon", [([30, 20, 10], [90, 0, -90]), ([10, 20, 30], [-90, 0, 90])])
def test_netcdf_map_reversal_preserves_cell_values(tmp_path, lat, lon):
    def build(dataset):
        for name in ("lat", "lon"):
            dataset.createDimension(name, 3)
        dataset.createVariable("lat", "f8", ("lat",))[:] = lat
        dataset.createVariable("lon", "f8", ("lon",))[:] = lon
        dataset.createVariable("value", "f8", ("lat", "lon"))[:] = np.arange(9).reshape(3, 3)
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "map")
    expected = np.arange(9).reshape(3, 3)[np.argsort(lat)[::-1]][:, np.argsort(lon)]
    assert result["values"] == expected.ravel().tolist()


@pytest.mark.parametrize("lat,lon", [([10, 20, 35], [-90, 0, 90]), ([10, 10, 30], [-90, 0, 90]), ([10, 100, 190], [-90, 0, 90]), ([10, 20, 30], [170, 180, 190]), ([10, 20, 30], [-180, 0, 180]), ([10, np.nan, 30], [-90, 0, 90])])
def test_netcdf_map_rejects_nonregular_invalid_or_ambiguous_wrap(tmp_path, lat, lon):
    def build(dataset):
        for name in ("lat", "lon"):
            dataset.createDimension(name, 3)
        dataset.createVariable("lat", "f8", ("lat",))[:] = lat
        dataset.createVariable("lon", "f8", ("lon",))[:] = lon
        dataset.createVariable("value", "f8", ("lat", "lon"))[:] = np.zeros((3, 3))
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "map")


@pytest.mark.parametrize("mode", ["no_coordinates", "curvilinear", "projected_units", "ambiguous_latitude", "rotated_latitude", "rotated_grid_mapping", "unknown_grid_mapping"])
def test_netcdf_map_does_not_invent_geographical_coordinates(tmp_path, mode):
    def build(dataset):
        dataset.createDimension("row", 2); dataset.createDimension("col", 3)
        dataset.createVariable("model", "f8", ("row", "col"))[:] = np.arange(6).reshape(2, 3)
        if mode == "no_coordinates":
            return
        dims = ("row", "col") if mode == "curvilinear" else ("row",)
        lat = dataset.createVariable("lat", "f8", dims)
        lat[:] = [[10] * 3, [20] * 3] if mode == "curvilinear" else [10, 20]
        lat.units = "metres" if mode == "projected_units" else "degrees_north"
        if mode == "rotated_latitude":
            lat.standard_name = "grid_latitude"
        lon = dataset.createVariable("lon", "f8", ("col",)); lon[:] = [0, 10, 20]; lon.units = "degrees_east"
        if mode == "ambiguous_latitude":
            dataset.createVariable("latitude", "f8", ("row",))[:] = [10, 20]
        if mode in {"rotated_grid_mapping", "unknown_grid_mapping"}:
            dataset.variables["model"].grid_mapping = "projection"
            if mode == "rotated_grid_mapping":
                projection = dataset.createVariable("projection", "i4")
                projection.grid_mapping_name = "rotated_latitude_longitude"
    data = netcdf_bytes(tmp_path, build)
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(data, "netcdf", "map", {"variable": "model"})
    result = worker.preview_bytes(data, "netcdf", "series", {"variable": "model", "x_dimension": "col", "indices": {"row": 1}})
    assert result["y"] == [3, 4, 5]


def test_netcdf_series_keeps_coordinate_precision(tmp_path):
    coordinates = [1.00000000001, 1.00000000002, 1.00000000003]
    def build(dataset):
        dataset.createDimension("step", 3)
        dataset.createVariable("step", "f8", ("step",))[:] = coordinates
        dataset.createVariable("signal", "f8", ("step",))[:] = [1, 2, 3]
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "series")
    assert result["x"] == coordinates
    assert len(set(result["x"])) == 3


@pytest.mark.parametrize("reference_attribute", ["bounds", "climatology", "ancillary_variables"])
def test_netcdf_default_prefers_actual_data_over_cf_auxiliary(tmp_path, reference_attribute):
    def build(dataset):
        for name, size in [("time", 2), ("lat", 2), ("lon", 3), ("bounds", 2)]:
            dataset.createDimension(name, size)
        time = dataset.createVariable("time", "f8", ("time",)); time[:] = [1, 2]
        setattr(time, reference_attribute, "auxiliary")
        dataset.createVariable("auxiliary", "f8", ("time", "bounds"))[:] = [[0, 1], [1, 2]]
        dataset.createVariable("lat", "f8", ("lat",))[:] = [10, 20]
        dataset.createVariable("lon", "f8", ("lon",))[:] = [0, 10, 20]
        dataset.createVariable("air", "f8", ("time", "lat", "lon"))[:] = np.arange(12).reshape(2, 2, 3)
    data = netcdf_bytes(tmp_path, build)
    for kind in ("series", "map"):
        result = worker.preview_bytes(data, "netcdf", kind)
        assert result["selected_variable"] == "air"
        assert "auxiliary" in [v["name"] for v in result["variables"]]
    explicit = worker.preview_bytes(data, "netcdf", "series", {"variable": "auxiliary"})
    assert explicit["selected_variable"] == "auxiliary"


def test_netcdf_default_allows_coordinate_when_it_is_the_only_series(tmp_path):
    def build(dataset):
        dataset.createDimension("step", 3)
        dataset.createVariable("step", "f8", ("step",))[:] = [0.25, 0.5, 0.75]
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "series")
    assert result["selected_variable"] == "step"
    assert result["y"] == [0.25, 0.5, 0.75]


def test_bundled_noaa_climatology_defaults_to_air_for_both_views():
    path = Path(__file__).resolve().parents[2] / "backend/app/resources/datasets/open-noaa-air-climatology/air.sig995.mon.ltm.1991-2020.nc"
    data = path.read_bytes()
    series = worker.preview_bytes(data, "netcdf", "series")
    map_result = worker.preview_bytes(data, "netcdf", "map")
    assert series["selected_variable"] == map_result["selected_variable"] == "air"
    assert series["metadata"]["indices"] == {"lat": 0, "lon": 0}
    assert len(series["x"]) == len(series["y"]) == 12
    with Dataset(path) as dataset:
        np.testing.assert_allclose(series["y"], dataset.variables["air"][:, 0, 0], rtol=1e-9)
    assert map_result["width"] <= 128 and map_result["height"] <= 128
    assert len(map_result["values"]) == map_result["width"] * map_result["height"]
    assert map_result["metadata"]["indices"] == {"time": 0}


def test_netcdf_series_sampling_is_bounded_and_labelled(tmp_path):
    def build(dataset):
        dataset.createDimension("step", 3001)
        dataset.createVariable("signal", "f8", ("step",))[:] = np.arange(3001) * 3
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "series")
    assert result["sampled"] is True
    assert result["x"] == list(range(0, 3001, 4))
    assert result["y"] == list(range(0, 9001, 12))
    assert len(result["x"]) <= worker.MAX_POINTS


def test_netcdf_map_sampling_has_explicit_coordinates_and_matching_values(tmp_path):
    def build(dataset):
        dataset.createDimension("lat", 257); dataset.createDimension("lon", 260)
        dataset.createVariable("lat", "f8", ("lat",))[:] = np.linspace(-64, 64, 257)
        dataset.createVariable("lon", "f8", ("lon",))[:] = np.linspace(-129.5, 129.5, 260)
        dataset.createVariable("data", "f8", ("lat", "lon"))[:] = np.arange(257 * 260).reshape(257, 260)
    result = worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "map")
    assert result["sampled"] is True
    assert result["width"] == result["height"] == 128
    assert len(result["values"]) == 128 ** 2
    assert result["values"][0] == 256 * 260
    assert result["values"][-1] == 259
    assert result["x"][0] == -129.5 and result["x"][-1] == 129.5
    assert result["y"][0] == 64 and result["y"][-1] == -64


@pytest.mark.parametrize("options", [{"indices": {"step": 0}}, {"indices": {"missing": 0}}, {"x_dimension": "missing"}, {"variable": "missing"}, {"hdu": 0}])
def test_netcdf_rejects_incompatible_selection(tmp_path, options):
    def build(dataset):
        dataset.createDimension("step", 4)
        dataset.createVariable("loss", "f8", ("step",))[:] = [1, 2, 3, 4]
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(netcdf_bytes(tmp_path, build), "netcdf", "series", options)


def test_fits_image_scales_blank_and_reports_shape_orientation():
    hdu = fits.PrimaryHDU(np.array([[1, 2, -999], [4, 5, 6]], dtype=np.int16))
    hdu.header["BLANK"] = -999; hdu.header["BSCALE"] = 2; hdu.header["BZERO"] = 10; hdu.header["BUNIT"] = "counts"
    result = worker.preview_bytes(fits_bytes(hdu), "fits", "image")
    assert result["values"] == [12, 14, None, 18, 20, 22]
    assert result["width"] == 3 and result["height"] == 2
    assert result["metadata"]["orientation"] == "array-row-zero-at-top"
    assert result["metadata"]["hdus"] == [{"index": 0, "shape": [2, 3], "kind": "image"}]
    assert result["metadata"]["spatial_dimensions"] == ["axis0", "axis1"]
    assert result["variables"][0]["dimensions"] == [{"name": "axis0", "size": 2}, {"name": "axis1", "size": 3}]


def test_fits_cube_slice_and_series_preserve_actual_values():
    cube = np.arange(24, dtype=np.float64).reshape(2, 3, 4)
    data = fits_bytes(fits.PrimaryHDU(cube))
    result = worker.preview_bytes(data, "fits", "image", {"indices": {"axis0": 1}})
    assert result["values"] == cube[1].ravel().tolist()
    assert result["metadata"]["indices"] == {"axis0": 1}
    series = worker.preview_bytes(data, "fits", "series", {"x_dimension": "axis0", "indices": {"axis1": 2, "axis2": 1}})
    assert series["x"] == [0, 1] and series["y"] == [9, 21]
    assert series["metadata"]["x_dimension"] == "axis0"


def test_fits_hdu_selection_distinguishes_image_and_series():
    data = fits_bytes(fits.PrimaryHDU(), fits.ImageHDU(np.array([2, 4, 6], dtype=float)), fits.ImageHDU(np.arange(6).reshape(2, 3)))
    image = worker.preview_bytes(data, "fits", "image")
    assert image["metadata"]["hdu"] == 2
    series = worker.preview_bytes(data, "fits", "series", {"hdu": 1})
    assert series["y"] == [2, 4, 6]
    with pytest.raises(worker.PreviewError, match="一维"):
        worker.preview_bytes(data, "fits", "image", {"hdu": 1})
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(data, "fits", "series", {"hdu": 99})


@pytest.mark.parametrize("kind", ["table", "compressed", "empty"])
def test_fits_unsupported_hdus_fail_explicitly(kind):
    if kind == "table":
        extra = fits.BinTableHDU.from_columns([fits.Column(name="flux", format="E", array=[1, 2])])
    elif kind == "compressed":
        extra = fits.CompImageHDU(np.ones((2, 3), dtype=np.int16))
    else:
        extra = fits.ImageHDU()
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(fits_bytes(fits.PrimaryHDU(), extra), "fits", "image")


def test_fits_sampling_limits_pixels_and_series():
    values = np.arange(260 * 270, dtype=float).reshape(260, 270)
    image = worker.preview_bytes(fits_bytes(fits.PrimaryHDU(values)), "fits", "image")
    assert image["sampled"] is True
    assert image["width"] <= worker.MAX_AXIS and image["height"] <= worker.MAX_AXIS
    assert image["values"] == values[::3, ::3].ravel().tolist()
    values = np.arange(2001, dtype=float)
    series = worker.preview_bytes(fits_bytes(fits.PrimaryHDU(values)), "fits", "series")
    assert series["x"] == list(range(0, 2001, 3))
    assert series["y"] == values[::3].tolist()
    assert len(series["y"]) <= worker.MAX_POINTS and series["sampled"]


@pytest.mark.parametrize("relative", ["nasa-hst-fos/FOSy19g0309t_c2f.fits", "nasa-hst-wfpc2/WFPC2ASSNu5780205bx.fits"])
def test_bundled_fits_image_and_series_keep_selected_hdu_values(relative):
    path = Path(__file__).resolve().parents[2] / "backend/app/resources/datasets" / relative
    data = path.read_bytes()
    for kind in ("image", "series"):
        result = worker.preview_bytes(data, "fits", kind)
        assert result["reader"] == "fits" and result["kind"] == kind
        with fits.open(path) as hdus:
            values = hdus[result["metadata"]["hdu"]].data
            if kind == "image":
                while values.ndim > 2:
                    values = values[0]
                values = values[::max(1, int(np.ceil(values.shape[0] / 128))), ::max(1, int(np.ceil(values.shape[1] / 128)))]
                assert result["values"] == worker.numbers(values)
            else:
                while values.ndim > 1:
                    values = values[0]
                values = values[::max(1, int(np.ceil(values.shape[0] / 1000)))]
                assert result["y"] == worker.numbers(values)


@pytest.mark.parametrize("kind,options", [("image", {"indices": {"axis0": 0}}), ("image", {"x_dimension": "axis0"}), ("series", {"variable": "HDU 0"}), ("series", {"indices": {"axis1": 1}}), ("series", {"indices": {"axis0": 2}}), ("series", {"indices": {"missing": 0}})])
def test_fits_rejects_invalid_or_ignored_options(kind, options):
    data = fits_bytes(fits.PrimaryHDU(np.ones((2, 3))))
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(data, "fits", kind, options)


def test_fastq_quality_is_position_weighted_and_explicit_encoding():
    data = fastq_record(b"ACGT", b"!+5?") + fastq_record(b"GC", b"5I")
    result = worker.preview_bytes(data, "fastq", "quality")
    assert result["kind"] == "quality"
    assert result["x"] == [1, 2, 3, 4]
    assert result["y"] == [10, 25, 20, 30]
    assert result["metadata"]["position_counts"] == [2, 2, 1, 1]
    assert result["metadata"]["gc_percent"] == 66.667
    assert result["metadata"]["reads_sampled"] == 2
    assert "Phred+33" in result["metadata"]["quality_encoding"]
    assert result["sampled"] is False


def test_fastq_record_and_position_limits_are_honest():
    data = fastq_record(b"A" * 501, b"I" * 501) * 1001
    result = worker.preview_bytes(data, "fastq", "quality")
    assert result["metadata"]["reads_sampled"] == 1000
    assert len(result["x"]) == len(result["y"]) == 500
    assert result["metadata"]["max_length"] == 501
    assert result["sampled"] is True


@pytest.mark.parametrize("tail", [b"@partial\n", b"@partial\nACGT\n+\nII", b"@partial\nACGT\n+\nIIII"])
def test_fastq_truncated_prefix_omits_incomplete_last_record(tail):
    result = worker.preview_bytes(fastq_record() + tail, "fastq", "quality", truncated=True)
    assert result["metadata"]["reads_sampled"] == 1
    assert result["y"] == [40, 40, 40, 40]
    assert result["sampled"] is True


def test_fastq_complete_final_record_need_not_end_newline():
    result = worker.preview_bytes(fastq_record().rstrip(b"\n"), "fastq", "quality")
    assert result["metadata"]["reads_sampled"] == 1


def test_fastq_accepts_ambiguous_nucleotides_and_rna_uracil():
    sequence = b"ACGUNRYKMSWBDHV"
    result = worker.preview_bytes(fastq_record(sequence, b"I" * len(sequence)), "fastq", "quality")
    assert result["metadata"]["bases_sampled"] == len(sequence)


@pytest.mark.parametrize("data", [b"@partial\nACGT\n+\n", b"@id\nACGT\n-\nIIII\n", b"id\nACGT\n+\nIIII\n", b"@id\nACGT\n+\nII\n", b"@id\nAC?T\n+\nIIII\n", b"@id\nACGT\n+\nII I\n", b"@id\nAC\nGT\n+\nIIII\n", b"@id\n\n+\n\n"])
def test_fastq_malformed_data_is_rejected(data):
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(data, "fastq", "quality")


@pytest.mark.parametrize("options", [[], "", 0, False, {"unknown": 1}, {"variable": "../secret"}, {"variable": ""}, {"variable": "a\nb"}, {"hdu": True}, {"hdu": 128}, {"indices": []}, {"indices": {"/tmp/secret": 0}}, {"indices": {"": 0}}, {"indices": {"step": True}}, {"indices": {"step": -1}}, {"indices": {str(i): 0 for i in range(9)}}])
def test_options_must_be_strict_and_bounded(options):
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(fastq_record(), "fastq", "quality", options)


def test_input_byte_limits_and_binary_truncation(monkeypatch):
    monkeypatch.setattr(worker, "MAX_INPUT_BYTES", 32)
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(b"x" * 33, "fits", "image")
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(b"", "fastq", "quality")
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(b"some data", "netcdf", "series", truncated=True)
    monkeypatch.setattr(worker, "MAX_FASTQ_BYTES", 2)
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(fastq_record(), "fastq", "quality")


def test_numbers_and_metadata_labels_hide_nonfinite_and_paths():
    assert worker.numbers(np.ma.array([1, 2, np.inf, np.nan], mask=[False, True, False, False])) == [1, None, None, None]
    assert worker.label("temperature\n(C)") == "temperature(C)"
    assert worker.label("/Users/private/secret.nc") == "[redacted]"
    assert worker.label("C:\\private\\secret.nc") == "[redacted]"
    assert len(worker.label("x" * 1000)) == 96


def test_worker_framing_succeeds_with_json_only():
    result, process = worker_process(fastq_record())
    assert result["ok"] is True
    assert result["data"]["contract_version"] == 1
    assert result["data"]["reader"] == "fastq"
    assert result["data"]["kind"] == "quality"
    assert not process.stdout.startswith(b"Traceback")


@pytest.mark.parametrize("patch", [{"contract_version": True}, {"contract_version": 2}, {"size": True}, {"size": 0}, {"size": worker.MAX_INPUT_BYTES + 1}, {"truncated": "false"}, {"extra": "ignored"}, {"options": []}])
def test_worker_rejects_invalid_protocol_fields(patch):
    data = fastq_record()
    header = dict(contract_version=1, reader="fastq", kind="quality", size=len(data), **{})
    header.update(patch)
    result, _ = worker_process(data, header=header)
    assert result["ok"] is False and "data" not in result


@pytest.mark.parametrize("raw_header", [b"{}", b"x" * 8193 + b"\n", b"[]\n", b"{invalid}\n"])
def test_worker_rejects_invalid_header_framing(raw_header):
    result, _ = worker_process(b"", raw_header=raw_header)
    assert result["ok"] is False


@pytest.mark.parametrize("size_delta", [-1, 1])
def test_worker_requires_exact_body_length(size_delta):
    data = fastq_record()
    header = dict(contract_version=1, reader="fastq", kind="quality", size=len(data) + size_delta)
    result, _ = worker_process(data, header=header)
    assert result["ok"] is False


def test_worker_hides_unexpected_parser_exception_details():
    result, process = worker_process(b"corrupt", header=dict(contract_version=1, reader="fits", kind="image", size=7))
    assert result["ok"] is False
    assert "Traceback" not in process.stdout.decode()
    assert "/tmp/" not in result["error"]
    assert "input.bin" not in result["error"]


def test_worker_python_and_native_diagnostics_cannot_corrupt_stdout():
    source = """
import ctypes, os
from app.services import visualization_worker as worker
def fake(*args, **kwargs):
    print('PYTHON DIAGNOSTIC')
    os.write(1, b'UNBUFFERED NATIVE DIAGNOSTIC\\n')
    ctypes.CDLL(None).printf(b'BUFFERED NATIVE DIAGNOSTIC\\n')
    return worker.base_result('fastq', 'quality')
worker.preview_bytes = fake
worker.main()
"""
    result, process = worker_process(b"x", source=source)
    assert result["ok"] is True
    assert b"DIAGNOSTIC" not in process.stdout
    assert b"PYTHON DIAGNOSTIC" in process.stderr
    assert b"UNBUFFERED NATIVE DIAGNOSTIC" in process.stderr
    assert b"BUFFERED NATIVE DIAGNOSTIC" in process.stderr


def test_worker_replaces_overlarge_output_with_bounded_error():
    source = """
from app.services import visualization_worker as worker
worker.preview_bytes = lambda *args, **kwargs: {'large': 'x' * worker.MAX_OUTPUT_BYTES}
worker.main()
"""
    result, _ = worker_process(b"x", source=source)
    assert result["ok"] is False and "上限" in result["error"]


def test_worker_replaces_nonfinite_output_with_static_error():
    source = """
from app.services import visualization_worker as worker
worker.preview_bytes = lambda *args, **kwargs: {'x': float('nan')}
worker.main()
"""
    result, _ = worker_process(b"x", source=source)
    assert result["ok"] is False
