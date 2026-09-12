"""Acceptance cleanup boundaries, using only in-memory metadata and Python ASTs.

These tests never import either HTTP runner or connect to Mongo, HTTP or Docker.
They also run independently with ``python -m unittest discover -s backend/tests
-p test_visualization_acceptance_safety.py`` without the integration conftest.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "visualization_acceptance_safety", SCRIPTS / "visualization_acceptance_safety.py"
)
assert SPEC is not None and SPEC.loader is not None
SAFETY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SAFETY
SPEC.loader.exec_module(SAFETY)

SOURCE = "visualization_http_regression"
RUN = "visualization-regression-" + "a" * 32
FILE_ID = "minio:" + "b" * 32
OTHER_ID = "minio:" + "c" * 32
OWNER = "anonymous"
REJECTED = (AssertionError, RuntimeError, ValueError, PermissionError)
_MISSING = object()


def _field(record, path):
    value = record
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _matches(record, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(record, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(record, item) for item in expected):
                return False
            continue
        actual = _field(record, key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in":
                    if actual not in operand:
                        return False
                elif operator == "$ne":
                    if actual == operand:
                        return False
                elif operator == "$exists":
                    if (actual is not _MISSING) != operand:
                        return False
                else:
                    raise AssertionError(f"Unsupported fake Mongo operator: {operator}")
        elif actual != expected:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    async def to_list(self, length=None):
        return copy.deepcopy(self.rows if length is None else self.rows[:length])


class Collection:
    """A read-only collection; unsupported writes deliberately do not exist."""

    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(copy.deepcopy(query))
        # Returning extra metadata is safe for these inclusion projections.
        return Cursor([row for row in self.rows if _matches(row, query)])

    async def find_one(self, query, projection=None):
        rows = await self.find(query, projection).limit(1).to_list()
        return rows[0] if rows else None

    async def count_documents(self, query, **kwargs):
        self.queries.append(copy.deepcopy(query))
        return sum(_matches(row, query) for row in self.rows)


class Database:
    def __init__(self, rows=()):
        self.stored_files = Collection(rows)

    def __getitem__(self, name):
        return getattr(self, name)


class FixtureLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = Database()
        self.ledger = SAFETY.FixtureLedger(self.database, SOURCE, RUN, user_id=OWNER)
        self.filename = self.ledger.declare("sample.csv", 4)
        self.record = {
            "_id": "fixture-document",
            "file_id": FILE_ID,
            "filename": self.filename,
            "size": 4,
            "provider": "minio",
            "user_id": OWNER,
            "metadata": {"source": SOURCE, "regression_run": RUN},
        }
        self.response = {"file_id": FILE_ID, "filename": self.filename, "size": 4}

    def install(self, *records):
        self.database.stored_files.rows = copy.deepcopy(list(records))

    async def test_accept_binds_only_verified_metadata(self):
        self.install(self.record)
        self.assertEqual(await self.ledger.accept(self.response), FILE_ID)
        self.assertEqual(set(self.ledger.records), {FILE_ID})
        bound = self.ledger.records[FILE_ID]
        for field in ("_id", "file_id", "filename", "user_id"):
            self.assertEqual(bound[field], self.record[field])
        self.assertTrue(await self.ledger.assert_owned(FILE_ID))

    async def test_forged_or_missing_upload_id_never_binds(self):
        self.install(self.record)
        for identifier in ("../original", "", None, OTHER_ID):
            with self.subTest(identifier=identifier):
                with self.assertRaises(REJECTED):
                    await self.ledger.accept({**self.response, "file_id": identifier})
                self.assertEqual(self.ledger.records, {})

    async def test_response_filename_and_size_must_be_declared(self):
        self.install(self.record)
        for changes in ({"filename": "original.csv"}, {"filename": RUN + "-other.csv"},
                        {"size": 5}, {"size": None}):
            with self.subTest(changes=changes):
                with self.assertRaises(REJECTED):
                    await self.ledger.accept({**self.response, **changes})
                self.assertEqual(self.ledger.records, {})

    async def test_database_tag_owner_provider_and_size_must_match(self):
        for changes in (
            {"filename": RUN + "-not-declared.csv"},
            {"metadata": {"source": SOURCE, "regression_run": "another-run"}},
            {"metadata": {"source": "user_upload", "regression_run": RUN}},
            {"user_id": "other-owner"}, {"user_id": ""},
            {"provider": "gridfs"}, {"provider": "dataset"}, {"size": 5},
        ):
            with self.subTest(changes=changes):
                self.install({**self.record, **changes})
                with self.assertRaises(REJECTED):
                    await self.ledger.accept(self.response)
                self.assertEqual(self.ledger.records, {})

    async def test_owner_is_required_even_without_expected_owner(self):
        ledger = SAFETY.FixtureLedger(self.database, SOURCE, RUN)
        ledger.declare("sample.csv", 4)
        self.install({**self.record, "user_id": ""})
        with self.assertRaises(REJECTED):
            await ledger.accept(self.response)
        self.assertEqual(ledger.records, {})

    async def test_duplicate_database_identity_is_rejected(self):
        self.install(self.record, {**self.record, "_id": "duplicate-document"})
        with self.assertRaises(REJECTED):
            await self.ledger.accept(self.response)
        self.assertEqual(self.ledger.records, {})

    async def test_two_upload_ids_cannot_claim_one_declared_filename(self):
        self.install(self.record)
        await self.ledger.accept(self.response)
        self.database.stored_files.rows.append({**self.record, "_id": "second", "file_id": OTHER_ID})
        with self.assertRaises(REJECTED):
            await self.ledger.accept({**self.response, "file_id": OTHER_ID})
        self.assertEqual(set(self.ledger.records), {FILE_ID})

    async def test_recovery_finds_lost_response_and_ignores_other_files(self):
        original = {**self.record, "_id": "original", "file_id": OTHER_ID,
                    "filename": "user-original.csv", "metadata": {"source": "user_upload"}}
        wrong_name = {**self.record, "_id": "wrong-name", "file_id": "minio:" + "d" * 32,
                      "filename": RUN + "-not-declared.csv"}
        self.install(self.record, original, wrong_name)
        await self.ledger.recover()
        self.assertEqual(set(self.ledger.records), {FILE_ID})
        await self.ledger.recover()
        self.assertEqual(set(self.ledger.records), {FILE_ID})
        self.assertEqual(len(self.database.stored_files.rows), 3)
        self.assertTrue(any(query.get("metadata.regression_run") == RUN
                            and query.get("metadata.source") == SOURCE
                            and "filename" in query
                            for query in self.database.stored_files.queries))

    async def test_recovery_rejects_owner_or_provider_mismatch(self):
        for changes in ({"user_id": "other-owner"}, {"provider": "dataset"}, {"size": 99}):
            with self.subTest(changes=changes):
                self.install({**self.record, **changes})
                with self.assertRaises(REJECTED):
                    await self.ledger.recover()
                self.assertEqual(self.ledger.records, {})

    async def test_recovery_rejects_duplicate_inventory(self):
        self.install(self.record, {**self.record, "_id": "second", "file_id": OTHER_ID})
        with self.assertRaises(REJECTED):
            await self.ledger.recover()
        self.assertEqual(self.ledger.records, {})

    async def test_delete_guard_rechecks_record_identity_and_ownership(self):
        self.install(self.record)
        await self.ledger.accept(self.response)
        for changes in ({"_id": "replacement"}, {"user_id": "other-owner"},
                        {"filename": "user-original.csv"},
                        {"metadata": {"source": SOURCE, "regression_run": "another-run"}}):
            with self.subTest(changes=changes):
                self.install({**self.record, **changes})
                with self.assertRaises(REJECTED):
                    await self.ledger.assert_owned(FILE_ID)

    async def test_unbound_existing_id_cannot_be_deleted(self):
        self.install(self.record)
        with self.assertRaises(REJECTED):
            await self.ledger.assert_owned(FILE_ID)
        with self.assertRaises(REJECTED):
            await self.ledger.assert_owned(OTHER_ID)

    async def test_absence_checks_are_global_by_file_id(self):
        self.install(self.record)
        await self.ledger.accept(self.response)
        self.install({**self.record, "user_id": "other-owner"})
        with self.assertRaises(REJECTED):
            await self.ledger.assert_absent(FILE_ID)
        self.install()
        self.assertFalse(await self.ledger.assert_owned(FILE_ID))
        await self.ledger.assert_absent(FILE_ID)

    async def test_final_inventory_detects_untracked_fixture(self):
        self.install(self.record)
        with self.assertRaises(REJECTED):
            await self.ledger.assert_clean()
        self.install({**self.record, "filename": "user-original.csv"})
        await self.ledger.assert_clean()

    def test_duplicate_declaration_is_rejected(self):
        with self.assertRaises(REJECTED):
            self.ledger.declare("sample.csv", 4)


def _extract_upload(script, namespace):
    """Execute only the nested upload definition, never the runner module."""
    tree = ast.parse((SCRIPTS / script).read_text())
    uploads = [node for node in ast.walk(tree)
               if isinstance(node, ast.AsyncFunctionDef) and node.name == "upload"]
    assert len(uploads) == 1
    module = ast.Module(body=[copy.deepcopy(uploads[0])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPTS / script), "exec"), namespace)
    return namespace["upload"]


class UploadResponse:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": 0, "data": copy.deepcopy(self.value)}


class UploadClient:
    """Persist metadata to the fake collection, optionally losing the response."""

    def __init__(self, database, *, lose_response=False, wrong_id=False):
        self.database = database
        self.lose_response = lose_response
        self.wrong_id = wrong_id
        self.posts = 0

    async def post(self, path, *, files, data):
        assert path == "/api/v1/files"
        self.posts += 1
        filename, body, _ = files["file"]
        record = {"_id": "uploaded-document", "file_id": FILE_ID, "filename": filename,
                  "size": len(body), "provider": "minio", "user_id": OWNER,
                  "metadata": json.loads(data["metadata"])}
        self.database.stored_files.rows.append(record)
        if self.lose_response:
            raise TimeoutError("The server accepted this synthetic upload; response was lost")
        return UploadResponse({**record, "file_id": OTHER_ID if self.wrong_id else FILE_ID})


class UploadRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, script, *, lose_response=False, wrong_id=False, existing=None):
        source = ("visualization_unified_http_regression" if "extended" in script else SOURCE)
        database = Database()
        scope = "visualization-file:" + hashlib.sha256(FILE_ID.encode()).hexdigest()
        database.analysis_jobs = Collection(
            [{"job_id": "original-job", "user_id": OWNER, "session_id": scope}]
            if existing == "job" else []
        )
        database.spill_artifacts = Collection(
            [{"artifact_id": "original-artifact", "owner_user_id": OWNER, "owner_session_id": scope}]
            if existing == "artifact" else []
        )
        ledger = SAFETY.FixtureLedger(database, source, RUN, user_id=OWNER)
        client = UploadClient(database, lose_response=lose_response, wrong_id=wrong_id)

        async def request(method, path, **kwargs):
            assert method == "POST"
            return (await client.post(path, **kwargs)).json()["data"]

        namespace = {"ledger": ledger, "database": database, "client": client,
                     "request": request, "prefix": RUN, "user_id": OWNER,
                     "uploaded": [], "scopes": {}, "blocked_files": set(),
                     "json": json, "sys": sys, "print": lambda *args, **kwargs: None,
                     "scope_for_file": lambda file_id: "visualization-file:" + hashlib.sha256(file_id.encode()).hexdigest()}
        return _extract_upload(script, namespace), namespace

    async def test_successful_upload_grants_only_its_verified_scope(self):
        upload, state = self.prepare("check_extended_visualization_http.py")
        self.assertEqual(await upload("sample.csv", b"x,y\n", "text/csv"), FILE_ID)
        self.assertEqual(state["uploaded"], [FILE_ID])
        self.assertEqual(state["scopes"], {FILE_ID: state["scope_for_file"](FILE_ID)})
        self.assertEqual(state["blocked_files"], set())

    async def test_existing_job_or_artifact_never_grants_cleanup_scope(self):
        for existing in ("job", "artifact"):
            with self.subTest(existing=existing):
                upload, state = self.prepare("check_extended_visualization_http.py", existing=existing)
                with self.assertRaises(REJECTED):
                    await upload("sample.csv", b"x,y\n", "text/csv")
                self.assertEqual(state["scopes"], {})
                self.assertEqual(state["uploaded"], [])
                self.assertEqual(state["blocked_files"], {FILE_ID})
                self.assertEqual(len(state["database"].analysis_jobs.rows)
                                 + len(state["database"].spill_artifacts.rows), 1)

    async def test_wrong_response_id_never_enters_cleanup_registry(self):
        for script in ("check_visualization_http.py", "check_extended_visualization_http.py"):
            with self.subTest(script=script):
                upload, state = self.prepare(script, wrong_id=True)
                with self.assertRaises(REJECTED):
                    await upload("sample.csv", b"x,y\n", "text/csv")
                self.assertEqual(state["uploaded"], [])
                self.assertEqual(state["scopes"], {})
                self.assertEqual(state["ledger"].records, {})

    async def test_lost_response_remains_recoverable_without_reposting(self):
        for script in ("check_visualization_http.py", "check_extended_visualization_http.py"):
            with self.subTest(script=script):
                upload, state = self.prepare(script, lose_response=True)
                with self.assertRaises(TimeoutError):
                    await upload("sample.csv", b"x,y\n", "text/csv")
                self.assertEqual(state["uploaded"], [])
                self.assertEqual(state["scopes"], {})
                self.assertEqual(state["ledger"].records, {})
                await state["ledger"].recover()
                self.assertEqual(set(state["ledger"].records), {FILE_ID})
                self.assertEqual(state["client"].posts, 1)


class LocalEndpointTests(unittest.TestCase):
    def test_only_supported_compose_or_loopback_endpoints_are_accepted(self):
        for value in ("http://frontend", "http://frontend:80", "http://localhost:7001",
                      "http://127.0.0.1:7001", "http://[::1]:7001"):
            with self.subTest(value=value):
                self.assertTrue(SAFETY.local_base(value))

    def test_remote_ambiguous_and_decorated_endpoints_are_rejected(self):
        for value in (
            "https://frontend", "http://39.106.98.67:7000", "http://localhost",
            "http://localhost:80", "http://127.0.0.1:7000", "http://[::1]:7000",
            "http://frontend:7001", "http://frontend/api", "http://user@frontend",
            "http://user:password@frontend", "http://@frontend", "http://:@frontend",
            "http://frontend?other=1", "http://frontend#x",
            "http://localhost.evil:7001", "//frontend", "file:///tmp/frontend",
        ):
            with self.subTest(value=value):
                with self.assertRaises(REJECTED):
                    SAFETY.local_base(value)

    def test_http_runners_disable_environment_proxies_and_retries(self):
        for name in ("check_visualization_http.py", "check_extended_visualization_http.py"):
            with self.subTest(script=name):
                tree = ast.parse((SCRIPTS / name).read_text())
                clients = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Attribute) and node.func.attr == "AsyncClient"]
                self.assertTrue(clients)
                for client in clients:
                    keywords = {item.arg: item.value for item in client.keywords}
                    self.assertIsInstance(keywords.get("trust_env"), ast.Constant)
                    self.assertIs(keywords["trust_env"].value, False)
                    transport = keywords.get("transport")
                    self.assertIsInstance(transport, ast.Call)
                    self.assertTrue(any(item.arg == "retries" and isinstance(item.value, ast.Constant)
                                        and item.value.value == 0 for item in transport.keywords))
                self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                    and node.func.id == "local_base" for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()
