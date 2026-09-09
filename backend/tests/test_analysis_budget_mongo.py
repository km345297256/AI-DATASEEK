"""Optional real-Mongo CAS test; uses and removes only a random temporary DB."""
import asyncio
import os
from uuid import uuid4

import pytest
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.domain.services.analysis_budget import AnalysisBudgetService, BudgetEvidence, BudgetPolicy
from app.infrastructure.repositories.mongo_analysis_budget_repository import MongoAnalysisBudgetRepository


@pytest.mark.asyncio
async def test_real_mongo_original_request_budget_survives_concurrency_and_continuation():
    uri = os.environ.get("ANALYSIS_BUDGET_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set ANALYSIS_BUDGET_TEST_MONGODB_URI for isolated real-Mongo verification")
    database_name = "dataseek_budget_test_" + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        collection = client[database_name]["analysis_budgets"]
        service = AnalysisBudgetService(MongoAnalysisBudgetRepository(collection), policy=BudgetPolicy(
            initial_batches=2, increment_batches=2, max_grants=2, hard_batches=6,
            model_call_limit=5, model_token_limit=500, grant_min_model_tokens=10,
        ))
        handles = await asyncio.gather(*(service.open(user_id="owner", session_id="session", origin_input_id="11",
                                                      scope_digest="a" * 64) for _ in range(10)))
        assert len({handle.lineage_id for handle in handles}) == 1
        assert await collection.count_documents({}) == 1
        proof = BudgetEvidence(scope_digest="a" * 64, confirmed_progress_units=1, progress_digest="b" * 64,
                               next_action_bounded=True)
        batches = await asyncio.gather(*(handle.reserve_tool_batch(proof) for handle in handles))
        assert sum(item.allowed for item in batches) == 4
        doc = await collection.find_one({"_id": handles[0].lineage_id})
        assert doc["grant_count"] == len(doc["grants"]) == 1
        assert doc["tool_batches_used"] == len(doc["tool_reservations"]) == 4
        models = await asyncio.gather(*(handle.reserve_model_request(100) for handle in handles))
        admitted = [item for item in models if item.allowed]
        assert len(admitted) == 5
        await asyncio.gather(*(handles[0].settle_model_request(item.reservation_id, 30) for item in admitted))
        resumed = await service.open(user_id="owner", session_id="session", origin_input_id="99",
                                     scope_digest="a" * 64, lineage_id=handles[0].lineage_id)
        snapshot = await resumed.snapshot()
        assert snapshot.model_calls == 5 and snapshot.charged_tokens == 150
        assert snapshot.tool_batches_used == 4 and snapshot.grant_count == 1
        assert not (await resumed.reserve_model_request(1)).allowed
        assert (await resumed.reserve_tool_batch(proof)).reason == "budget_no_confirmed_progress"
    finally:
        # Never use the configured application's DB name or broad cleanup.
        await client.drop_database(database_name)
        await client.close()


@pytest.mark.asyncio
async def test_real_mongo_unlimited_usage_is_atomic_per_call_and_lineage_document_stays_bounded():
    uri = os.environ.get("ANALYSIS_BUDGET_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set ANALYSIS_BUDGET_TEST_MONGODB_URI for isolated real-Mongo verification")
    database_name = "dataseek_usage_test_" + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        collection = client[database_name]["analysis_budgets"]
        operations = client[database_name]["analysis_usage_operations"]
        service = AnalysisBudgetService(MongoAnalysisBudgetRepository(collection, operations),
                                        policy=BudgetPolicy.from_settings(None))
        handle = await service.open(user_id="owner", session_id="session", origin_input_id="11", scope_digest="a" * 64)
        before = await collection.find_one({"_id": handle.lineage_id})
        proof = BudgetEvidence(scope_digest="a" * 64, confirmed_progress_units=0, progress_digest="b" * 64)
        tool_id, model_id = uuid4().hex, uuid4().hex
        tools = await asyncio.gather(*(handle.reserve_tool_batch(proof, reservation_id=tool_id) for _ in range(20)))
        models = await asyncio.gather(*(handle.reserve_model_request(100, reservation_id=model_id) for _ in range(20)))
        assert sum(item.allowed for item in tools) == sum(item.allowed for item in models) == 1
        await asyncio.gather(*(handle.settle_model_request(model_id, 30) for _ in range(20)))
        for _ in range(14):
            batch = await asyncio.gather(*(handle.reserve_model_request(10000) for _ in range(10)))
            assert all(item.allowed for item in batch)
        batches = await asyncio.gather(*(handle.reserve_tool_batch(proof) for _ in range(70)))
        assert all(item.allowed for item in batches)
        resumed = await service.open(user_id="owner", session_id="session", origin_input_id="99",
                                     scope_digest="a" * 64, lineage_id=handle.lineage_id)
        snapshot = await resumed.snapshot()
        assert (snapshot.tool_batches_used, snapshot.model_calls, snapshot.charged_tokens) == (71, 141, 1400030)
        assert snapshot.deadline_at is None and snapshot.hard_limit is None
        assert snapshot.model_call_limit is None and snapshot.model_token_limit is None
        assert await collection.find_one({"_id": handle.lineage_id}) == before
        assert await operations.count_documents({}) == 212
        assert not (await resumed.reserve_model_request(100, reservation_id=model_id)).allowed
    finally:
        await client.drop_database(database_name)
        await client.close()
