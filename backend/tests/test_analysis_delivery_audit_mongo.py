"""Optional real-driver check, confined to one random disposable database."""
import os
from uuid import uuid4

import pytest
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.domain.models.analysis_outcome import AnalysisOutcome, DeliverableRequirement
from app.domain.services.analysis_delivery_audit import AnalysisDeliveryAuditStore
from app.infrastructure.models.documents import SessionDocument


@pytest.mark.asyncio
async def test_real_mongo_delivery_audit_is_linked_filtered_and_complete_without_beanie(monkeypatch):
    uri = os.environ.get("ANALYSIS_BUDGET_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set ANALYSIS_BUDGET_TEST_MONGODB_URI for isolated real-Mongo verification")
    database_name = "dataseek_delivery_audit_test_" + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        # A collection override must use the real production PyMongo driver
        # without initializing Beanie or resolving the application's database.
        def forbidden_application_collection(*args, **kwargs):
            raise AssertionError("Audit test must not access the application collection")
        monkeypatch.setattr(SessionDocument, "get_pymongo_collection", forbidden_application_collection)
        collection = client[database_name]["analysis_delivery_checks"]
        store = AnalysisDeliveryAuditStore(collection)
        assert store.collection is collection
        records = [{
            "path": f"/home/ubuntu/output/group-{index}/result.csv", "kind": "table", "valid": False,
            "reason": "inconsistent_table_width", "size": 31, "sha256": "a" * 64,
            "diagnostics": {"row_number": 3, "expected_columns": 4, "actual_columns": 5,
                            "row_count": True, "column_count": -1, "size_bytes": 2**64,
                            "raw_row": "private-cell-value", "source_path": "/Users/private/data.csv"},
            "contents": "private-cell-value", "parser_error": "private-parser-error",
            "metadata": {"host_path": "/Users/private/data.csv"},
        } for index in range(65)]
        records.append({"path": "/Users/private/data.csv", "valid": False,
                        "reason": "unavailable_or_unsafe_path", "kind": "table"})
        await store.record(user_id="audit-owner", session_id="audit-session", input_seq=107, step_id="analysis-step",
            records=records, requirements=[DeliverableRequirement(kind="table", formats=["csv"])],
            outcome=AnalysisOutcome(status="failed", reason_code="artifact_validation_failed"),
            repair_reason="local_artifact_repair_allowed")
        header = await collection.find_one({"document_type": "attempt"})
        assert header is not None and header["state"] == "complete" and header["receipt_count"] == 66
        assert header["created_at"] <= header["completed_at"]
        assert "records" not in header and "contents" not in header
        identity = {"owner_id": "audit-owner", "session_id": "audit-session", "input_seq": 107,
                    "step_id": "analysis-step", "attempt_id": header["attempt_id"]}
        documents = await collection.find(identity).to_list(length=100)
        assert len(documents) == await collection.count_documents({}) == 67
        rows = sorted((item for item in documents if item["document_type"] == "receipt"), key=lambda item: item["ordinal"])
        assert [item["ordinal"] for item in rows] == list(range(66))
        for document in documents:
            assert {key: document[key] for key in identity} == identity
        for index, row in enumerate(rows[:65]):
            assert row["_id"] == f"{header['attempt_id']}:{index}"
            assert row["receipt"] == {
                "path": records[index]["path"], "valid": False, "kind": "table",
                "reason": "inconsistent_table_width", "size": 31, "sha256": "a" * 64,
                "diagnostics": {"row_number": 3, "expected_columns": 4, "actual_columns": 5},
            }
        assert rows[-1]["receipt"]["path"] is None
        serialized = str(documents)
        assert all(value not in serialized for value in ("private-cell-value", "private-parser-error", "/Users/private"))
    finally:
        # Only this exact test-created UUID database may be removed. Never
        # derive the deletion target from the configured connection's DB name.
        await client.drop_database(database_name)
        await client.close()
