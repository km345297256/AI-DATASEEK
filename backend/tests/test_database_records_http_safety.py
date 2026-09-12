"""Offline doubles only: never imports/runs the live HTTP or Docker runner."""
import ast
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest
from urllib.parse import quote
import uuid

from test_database_visualization_http_safety import (
    namespace as previous_namespace, fake_database, Response, CleanupClient,
)
from test_visualization_acceptance_safety import SAFETY, UploadClient, FILE_ID, OTHER_ID, OWNER, REJECTED

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend/scripts/check_database_records_http.py"
TREE = ast.parse(SCRIPT.read_text())
RUN = "records-viz-http-" + "a" * 32


def namespace(**extra):
    nodes = [copy.deepcopy(n) for n in TREE.body if isinstance(n, (ast.Assign, ast.FunctionDef, ast.AsyncFunctionDef))]
    previous = previous_namespace()
    state = {"asyncio": asyncio, "base64": base64, "hashlib": hashlib, "json": json, "re": re,
             "uuid": uuid, "quote": quote, "FixtureLedger": SAFETY.FixtureLedger,
             "assert_idle": previous["assert_idle"], "cleanup_fixture": previous["cleanup_fixture"],
             "print": lambda *a, **k: None, **extra}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SCRIPT), "exec"), state)
    return state


def wire_fixtures(code):
    files = {name: (ROOT / "sandbox/tests/fixtures" / spec[0]).read_bytes() for name, spec in code["FIXTURE_PATHS"].items()}
    files.update({"bad.sql": b"CREATE TABLE t(a INT); INSERT INTO t VALUES(load_extension('never'));",
                  "bad.dump": b"PGDMP" + bytes(64), "bad.bson": b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK",
                  "bad.rdb": files["sample.rdb"][:-1] + bytes([files["sample.rdb"][-1] ^ 1])})
    return {"versions": code["VERSIONS"], "files": {name: base64.b64encode(data).decode() for name, data in files.items()}}


class FixtureTransportTests(unittest.TestCase):
    def test_exact_original_fixture_hashes_and_file_inventory(self):
        code = namespace(); data = wire_fixtures(code); raw = json.dumps(data).encode()
        decoded, versions = code["decode_fixtures"]([raw[:10], raw[10:]])
        self.assertEqual(set(decoded), code["EXPECTED_FILES"]); self.assertEqual(versions, code["VERSIONS"])

    def test_missing_extra_changed_hash_unknown_version_and_bad_base64_refused(self):
        code = namespace()
        for mode in ("missing", "extra", "modified", "badbase64", "version", "unexpected-bad-file"):
            with self.subTest(mode=mode):
                data = wire_fixtures(code)
                if mode == "missing": del data["files"]["sample.bson"]
                elif mode == "extra": data["files"]["business.bson"] = "YQ=="
                elif mode == "modified": data["files"]["sample.bson"] = base64.b64encode(b"other").decode()
                elif mode == "badbase64": data["files"]["sample.bson"] = "!bad!"
                elif mode == "version": data["versions"] = {**data["versions"], "rdb_fixture": "latest"}
                else: data["files"]["bad.sql"] = base64.b64encode(b"SELECT original_file").decode()
                with self.assertRaises((AssertionError, ValueError)): code["decode_fixtures"]([json.dumps(data).encode()])

    def test_output_budget_checked_before_json(self):
        code = namespace()
        for chunks in ([b"x" * (1048576 + 1)], [b"x" * 1048576, b"x"], ["not-bytes"]):
            with self.subTest(length=len(chunks)), self.assertRaises(AssertionError): code["decode_fixtures"](chunks)


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_upload_is_not_retried_and_cleanup_requires_binding(self):
        code = namespace(); database = fake_database(); ledger = SAFETY.FixtureLedger(database, code["SOURCE"], RUN, user_id=OWNER)
        client = UploadClient(database, lose_response=True)
        with self.assertRaises(TimeoutError): await code["upload_fixture"](client, ledger, "sample.bson", b"12345")
        self.assertEqual(client.posts, 1); self.assertEqual(ledger.records, {})
        await ledger.recover(); self.assertEqual(set(ledger.records), {FILE_ID})
        cleanup = CleanupClient(database); await code["cleanup_fixture"](cleanup, ledger, FILE_ID)
        self.assertEqual(cleanup.deleted, [FILE_ID]); await ledger.assert_clean()

    async def test_foreign_response_id_and_changed_owner_not_deletion_authority(self):
        code = namespace(); database = fake_database(); ledger = SAFETY.FixtureLedger(database, code["SOURCE"], RUN, user_id=OWNER)
        client = UploadClient(database, wrong_id=True)
        with self.assertRaises(REJECTED): await code["upload_fixture"](client, ledger, "sample.bson", b"12345")
        cleanup = CleanupClient(database)
        with self.assertRaises(REJECTED): await code["cleanup_fixture"](cleanup, ledger, OTHER_ID)
        self.assertEqual(cleanup.deleted, [])
        await ledger.recover(); database.stored_files.rows[0]["user_id"] = "foreign"
        with self.assertRaises(REJECTED): await code["cleanup_fixture"](cleanup, ledger, FILE_ID)
        self.assertEqual(cleanup.deleted, [])

    async def test_active_jobs_or_sessions_fail_before_first_request(self):
        code = namespace()
        for collection, statuses in [("sessions", ["pending", "running", "waiting"]), ("analysis_jobs", ["queued", "running", "cancelling"])]:
            for status in statuses:
                with self.subTest(collection=collection, status=status):
                    database = fake_database(); getattr(database, collection).rows.append({"status": status})
                    with self.assertRaises(RuntimeError):
                        await code["run_acceptance"](database, object(), {n: b"x" for n in code["EXPECTED_FILES"]}, {}, OWNER, RUN)
                    self.assertEqual(database.stored_files.rows, [])


class PreferenceClient(CleanupClient):
    def __init__(self, database, plugins, *, lost_patch=False):
        super().__init__(database); self.plugins = plugins
        self.states = dict(zip(plugins, [False, True, False, True])); self.originals = dict(self.states)
        self.patches = []; self.posts = 0; self.lost_patch = lost_patch

    def catalog(self):
        return {"engine": "cordis", "plugins": [
            *[{"id": p, "enabled": self.states[p], "reader": r, "adapter": a, "contract_version": 2,
               "capabilities": {"operations": ["preview"], "shared": False}} for p, (r, a) in self.plugins.items()],
            *[{"id": "unchanged-" + str(i), "enabled": True} for i in range(77)]]}

    async def request(self, method, path, **kwargs):
        if method == "GET": return Response(self.catalog())
        assert method == "PATCH" and path.endswith("/state")
        plugin = path.split("/")[-2]; assert plugin in self.states
        self.states[plugin] = kwargs["json"]["enabled"]; self.patches.append((plugin, self.states[plugin]))
        if self.lost_patch and len(self.patches) == 1: raise TimeoutError("uncertain persisted patch")
        return Response({})

    async def post(self, path, **kwargs):
        if path.endswith("/visualization"): return Response(status=500)
        assert path == "/api/v1/files"
        filename, data, _ = kwargs["files"]["file"]; self.posts += 1
        record = {"_id": "fixture-doc-" + str(self.posts), "file_id": "fixture:" + str(self.posts), "filename": filename,
                  "size": len(data), "provider": "minio", "user_id": OWNER, "metadata": json.loads(kwargs["data"]["metadata"])}
        self.database.stored_files.rows.append(record); return Response(record)


class FinallyTests(unittest.IsolatedAsyncioTestCase):
    async def test_midrun_or_uncertain_patch_restore_only_own_preferences_and_files(self):
        async def snapshot(database): return copy.deepcopy(database.stored_files.rows)
        for lost in (False, True):
            with self.subTest(lost=lost):
                database = fake_database(); unrelated = {"file_id": OTHER_ID, "filename": "business-original", "user_id": OWNER}
                database.stored_files.rows.append(unrelated)
                code = namespace(business_integrity_snapshot=snapshot); client = PreferenceClient(database, code["PLUGINS"], lost_patch=lost)
                with self.assertRaises(RuntimeError):
                    await code["run_acceptance"](database, client, {n: b"x" for n in code["EXPECTED_FILES"]}, code["VERSIONS"], OWNER, RUN)
                self.assertEqual(client.states, client.originals)
                self.assertEqual(database.stored_files.rows, [unrelated])
                self.assertNotIn(OTHER_ID, client.deleted)
                self.assertEqual(client.posts, 0 if lost else len(code["EXPECTED_FILES"]))


class StaticSafetyTests(unittest.TestCase):
    def test_declared_plugin_ids_readers_adapters_match_real_manifests(self):
        code = namespace()
        expected = {"viz-sql-dump": ("sql-dump", "database-dump"),
                    "viz-postgres-dump": ("pg-dump", "database-dump"),
                    "viz-bson": ("bson", "database-records"),
                    "viz-redis-rdb": ("redis-rdb", "database-records")}
        self.assertEqual(code["PLUGINS"], expected)
        for plugin, pair in expected.items():
            filename = plugin.removeprefix("viz-") + ".json"
            manifest = json.loads((ROOT / "plugin-host/visualizations" / filename).read_text())
            self.assertEqual(manifest["id"], plugin)
            self.assertEqual((manifest["reader"], manifest["adapter"]), pair)
            self.assertEqual(manifest["contract_version"], 2)
            self.assertEqual(manifest["capabilities"], {"operations": ["preview"], "input_mode": "whole", "shared": False})

    def test_offline_fixed_fixture_transport_and_no_database_restore(self):
        code = namespace(); source = code["NATIVE_CODE"]
        self.assertNotIn("subprocess", source); self.assertNotIn("pg_restore", source)
        self.assertNotIn("MongoClient", source); self.assertNotIn("redis-server", source)
        self.assertIn("hashlib.sha256(data).hexdigest()==digest", source)
        text = SCRIPT.read_text()
        self.assertIn('network_mode="none", read_only=True', text)
        self.assertIn('user="65534:65534"', text)
        self.assertIn('not container.attrs.get("Mounts")', text)
        self.assertIn('transport=httpx.AsyncHTTPTransport(retries=0)', text)
        self.assertNotIn('update_one(', text); self.assertNotIn('drop_database(', text)
