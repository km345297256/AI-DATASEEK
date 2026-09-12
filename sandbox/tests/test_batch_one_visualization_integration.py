"""Exercise batch-one readers through the existing private worker protocol."""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile

import h5py
import numpy as np
import pytest
from netCDF4 import Dataset
from scipy.io import savemat

from app.services import extended_visualization_worker as worker
from app.services import visualization_worker as scientific


def encoded(data, *, reader, kind, format, options=None, **extra):
    header = {"contract_version": 2, "size": len(data), "reader": reader,
              "kind": kind, "format": format, "options": options or {}}
    header.update(extra)
    return json.loads(worker._encoded_result(io.BytesIO(json.dumps(header).encode() + b"\n" + data)))


def archive_bytes(fmt):
    output = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("measurements.csv", b"time,value\n0,1.5\n")
    elif fmt == "tar":
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            data = b"time,value\n0,1.5\n"
            entry = tarfile.TarInfo("measurements.csv")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    else:
        return gzip.compress(b"time,value\n0,1.5\n")
    return output.getvalue()


@pytest.mark.parametrize("fmt,data", [("json", b'{"value":9007199254740993}'),
                                      ("xml", b'<sample unit="K">280.25</sample>'),
                                      ("yaml", b"value: 9007199254740993\n"),
                                      ("yml", b"values: [1, 2.5]\n")])
def test_structure_private_protocol_end_to_end(fmt, data):
    result = encoded(data, reader="structure", kind="tree", format=fmt)
    assert result["ok"] is True
    payload = result["data"]
    assert payload["contract_version"] == 2 and payload["reader"] == "structure"
    assert payload["tree"][0]["path"] == "/0"
    assert payload["metadata"]["format"] == fmt
    if fmt in {"json", "yaml"}:
        assert payload["tree"][1]["attributes"]["value"] == "9007199254740993"


@pytest.mark.parametrize("fmt", ["zip", "tar", "gz", "gzip", "tgz"])
def test_archive_private_protocol_end_to_end(fmt):
    result = encoded(archive_bytes(fmt), reader="archive", kind="table", format=fmt)
    assert result["ok"] is True
    payload = result["data"]
    assert payload["reader"] == "archive" and payload["kind"] == "table"
    assert payload["table"]["total_rows"] == 1
    assert payload["metadata"]["contents_verified"] is False
    if fmt in {"zip", "tar"}:
        assert payload["table"]["rows"][0][0] == "measurements.csv"
    else:
        assert payload["metadata"]["stream_count"] is None


@pytest.mark.parametrize("options", [{"path": "/etc/passwd"}, {"code": "print(1)"}, {"row_offset": 0}])
def test_structure_cannot_inherit_other_readers_options(options):
    result = encoded(b"{}", reader="structure", kind="tree", format="json", options=options)
    assert result["ok"] is False and "/etc/" not in result["error"]


@pytest.mark.parametrize("options", [{"extract": "measurements.csv"}, {"row_offset": True}, {"row_offset": 4096}, {"row_offset": -1}])
def test_archive_private_protocol_cannot_extract_or_escape_pagination(options):
    assert encoded(archive_bytes("zip"), reader="archive", kind="table", format="zip", options=options)["ok"] is False


@pytest.mark.parametrize("selection", [("structure", "tree", "zip"), ("archive", "table", "json"),
                                      ("archive", "tree", "zip"), ("structure", "table", "json"),
                                      ("archive", "table", "rar"), ("archive", "table", "7z")])
def test_reader_kind_format_matrix_remains_fail_closed(selection):
    reader, kind, fmt = selection
    assert encoded(b"{}", reader=reader, kind=kind, format=fmt)["ok"] is False


def test_new_readers_reject_truncated_inputs_and_keep_output_budget(monkeypatch):
    assert encoded(b"{}", reader="structure", kind="tree", format="json", truncated=True)["ok"] is False
    assert encoded(archive_bytes("zip"), reader="archive", kind="table", format="zip", truncated=True)["ok"] is False
    monkeypatch.setattr(worker, "MAX_OUTPUT_BYTES", 64)
    result = encoded(b'{"value":1}', reader="structure", kind="tree", format="json")
    assert result["ok"] is False and "预算" in result["error"]


def test_new_readers_do_not_materialize_an_input_path(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure structure/archive readers must not create temporary input files")
    monkeypatch.setattr(worker.tempfile, "TemporaryDirectory", forbidden)
    assert encoded(b"{}", reader="structure", kind="tree", format="json")["ok"] is True
    assert encoded(archive_bytes("zip"), reader="archive", kind="table", format="zip")["ok"] is True


def matlab_v73_bytes(tmp_path):
    path = tmp_path / "synthetic-v73.mat"
    with h5py.File(path, "w", userblock_size=512) as root:
        node = root.create_dataset("signal", data=np.arange(6).reshape(2, 3))
        node.attrs["MATLAB_class"] = np.bytes_("double")
    # Disposable fixture only: reproduce the fixed MATLAB v7.3 user-block
    # signature without claiming MATLAB object/cell/reference compatibility.
    header = b"MATLAB 7.3 MAT-file, Platform: synthetic, Created for bounded reader regression"
    header = header.ljust(116, b" ") + b"\x00" * 8 + b"\x00\x02IM"
    with path.open("r+b") as stream:
        stream.write(header)
    return path.read_bytes()


@pytest.mark.parametrize("kind", ["tree", "series", "heatmap"])
def test_matlab_v73_hdf5_views_mark_storage_dimension_order(tmp_path, kind):
    data = matlab_v73_bytes(tmp_path)
    options = {} if kind == "tree" else {"path": "/signal"}
    result = encoded(data, reader="hdf5", kind=kind, format="mat", options=options)
    assert result["ok"] is True
    payload = result["data"]
    assert payload["metadata"]["dimension_order"] == "hdf5-storage"
    assert payload["metadata"]["source_format"] == "matlab-v7.3"
    assert payload["tree"][0]["shape"] == [2, 3]
    assert "MATLAB" in "".join(payload["warnings"])
    if kind == "series":
        assert payload["array"]["values"] == [0, 1, 2]
    if kind == "heatmap":
        assert payload["array"]["values"] == [0, 1, 2, 3, 4, 5]


def test_classic_mat_stays_on_scipy_reader_not_hdf5():
    stream = io.BytesIO()
    savemat(stream, {"signal": np.arange(6).reshape(2, 3)})
    data = stream.getvalue()
    assert encoded(data, reader="hdf5", kind="tree", format="mat")["ok"] is False
    result = encoded(data, reader="tabular", kind="heatmap", format="mat", options={"variable": "signal"})
    assert result["ok"] is True and result["data"]["array"]["values"] == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("fmt", ["NETCDF3_CLASSIC", "NETCDF3_64BIT_OFFSET", "NETCDF4"])
def test_netcdf_reader_routing_and_numeric_values_remain_unchanged(tmp_path, fmt):
    path = tmp_path / "synthetic.nc"
    with Dataset(path, "w", format=fmt) as dataset:
        dataset.createDimension("step", 3)
        dataset.createVariable("step", "f8", ("step",))[:] = [0, 0.25, 1.5]
        dataset.createVariable("loss", "f8", ("step",))[:] = [8, 4, 2]
    data = path.read_bytes()
    result = scientific.preview_bytes(data, "netcdf", "series", {"variable": "loss", "x_dimension": "step"})
    assert result["x"] == [0, 0.25, 1.5] and result["y"] == [8, 4, 2]
    tree = encoded(data, reader="hdf5", kind="tree", format="nc")
    assert tree["ok"] is (fmt == "NETCDF4")
    if fmt == "NETCDF4":
        assert "dimension_order" not in tree["data"]["metadata"]
