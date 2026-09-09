"""Private audit projections and acknowledged, independently bounded writes."""
import asyncio
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

from bson import BSON
import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome, ArtifactIssue, DeliverableRequirement
from app.domain.services.analysis_delivery_audit import AnalysisDeliveryAuditStore, RECEIPT_BATCH_SIZE


class Collection:
    def __init__(self):
        self.documents = []
        self.batches = []
        self.updates = []
        self.fail_batch = None
        self.acknowledged = True
        self.matched_count = 1

    async def insert_one(self, document):
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def insert_many(self, documents, *, ordered):
        assert ordered is True
        self.batches.append(deepcopy(documents))
        if len(self.batches) == self.fail_batch:
            raise RuntimeError("test_audit_write_failed")
        self.documents.extend(deepcopy(documents))
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def update_one(self, query, update):
        self.updates.append((deepcopy(query), deepcopy(update)))
        matched = 0
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()) and self.matched_count:
                document.update(deepcopy(update["$set"]))
                matched += 1
        return SimpleNamespace(acknowledged=self.acknowledged, matched_count=matched)


def receipt(**changes):
    return {"path": "/home/ubuntu/output/table.csv", "kind": "table", "valid": False,
            "reason": "inconsistent_table_width", "sha256": "a" * 64, "size": 31,
            "diagnostics": {"row_number": 3, "expected_columns": 4, "actual_columns": 5}, **changes}


def request(**changes):
    return {"user_id": "owner", "session_id": "session", "input_seq": 12, "step_id": "step",
            "records": [receipt()], "requirements": [DeliverableRequirement(kind="table", formats=["csv"])],
            "outcome": AnalysisOutcome(status="failed", reason_code="artifact_validation_failed"),
            "repair_reason": "local_artifact_repair_allowed", **changes}


def receipts(collection):
    return [item["receipt"] for item in collection.documents if item["document_type"] == "receipt"]


@pytest.mark.asyncio
async def test_each_attempt_and_receipt_has_the_complete_private_identity():
    collection = Collection()
    await AnalysisDeliveryAuditStore(collection).record(**request())
    header, row = collection.documents
    assert header["state"] == "complete" and header["receipt_count"] == 1
    assert header["step_id"] == "step" and header["step_id_encoding"] == "plain"
    assert header["created_at"] <= header["completed_at"]
    assert row["ordinal"] == 0 and row["_id"] == header["attempt_id"] + ":0"
    for item in collection.documents:
        assert {key: item[key] for key in ("owner_id", "session_id", "input_seq", "step_id", "attempt_id")} == {
            "owner_id": "owner", "session_id": "session", "input_seq": 12, "step_id": "step",
            "attempt_id": header["attempt_id"],
        }
    assert "records" not in header
    assert receipts(collection) == [receipt()]
    assert collection.updates[0][0]["state"] == "writing"


@pytest.mark.asyncio
async def test_projection_discards_raw_contents_paths_metadata_and_error_messages():
    collection = Collection()
    raw = receipt(content="private-cell", error="private-parser-excerpt", metadata={"host_path": "/Users/private/source"},
                  diagnostics={"row_number": 3, "expected_columns": 4, "actual_columns": 5,
                               "row_count": True, "column_count": -1, "size_bytes": 2**64,
                               "path": "/Users/private/source", "raw": "private-cell"})
    outcome = AnalysisOutcome(status="partial", reason_code="unknown_safe_code", can_resume=True,
        resume_from="f" * 32, issues=[ArtifactIssue(artifact_name="table.csv", kind="table",
                                                 reason_code="inconsistent_table_width", blocking=True)])
    await AnalysisDeliveryAuditStore(collection).record(**request(records=[raw], outcome=outcome,
                                                                  repair_reason="/Users/private/error"))
    assert receipts(collection) == [receipt()]
    header = collection.documents[0]
    assert header["repair_reason"] == "invalid_validation_evidence"
    assert header["outcome"]["reason_code"] == "execution_failed"
    assert "resume_from" not in header["outcome"] and "can_resume" not in header["outcome"]
    serialized = str(collection.documents)
    assert all(value not in serialized for value in ("private-cell", "private-parser", "/Users/private", "f" * 32))


@pytest.mark.parametrize("path", [
    "/Users/private/table.csv", "/home/ubuntu/datasets/data.csv", "/home/ubuntu/output/../secret.csv",
    "/home/ubuntu/output/./table.csv", "/home/ubuntu/output//table.csv", "/home/ubuntu/output/table.csv/",
    "/home/ubuntu/output\\table.csv", "/home/ubuntu/output/tab\x00le.csv", "/home/ubuntu/output/tab\x7fle.csv",
    "/home/ubuntu/output/" + "x" * 4096, None, 3, {"bad": "path"},
])
@pytest.mark.asyncio
async def test_only_bounded_canonical_output_paths_are_persisted(path):
    collection = Collection()
    await AnalysisDeliveryAuditStore(collection).record(**request(records=[receipt(path=path)]))
    assert receipts(collection)[0]["path"] is None


@pytest.mark.parametrize("changes", [
    {"valid": "true"}, {"valid": None}, {"reason": "raw-parser-message"},
    {"valid": True, "sha256": "x" * 64}, {"valid": True, "size": True},
    {"valid": True, "size": 2**64}, {"valid": True, "kind": "image"},
    {"valid": True, "path": "/Users/private/chart.png"}, {"valid": True, "kind": []},
])
@pytest.mark.asyncio
async def test_malformed_validation_cannot_be_stored_as_a_valid_receipt(changes):
    collection = Collection()
    await AnalysisDeliveryAuditStore(collection).record(**request(records=[receipt(**changes)]))
    stored = receipts(collection)[0]
    assert stored["valid"] is False and stored["reason"] == "validation_receipt_invalid"


@pytest.mark.asyncio
async def test_valid_json_table_and_malformed_nonobject_have_fixed_projections():
    collection = Collection()
    await AnalysisDeliveryAuditStore(collection).record(**request(records=[
        receipt(path="/home/ubuntu/output/table.json", valid=True), "private-unstructured-payload",
    ]))
    valid, invalid = receipts(collection)
    assert valid["valid"] is True and valid["reason"] == "validated" and valid["kind"] == "table"
    assert invalid == {"path": None, "valid": False, "kind": "any", "reason": "validation_receipt_invalid",
                       "sha256": None, "size": None, "diagnostics": {}}


@pytest.mark.asyncio
async def test_large_attempt_uses_small_independent_documents_and_bounded_batches():
    collection = Collection()
    # The total exceeds Mongo's single-document limit; every stored document
    # and every insert batch remains small. Input may be a streaming iterable.
    count = 4300
    rows = (receipt(path=f"/home/ubuntu/output/{index}/" + "x" * 3980 + ".csv") for index in range(count))
    await AnalysisDeliveryAuditStore(collection).record(**request(records=rows))
    sizes = [len(BSON.encode(item)) for item in collection.documents]
    assert sum(sizes) > 16 * 1024 * 1024
    assert max(sizes) < 10 * 1024
    assert all(len(batch) <= RECEIPT_BATCH_SIZE for batch in collection.batches)
    assert all(sum(len(BSON.encode(item)) for item in batch) < 320 * 1024 for batch in collection.batches)
    assert collection.documents[0]["receipt_count"] == count


@pytest.mark.asyncio
async def test_receipt_failure_retains_incomplete_attempt_without_retry_or_false_completion():
    collection = Collection()
    collection.fail_batch = 2
    with pytest.raises(RuntimeError, match="test_audit_write_failed"):
        await AnalysisDeliveryAuditStore(collection).record(**request(records=[receipt()] * 70))
    assert collection.documents[0]["state"] == "writing"
    assert "receipt_count" not in collection.documents[0]
    assert len(collection.batches) == 2 and len(receipts(collection)) == 32
    assert collection.updates == []


@pytest.mark.asyncio
async def test_unacknowledged_header_stops_without_receipt_writes():
    collection = Collection()
    collection.acknowledged = False
    with pytest.raises(RuntimeError, match="audit_write_unconfirmed"):
        await AnalysisDeliveryAuditStore(collection).record(**request())
    assert not collection.batches and not collection.updates


@pytest.mark.asyncio
async def test_unconfirmed_final_transition_is_not_a_successful_audit():
    collection = Collection()
    collection.matched_count = 0
    with pytest.raises(RuntimeError, match="audit_completion_unconfirmed"):
        await AnalysisDeliveryAuditStore(collection).record(**request())
    assert collection.documents[0]["state"] == "writing"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_cannot_mark_an_incomplete_attempt_complete():
    started = asyncio.Event()
    class BlockingCollection(Collection):
        async def insert_many(self, documents, *, ordered):
            started.set()
            await asyncio.Event().wait()
    collection = BlockingCollection()
    task = asyncio.create_task(AnalysisDeliveryAuditStore(collection).record(**request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert collection.documents[0]["state"] == "writing" and not collection.updates


@pytest.mark.parametrize("changes", [
    {"input_seq": None}, {"input_seq": True}, {"input_seq": 0}, {"input_seq": 2**64},
    {"user_id": "/Users/private/user"}, {"session_id": "x" * 161}, {"step_id": ""}, {"step_id": None},
    {"requirements": [DeliverableRequirement(kind="image")] * 17},
])
@pytest.mark.asyncio
async def test_invalid_identity_or_contract_fails_before_any_storage(changes):
    collection = Collection()
    with pytest.raises(ValueError):
        await AnalysisDeliveryAuditStore(collection).record(**request(**changes))
    assert not collection.documents


@pytest.mark.parametrize("step_id", ["统计 步骤一", "step one", "/Users/private/model-step", "bad\nstep",
                                   "x" * 10000, "\ud800", " "])
@pytest.mark.asyncio
async def test_valid_nonstandard_step_ids_get_stable_bounded_opaque_identities(step_id):
    collection = Collection()
    store = AnalysisDeliveryAuditStore(collection)
    await store.record(**request(step_id=step_id))
    await store.record(**request(step_id=step_id))
    expected = "sha256:" + sha256(step_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    assert all(item["step_id"] == expected and item["step_id_encoding"] == "sha256" for item in collection.documents)
    assert len({item["attempt_id"] for item in collection.documents}) == 2
    assert all(item["state"] == "complete" for item in collection.documents if item["document_type"] == "attempt")
    assert len(expected) == 71
    if step_id not in {" ", "\ud800"}:
        assert step_id not in str(collection.documents)


@pytest.mark.asyncio
async def test_literal_digest_looking_step_id_cannot_collide_with_an_encoded_identity():
    collection = Collection()
    store = AnalysisDeliveryAuditStore(collection)
    raw = "统计 步骤"
    literal = "sha256:" + sha256(raw.encode()).hexdigest()
    await store.record(**request(step_id=raw))
    await store.record(**request(step_id=literal))
    headers = [item for item in collection.documents if item["document_type"] == "attempt"]
    assert [item["step_id"] for item in headers] == [literal, literal]
    assert [item["step_id_encoding"] for item in headers] == ["sha256", "plain"]


@pytest.mark.asyncio
async def test_concurrent_attempts_and_an_empty_attempt_keep_separate_associations():
    collection = Collection()
    store = AnalysisDeliveryAuditStore(collection)
    await asyncio.gather(*(store.record(**request(input_seq=seq, records=[] if seq == 1 else [receipt()]))
                           for seq in range(1, 6)))
    headers = [item for item in collection.documents if item["document_type"] == "attempt"]
    assert len({item["attempt_id"] for item in headers}) == 5
    for header in headers:
        rows = [item for item in collection.documents if item["document_type"] == "receipt"
                and item["attempt_id"] == header["attempt_id"]]
        assert header["state"] == "complete" and header["receipt_count"] == len(rows)
        assert all(item["input_seq"] == header["input_seq"] for item in rows)
