"""Real HTTP acceptance with uniquely owned disposable visualization fixtures.

Only this run's uploads, file-hash-scoped QC jobs/artifacts, and a temporarily
restored FastQC preference are modified. Existing sessions/files/model records
are never written. No upload or job-start request is automatically retried.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import quote
import uuid

from beanie import init_beanie
import docker
import httpx

from check_extended_visualization_workers import JCAMP, METPY_OPTIONS, _fixtures, _remove_owned
from check_visualization_http import business_snapshot
from visualization_acceptance_safety import FixtureLedger, local_base
from app.application.services.unified_visualization import VisualizationResult
from app.application.services.visualization_jobs import scope_for_file
from app.core.config import get_settings
from app.domain.models.spill import SpillArtifactOwner
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.infrastructure.external.file.spill_factory import get_spill_artifact_store
from app.infrastructure.models.analysis_job import AnalysisJobDocument
from app.infrastructure.models.documents import SpillArtifactDocument, StoredFileDocument
from app.infrastructure.storage.mongodb import get_mongodb
from app.interfaces.dependencies import get_current_user


TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
ACTIVE = {"queued", "running", "cancelling"}


async def snapshot(database):
    result = await business_snapshot(database)
    for name in ("analysis_jobs", "spill_artifacts"):
        result[name] = await database[name].count_documents({})
        records = await database[name].find({}).sort("_id", 1).to_list()
        result[name + "_sha256"] = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    return result


async def main():
    prefix = "visualization-unified-http-" + uuid.uuid4().hex
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    uploaded, deleted, scopes, cleanup_errors = [], [], {}, []
    checks, negatives, job_checks = [], {}, {}
    original_fastqc = None
    restore_fastqc = False
    preference_restored = False
    error = None
    user_id = None
    blocked_files = set()
    cleanup_jobs = cleanup_artifacts = 0
    mongo = get_mongodb()
    await mongo.initialize()
    database = mongo.client[get_settings().mongodb_database]
    # Bind only the pre-existing document schemas; never create/drop indexes.
    await init_beanie(database=database, document_models=[AnalysisJobDocument, SpillArtifactDocument, StoredFileDocument], skip_indexes=True)
    jobs, artifacts = get_analysis_job_service(), get_spill_artifact_store()
    before = await snapshot(database)
    try:
        user_id = (await get_current_user()).id
        assert isinstance(user_id, str) and user_id
        ledger = FixtureLedger(database, "visualization_unified_http_regression", prefix, user_id=user_id)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10), trust_env=False,
                follow_redirects=False, transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                envelope = response.json()
                assert envelope.get("code") == 0, "API returned a non-success envelope"
                return envelope["data"]

            def file_path(file_id, suffix=""):
                return "/api/v1/files/" + quote(file_id, safe="") + suffix

            async def expect(method, path, status, label, **kwargs):
                response = await client.request(method, path, **kwargs)
                assert response.status_code == status, f"{label}: expected HTTP {status}, received {response.status_code}"
                negatives[label] = status

            async def upload(name, data, content_type="application/octet-stream"):
                filename = ledger.declare(name, len(data))  # Recoverable even if POST response is lost.
                response = await client.post("/api/v1/files", files={"file": (filename, data, content_type)},
                                            data={"metadata": json.dumps({"source": "visualization_unified_http_regression", "regression_run": prefix})})
                response.raise_for_status()
                envelope = response.json()
                value = envelope.get("data", {})
                assert envelope.get("code") == 0
                file_id = await ledger.accept(value)
                scope = scope_for_file(file_id)
                # Existing scoped work is a failed safety precondition, never
                # authority to delete it. Also retain its file for inspection.
                blocked_files.add(file_id)
                assert await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": scope}) == 0
                assert await database.spill_artifacts.count_documents({"owner_user_id": user_id, "owner_session_id": scope}) == 0
                blocked_files.discard(file_id)
                uploaded.append(file_id)
                scopes[file_id] = scope
                print(json.dumps({"fixture_uploaded": file_id, "run": prefix}), file=sys.stderr, flush=True)
                return file_id

            async def preview(file_id, plugin, reader, kind, options=None):
                value = await request("POST", file_path(file_id, "/visualization"),
                                      json={"plugin_id": plugin, "operation": "preview", "kind": kind, "options": options or {}})
                assert value["plugin_id"] == plugin
                assert re.fullmatch(r"[0-9a-f]{64}", value["version"]) and re.fullmatch(r"[0-9a-f]{64}", value["revision"])
                parsed = VisualizationResult.model_validate(value)
                assert parsed.payload["view_kind"] == kind
                assert parsed.kind in {"table", "array", "tree", "media", "report", "series"}
                checks.append({"plugin": plugin, "reader": reader, "kind": parsed.kind, "media_type": parsed.payload.get("media_type")})
                return {**parsed.payload, "metadata": parsed.metadata}

            async def wait_job(file_id, job_id, *, database_only=False):
                deadline = time.monotonic() + 70
                while time.monotonic() < deadline:
                    if database_only:
                        value = await database.analysis_jobs.find_one({"job_id": job_id, "user_id": user_id, "session_id": scopes[file_id]})
                        assert value is not None, "Owned job disappeared before cleanup"
                    else:
                        value = await request("GET", file_path(file_id, "/visualization/jobs/" + job_id))
                    if value["status"] in TERMINAL:
                        return value
                    await asyncio.sleep(.25)
                raise AssertionError("Owned QC job did not reach a terminal state within its timeout")

            async def set_fastqc(enabled):
                nonlocal restore_fastqc
                restore_fastqc = True  # Also restore after an uncertain PATCH response.
                value = await request("PATCH", "/api/v1/visualizations/viz-fastqc/state", json={"enabled": enabled})
                assert value["enabled"] is enabled

            try:
                # Authentication routes are intentionally removed. Reuse the
                # current API's fixed system-identity dependency, not a legacy
                # /auth/me endpoint or a guessed administrator identifier.
                unified = await request("GET", "/api/v1/visualizations")
                assert unified["engine"] == "cordis"
                # This checks the original capabilities, not the historical
                # catalog size: newly approved domain plugins may coexist.
                assert len({p["id"] for p in unified["plugins"]}) == len(unified["plugins"])
                assert all(type(p["contract_version"]) is int and p["contract_version"] == 2 for p in unified["plugins"])
                assert all("data_kind" not in p and p["capabilities"]["operations"] for p in unified["plugins"])
                catalog = {p["id"]: p for p in unified["plugins"]}
                needed = {"viz-plotly", "viz-h5web", "viz-excel", "viz-rdkit", "viz-metpy", "viz-word", "viz-powerpoint", "viz-jsroot", "viz-nmrium", "viz-pdfjs", "netcdf-series"}
                assert needed <= set(catalog) and all(catalog[p]["enabled"] for p in needed), "A required capability is disabled; no unrelated user preference will be changed"
                original_fastqc = catalog["viz-fastqc"]["enabled"]
                if not original_fastqc:
                    await set_fastqc(True)
                checks.append({"catalog": "single_protocol", "plugins": len(unified["plugins"])})
                obsolete_query = await request("GET", "/api/v1/visualizations?contract_version=1")
                assert [(p["id"], p["contract_version"], p["capabilities"]) for p in obsolete_query["plugins"]] == [(p["id"], p["contract_version"], p["capabilities"]) for p in unified["plugins"]]

                # Numeric 1 equals True in Python, but must never be an explicit
                # user confirmation. Reject before file lookup or job writes.
                # This unique opaque ID has no file and requires no upload.
                confirmation_id = prefix + "-numeric-confirmation"
                confirmation_scope = scope_for_file(confirmation_id)
                jobs_before_confirmation = await database.analysis_jobs.count_documents({})
                assert not await database.stored_files.count_documents({"file_id": confirmation_id})
                assert await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": confirmation_scope}) == 0
                assert await database.spill_artifacts.count_documents({"owner_user_id": user_id, "owner_session_id": confirmation_scope}) == 0
                scopes[confirmation_id] = confirmation_scope
                for value, label in [(1, "qc_integer_confirmation_rejected"), (1.0, "qc_float_confirmation_rejected")]:
                    await expect("POST", file_path(confirmation_id, "/visualization/jobs"), 422, label,
                                 json={"plugin_id": "viz-fastqc", "kind": "report", "options": {"confirm": value}})
                    assert await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": confirmation_scope}) == 0
                    assert await database.analysis_jobs.count_documents({}) == jobs_before_confirmation
                job_checks["numeric_confirmation_rejected_before_persistence"] = "passed"

                docker_client = docker.from_env(timeout=5)
                owned_containers = []
                try:
                    files = await asyncio.to_thread(_fixtures, docker_client, get_settings().sandbox_image, owned_containers)
                finally:
                    await asyncio.to_thread(_remove_owned, docker_client, owned_containers)
                    docker_client.close()
                csv = b"x,y\n0,1.5\n1,2.5\n"
                table_id = await upload("table.csv", csv, "text/csv")
                table = await preview(table_id, "viz-plotly", "tabular", "table")
                assert table["table"]["rows"] == [["0", "1.5"], ["1", "2.5"]]
                legacy_csv = (await request("POST", file_path(table_id, "/visualization"), json={"plugin_id": "csv", "operation": "page"}))["payload"]
                assert legacy_csv["headers"] == ["x", "y"] and legacy_csv["rows"] == [["0", "1.5"], ["1", "2.5"]]
                text_id = await upload("text.txt", csv, "text/plain")
                legacy_text = (await request("POST", file_path(text_id, "/visualization"), json={"plugin_id": "text", "operation": "page"}))["payload"]
                assert legacy_text["text"] == csv.decode()
                downloaded = await client.get(file_path(table_id, "/download"))
                downloaded.raise_for_status()
                assert downloaded.content == csv
                checks.append({"legacy": "csv,text,download", "passed": True})

                hdf_id = await upload("tree.h5", files["sample.h5"])
                await preview(hdf_id, "viz-h5web", "hdf5", "tree")
                hdf = await preview(hdf_id, "viz-h5web", "hdf5", "heatmap", {"path": "/entry/signal", "indices": [1]})
                assert hdf["array"]["values"] == list(range(12, 24))
                nc_id = await upload("classic-compatible.nc", files["NETCDF4.nc"])
                legacy_science = await request("POST", file_path(nc_id, "/visualization"), json={"plugin_id": "netcdf-series", "operation": "preview"})
                science = VisualizationResult.model_validate(legacy_science)
                assert science.kind == "series" and science.payload["y"] == [1.5, 2.5, 3.5]
                checks.append({"existing_feature": "netcdf_series", "passed": True})

                excel_id = await upload("book.xlsx", files["book.xlsx"])
                excel = await preview(excel_id, "viz-excel", "excel", "table", {"sheet": "Measurements"})
                assert excel["table"]["rows"][1] == [1, 20.5, None]
                assert excel["table"]["formulas"][1] == [None, None, "=A2+B2"]
                rdkit_id = await upload("molecule.smi", b"CCO ethanol\n")
                await preview(rdkit_id, "viz-rdkit", "rdkit", "image")
                metpy_id = await upload("sounding.csv", b"p,t,td\n1000,20,16\n850,10,6\n700,0,-4\n500,-20,-25\n")
                await preview(metpy_id, "viz-metpy", "metpy", "image", METPY_OPTIONS)
                word_id = await upload("document.docx", files["document.docx"])
                word = await preview(word_id, "viz-word", "office", "pdf")
                ppt_id = await upload("slides.pptx", files["slides.pptx"])
                await preview(ppt_id, "viz-powerpoint", "office", "pdf")
                pdf = base64.b64decode(word["data_base64"], validate=True)
                pdf_id = await upload("converted.pdf", pdf, "application/pdf")
                binary = await client.post(file_path(pdf_id, "/visualization"), json={"plugin_id": "viz-pdfjs", "operation": "bytes"})
                binary.raise_for_status()
                assert binary.content == pdf and binary.headers["x-content-type-options"] == "nosniff"
                assert binary.headers["cache-control"] == "no-store" and re.fullmatch(r"[0-9a-f]{64}", binary.headers["x-preview-version"])
                checks.append({"plugin": "viz-pdfjs", "kind": "binary", "byte_identity": True})
                root_id = await upload("histograms.root", files["histograms.root"])
                await preview(root_id, "viz-jsroot", "root", "series", {"path": "/detector/counts"})
                await preview(root_id, "viz-jsroot", "root", "heatmap", {"path": "/density"})
                nmr_id = await upload("spectrum.jdx", JCAMP)
                await preview(nmr_id, "viz-nmrium", "jcamp", "series")

                endpoint = file_path(table_id, "/visualization")
                await expect("POST", endpoint, 409, "stale_file_version", json={"plugin_id": "viz-plotly", "operation": "preview", "version": "0" * 64})
                await expect("POST", endpoint, 422, "format_mismatch", json={"plugin_id": "viz-excel", "operation": "preview"})
                await expect("POST", endpoint, 404, "missing_plugin", json={"plugin_id": "missing-plugin", "operation": "preview"})
                await expect("POST", file_path(prefix + "-missing", "/visualization"), 404, "missing_file", json={"plugin_id": "viz-plotly", "operation": "preview"})
                await expect("POST", file_path(table_id, "/visualization-v2"), 404, "obsolete_v2_route_removed", json={"plugin_id": "viz-plotly"})
                await expect("POST", file_path(table_id, "/visualization-content"), 404, "obsolete_content_route_removed", json={"plugin_id": "viz-plotly"})
                await expect("GET", file_path(table_id, "/preview"), 404, "obsolete_page_route_removed")

                fastq = b"".join(f"@read{i}\n".encode() + b"ACGT" * 25 + b"\n+\n" + b"I" * 100 + b"\n" for i in range(500))
                fastq_id = await upload("quality.fastq", fastq)
                other_fastq_id = await upload("other-quality.fastq", b"@one\nACGT\n+\nIIII\n")
                job_base = file_path(fastq_id, "/visualization/jobs")
                body = {"plugin_id": "viz-fastqc", "kind": "report", "options": {"confirm": True}}
                await expect("POST", file_path(fastq_id, "/visualization"), 422, "qc_not_automatic_preview", json={**body, "operation": "preview"})
                await expect("POST", job_base, 422, "qc_requires_confirmation", json={"plugin_id": "viz-fastqc", "options": {}})
                await expect("POST", job_base, 409, "qc_stale_version", json={**body, "version": "0" * 64})
                created = await request("POST", job_base, json=body)
                job_id = created["job_id"]
                assert re.fullmatch(r"[0-9a-f]{32}", job_id)
                assert created["tool_name"] == "visualization:viz-fastqc"
                await expect("GET", file_path(other_fastq_id, "/visualization/jobs/" + job_id), 404, "qc_cross_file_owner_isolation")
                completed = await wait_job(fastq_id, job_id)
                assert completed["status"] == "succeeded", "Explicit QC job did not succeed"
                assert re.fullmatch(r"spill://artifact/[0-9a-f]{32}", completed["result_spill"]["locator"])
                result = await request("GET", job_base + "/" + job_id + "/result")
                assert VisualizationResult.model_validate(result).kind == "report"
                basic = next(section for section in result["payload"]["sections"] if section["name"] == "Basic Statistics")
                assert ["Total Sequences", "500"] in basic["rows"]
                assert result["metadata"]["engines"] == ["FastQC", "MultiQC"]
                artifact_records = await database.spill_artifacts.find({"owner_user_id": user_id, "owner_session_id": scopes[fastq_id]}).to_list()
                assert len(artifact_records) == 1
                await expect("GET", file_path(artifact_records[0]["storage_file_id"], "/info"), 404, "qc_artifact_not_public_file")
                job_checks["explicit_complete_result"] = "passed"

                second = await request("POST", job_base, json=body)
                second_id = second["job_id"]
                await expect("POST", job_base + "/" + second_id + "/cancel", 400, "qc_cancel_requires_action_header")
                await request("POST", job_base + "/" + second_id + "/cancel", headers={"X-Analysis-Job-Action": "cancel"})
                cancelled = await wait_job(fastq_id, second_id)
                assert cancelled["status"] == "cancelled", "Explicit cancellation did not terminalize the owned job"
                await expect("GET", job_base + "/" + second_id + "/result", 422, "qc_cancelled_job_has_no_result")
                job_checks["explicit_cancellation"] = "passed"
                await set_fastqc(False)
                for method, path, kwargs, label in [
                    ("POST", job_base, {"json": body}, "qc_disabled_start"),
                    ("GET", job_base, {}, "qc_disabled_list"),
                    ("GET", job_base + "/" + job_id, {}, "qc_disabled_status"),
                    ("GET", job_base + "/" + job_id + "/result", {}, "qc_disabled_result"),
                ]:
                    await expect(method, path, 403, label, **kwargs)
                # Cancelling owned work remains available even after disable.
                stopped = await request("POST", job_base + "/" + second_id + "/cancel", headers={"X-Analysis-Job-Action": "cancel"})
                assert stopped["status"] == "cancelled"
                job_checks["cancel_available_while_disabled"] = "passed"
            except BaseException as caught:
                error = caught
            finally:
                # Cancel only this run's file-hash owners, including jobs whose
                # create response might have been lost. Never guess a job ID.
                if user_id is not None:
                    for file_id, scope in scopes.items():
                        try:
                            if file_id in ledger.records:
                                assert await ledger.assert_owned(file_id), "Scoped fixture disappeared; refusing owner cleanup"
                            else:
                                assert file_id == confirmation_id and scope == confirmation_scope
                                assert not await database.stored_files.count_documents({"file_id": file_id})
                            records = await database.analysis_jobs.find({"user_id": user_id, "session_id": scope}).to_list()
                            for record in records:
                                if record["status"] in ACTIVE:
                                    response = await client.post(file_path(file_id, "/visualization/jobs/" + record["job_id"] + "/cancel"), headers={"X-Analysis-Job-Action": "cancel"})
                                    response.raise_for_status()
                                    await wait_job(file_id, record["job_id"], database_only=True)
                            assert not await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": scope, "status": {"$in": list(ACTIVE)}})
                            cleanup_artifacts += await artifacts.delete_owner(SpillArtifactOwner(user_id=user_id, session_id=scope))
                            await jobs.delete_owner(user_id, scope)
                            cleanup_jobs += len(records)
                            assert await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": scope}) == 0
                            assert await database.spill_artifacts.count_documents({"owner_user_id": user_id, "owner_session_id": scope}) == 0
                        except Exception:
                            blocked_files.add(file_id)
                            cleanup_errors.append("owned_job_or_artifact_cleanup_failed:" + file_id)
                if restore_fastqc and original_fastqc is not None:
                    try:
                        await request("PATCH", "/api/v1/visualizations/viz-fastqc/state", json={"enabled": original_fastqc})
                        catalog = await request("GET", "/api/v1/visualizations")
                        preference_restored = next(p for p in catalog["plugins"] if p["id"] == "viz-fastqc")["enabled"] == original_fastqc
                        assert preference_restored
                    except Exception:
                        cleanup_errors.append("fastqc_preference_restore_failed")
                else:
                    preference_restored = True
                try:
                    await ledger.recover()
                except Exception:
                    cleanup_errors.append("fixture_inventory_failed")
                uploaded[:] = ledger.records
                for file_id in reversed(uploaded):
                    try:
                        assert file_id not in blocked_files, "Scoped work was not safe to clean; refusing file deletion"
                        # A response-lost upload has no granted job-cleanup
                        # scope. Do not remove it if unexpected work appeared.
                        scope = scope_for_file(file_id)
                        assert not await database.analysis_jobs.count_documents({"user_id": user_id, "session_id": scope})
                        assert not await database.spill_artifacts.count_documents({"owner_user_id": user_id, "owner_session_id": scope})
                        if await ledger.assert_owned(file_id):
                            response = await client.delete(file_path(file_id))
                            assert response.status_code in {200, 404}
                            if response.status_code == 200: assert response.json()["code"] == 0
                        absent = await client.get(file_path(file_id, "/info"))
                        assert absent.status_code == 404
                        await ledger.assert_absent(file_id)
                        deleted.append(file_id)
                    except Exception:
                        cleanup_errors.append("fixture_delete_failed:" + file_id)
                try:
                    await ledger.assert_clean()
                except Exception:
                    cleanup_errors.append("fixture_readback_failed")
            after = await snapshot(database)
            summary = {"run": prefix, "passed": error is None and not cleanup_errors and before == after and set(uploaded) == set(deleted),
                       "checks": checks, "negative_http_checks": negatives, "qc_jobs": job_checks,
                       "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted),
                       "owned_jobs_deleted": cleanup_jobs, "owned_artifacts_deleted": cleanup_artifacts,
                       "fastqc_preference_restored": preference_restored, "business_state_preserved": before == after,
                       "business_counts_before": before, "business_counts_after": after,
                       "existing_user_files_accessed": 0, "script_model_endpoint_calls": 0, "cleanup_errors": cleanup_errors}
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            if error is not None:
                raise RuntimeError("Extended HTTP acceptance failed; inspect the summary and cleanup status") from error
            if cleanup_errors or before != after:
                raise RuntimeError("Extended HTTP cleanup or business-state preservation check failed")
    finally:
        await jobs.shutdown()
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
