"""Offline safety regression tests; no application/network/DB mutation."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_safety", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load_script("prepare_visualization_datasets")
checks = load_script("check_plugin_dataset_samples")


@pytest.mark.parametrize("name,plugin", [
    ("nested/scan.ome.tiff", {"extensions": ["ome.tiff"], "filenames": []}),
    ("SCAN.OME.TIFF", {"extensions": ["ome.tiff"], "filenames": []}),
    ("store/.zgroup", {"extensions": [], "filenames": [".zgroup", ".zattrs"]}),
    ("store/.zattrs", {"extensions": [], "filenames": [".zgroup", ".zattrs"]}),
    ("POSCAR", {"extensions": ["vasp"], "filenames": ["poscar", "contcar"]}),
])
def test_compound_suffixes_and_exact_names(name, plugin):
    prepare.validate_plugin_entry({"path": name, "role": "data"}, plugin)


@pytest.mark.parametrize("name,role", [
    ("LICENSE.txt", "documentation"), ("license.sdf", "documentation"),
    ("other.txt", "data"), ("fake.sdf.txt", "data"),
])
def test_wrong_format_and_documentation_cannot_be_plugin_entry(name, role):
    with pytest.raises(ValueError, match="data file.*registered"):
        prepare.validate_plugin_entry({"path": name, "role": role}, {"extensions": ["sdf"], "filenames": []})


def test_plain_tiff_is_not_automatically_compound_ome_tiff():
    with pytest.raises(ValueError):
        prepare.validate_plugin_entry({"path": "scan.tiff", "role": "data"}, {"extensions": ["ome.tiff"]})


@pytest.mark.parametrize("files", [
    [{"path": "a", "derived_from": ["b"]}, {"path": "b", "derived_from": ["a"]}],
    [{"path": "a", "url": "https://example.org/a", "derived_from": ["b"]}, {"path": "b", "derived_from": ["a"]}],
    [{"path": "a", "derived_from": ["a"]}],
    [{"path": "a", "derived_from": ["missing"]}],
    [{"path": "a", "derived_from": []}],
    [{"path": "a", "derived_from": ["b", "b"]}, {"path": "b", "url": "https://example.org/b"}],
    [{"path": "a"}],
])
def test_derivations_cannot_cycle_or_end_without_reviewed_source(files):
    with pytest.raises(ValueError):
        prepare.validate_derivations(files, profile="plugin-tests")


def test_large_derivation_chain_is_iterative_and_accepts_reviewed_leaves():
    files = [{"path": "0", "url": "https://example.org/original"}]
    files += [{"path": str(i), "derived_from": [str(i - 1)]} for i in range(1, 3000)]
    prepare.validate_derivations(files, profile="open-science")
    files[0] = {"path": "0", "origin": {"kind": "generated_fixture"}}
    prepare.validate_derivations(files, profile="plugin-tests")
    with pytest.raises(ValueError):
        prepare.validate_derivations(files, profile="open-science")


@pytest.mark.parametrize("base", ["https://example.org", "http://example.org", "http://127.0.0.1:7001/user", "http://x:y@127.0.0.1", "http://127.0.0.1/?key=1"])
def test_smoke_checker_rejects_nonloopback_origins(base):
    with pytest.raises(ValueError, match="loopback"):
        checks.LocalCheck(base)


def test_loopback_checker_does_not_load_environment_proxies(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://external-proxy.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://external-proxy.invalid:8080")
    monkeypatch.setenv("no_proxy", "")
    client = checks.LocalCheck("http://127.0.0.1:7001")
    assert all(not getattr(handler, "proxies", {}) for handler in client.opener.handlers)
    assert any(isinstance(handler, checks.NoRedirect) for handler in client.opener.handlers)


@pytest.mark.parametrize("destination", ["https://external.invalid/stolen", "http://127.0.0.1:7002/other", "http://127.0.0.1:7001/same-origin"])
def test_all_redirects_are_blocked_before_followup(destination):
    request = urllib.request.Request("http://127.0.0.1:7001/api/v1/datasets")
    with pytest.raises(ValueError, match="Redirects"):
        checks.NoRedirect().redirect_request(request, None, 302, "Found", {}, destination)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, url):
        super().__init__(payload)
        self.url = url

    def geturl(self):
        return self.url


def test_request_rejects_unexpected_final_origin_without_reading_body():
    client = checks.LocalCheck("http://127.0.0.1:7001")
    response = FakeResponse(b'{"code":0,"data":{}}', "https://external.invalid/")
    client.opener = SimpleNamespace(open=lambda *a, **kw: response)
    with pytest.raises(ValueError, match="left the loopback"):
        client.request("/datasets")


def byte_client(payload):
    client = checks.LocalCheck("http://127.0.0.1:7001")
    digest = hashlib.sha256(payload).hexdigest()

    def request(path, body=None, *, binary=False):
        if path.endswith("/files/preview"):
            return {"file": {"file_id": "safe-file-id"}}
        if path.endswith("/visualization"):
            assert binary and body["operation"] == "bytes"
            return {"bytes": len(payload), "sha256": digest}
        return {"files": [{"path": "sample.obj", "size": len(payload)}]}

    client.request = request
    plugins = {"obj": {"enabled": True, "capabilities": {"operations": ["bytes"]}}}
    case = {"plugin_id": "obj", "dataset_id": "viz-sample", "entry_file": "sample.obj"}
    return client, plugins, case


def test_correct_expected_hash_passes_and_optional_hash_is_backward_compatible():
    client, plugins, case = byte_client(b"expected geometry")
    assert client.check(case, plugins)["status"] == "passed"
    case["expected_file_sha256"] = hashlib.sha256(b"expected geometry").hexdigest()
    assert client.check(case, plugins)["status"] == "passed"


def test_same_size_wrong_preview_bytes_are_rejected_by_expected_hash():
    client, plugins, case = byte_client(b"other")
    case["expected_file_sha256"] = hashlib.sha256(b"right").hexdigest()
    with pytest.raises(ValueError, match="expected file SHA256"):
        client.check(case, plugins)


@pytest.mark.parametrize("digest", ["bad", "A" * 64, 123])
def test_malformed_expected_hash_is_rejected(digest):
    client, plugins, case = byte_client(b"data")
    case["expected_file_sha256"] = digest
    with pytest.raises(ValueError, match="Invalid expected"):
        client.check(case, plugins)


def test_disabled_plugins_are_not_enabled_or_marked_passed():
    client, plugins, case = byte_client(b"data")
    plugins["obj"]["enabled"] = False
    client.request = lambda *a, **k: pytest.fail("Disabled plugin must not trigger preview")
    assert client.check(case, plugins)["status"] == "disabled"


@pytest.mark.parametrize("status,exit_code", [("failed", 1), ("passed", 0), ("disabled", 0), ("not_run", 0)])
def test_cli_failure_status_controls_exit_code_and_report(tmp_path, monkeypatch, status, exit_code):
    current = {"datasets": {"viz-sample": "fingerprint"}, "plugins": {"obj": True}}

    class FakeClient:
        def __init__(self, base):
            pass

        def snapshot(self):
            return deepcopy(current)

        def request(self, path):
            return {"plugins": [{"id": "obj", "enabled": True}]}

        def check(self, case, plugins):
            if status == "failed":
                raise ValueError("intentional safe test failure")
            return {"status": status}

    baseline, matrix, report = [tmp_path / name for name in ("baseline.json", "matrix.json", "report.json")]
    baseline.write_text(json.dumps(current))
    matrix.write_text(json.dumps([{"plugin_id": "obj", "dataset_id": "viz-sample", "entry_file": "sample.obj"}]))
    monkeypatch.setattr(checks, "LocalCheck", FakeClient)
    monkeypatch.setattr(sys, "argv", ["check", "check", "--baseline", str(baseline), "--matrix", str(matrix), "--report", str(report)])
    if exit_code:
        with pytest.raises(SystemExit) as error:
            checks.main()
        assert error.value.code == exit_code
    else:
        checks.main()
    assert json.loads(report.read_text())["results"][0]["status"] == status


VERSION = hashlib.sha256(b"current-file-snapshot").hexdigest()
REVISION = hashlib.sha256(b"current-cordis-catalog").hexdigest()


def envelope(pid, kind, *, version=VERSION, revision=REVISION, payload=None):
    return {"contract_version": 2, "plugin_id": pid, "kind": kind,
            "version": version, "revision": revision, "payload": payload or {},
            "metadata": {}, "warnings": [], "sampled": False}


def preview_client(reader, *, options=None):
    client = checks.LocalCheck("http://127.0.0.1:7001")
    case = {"plugin_id": "viz-sample", "dataset_id": "viz-dataset", "entry_file": "sample.bin",
            "preview_options": options or {"kind": "image", "roi": [0, 0, 10, 10]}}
    plugins = {"viz-sample": {"enabled": True, "reader": reader, "capabilities": {"operations": ["preview"]}}}
    requests = []

    def request(path, body=None, *, binary=False):
        requests.append((path, deepcopy(body)))
        if path.endswith("/files/preview"):
            return {"file": {"file_id": "source-file"}}
        if path.endswith("/visualization"):
            return envelope("viz-sample", "tree" if body.get("kind") == "tree" else "raster")
        return {"files": [{"path": "sample.bin", "size": 100}]}

    client.request = request
    return client, plugins, case, requests


@pytest.mark.parametrize("reader", sorted(checks.CATALOG_FIRST_READERS))
def test_rich_preview_pins_actual_catalog_version_before_selection(reader):
    client, plugins, case, requests = preview_client(reader)
    original = deepcopy(case)
    result = client.check(case, plugins)
    reads = [body for path, body in requests if path.endswith("/visualization")]
    assert reads == [
        {"plugin_id": "viz-sample", "operation": "preview", "kind": "tree", "options": {}},
        {"plugin_id": "viz-sample", "operation": "preview", "kind": "image",
         "options": {"roi": [0, 0, 10, 10]}, "version": VERSION},
    ]
    assert result["status"] == "passed" and result["checks"][0]["phase"] == "catalog"
    assert case == original


def test_direct_numeric_reader_does_not_invent_unsupported_tree_request():
    client, plugins, case, requests = preview_client("mca", options={"kind": "series"})
    assert client.check(case, plugins)["status"] == "passed"
    reads = [body for path, body in requests if path.endswith("/visualization")]
    assert len(reads) == 1 and reads[0]["kind"] == "series" and "version" not in reads[0]


def test_initial_catalog_without_selection_is_not_read_twice():
    client, plugins, case, requests = preview_client("mass-spectrum", options={"kind": "tree"})
    assert client.check(case, plugins)["status"] == "passed"
    assert sum(path.endswith("/visualization") for path, _ in requests) == 1


@pytest.mark.parametrize("reader,options", [
    ("archive-member", {"member_id": "safe-member"}),
    ("mass-spectrum", {"kind": "tree", "offset": 64}),
    ("grib-window", {"offset": 32}),
    ("seismic-window", {"kind": "tree", "record_offset": 1}),
])
def test_catalog_continuations_also_pin_first_unfiltered_catalog(reader, options):
    client, plugins, case, requests = preview_client(reader, options=options)
    assert client.check(case, plugins)["status"] == "passed"
    reads = [body for path, body in requests if path.endswith("/visualization")]
    assert len(reads) == 2 and reads[0]["options"] == {} and reads[1]["version"] == VERSION


def test_static_matrix_version_cannot_bypass_live_catalog():
    client, plugins, case, requests = preview_client("nexus-window")
    case["preview_options"]["version"] = "0" * 64
    with pytest.raises(ValueError, match="Static preview versions"):
        client.check(case, plugins)
    assert not any(path.endswith("/visualization") for path, _ in requests)


@pytest.mark.parametrize("mutation", ["version", "revision", "plugin_id", "contract_version", "kind", "payload"])
def test_invalid_catalog_envelope_is_rejected_before_selected_read(mutation):
    client, plugins, case, requests = preview_client("nexus-window")
    original = client.request

    def request(path, body=None, **kwargs):
        value = original(path, body, **kwargs)
        if path.endswith("/visualization"):
            value[mutation] = None
        return value

    client.request = request
    with pytest.raises(ValueError, match="Invalid unified"):
        client.check(case, plugins)
    assert sum(path.endswith("/visualization") for path, _ in requests) == 1


@pytest.mark.parametrize("field", ["version", "revision"])
def test_selected_response_must_match_dynamic_catalog_pin(field):
    client, plugins, case, _ = preview_client("nexus-window")
    original = client.request

    def request(path, body=None, **kwargs):
        result = original(path, body, **kwargs)
        if path.endswith("/visualization") and body.get("kind") == "image":
            result[field] = "1" * 64
        return result

    client.request = request
    with pytest.raises(ValueError, match="changed after catalog"):
        client.check(case, plugins)


JOB_ID = "7" * 32


def fastqc_client(*, status="succeeded", tool="visualization:viz-fastqc", supplied=True):
    client = checks.LocalCheck("http://127.0.0.1:7001")
    case = {"plugin_id": "viz-fastqc", "dataset_id": "viz-fastq", "entry_file": "sample.fastq",
            "preview_options": {"confirm": True}}
    if supplied:
        client.completed_jobs = {"viz-fastqc": {"dataset_id": "viz-fastq", "entry_file": "sample.fastq", "job_id": JOB_ID}}
    plugins = {"viz-fastqc": {"enabled": True, "reader": "fastqc", "capabilities": {"operations": ["job"]}}}
    requests = []

    def request(path, body=None, **kwargs):
        requests.append((path, deepcopy(body)))
        if path.endswith("/files/preview"):
            return {"file": {"file_id": "source-fastq"}}
        if "/jobs/" in path:
            assert body is None, "Jobs must be GET-only"
            if path.endswith("/result"):
                return envelope("viz-fastqc", "report", payload={"sections": [{"name": "Basic statistics"}]})
            return {"job_id": JOB_ID, "tool_name": tool, "status": status}
        return {"files": [{"path": "sample.fastq", "size": 123}]}

    client.request = request
    return client, plugins, case, requests


def test_fastqc_reads_only_explicit_successful_file_scoped_job_and_report():
    client, plugins, case, requests = fastqc_client()
    result = client.check(case, plugins)
    assert result["status"] == "passed" and result["checks"][0]["method"] == "GET"
    assert [(path, body) for path, body in requests if "/jobs/" in path] == [
        ("/files/source-fastq/visualization/jobs/" + JOB_ID, None),
        ("/files/source-fastq/visualization/jobs/" + JOB_ID + "/result", None),
    ]
    assert not any(path.endswith("/visualization") for path, _ in requests)


def test_fastqc_missing_snapshot_is_not_run_and_never_automatically_selects_a_job():
    client, plugins, case, requests = fastqc_client(supplied=False)
    assert client.check(case, plugins)["status"] == "not_run"
    assert not any("/jobs" in path for path, _ in requests)


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "interrupted"])
def test_fastqc_non_success_is_not_run_and_never_cancelled_or_rerun(status):
    client, plugins, case, requests = fastqc_client(status=status)
    assert client.check(case, plugins)["status"] == "not_run"
    assert not any(path.endswith("/result") for path, _ in requests)


def test_fastqc_other_tool_is_not_accepted_as_success():
    client, plugins, case, requests = fastqc_client(tool="other-tool")
    with pytest.raises(ValueError, match="not the requested FastQC"):
        client.check(case, plugins)
    assert not any(path.endswith("/result") for path, _ in requests)


def test_fastqc_snapshot_bound_to_different_dataset_is_rejected():
    client, plugins, case, requests = fastqc_client()
    client.completed_jobs["viz-fastqc"]["dataset_id"] = "different-dataset"
    with pytest.raises(ValueError, match="different sample"):
        client.check(case, plugins)
    assert not any("/jobs/" in path for path, _ in requests)


@pytest.mark.parametrize("value", [[], {"arbitrary-plugin": {}}, {"viz-fastqc": {"job_id": JOB_ID}},
    {"viz-fastqc": {"dataset_id": "x", "entry_file": "x", "job_id": "../../arbitrary"}},
    {"viz-fastqc": {"dataset_id": "x", "entry_file": "x", "job_id": JOB_ID, "confirm": True}}])
def test_invalid_runtime_job_snapshots_are_rejected(value):
    with pytest.raises(ValueError):
        checks.completed_jobs(value)


@pytest.mark.parametrize("suffix", ["", "/cancel", "/result"])
def test_transport_guard_refuses_any_job_post(suffix):
    client = checks.LocalCheck("http://127.0.0.1:7001")
    client.opener = SimpleNamespace(open=lambda *a, **kw: pytest.fail("Job POST must be refused before network"))
    with pytest.raises(ValueError, match="Only read-preview POST"):
        client.request("/files/safe/visualization/jobs/" + JOB_ID + suffix, {})


@pytest.mark.parametrize("code", [404, 409])
def test_missing_or_stale_fastqc_job_is_not_run(code):
    client, plugins, case, _ = fastqc_client()
    original = client.request

    def request(path, body=None, **kwargs):
        if "/jobs/" in path:
            raise checks.urllib.error.HTTPError("http://127.0.0.1/", code, "unavailable", {}, None)
        return original(path, body, **kwargs)

    client.request = request
    assert client.check(case, plugins)["status"] == "not_run"
