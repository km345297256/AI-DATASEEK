"""The HTTP acceptance runner is inspected/exercised with in-memory doubles.

No runner imports, Docker/HTTP/Mongo connections, user file reads or plugin
state changes occur. Native fixture bytes are tested separately in the sandbox.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from urllib.parse import quote
import uuid

from test_visualization_acceptance_safety import (
    Collection, Database, FILE_ID, OTHER_ID, OWNER, REJECTED, SAFETY, UploadClient,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_database_visualization_http.py"
TREE = ast.parse(SCRIPT.read_text())
RUN = "database-viz-http-" + "a" * 32


def namespace(**extra):
    definitions = [copy.deepcopy(n) for n in TREE.body if isinstance(n, (ast.Assign, ast.FunctionDef, ast.AsyncFunctionDef))]
    state = {"asyncio": asyncio, "base64": base64, "hashlib": hashlib, "json": json, "re": re, "uuid": uuid,
        "quote": quote, "FixtureLedger": SAFETY.FixtureLedger, "print": lambda *a, **k: None, **extra}
    exec(compile(ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[])), str(SCRIPT), "exec"), state)
    return state


def fake_database():
    database = Database(); database.sessions = Collection(); database.analysis_jobs = Collection()
    return database


class Response:
    def __init__(self, value=None, status=200, content=b"{}"):
        self.value, self.status_code, self.content = value, status, content

    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError("Synthetic HTTP failure")

    def json(self): return {"code": 0, "data": copy.deepcopy(self.value)}


class CleanupClient:
    def __init__(self, database): self.database, self.deleted = database, []

    async def delete(self, path):
        identifier = path.rsplit("/", 1)[1].replace("%3A", ":")
        self.deleted.append(identifier)
        self.database.stored_files.rows = [r for r in self.database.stored_files.rows if r["file_id"] != identifier]
        return Response({})

    async def get(self, path):
        identifier = path.split("/")[-2].replace("%3A", ":")
        exists = any(r["file_id"] == identifier for r in self.database.stored_files.rows)
        return Response(status=200 if exists else 404)


class UploadAndCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, **kwargs):
        database = fake_database(); code = namespace()
        ledger = SAFETY.FixtureLedger(database, code["SOURCE"], RUN, user_id=OWNER)
        return code, database, ledger, UploadClient(database, **kwargs)

    async def test_lost_upload_response_recovers_exact_record_without_second_post(self):
        code, database, ledger, client = self.setup_case(lose_response=True)
        with self.assertRaises(TimeoutError): await code["upload_fixture"](client, ledger, "sample.dbf", b"abcd")
        self.assertEqual(client.posts, 1); self.assertEqual(ledger.records, {})
        self.assertEqual(ledger.expected, {RUN + "-sample.dbf": 4})
        await ledger.recover()
        cleanup = CleanupClient(database)
        await code["cleanup_fixture"](cleanup, ledger, FILE_ID)
        self.assertEqual(cleanup.deleted, [FILE_ID]); await ledger.assert_clean()

    async def test_unrelated_response_id_never_authorizes_delete(self):
        code, database, ledger, client = self.setup_case(wrong_id=True)
        with self.assertRaises(REJECTED): await code["upload_fixture"](client, ledger, "sample.mdb", b"abcd")
        self.assertEqual(ledger.records, {})
        cleanup = CleanupClient(database)
        with self.assertRaises(REJECTED): await code["cleanup_fixture"](cleanup, ledger, OTHER_ID)
        self.assertEqual(cleanup.deleted, [])
        await ledger.recover(); self.assertEqual(set(ledger.records), {FILE_ID})

    async def test_owner_or_document_binding_change_refuses_delete(self):
        for field, value in [("user_id", "foreign-owner"), ("_id", "changed-document"), ("provider", "dataset"), ("size", 99), ("filename", "business.mdb")]:
            with self.subTest(field=field):
                code, database, ledger, client = self.setup_case()
                identifier = await code["upload_fixture"](client, ledger, "sample.mdb", b"abcd")
                database.stored_files.rows[0][field] = value
                cleanup = CleanupClient(database)
                with self.assertRaises(REJECTED): await code["cleanup_fixture"](cleanup, ledger, identifier)
                self.assertEqual(cleanup.deleted, [])

    async def test_undeclared_inventory_is_never_accepted_by_prefix(self):
        code, database, ledger, client = self.setup_case()
        await code["upload_fixture"](client, ledger, "sample.mdb", b"abcd")
        rogue = copy.deepcopy(database.stored_files.rows[0])
        rogue.update({"file_id": OTHER_ID, "_id": "other", "filename": RUN + "-business-original.mdb"})
        database.stored_files.rows.append(rogue)
        await ledger.recover()
        cleanup = CleanupClient(database)
        await code["cleanup_fixture"](cleanup, ledger, FILE_ID)
        self.assertEqual(database.stored_files.rows, [rogue])


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_active_statuses_exit_without_any_application_mutation(self):
        for collection, statuses in [("sessions", ["pending", "running", "waiting"]), ("analysis_jobs", ["queued", "running", "cancelling"])]:
            for status in statuses:
                with self.subTest(collection=collection, status=status):
                    db = fake_database(); getattr(db, collection).rows.append({"status": status})
                    code = namespace()
                    with self.assertRaises(RuntimeError): await code["assert_idle"](db)
                    self.assertEqual(db.stored_files.rows, [])
                    # A client with no request/post/patch methods proves early
                    # rejection happens before the first HTTP interaction.
                    with self.assertRaises(RuntimeError):
                        await code["run_acceptance"](db, object(), {n:b"x" for n in code["EXPECTED_FILES"]}, {}, OWNER, RUN)

    async def test_completed_work_does_not_block(self):
        db = fake_database(); db.sessions.rows = [{"status":"completed"}]
        db.analysis_jobs.rows = [{"status":s} for s in ["succeeded","failed","cancelled","timed_out","interrupted"]]
        await namespace()["assert_idle"](db)


class PreferenceClient(CleanupClient):
    def __init__(self, database, plugins, *, lose_first_patch=False):
        super().__init__(database)
        self.states = dict(zip(plugins, [False, True, False])); self.originals = dict(self.states)
        self.patches, self.posts, self.lose_first_patch = [], 0, lose_first_patch

    def catalog(self):
        return {"engine":"cordis", "plugins":[
            *[{"id":p,"enabled":v,"reader":"database-table","adapter":"database-table","contract_version":2,
               "capabilities":{"operations":["preview"],"shared":False}} for p,v in self.states.items()],
            *[{"id":"unchanged-"+str(i),"enabled":True} for i in range(78)]]}

    async def request(self, method, path, **kwargs):
        if method == "GET":
            assert path == "/api/v1/visualizations"; return Response(self.catalog())
        assert method == "PATCH" and path.endswith("/state")
        plugin = path.split("/")[-2]; assert plugin in self.states
        self.states[plugin] = kwargs["json"]["enabled"]; self.patches.append((plugin,self.states[plugin]))
        if self.lose_first_patch and len(self.patches) == 1: raise TimeoutError("PATCH response lost after persistence")
        return Response({"enabled":self.states[plugin]})

    async def post(self, path, **kwargs):
        if path.endswith("/visualization"):
            return Response(status=500)  # Exercise recovery after all uploads.
        assert path == "/api/v1/files"
        filename, data, _ = kwargs["files"]["file"]
        self.posts += 1
        record = {"_id":"fixture-doc-"+str(self.posts), "file_id":"fixture:"+str(self.posts), "filename":filename,
            "size":len(data),"provider":"minio","user_id":OWNER,"metadata":json.loads(kwargs["data"]["metadata"])}
        self.database.stored_files.rows.append(record)
        return Response(record)


class FinallyRestorationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mid_preview_failure_restores_all_preferences_and_only_owned_files(self):
        code = namespace(); db = fake_database(); client = PreferenceClient(db, code["PLUGINS"])
        summaries = []
        async def snapshot(database): return copy.deepcopy(database.stored_files.rows)
        code["business_integrity_snapshot"] = snapshot
        code["print"] = lambda value, **kwargs: summaries.append(json.loads(value))
        with self.assertRaises(RuntimeError):
            await code["run_acceptance"](db, client, {n:b"synthetic" for n in code["EXPECTED_FILES"]}, {}, OWNER, RUN)
        self.assertEqual(client.states, client.originals)
        self.assertEqual(client.posts, 9); self.assertEqual(len(client.deleted), 9)
        self.assertEqual(db.stored_files.rows, [])
        summary = summaries[-1]
        self.assertFalse(summary["passed"]); self.assertTrue(summary["business_state_preserved"])
        self.assertTrue(summary["plugin_preferences_restored"]); self.assertEqual(summary["cleanup_errors"], [])

    async def test_uncertain_patch_is_registered_before_attempt_and_restored(self):
        code = namespace(); db = fake_database(); client = PreferenceClient(db, code["PLUGINS"], lose_first_patch=True)
        async def snapshot(database): return copy.deepcopy(database.stored_files.rows)
        code["business_integrity_snapshot"] = snapshot
        with self.assertRaises(RuntimeError):
            await code["run_acceptance"](db, client, {n:b"synthetic" for n in code["EXPECTED_FILES"]}, {}, OWNER, RUN)
        self.assertEqual(client.states, client.originals); self.assertEqual(client.posts, 0)
        self.assertEqual(client.patches, [(code["PLUGINS"][0],True),(code["PLUGINS"][0],False)])


class FixtureEnvelopeTests(unittest.TestCase):
    def payload(self, code):
        return {"versions":{"duckdb":"1.5.5","dbfread":"2.0.7","libmdb":"1.0.1","access_writer":"Jackcess 4.0.11 original synthetic fixtures"},
            "files":{name:base64.b64encode(b"synthetic").decode() for name in code["EXPECTED_FILES"]}}

    def test_exact_manifest_and_fixed_versions(self):
        code = namespace(); raw = json.dumps(self.payload(code)).encode()
        files, versions = code["decode_fixtures"]([raw[:10], raw[10:]])
        self.assertEqual(set(files), code["EXPECTED_FILES"]); self.assertEqual(versions["duckdb"],"1.5.5")

    def test_unknown_paths_names_versions_and_invalid_base64_rejected(self):
        code = namespace()
        for mutation in [lambda p:p["files"].update({"/private/user.mdb":"eA=="}), lambda p:p["files"].pop("sample.mdb"),
            lambda p:p["versions"].update({"duckdb":"1.5.4"}),lambda p:p["files"].update({"sample.mdb":"not base64"})]:
            payload = self.payload(code); mutation(payload)
            with self.assertRaises((AssertionError, ValueError)): code["decode_fixtures"]([json.dumps(payload).encode()])

    def test_stream_output_is_bounded_before_second_chunk_is_buffered(self):
        code = namespace(); code["FIXTURE_BYTES"] = 10; visited = []
        def chunks():
            visited.append(1); yield b"x"*9
            visited.append(2); yield b"x"*2
            visited.append(3); raise AssertionError("Must not request more output")
        with self.assertRaises(AssertionError): code["decode_fixtures"](chunks())
        self.assertEqual(visited,[1,2])

    def test_native_fixture_source_only_reads_approved_repo_samples(self):
        code = namespace(); native = ast.parse(code["NATIVE_CODE"])
        text = code["NATIVE_CODE"]
        self.assertIn('/app/tests/fixtures/database',text)
        self.assertIn('os.getuid()==65534',text)
        self.assertNotIn('requests',text); self.assertNotIn('httpx',text)
        self.assertNotIn('pip install',text); self.assertNotIn('getenv',text)
        self.assertIn('len(encoded.encode())<=4*1024**2',text)
        self.assertIn('ctypes.CDLL("/opt/dataseek-database/lib/libmdb.so.3")',text)
        self.assertNotIn('LD_LIBRARY_PATH',text)
        calls = [n for n in ast.walk(native) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='read_bytes']
        self.assertEqual(len(calls),1)  # Exact hardcoded Access mapping only.

    def test_fixture_container_is_networkless_readonly_unprivileged_no_mounts(self):
        code = namespace(); created = []; removed = []; payload = json.dumps(self.payload(code)).encode()
        class Container:
            attrs={"HostConfig":{"NetworkMode":"none","ReadonlyRootfs":True},"Config":{"User":"65534:65534"},"Mounts":[]}
            def reload(self): pass
            def start(self): pass
            def wait(self,timeout): return {"StatusCode":0}
            def logs(self,**kwargs):
                assert kwargs=={"stdout":True,"stderr":False,"stream":True,"follow":False}; return iter([payload])
        container=Container()
        class Containers:
            def create(self,**kwargs):
                created.append(kwargs); container.attrs["Config"]["Labels"] = kwargs["labels"]; return container
        closed=[]
        client=SimpleNamespace(containers=Containers(),close=lambda:closed.append(True))
        code.update(docker=SimpleNamespace(from_env=lambda **kwargs:client),get_settings=lambda:SimpleNamespace(sandbox_image="existing-sandbox"),_remove_worker=removed.append)
        code["fixtures"]()
        config=created[0]
        self.assertEqual(config["image"],"existing-sandbox");self.assertEqual(config["network_mode"],"none")
        self.assertTrue(config["read_only"]);self.assertEqual(config["user"],"65534:65534")
        self.assertEqual(config["cap_drop"],["ALL"]);self.assertEqual(config["security_opt"],["no-new-privileges:true"])
        self.assertNotIn("mounts",config);self.assertNotIn("volumes",config);self.assertNotIn("privileged",config)
        self.assertEqual(removed,[container]);self.assertEqual(closed,[True])

    def test_container_name_collision_does_not_authorize_removing_existing_container(self):
        code = namespace(); removed = []; closed = []
        class OtherContainer:
            attrs={"Config":{"Labels":{"ai-dataseek.component":"user-service"}}}
            def reload(self): pass
        class Containers:
            def create(self,**kwargs): raise RuntimeError("Synthetic name conflict")
            def get(self,name): return OtherContainer()
        client=SimpleNamespace(containers=Containers(),close=lambda:closed.append(True))
        code.update(docker=SimpleNamespace(from_env=lambda **kwargs:client),get_settings=lambda:SimpleNamespace(sandbox_image="existing-sandbox"),
            _remove_worker=removed.append,time=SimpleNamespace(monotonic=lambda:0))
        with self.assertRaises(AssertionError): code["fixtures"]()
        self.assertEqual(removed,[]);self.assertEqual(closed,[True])

    def test_main_checks_idle_before_fixture_creation_and_has_zero_transport_retries(self):
        main=next(n for n in TREE.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        source=ast.unparse(main)
        self.assertLess(source.index('await assert_idle'),source.index('asyncio.to_thread(fixtures)'))
        self.assertIn('retries=0',source);self.assertIn('follow_redirects=False',source);self.assertIn('trust_env=False',source)
        self.assertIn('await mongo.shutdown()',source)

    def test_runner_has_no_direct_database_writes_or_owner_mutation(self):
        methods={n.func.attr for n in ast.walk(TREE) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertFalse(methods & {'update_one','update_many','delete_one','delete_many','insert_one','insert_many','drop','create_index'})
        source=SCRIPT.read_text()
        self.assertNotIn('/api/v1/sessions',source);self.assertNotIn('/chat',source);self.assertNotIn('foreign-owner-',source)


if __name__ == '__main__': unittest.main()
