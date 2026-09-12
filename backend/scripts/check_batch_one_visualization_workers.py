"""Verify batch-one visualization readers through real isolated Docker workers.

Run from a one-off backend container after building the existing sandbox image:
``PYTHONPATH=/workspace/backend python scripts/check_batch_one_visualization_workers.py``.
Only synthetic in-memory files are used. No database, user file, session, job,
model, service or published port is involved. The existing production gateway
transfers bytes and returns actual parser results; production payload validation
is applied to every successful preview. All owned containers are cleaned up.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import stat
import tarfile
import threading
import time
import uuid
import zipfile

import docker

from app.application.services.extended_visualization import MAX_OUTPUT, validate_options, validate_payload
from app.core.config import get_settings
from app.infrastructure.external.sandbox import extended_visualization_worker as gateway
from check_extended_visualization_workers import (
    _assert_owned_removed,
    _inspect_isolation,
    _observe_real_worker_containers,
    _remove_owned,
)


MAT_FIXTURE_CODE = r'''
import base64, io, json, os, tempfile
from pathlib import Path
import h5py
import numpy as np
from scipy.io import savemat
assert os.getuid() == 65534
with tempfile.TemporaryDirectory(prefix='batch-one-synthetic-') as directory:
    path = Path(directory) / 'synthetic-v73.mat'
    with h5py.File(path, 'w', userblock_size=512) as root:
        node = root.create_dataset('signal', data=np.arange(6).reshape(2, 3))
        node.attrs['MATLAB_class'] = np.bytes_('double')
    header = b'MATLAB 7.3 MAT-file, Platform: synthetic, DataSeek bounded reader regression'
    header = header.ljust(116, b' ') + b'\0' * 8 + b'\0\x02IM'
    with path.open('r+b') as stream:
        stream.write(header)
    v73 = path.read_bytes()
    classic = io.BytesIO()
    savemat(classic, {'signal': np.arange(6).reshape(2, 3)})
    assert len(v73) < 65536 and len(classic.getvalue()) < 65536
    print(json.dumps({'uid': os.getuid(), 'v73': base64.b64encode(v73).decode('ascii'),
                      'classic': base64.b64encode(classic.getvalue()).decode('ascii')}, separators=(',', ':')))
'''


def _mat_fixtures(client, image, owned):
    container = client.containers.create(
        image=image, name="ai-dataseek-batch-one-fixtures-" + uuid.uuid4().hex,
        entrypoint=["/app/.venv/bin/python"], command=["-c", MAT_FIXTURE_CODE], working_dir="/app",
        user="65534:65534", network_mode="none", read_only=True,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        mem_limit="1g", memswap_limit="1g", nano_cpus=1_000_000_000, pids_limit=96,
        tmpfs={"/tmp": "rw,noexec,nosuid,size=512m,mode=1777"},
        environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        labels={"ai-dataseek.component": "visualization-fixture-check"},
    )
    owned.append(container.id)
    try:
        _inspect_isolation(container)
        container.start()
        assert container.wait(timeout=30).get("StatusCode") == 0, "Synthetic MAT producer failed"
        output = container.logs(stdout=True, stderr=False)
        assert len(output) < 256 * 1024, "Synthetic MAT producer exceeded its output budget"
        result = json.loads(output)
        assert set(result) == {"uid", "v73", "classic"} and result["uid"] == 65534
        return {name: base64.b64decode(result[name], validate=True) for name in ("v73", "classic")}
    finally:
        gateway._remove_worker(container)


def _zip(entries, *, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return output.getvalue()


def _tar(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, value in entries:
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Already-built sandbox image; defaults to application settings")
    arguments = parser.parse_args()
    image = arguments.image or get_settings().sandbox_image
    if not image:
        raise SystemExit("Configure the existing sandbox image before running this check")
    client = docker.from_env(timeout=5)
    owned, verified, checked, rejected = [], [], [], []
    started = time.monotonic()
    try:
        mat = _mat_fixtures(client, image, owned)
        entries = [(f"sample-{i:04d}.csv", b"time,value\n0,1.5\n") for i in range(405)]
        archives = {"zip": _zip(entries), "tar": _tar(entries)}
        # A valid gzip header surrounding deliberately invalid DEFLATE content.
        # Its successful *header-only* preview proves this check does not rely
        # on hidden decompression, payload validation or trusted trailer ISIZE.
        invalid_gzip_payload = b"\x1f\x8b\x08\x00" + b"\0" * 6 + b"invalid-deflate-payload" + b"\xff" * 8
        gzip_concat = gzip.compress(b"a" * (2 * 1024 * 1024)) + gzip.compress(b"second synthetic stream")
        positives = [
            ("json-exact-numbers", "structure", "tree", "json", b'{"values":[9007199254740993,0.123456789012345678901,-0]}', {}),
            ("xml-mixed-content", "structure", "tree", "xml", b'<sample unit="K">left<value>280.25</value>right</sample>', {}),
            ("yaml-inert-values", "structure", "tree", "yaml", b"value: 9007199254740993\nsequence: [1, 2.5]\n", {}),
            ("structure-display-budget", "structure", "tree", "json", json.dumps(list(range(500))).encode(), {}),
            ("gzip-concatenated-header-only", "archive", "table", "gz", gzip_concat, {}),
            ("gzip-invalid-payload-not-decoded", "archive", "table", "tgz", invalid_gzip_payload, {}),
        ]
        for fmt, data in archives.items():
            for offset in (0, 200, 400):
                positives.append((f"{fmt}-page-{offset}", "archive", "table", fmt, data, {"row_offset": offset}))
        for kind in ("tree", "series", "heatmap"):
            positives.append(("matlab-v73-" + kind, "hdf5", kind, "mat", mat["v73"], {} if kind == "tree" else {"path": "/signal"}))
        symlink = zipfile.ZipInfo("unsafe-link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        negatives = [
            ("xml-external-entity", "structure", "tree", "xml", b'<!DOCTYPE root [<!ENTITY x SYSTEM "file:///forbidden/entity">]><root>&x;</root>', {}),
            ("xml-stylesheet-instruction", "structure", "tree", "xml", b'<?xml-stylesheet href="https://invalid.example/style"?><root/>', {}),
            ("yaml-python-constructor", "structure", "tree", "yaml", b"!!python/object/apply:os.system ['forbidden']", {}),
            ("yaml-alias-cycle", "structure", "tree", "yaml", b"value: &value [*value]", {}),
            ("zip-path-traversal", "archive", "table", "zip", _zip([("../forbidden", b"x")]), {}),
            ("tar-path-traversal", "archive", "table", "tar", _tar([("../forbidden", b"x")]), {}),
            ("zip-hidden-unsafe-tail", "archive", "table", "zip", _zip(entries + [("../forbidden", b"x")]), {}),
            ("zip-symlink", "archive", "table", "zip", _zip([(symlink, b"forbidden")]), {}),
            ("zip-ratio-budget", "archive", "table", "zip", _zip([("large.txt", b"0" * (2 * 1024 * 1024))], compression=zipfile.ZIP_DEFLATED), {}),
            ("archive-extraction-option", "archive", "table", "zip", archives["zip"], {"extract": "sample-0000.csv"}),
            ("archive-pagination-budget", "archive", "table", "zip", archives["zip"], {"row_offset": 4096}),
            ("classic-mat-not-hdf5", "hdf5", "tree", "mat", mat["classic"], {}),
        ]
        with _observe_real_worker_containers(owned, verified):
            for label, reader, kind, fmt, data, options in positives:
                validate_options(reader, options)
                result = gateway.run_extended_visualization_worker(
                    image, data, reader=reader, kind=kind, format=fmt, options=options,
                    truncated=False, cancelled=threading.Event(),
                )
                assert result.get("ok") is True, label + ": " + result.get("error", "invalid envelope")
                payload = validate_payload(result["data"], reader, kind, 1024**2 if reader in {"structure", "archive"} else MAX_OUTPUT)
                if label == "json-exact-numbers":
                    assert [n["attributes"]["value"] for n in payload["tree"][2:]] == ["9007199254740993", "0.123456789012345678901", "-0"]
                elif label == "yaml-inert-values":
                    assert payload["tree"][1]["attributes"]["value"] == "9007199254740993"
                elif label == "xml-mixed-content":
                    assert [n["node_type"] for n in payload["tree"]] == ["element", "attribute", "text", "element", "text", "text"]
                elif label == "structure-display-budget":
                    assert len(payload["tree"]) == 256 and payload["sampled"] is True
                if reader == "archive":
                    assert payload["metadata"]["listing_only"] is True and payload["metadata"]["contents_verified"] is False
                    table = payload["table"]
                    if fmt in {"zip", "tar"}:
                        offset = options["row_offset"]
                        assert table["total_rows"] == 405 and table["row_offset"] == offset
                        assert len(table["rows"]) == min(200, 405 - offset)
                        assert table["rows"][0][0] == f"sample-{offset:04d}.csv"
                    else:
                        assert payload["metadata"]["gzip_header_only"] is True
                        assert payload["metadata"]["stream_count"] is None
                        assert payload["metadata"]["declared_uncompressed_bytes"] is None
                        assert payload["metadata"]["compression_ratio"] is None and table["rows"][0][2] is None
                if reader == "hdf5":
                    assert payload["metadata"]["source_format"] == "matlab-v7.3"
                    assert payload["metadata"]["dimension_order"] == "hdf5-storage"
                    assert payload["tree"][0]["shape"] == [2, 3]
                    if kind == "series":
                        assert payload["array"]["values"] == [0, 1, 2]
                    if kind == "heatmap":
                        assert payload["array"]["values"] == [0, 1, 2, 3, 4, 5]
                checked.append({"case": label, "reader": reader, "kind": kind, "format": fmt,
                                "input_bytes": len(data), "output_bytes": len(json.dumps(payload).encode())})
            for label, reader, kind, fmt, data, options in negatives:
                result = gateway.run_extended_visualization_worker(
                    image, data, reader=reader, kind=kind, format=fmt, options=options,
                    truncated=False, cancelled=threading.Event(),
                )
                assert result.get("ok") is False and isinstance(result.get("error"), str), label
                assert "Traceback" not in result["error"] and "/forbidden" not in result["error"], label
                rejected.append(label)
        _assert_owned_removed(client, owned)
        print(json.dumps({"status": "passed", "positive_cases": checked, "safe_rejections": rejected,
                          "production_payload_validation": True, "actual_worker_isolation_verified": len(verified),
                          "network": "none", "read_only_root": True, "nonroot": "65534:65534",
                          "host_mounts": 0, "published_ports": 0, "owned_containers_remaining": 0,
                          "gzip_policy": "header metadata only; invalid compressed payload deliberately not decoded",
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "user_files_accessed": 0, "sessions_created": 0, "jobs_created": 0, "model_calls": 0},
                         ensure_ascii=False, indent=2))
    finally:
        _remove_owned(client, owned)
        client.close()


if __name__ == "__main__":
    main()
