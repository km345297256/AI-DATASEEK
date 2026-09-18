"""Serial local-only smoke checks of an explicit plugin/sample matrix.

Does not start Agent sessions, call models, change plugin preferences or mutate
dataset metadata. File-preview POSTs use the application's existing read cache.
This checks contract/readability, not every frontend interaction or format.
Rich previews first read the file's catalog and pin its returned version. Job
plugins never start or cancel work: an explicit completed-job snapshot is needed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request


# Mirrors the application's catalog-first readers, including bundle-scoped
# formats handled before unified_visualization's ordinary file-version fence.
CATALOG_FIRST_READERS = frozenset({
    "matrix-workbench", "astronomy-workbench", "alignment-browser",
    "sequence-browser", "genome-tracks", "blast-hits", "archive-member", "czi",
    "sqlite-table", "database-table", "sql-dump", "pg-dump", "bson", "redis-rdb",
    "dicom-window", "spatial-window", "pointcloud-window", "gro-trajectory",
    "simulation-mesh", "radar-window", "ugrid-window", "array-window",
    "czi-window", "instrument-window", "nexus-window", "columnar-window",
    "seismic-window", "grib-window", "fcs-window", "mass-spectrum", "diffraction",
    "ome-zarr", "envi-window", "ripple-window",
})
RESULT_KINDS = frozenset({"page", "series", "raster", "table", "array", "tree",
                         "media", "report", "molecule", "resources", "features", "graph", "geometry"})


def checked_result(result, plugin_id, *, version=None, revision=None):
    """Validate the public envelope and the pin; never manufacture a version."""
    if (not isinstance(result, dict) or result.get("contract_version") != 2
            or result.get("plugin_id") != plugin_id or result.get("kind") not in RESULT_KINDS
            or not isinstance(result.get("payload"), dict)
            or any(not isinstance(result.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", result[key])
                   for key in ("version", "revision"))):
        raise ValueError("Invalid unified visualization response")
    if version is not None and result["version"] != version:
        raise ValueError("Preview file version changed after catalog read")
    if revision is not None and result["revision"] != revision:
        raise ValueError("Plugin revision changed after catalog read")
    return {"kind": result["kind"], "version": result["version"], "revision": result["revision"]}


def completed_jobs(value):
    """Runtime-only snapshots bind job IDs to an exact declared sample."""
    if not isinstance(value, dict) or set(value) - {"viz-fastqc"}:
        raise ValueError("Only explicit FastQC completed-job snapshots are supported")
    for spec in value.values():
        if (not isinstance(spec, dict) or set(spec) != {"dataset_id", "entry_file", "job_id"}
                or not all(isinstance(spec.get(k), str) and spec[k] for k in spec)
                or not re.fullmatch(r"[0-9a-f]{32}", spec["job_id"])):
            raise ValueError("Invalid completed-job snapshot")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError("Redirects are not permitted for local preview checks")


class LocalCheck:
    def __init__(self, base):
        url = urlsplit(base)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("Only an explicit loopback application origin is allowed")
        self.base = base.rstrip("/") + "/api/v1"
        # The operator's proxy environment and server redirects must not turn a
        # loopback-only check into a request to another machine.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.completed_jobs = {}

    def request(self, path, body=None, *, binary=False):
        if not path.startswith("/") or ".." in path or "://" in path:
            raise ValueError("Invalid API path")
        if body is not None and not (path.endswith("/files/preview") or path.endswith("/visualization")):
            raise ValueError("Only read-preview POST operations are permitted")
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=100) as response:
            final = urlsplit(response.geturl())
            origin = urlsplit(self.base)
            if (final.scheme, final.hostname, final.port) != (origin.scheme, origin.hostname, origin.port):
                raise ValueError("Preview response left the loopback application origin")
            if binary:
                digest, size = hashlib.sha256(), 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 268435456: raise ValueError("Whole-file response exceeded hard budget")
                    digest.update(chunk)
                return {"bytes": size, "sha256": digest.hexdigest()}
            raw = response.read(20 * 1024**2 + 1)
            if len(raw) > 20 * 1024**2: raise ValueError("JSON response too large")
        value = json.loads(raw)
        if value.get("code") != 0: raise ValueError(str(value.get("msg", "API rejected request"))[:250])
        return value["data"]

    def snapshot(self):
        datasets = []
        while True:
            page = self.request(f"/datasets/manage?limit=100&offset={len(datasets)}")
            datasets.extend(page["datasets"])
            if len(datasets) >= page["total"]: break
            if not page["datasets"]: raise ValueError("Dataset pagination did not advance")
        plugins = self.request("/visualizations")["plugins"]
        return {"datasets": {d["dataset_id"]: hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest() for d in datasets},
                "plugins": {p["id"]: p["enabled"] for p in plugins}}

    def completed_fastqc(self, case, endpoint):
        spec = completed_jobs(self.completed_jobs).get("viz-fastqc")
        if spec is None:
            return {"status": "not_run", "reason": "No explicit completed FastQC job supplied; this check never starts jobs"}
        if any(spec[key] != case[key] for key in ("dataset_id", "entry_file")):
            raise ValueError("Completed job snapshot belongs to a different sample")
        path = endpoint + "/jobs/" + spec["job_id"]
        try:
            job = self.request(path)
            if not isinstance(job, dict) or job.get("job_id") != spec["job_id"] or job.get("tool_name") != "visualization:viz-fastqc":
                raise ValueError("Existing job is not the requested FastQC job")
            if job.get("status") != "succeeded":
                return {"status": "not_run", "reason": "Specified FastQC job has not succeeded; no work started or cancelled"}
            result = self.request(path + "/result")
        except urllib.error.HTTPError as error:
            if error.code in {404, 409}:
                return {"status": "not_run", "reason": "Completed FastQC job/result is unavailable or stale; no rerun started"}
            raise
        checked = checked_result(result, "viz-fastqc")
        sections = result["payload"].get("sections")
        if result["kind"] != "report" or not isinstance(sections, list) or not 1 <= len(sections) <= 32:
            raise ValueError("Completed FastQC result is not a bounded report")
        return {"status": "passed", "checks": [{"operation": "completed_job_result", "method": "GET",
            "job_id": spec["job_id"], "sections": len(sections), **checked}]}

    def check(self, case, plugins):
        pid = case["plugin_id"]
        plugin = plugins[pid]
        if not plugin["enabled"]:
            return {"status": "disabled", "reason": "Existing plugin preference preserved; not enabled by this check"}
        did, entry = case["dataset_id"], case["entry_file"]
        expected_digest = case.get("expected_file_sha256")
        if expected_digest is not None and (not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
            raise ValueError("Invalid expected file SHA256")
        dataset = self.request("/datasets/" + quote(did, safe=""))
        sources = [f for f in dataset["files"] if f["path"] == entry or f["path"].endswith("/" + entry)]
        if len(sources) != 1: raise ValueError("Expected exactly one preview entry")
        source = sources[0]
        prepared = self.request("/datasets/" + quote(did, safe="") + "/files/preview", {"path": source["path"], "plugin_id": pid})
        identifier = prepared["file"]["file_id"]
        endpoint = "/files/" + quote(identifier, safe="") + "/visualization"
        operations = plugin["capabilities"]["operations"]
        if pid == "viz-fastqc" and operations == ["job"]:
            return self.completed_fastqc(case, endpoint)
        checked = []
        version, revision = None, None
        for operation in ("page", "prepare", "preview", "bytes"):
            if operation not in operations: continue
            payload = {"plugin_id": pid, "operation": operation}
            if operation == "preview":
                options = dict(case.get("preview_options", {}))
                if "version" in options:
                    raise ValueError("Static preview versions are not accepted; use the live catalog version")
                kind = options.pop("kind", None)
                if kind is not None:
                    payload["kind"] = kind
                needs_catalog = (plugin.get("reader") in CATALOG_FIRST_READERS and (
                    kind not in {None, "tree"} or "member_id" in options
                    or plugin.get("reader") == "seismic-window" and bool(options)
                    or plugin.get("reader") in {"mass-spectrum", "grib-window"} and options.get("offset", 0) != 0))
                if needs_catalog:
                    catalog = self.request(endpoint, {"plugin_id": pid, "operation": "preview", "kind": "tree", "options": {}})
                    initial = checked_result(catalog, pid, version=version, revision=revision)
                    if initial["kind"] != "tree":
                        raise ValueError("Catalog-first reader did not return a catalog")
                    version, revision = initial["version"], initial["revision"]
                    checked.append({"operation": "preview", "phase": "catalog", **initial})
                payload["options"] = options
            if version is not None:
                payload["version"] = version
            result = self.request(endpoint, payload, binary=operation == "bytes")
            if operation == "bytes":
                if result["bytes"] != source["size"]: raise ValueError("Preview bytes differ from registered source size")
                if expected_digest is not None and result["sha256"] != expected_digest:
                    raise ValueError("Preview bytes differ from expected file SHA256")
                checked.append({"operation": operation, **result})
            else:
                info = checked_result(result, pid, version=version, revision=revision)
                version, revision = info["version"], info["revision"]
                checked.append({"operation": operation, **info})
        if not checked:
            raise ValueError("Plugin exposes no supported smoke-check operation")
        return {"status": "passed", "checks": checked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["baseline", "check"])
    parser.add_argument("--base", default="http://127.0.0.1:7001")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--completed-jobs-json", type=Path,
        help='Optional runtime snapshot: {"viz-fastqc":{"dataset_id":"...","entry_file":"...","job_id":"32 hex"}}; GET only')
    args = parser.parse_args()
    client = LocalCheck(args.base)
    if args.completed_jobs_json is not None:
        client.completed_jobs = completed_jobs(json.loads(args.completed_jobs_json.read_text()))
    current = client.snapshot()
    if args.phase == "baseline":
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        with args.baseline.open("x") as stream: json.dump(current, stream, indent=2)
        print(json.dumps({"datasets": len(current["datasets"]), "plugins": len(current["plugins"])})); return
    if args.matrix is None or args.report is None: parser.error("check requires matrix and report")
    baseline = json.loads(args.baseline.read_text())
    if current["plugins"] != baseline["plugins"]: raise ValueError("Plugin preferences changed since baseline")
    if any(current["datasets"].get(k) != v for k, v in baseline["datasets"].items()): raise ValueError("Original dataset metadata changed")
    matrix = json.loads(args.matrix.read_text())
    if not isinstance(matrix, list) or len({c['plugin_id'] for c in matrix}) != len(matrix): raise ValueError("Duplicate or invalid matrix")
    plugins = {p["id"]: p for p in client.request("/visualizations")["plugins"]}
    rows = []
    for case in matrix:
        start = time.monotonic()
        try: result = client.check(case, plugins)
        except Exception as error:
            # Do not persist response bodies, tokens, host paths or stack traces.
            reason = str(error)
            if any(p in reason for p in ("/Users/", "/private/", "token", "secret")): reason = type(error).__name__
            result = {"status": "failed", "reason": reason[:250]}
        row = {"plugin_id": case["plugin_id"], "dataset_id": case["dataset_id"], "entry_file": case["entry_file"], "seconds": round(time.monotonic()-start, 3), **result}
        rows.append(row)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "scope": "API preview smoke check, not exhaustive feature verification", "results": rows}, ensure_ascii=False, indent=2)+"\n")
        print(row["status"].upper(), case["plugin_id"], row.get("reason", ""), flush=True)
    after = client.snapshot()
    if after != current: raise ValueError("Dataset or preference state changed during preview checks")
    print(json.dumps({"original_datasets_unchanged": len(baseline["datasets"]), "total_datasets": len(current["datasets"]), "plugins_unchanged": len(current["plugins"]), "checked": len(rows), "passed": sum(r["status"] == "passed" for r in rows), "disabled": sum(r["status"] == "disabled" for r in rows), "not_run": sum(r["status"] == "not_run" for r in rows), "failed": sum(r["status"] == "failed" for r in rows)}))
    if any(row["status"] == "failed" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
