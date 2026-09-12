"""HTTP acceptance of batch-one visualizations with six disposable fixtures.

The only application writes are six uniquely tagged uploads, their deletion,
and a temporary change to the structure plugin preference that is restored and
read back in finally. Uploads are never retried. No existing file content is
read; business record counts and metadata digests are read-only integrity gates.
Run only after the existing local services and sandbox image have been updated.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import struct
import sys
import uuid
import wave
import zipfile
from urllib.parse import quote, urlparse

import httpx

from app.application.services.unified_visualization import VisualizationResult
from app.application.services.visualization_probe import ContentProfile
from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from check_visualization_http import business_snapshot


def synthetic_fixtures():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(205):
            archive.writestr(f"sample-{index:04d}.csv", b"time,value\n0,1.5\n")
    wav = io.BytesIO()
    with wave.open(wav, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(8000)
        sound.writeframes(b"".join(struct.pack("<h", (i % 40 - 20) * 100) for i in range(800)))
    # Minimal baseline, uncompressed 8-bit grayscale TIFF, generated without
    # reading any sample image or requiring host-side scientific libraries.
    width = height = 16
    count = 9
    strip_offset = 8 + 2 + count * 12 + 4
    tags = [(256, 4, width), (257, 4, height), (258, 3, 8), (259, 3, 1),
            (262, 3, 1), (273, 4, strip_offset), (277, 3, 1), (278, 4, height),
            (279, 4, width * height)]
    tiff = b"II" + struct.pack("<HIH", 42, 8, count)
    tiff += b"".join(struct.pack("<HHII", tag, kind, 1, value) for tag, kind, value in tags)
    tiff += struct.pack("<I", 0) + bytes(range(256))
    assert len(tiff) == strip_offset + 256
    return [
        ("structure.json", b'{"values":[9007199254740993,2.5,true,null]}', "application/json"),
        ("structure.xml", b'<sample unit="K">left<value>280.25</value>right</sample>', "application/xml"),
        ("structure.yaml", b"value: 9007199254740993\nsequence: [1, 2.5]\n", "application/yaml"),
        ("directory.zip", output.getvalue(), "application/zip"),
        ("audio.wav", wav.getvalue(), "audio/wav"),
        ("grayscale.tif", tiff, "image/tiff"),
    ]


async def _integrity_snapshot(database):
    result = await business_snapshot(database)
    projections = {
        "session_events": {"_id": 1, "session_id": 1, "seq": 1, "version": 1, "created_at": 1,
                           "producer_event_key": 1, "payload_digest": 1, "event.id": 1, "event.type": 1},
        "model_traces": {"_id": 1, "trace_id": 1, "session_id": 1, "record.status": 1,
                         "record.created_at": 1, "record.finished_at": 1, "record.request_hmac_before": 1,
                         "record.request_hmac_after": 1, "record.actual_total_tokens": 1,
                         "record.task_tokens_charged": 1, "record.kind": 1},
        "token_usage": {"_id": 1, "usage_id": 1, "session_id": 1, "created_at": 1,
                        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1},
    }
    for name, projection in projections.items():
        digest, count = hashlib.sha256(), 0
        async for record in database[name].find({}, projection).sort("_id", 1):
            count += 1
            if count > 100000:
                raise RuntimeError("Business metadata integrity scan exceeded its record budget")
            digest.update(json.dumps(record, sort_keys=True, default=str, separators=(",", ":")).encode())
            digest.update(b"\n")
        result[name + "_metadata_sha256"] = digest.hexdigest()
    return result


async def main():
    prefix = "batch-one-visualization-regression-" + uuid.uuid4().hex
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"frontend", "localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("This acceptance check is restricted to the existing local frontend")
    uploaded, deleted, cleanup_errors, checks = [], [], [], []
    negatives = {}
    structure_id, original_enabled = None, None
    restore_preference, preference_restored = False, False
    error = None
    mongo = get_mongodb()
    await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await _integrity_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10)) as client:
            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                envelope = response.json()
                assert envelope.get("code") == 0, "API returned a non-success envelope"
                return envelope["data"]

            async def catalog():
                result = await request("GET", "/api/v1/visualizations")
                assert result["engine"] == "cordis"
                return result

            async def upload(name, data, media_type):
                # Deliberately one POST, with no retry or transport retry policy.
                response = await client.post("/api/v1/files",
                    files={"file": (prefix + "-" + name, data, media_type)},
                    data={"metadata": json.dumps({"source": "batch_one_visualization_http_regression", "regression_run": prefix})})
                envelope = response.json()
                value = envelope.get("data")
                file_id = value.get("file_id") if isinstance(value, dict) else None
                if isinstance(file_id, str) and file_id:
                    # Track the opaque returned ID before status, filename or
                    # syntax assertions, so a later failure still cleans it up.
                    uploaded.append(file_id)
                    print(json.dumps({"fixture_uploaded": file_id, "run": prefix}), file=sys.stderr, flush=True)
                else:
                    cleanup_errors.append("upload_response_has_no_trackable_id:" + name)
                response.raise_for_status()
                assert envelope.get("code") == 0
                assert isinstance(file_id, str) and file_id and re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", file_id)
                assert value["filename"] == prefix + "-" + name
                return file_id

            def endpoint(file_id):
                return "/api/v1/files/" + quote(file_id, safe="") + "/visualization"

            async def preview(file_id, plugin_id, *, operation="preview", version=None, options=None):
                body = {"plugin_id": plugin_id, "operation": operation, "options": options or {}}
                if version is not None:
                    body["version"] = version
                result = VisualizationResult.model_validate(await request("POST", endpoint(file_id), json=body))
                assert result.plugin_id == plugin_id and re.fullmatch(r"[0-9a-f]{64}", result.version)
                assert re.fullmatch(r"[0-9a-f]{64}", result.revision)
                if version:
                    assert result.version == version
                checks.append({"plugin": plugin_id, "operation": operation, "kind": result.kind})
                return result

            async def binary(file_id, plugin_id, expected, *, version=None):
                body = {"plugin_id": plugin_id, "operation": "bytes"}
                if version:
                    body["version"] = version
                response = await client.post(endpoint(file_id), json=body)
                response.raise_for_status()
                assert response.content == expected, "Bounded bytes changed fixture content"
                assert response.headers.get("x-visualization-plugin") == plugin_id
                assert re.fullmatch(r"[0-9a-f]{64}", response.headers.get("x-preview-version", ""))
                assert re.fullmatch(r"[0-9a-f]{64}", response.headers.get("x-visualization-revision", ""))
                assert response.headers.get("cache-control") == "no-store"
                assert response.headers.get("x-content-type-options") == "nosniff"
                if version:
                    assert response.headers["x-preview-version"] == version
                checks.append({"plugin": plugin_id, "operation": "bytes", "bytes": len(expected)})
                return response.headers["x-preview-version"]

            async def expect_status(name, file_id, plugin_id, status, **body):
                response = await client.post(endpoint(file_id), json={"operation": "preview", "plugin_id": plugin_id, **body})
                assert response.status_code == status, f"{name}: expected {status}, received {response.status_code}"
                negatives[name] = status

            try:
                initial = await catalog()
                by_id = {item["id"]: item for item in initial["plugins"]}
                structure_id, archive_id, audio_id, tiff_id = "viz-structured-tree", "viz-archive-directory", "viz-audio-waveform", "tiff"
                assert all(key in by_id for key in (structure_id, archive_id, audio_id, tiff_id)), "Required plugins are absent"
                assert all(by_id[key]["enabled"] for key in (archive_id, audio_id, tiff_id)), "A required unrelated plugin is disabled; its preference will not be changed"
                original_enabled = by_id[structure_id]["enabled"]
                assert type(original_enabled) is bool
                if not original_enabled:
                    restore_preference = True
                    await request("PATCH", f"/api/v1/visualizations/{structure_id}/state", json={"enabled": True})

                files = {}
                originals = {}
                for name, data, media_type in synthetic_fixtures():
                    files[name] = await upload(name, data, media_type)
                    originals[name] = data
                json_result = await preview(files["structure.json"], structure_id)
                assert json_result.kind == "tree" and json_result.payload["tree"][2]["attributes"]["value"] == "9007199254740993"
                xml_result = await preview(files["structure.xml"], structure_id)
                assert [n["node_type"] for n in xml_result.payload["tree"]] == ["element", "attribute", "text", "element", "text", "text"]
                yaml_result = await preview(files["structure.yaml"], structure_id)
                assert yaml_result.payload["tree"][1]["attributes"]["value"] == "9007199254740993"
                first = await preview(files["directory.zip"], archive_id, options={"row_offset": 0})
                assert first.kind == "table" and len(first.payload["table"]["rows"]) == 200
                second = await preview(files["directory.zip"], archive_id, version=first.version, options={"row_offset": 200})
                assert second.payload["table"]["rows"][0][0] == "sample-0200.csv"
                assert len(second.payload["table"]["rows"]) == 5 and second.payload["table"]["total_rows"] == 205
                assert second.metadata["contents_verified"] is False and second.revision == first.revision
                wav_version = await binary(files["audio.wav"], audio_id, originals["audio.wav"])
                profile_result = await preview(files["grayscale.tif"], tiff_id, operation="prepare")
                profile = ContentProfile.model_validate(profile_result.payload["profile"])
                assert profile_result.kind == "resources" and profile.container == profile.dialect == "tiff"
                assert profile.evidence == ["tiff-header"] and not profile.traits and 0 < profile.bytes_read <= 65536
                await binary(files["grayscale.tif"], tiff_id, originals["grayscale.tif"], version=profile_result.version)

                await expect_status("archive_stale_version", files["directory.zip"], archive_id, 409, version="0" * 64, options={"row_offset": 200})
                await expect_status("archive_extract_not_allowed", files["directory.zip"], archive_id, 422, options={"extract": "sample-0000.csv"})
                await expect_status("archive_bad_offset", files["directory.zip"], archive_id, 422, options={"row_offset": True})
                await expect_status("format_mismatch", files["structure.json"], archive_id, 422)
                await expect_status("structure_undeclared_bytes", files["structure.json"], structure_id, 422, operation="bytes")
                await expect_status("audio_stale_version", files["audio.wav"], audio_id, 409, operation="bytes", version="0" * 64)
                await expect_status("audio_extra_options", files["audio.wav"], audio_id, 422, operation="bytes", version=wav_version, options={"url": "https://invalid.example"})
                await expect_status("prepare_path_not_allowed", files["grayscale.tif"], tiff_id, 422, operation="prepare", options={"path": "/forbidden"})
                restore_preference = True  # Restore even when PATCH response is uncertain.
                await request("PATCH", f"/api/v1/visualizations/{structure_id}/state", json={"enabled": False})
                stopped = await catalog()
                assert next(item for item in stopped["plugins"] if item["id"] == structure_id)["enabled"] is False
                await expect_status("structure_disabled", files["structure.json"], structure_id, 403)
            except BaseException as caught:
                error = caught
            finally:
                if restore_preference and structure_id is not None and original_enabled is not None:
                    try:
                        await request("PATCH", f"/api/v1/visualizations/{structure_id}/state", json={"enabled": original_enabled})
                        restored = await catalog()
                        preference_restored = next(item for item in restored["plugins"] if item["id"] == structure_id)["enabled"] == original_enabled
                        assert preference_restored
                    except Exception:
                        cleanup_errors.append("structure_preference_restore_failed")
                else:
                    preference_restored = True
                for file_id in reversed(uploaded):
                    try:
                        path = "/api/v1/files/" + quote(file_id, safe="")
                        response = await client.delete(path)
                        assert response.status_code in (200, 404)
                        absent = await client.get(path + "/info")
                        assert absent.status_code == 404, "Fixture remains readable after deletion"
                        deleted.append(file_id)
                    except Exception:
                        cleanup_errors.append("fixture_delete_failed:" + file_id)
            after = await _integrity_snapshot(database)
            passed = error is None and not cleanup_errors and before == after
            print(json.dumps({"run": prefix, "passed": passed, "successful_checks": checks,
                              "negative_http_checks": negatives, "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted),
                              "structure_preference_restored": preference_restored, "business_state_preserved": before == after,
                              "business_counts_before": before, "business_counts_after": after,
                              "digest_scope": "record counts, session snapshot and event/model/token metadata; no message/file content read",
                              "sessions_or_model_requests_created": 0, "cleanup_errors": cleanup_errors},
                             ensure_ascii=False, indent=2), flush=True)
            if error is not None:
                raise RuntimeError("Batch-one HTTP acceptance failed; inspect its summary and cleanup status") from error
            if not passed:
                raise RuntimeError("Batch-one HTTP cleanup or business-state preservation failed")
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
