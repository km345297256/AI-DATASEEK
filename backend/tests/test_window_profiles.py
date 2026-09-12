"""Reader-specific broker budgets and resource scope never widen legacy EDF."""
import copy
import json
import threading

import pytest

from app.infrastructure.external.sandbox import window_visualization_worker as module
from test_window_visualization_worker import invoke, frame, line, request, SUCCESS

PROFILES = [("fcs-window", "fcs", 8*1024**2, 128, 2*1024**2), ("grib-window", "grib2", 8*1024**2, 128, 2*1024**2), ("seismic-window", "mseed", 8*1024**2, 128, 2*1024**2),
            ("columnar-window", "parquet", 8*1024**2, 128, 2*1024**2), ("nexus-window", "nxs", 8*1024**2, 128, 2*1024**2),
            ("array-window", "h5", 8*1024**2, 128, 2*1024**2), ("czi-window", "czi", 32*1024**2, 4096, 8*1024**2),
            ("instrument-window", "spe", 32*1024**2, 2048, 8*1024**2), ("ome-zarr", "zarr", 32*1024**2, 256, 2*1024**2)]


def resources():
    return [{"key": ".zattrs", "offset": 0, "size": 100}, {"key": ".zgroup", "offset": 100, "size": 100}, {"key": "0/.zarray", "offset": 200, "size": 100}, {"key": "0/0.0", "offset": 300, "size": 100}]


@pytest.mark.parametrize("reader,fmt,total,reads,output", PROFILES)
def test_profile_isolation_and_host_environment(monkeypatch, reader, fmt, total, reads, output):
    limits = {"max_read_bytes": 1024**2, "max_total_bytes": total, "max_reads": reads}
    extra = {"resources": resources(), "size": 400} if reader == "ome-zarr" else {}
    client, observed, run = invoke(monkeypatch, frame(line(request())) + frame(line(SUCCESS)), reader=reader, kind="tree", format=fmt, limits=limits, max_output_bytes=output, **extra)
    assert run()["ok"] and observed == [(0, 4)]
    init = json.loads(bytes(client.wire.sent).splitlines()[0])
    assert init["reader"] == reader and init["limits"] == limits
    assert client.settings["read_only"] and client.settings["network_mode"] == "none" and client.settings["user"] == "65534:65534"
    assert client.settings["environment"]["HDF5_PLUGIN_PRELOAD"] == "::"
    assert not {"mounts", "volumes", "ports", "privileged"} & client.settings.keys()
    assert client.cleaned and client.closed and client.wire.closed
    for key in limits:
        bad = dict(limits); bad[key] += 1
        with pytest.raises(module.VisualizationWorkerError):
            module.validate_limits(bad, reader)


@pytest.mark.parametrize("reader,fmt,total,reads,output", PROFILES)
def test_profile_specific_limits_do_not_expand_edf(reader, fmt, total, reads, output):
    assert module.WINDOW_PROFILES["edf"]["reads"] == 128 and module.WINDOW_PROFILES["edf"]["total"] == 8*1024**2 and module.WINDOW_PROFILES["edf"]["output"] == 2*1024**2
    with pytest.raises(module.VisualizationWorkerError):
        module.validate_limits({"max_read_bytes": 1024**2, "max_total_bytes": max(total, 8*1024**2+1), "max_reads": max(reads, 129)}, "edf")


@pytest.mark.parametrize("reader,fmt,total,reads,output", PROFILES)
@pytest.mark.parametrize("extra", [frame(line(SUCCESS)), frame(b"private diagnostic", 2), frame(line(request(id=2)))])
def test_each_new_profile_rejects_output_after_terminal(monkeypatch, reader, fmt, total, reads, output, extra):
    kwargs = {"resources": resources(), "size": 400} if reader == "ome-zarr" else {}
    client, observed, run = invoke(monkeypatch, frame(line(SUCCESS)) + extra, reader=reader, kind="tree", format=fmt, limits={"max_read_bytes": 1024**2, "max_total_bytes": total, "max_reads": reads}, max_output_bytes=output, **kwargs)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert not observed and client.cleaned and client.closed and client.wire.closed


@pytest.mark.parametrize("reader,fmt,total,reads,output", PROFILES)
def test_each_new_profile_cancel_during_range_cleans_exact_worker(monkeypatch, reader, fmt, total, reads, output):
    cancelled = threading.Event()
    def read(_offset, length):
        cancelled.set()
        return b"x" * length
    kwargs = {"resources": resources(), "size": 400} if reader == "ome-zarr" else {}
    client, observed, run = invoke(monkeypatch, frame(line(request())) + frame(line(SUCCESS)), read=read, cancelled=cancelled, reader=reader, kind="tree", format=fmt, limits={"max_read_bytes": 1024**2, "max_total_bytes": total, "max_reads": reads}, max_output_bytes=output, **kwargs)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert observed == [(0, 4)] and client.cleaned and client.closed


@pytest.mark.parametrize("key", ["../secret", "/private/x", "file:///x", "https://x", "0/../../x", "0/00.0", "0/0.0/../../../x", "0/.zarray?x", "x/.zarray"])
def test_resource_paths_not_a_filesystem_or_url_capability(key):
    rows = resources(); rows[-1]["key"] = key
    with pytest.raises(module.VisualizationWorkerError):
        module.validate_resources(rows, 400)


@pytest.mark.parametrize("mutate", [lambda r: r[1].__setitem__("offset", 99), lambda r: r[1].__setitem__("offset", 101),
    lambda r: r[1].__setitem__("size", True), lambda r: r[1].__setitem__("size", 0), lambda r: r[0].__setitem__("file_id", "foreign"),
    lambda r: r[-1].__setitem__("key", ".zattrs"), lambda r: r.pop(0)])
def test_resource_virtual_offsets_and_exact_fields_are_strict(mutate):
    rows = resources(); mutate(rows)
    with pytest.raises(module.VisualizationWorkerError):
        module.validate_resources(rows, 400)


def test_resource_map_cannot_expand_other_readers(monkeypatch):
    client, observed, run = invoke(monkeypatch, frame(line(SUCCESS)), resources=resources())
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.settings is None and not observed


def test_scope_range_cannot_straddle_two_members(monkeypatch):
    client, observed, run = invoke(monkeypatch, frame(line(request(offset=98, length=4))) + frame(line(SUCCESS)), reader="ome-zarr", kind="tree", format="zarr", size=400, resources=resources())
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert not observed and client.cleaned


def envi_resources():
    """Opaque two-member grant, never dataset names or host paths."""
    return [{"key": "header", "offset": 0, "size": 128},
            {"key": "data", "offset": 128, "size": 512}]


def test_envi_profile_exact_pair_and_confinement(monkeypatch):
    limits = {"max_read_bytes": 1024**2, "max_total_bytes": 8*1024**2, "max_reads": 256}
    content = frame(line(request())) + frame(line(request(id=2, offset=128))) + frame(line(SUCCESS))
    client, observed, run = invoke(monkeypatch, content, reader="envi-window", format="hdr", kind="tree",
                                  resources=envi_resources(), size=640, limits=limits)
    assert run()["ok"] and observed == [(0, 4), (128, 4)]
    init = json.loads(bytes(client.wire.sent).splitlines()[0])
    assert init["resources"] == envi_resources() and init["limits"] == limits
    assert init["reader"] == "envi-window" and init["format"] == "hdr"
    settings = client.settings
    assert settings["user"] == "65534:65534" and settings["read_only"] and settings["network_mode"] == "none"
    assert settings["mem_limit"] == settings["memswap_limit"] == "1g" and settings["pids_limit"] == 96
    assert settings["cap_drop"] == ["ALL"] and settings["security_opt"] == ["no-new-privileges:true"]
    assert "noexec" in settings["tmpfs"]["/tmp"] and settings["environment"]["HDF5_PLUGIN_PRELOAD"] == "::"
    assert not {"mounts", "volumes", "ports", "privileged"} & settings.keys()
    assert client.cleaned and client.closed and client.wire.closed
    for key in limits:
        raised = {**limits, key: limits[key] + 1}
        with pytest.raises(module.VisualizationWorkerError):
            module.validate_limits(raised, "envi-window")
    with pytest.raises(module.VisualizationWorkerError):
        module.validate_limits(limits, "edf")


@pytest.mark.parametrize("mutate", [lambda r: r.reverse(), lambda r: r.pop(),
    lambda r: r.append({"key": "extra", "offset": 640, "size": 1}),
    lambda r: r[0].__setitem__("key", "cube.hdr"), lambda r: r[1].__setitem__("key", "../data"),
    lambda r: r[1].__setitem__("key", "https://example.invalid/data"),
    lambda r: r[1].__setitem__("file_id", "foreign"),
    lambda r: r[0].__setitem__("offset", False), lambda r: r[1].__setitem__("size", True),
    lambda r: r[1].__setitem__("size", 0), lambda r: r[1].__setitem__("offset", 127),
    lambda r: r[1].__setitem__("offset", 129), lambda r: r[0].__setitem__("size", 65537)])
def test_envi_resource_capability_rejected_before_worker_creation(monkeypatch, mutate):
    rows = envi_resources(); mutate(rows)
    client, observed, run = invoke(monkeypatch, frame(line(SUCCESS)), reader="envi-window", format="hdr", kind="tree",
                                  resources=rows, size=640)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.settings is None and not observed


@pytest.mark.parametrize("reader,fmt,kind", [("edf", "edf", "series"), ("array-window", "h5", "tree"),
                                            ("ome-zarr", "zarr", "tree")])
def test_envi_pair_is_not_a_resource_grant_for_other_readers(monkeypatch, reader, fmt, kind):
    client, observed, run = invoke(monkeypatch, frame(line(SUCCESS)), reader=reader, format=fmt, kind=kind,
                                  resources=envi_resources(), size=640)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.settings is None and not observed


@pytest.mark.parametrize("change", [{"offset": 126}, {"offset": 638}, {"offset": 640}, {"offset": True},
                                   {"path": "cube.bin"}, {"resource": "data"}])
def test_envi_read_cannot_cross_members_or_invent_address_fields(monkeypatch, change):
    client, observed, run = invoke(monkeypatch, frame(line(request(**change))) + frame(line(SUCCESS)),
                                  reader="envi-window", format="hdr", kind="tree", resources=envi_resources(), size=640)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert not observed and client.cleaned and client.closed and client.wire.closed


def test_envi_cancelled_range_never_sends_bytes_and_cleans_worker(monkeypatch):
    cancelled = threading.Event()
    def read(_offset, length):
        cancelled.set()
        return b"x" * length
    client, observed, run = invoke(monkeypatch, frame(line(request())) + frame(line(SUCCESS)), read=read,
                                  cancelled=cancelled, reader="envi-window", format="hdr", kind="tree",
                                  resources=envi_resources(), size=640)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert observed == [(0, 4)] and len(bytes(client.wire.sent).splitlines()) == 1
    assert client.cleaned and client.closed and client.wire.closed


def test_envi_range_count_limit_is_reader_specific(monkeypatch):
    content = b"".join(frame(line(request(id=i))) for i in range(1, 258)) + frame(line(SUCCESS))
    client, observed, run = invoke(monkeypatch, content, reader="envi-window", format="hdr", kind="tree",
                                  resources=envi_resources(), size=640,
                                  limits={"max_read_bytes": 1024**2, "max_total_bytes": 8*1024**2, "max_reads": 256})
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert observed == [(0, 4)] * 256 and client.cleaned and client.closed and client.wire.closed


@pytest.mark.parametrize("extra", [frame(line(SUCCESS)), frame(b"private diagnostic", 2), frame(line(request(id=2)))])
def test_envi_terminal_state_rejects_trailing_output(monkeypatch, extra):
    client, observed, run = invoke(monkeypatch, frame(line(SUCCESS)) + extra, reader="envi-window", format="hdr", kind="tree",
                                  resources=envi_resources(), size=640)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert not observed and client.cleaned and client.closed and client.wire.closed
