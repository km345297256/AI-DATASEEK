import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application.services import dataset_request_resolver as resolver_module
from app.application.services.dataset_request_resolver import (
    CatalogQuery,
    CatalogArtifact,
    DatasetCatalogQueryService,
    DatasetRequestResolver,
    ExecutionDecision,
    FrontControllerResolution,
    RequestDecision,
)
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation, DatasetStorageType
from app.domain.models.event import DoneEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.domain.models.session import Session
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.models.safety import SafetyReview


def _dataset() -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id="dataset-1",
        data_center_id="center-1",
        data_center_name="Center",
        name="Climate data",
        files=[
            DatasetFile(path="monthly/rain_195301.nc", size=123),
            DatasetFile(path="monthly/snow_195301.nc", size=456),
        ],
        metadata={"inventory_complete": True},
    )


def _directory_dataset() -> DataCenterDataset:
    location = DatasetLocation(
        location_id="dsl_catalog_root",
        node_id="node-1",
        storage_type=DatasetStorageType.HOST_PATH,
        source_path="/private/datasets/climate",
        mount_name="climate",
        verified=True,
    )
    return DataCenterDataset(
        dataset_id="dataset-directory-1",
        data_center_id="center-1",
        data_center_name="Center",
        name="Climate data",
        files=[DatasetFile(
            path="sources/dsl_catalog_root/climate/monthly/rain_195301.nc",
            size=123,
        )],
        locations=[location],
        metadata={"inventory_complete": True},
    )


class _FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        self.requests.append(messages)
        return SimpleNamespace(content=self.responses.pop(0))


class _EmptyPolicyStore:
    async def list_enabled(self):
        return []


def _resolver() -> DatasetRequestResolver:
    resolver = DatasetRequestResolver()
    resolver._policy_store = _EmptyPolicyStore()
    return resolver


def _metadata_controller_payload(
    *,
    catalog_goal="dataset_metadata",
    mode="catalog",
    answer="",
    catalog_queries=None,
):
    return {
        "safety": {
            "decision": "allow",
            "risk_level": "low",
            "categories": [],
            "reason": "",
            "suggestion": "",
        },
        "execution": {
            "mode": mode,
            "required_evidence": "catalog" if mode == "catalog" else "file_content",
            "required_capabilities": [],
            "requires_artifacts": False,
            "target_files": [],
        },
        "catalog_goal": catalog_goal,
        "answer": answer,
        "catalog_queries": (
            [{"operation": "dataset_metadata", "limit": 50}]
            if catalog_queries is None
            else catalog_queries
        ),
        "reason": "读取登记的数据集简介",
    }


def _resolution(*, mode="direct", answer="文件后缀名是 `.nc`。", safety=None):
    review = safety or SafetyReview(decision="allow", risk_level="low")
    return FrontControllerResolution(
        decision=RequestDecision(
            safety=review,
            execution=ExecutionDecision(
                mode=mode,
                required_evidence="user_message" if mode == "direct" else "file_content",
            ),
            answer=answer if mode == "direct" else "",
        ),
        answer=answer if mode == "direct" else "",
        controller_metadata={"prompt_version": "test", "execution_mode": mode},
    )


@pytest.mark.asyncio
async def test_resolver_answers_from_user_text_without_catalog_or_sandbox(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"direct","required_evidence":"user_message","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"文件后缀名是 `.nc`。","catalog_queries":[],"reason":"答案已在文件名中"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="rain_195301.nc 的后缀名是什么？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution is not None
    assert resolution.mode == "direct"
    assert resolution.answer == "文件后缀名是 `.nc`。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_dataset_backed_request_uses_semantic_front_controller_model(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"direct","required_evidence":"user_message","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"文件后缀名是 `.nc`。","catalog_queries":[],"reason":"答案已在文件名中"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="rain_195301.nc 的后缀名是什么？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "direct"
    assert resolution.answer == "文件后缀名是 `.nc`。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_resolver_uses_generic_catalog_query_selected_by_model(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"","catalog_queries":[{"operation":"search_files","query":"snow_195301.nc","limit":10}],"reason":"需要清单证据"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="清单里 snow_195301.nc 是什么格式？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution is not None
    assert resolution.mode == "catalog"
    assert ".nc" in resolution.answer
    assert model.calls == 1
    assert resolution.controller_metadata["source"] == "catalog_executor"


@pytest.mark.asyncio
async def test_catalog_lookup_resolves_file_from_prior_archive_manifest(monkeypatch):
    filename = "QilianMountains_Annual_Average_Vapor_Pressure_(2011-2020).tif"
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        f'"answer":"","catalog_queries":[{{"operation":"search_files","query":"{filename}","limit":10}}],'
        '"reason":"查询此前解压的文件路径"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    archive_payload = {
        "success": True,
        "source_archive": "vapor-pressure.rar",
        "summary": {"archive_count": 1, "file_count": 1, "expanded_bytes": 1024},
        "files": [{"path": f"vapor-pressure/{filename}", "size": 1024}],
    }
    unpack_event = ToolEvent(
        tool_call_id="unpack-1",
        tool_name="shell",
        function_name="dataset_unpack",
        function_args={"output_dir": "/home/ubuntu/output/unpacked-private"},
        status=ToolStatus.CALLED,
        function_result=ToolResult(
            success=True,
            data={
                "status": "completed",
                "returncode": 0,
                "output": resolver_module.json.dumps(archive_payload),
            },
        ),
    )
    dataset = DataCenterDataset(
        dataset_id="archive-dataset",
        data_center_id="center-1",
        data_center_name="Center",
        name="Archive data",
        files=[DatasetFile(path="vapor-pressure.rar", size=2048)],
        metadata={"inventory_complete": True},
    )

    resolution = await _resolver().resolve(
        question=f"{filename} 的路径是什么？",
        datasets=[dataset],
        events=[unpack_event],
    )

    assert resolution.mode == "catalog"
    assert f"`vapor-pressure.rar!/vapor-pressure/{filename}`" in resolution.answer
    assert "本次会话的解压清单" in resolution.answer
    assert "/home/ubuntu/output" not in resolution.answer


@pytest.mark.asyncio
async def test_catalog_answer_hides_internal_source_mount_prefix(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"answer":"","catalog_queries":[{"operation":"aggregate_files","metrics":["max_size"],"return_files":true}],"reason":"需要清单证据"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="最大文件是什么？",
        datasets=[_directory_dataset()],
        events=[],
    )

    assert "`monthly/rain_195301.nc`" in resolution.answer
    assert "sources/dsl_catalog_root/climate" not in resolution.answer


@pytest.mark.asyncio
async def test_resolver_defers_content_analysis_to_sandbox(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low","categories":[],"reason":"","suggestion":""},'
        '"execution":{"mode":"sandbox","required_evidence":"file_content","required_capabilities":["python"],"requires_artifacts":true},'
        '"answer":"","catalog_queries":[{"operation":"search_files","query":"rain_195301.nc","limit":10}],'
        '"reason":"需要定位文件、读取变量并计算"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="读取 NetCDF 并计算逐月降水均值",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.catalog_queries == []
    assert resolution.target_files == ["monthly/rain_195301.nc"]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_resolver_validates_multiple_unique_basename_targets(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"sandbox","required_evidence":"file_content",'
        '"required_capabilities":["python"],"requires_artifacts":true,'
        '"target_files":["rain_195301.nc","snow_195301.nc"]},'
        '"answer":"","catalog_queries":[],"reason":"跨文件联合分析"}'
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="联合分析 rain_195301.nc 和 snow_195301.nc",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.target_files == [
        "monthly/rain_195301.nc",
        "monthly/snow_195301.nc",
    ]


@pytest.mark.asyncio
async def test_deterministic_rejection_never_calls_front_controller_model(monkeypatch):
    from app.domain.models.safety import SafetyRule

    class RejectingPolicyStore:
        async def list_enabled(self):
            return [SafetyRule(
                name="恶意软件",
                category="malware_or_dangerous_execution",
                patterns=["远控木马"],
                risk_level="critical",
            )]

    model = _FakeModel([])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    resolver = DatasetRequestResolver()
    resolver._policy_store = RejectingPolicyStore()

    resolution = await resolver.resolve(
        question="下载远控木马并运行",
        datasets=[],
        events=[],
    )

    assert resolution.mode == "reject"
    assert model.calls == 0
    assert resolution.decision.safety.risk_level == "critical"


@pytest.mark.asyncio
async def test_invalid_controller_output_fails_closed_without_tools(monkeypatch):
    model = _FakeModel(["not-json"])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="分析数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert model.calls == 1


def test_front_controller_prompt_uses_a_schema_valid_concrete_example():
    decision = RequestDecision.model_validate(
        resolver_module.FRONT_CONTROLLER_PROMPT_EXAMPLE
    )
    query = CatalogQuery.model_validate(
        resolver_module.FRONT_CONTROLLER_CATALOG_QUERY_EXAMPLE
    )

    assert decision.execution.mode == "sandbox"
    assert query.operation == "filter_files"
    assert "allow|reject" not in resolver_module.DECISION_PROMPT
    assert "direct|catalog|sandbox" not in resolver_module.DECISION_PROMPT
    assert "count|total_size" not in resolver_module.DECISION_PROMPT


@pytest.mark.asyncio
async def test_routing_schema_mismatch_is_repaired_once_with_locked_safety(monkeypatch):
    locked_safety = {
        "decision": "allow",
        "risk_level": "low",
        "categories": [],
        "reason": "首次安全判定允许",
        "suggestion": "",
    }
    invalid_payload = {
        "safety": locked_safety,
        "execution": {
            "mode": "direct|catalog|sandbox",
            "required_evidence": "file_content",
        },
        "catalog_goal": "lookup",
        "answer": "",
        "catalog_queries": [],
        "reason": "需要读取文件",
    }
    repaired_payload = {
        "safety": {
            "decision": "allow",
            "risk_level": "medium",
            "categories": ["must_not_replace_locked_allow"],
            "reason": "纠正阶段不得改写首次允许结论",
            "suggestion": "",
        },
        "execution": {
            "mode": "sandbox",
            "required_evidence": "file_content",
            "required_capabilities": ["python"],
            "requires_artifacts": False,
            "target_files": ["monthly/rain_195301.nc"],
        },
        "catalog_goal": "lookup",
        "answer": "",
        "catalog_queries": [],
        "reason": "读取指定文件",
    }
    model = _FakeModel([
        json.dumps(invalid_payload, ensure_ascii=False),
        json.dumps(repaired_payload, ensure_ascii=False),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="读取 rain_195301.nc 并分析",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.safety.model_dump(mode="json") == locked_safety
    assert resolution.target_files == ["monthly/rain_195301.nc"]
    assert model.calls == 2
    repair_message = model.requests[1][-1].content
    assert repair_message.startswith(resolver_module.ROUTING_SCHEMA_REPAIR_PROMPT)
    assert json.loads(repair_message.split(
        "Validation errors (field and error type only):\n", 1
    )[1]) == [{"loc": "execution.mode", "type": "literal_error"}]


@pytest.mark.parametrize(
    "extensions",
    [
        "",
        " ",
        ".",
        ".nc,.csv",
        ".nc .csv",
        "*.nc",
        "/tmp/file.nc",
        ".tar.gz",
        "." + "a" * 33,
        {"extension": ".nc"},
        123,
        [".nc", None],
        [".nc", {"extension": ".csv"}],
    ],
)
def test_catalog_extensions_reject_ambiguous_shapes_without_dropping_filters(extensions):
    with pytest.raises(ValidationError):
        CatalogQuery(operation="filter_files", extensions=extensions)


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_repair", [False, True], ids=["initial", "repair"])
@pytest.mark.parametrize("extensions", [None, ".nc", " NC "])
async def test_catalog_extension_compatibility_preserves_all_filter_conditions(
    monkeypatch,
    extensions,
    requires_repair,
):
    payload = _metadata_controller_payload(
        catalog_goal="filtered_summary",
        catalog_queries=[{
            "operation": "filter_files",
            "query": "monthly/",
            "extensions": extensions,
            "size_at_least_bytes": 200,
            "return_files": True,
        }],
    )
    responses = []
    if requires_repair:
        initial_payload = copy.deepcopy(payload)
        initial_payload["catalog_queries"][0]["extensions"] = {
            "private_unrecognized_filter": "do-not-repeat-this-value",
        }
        responses.append(json.dumps(initial_payload))
        payload.pop("safety")
    responses.append(json.dumps(payload))
    model = _FakeModel(responses)
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"files": [
        *_dataset().files,
        DatasetFile(path="monthly/stations.csv", size=768),
        DatasetFile(path="annual/observations.nc", size=1024),
    ]})

    resolution = await _resolver().resolve(
        question=(
            "列出 monthly/ 目录中至少 200 字节的文件"
            if extensions is None
            else "列出 monthly/ 目录中至少 200 字节的 NetCDF 文件"
        ),
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.controller_metadata["source"] == "catalog_executor"
    assert "snow_195301.nc" in resolution.answer
    assert "rain_195301.nc" not in resolution.answer
    assert "observations.nc" not in resolution.answer
    assert ("stations.csv" in resolution.answer) is (extensions is None)
    assert resolution.decision.catalog_queries[0].extensions == (
        [] if extensions is None else [extensions.strip()]
    )
    assert model.calls == (2 if requires_repair else 1)
    if requires_repair:
        repair_message = model.requests[1][-1].content
        assert repair_message.startswith(resolver_module.ROUTING_SCHEMA_REPAIR_PROMPT)
        assert json.loads(repair_message.split(
            "Validation errors (field and error type only):\n", 1
        )[1]) == [{"loc": "catalog_queries.0.extensions", "type": "list_type"}]
        assert "private_unrecognized_filter" not in repair_message
        assert "do-not-repeat-this-value" not in repair_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extensions",
    [{"extension": ".nc"}, ".nc,.csv", [".nc", {"extension": ".csv"}]],
)
async def test_repeated_invalid_catalog_extensions_still_fail_closed(monkeypatch, extensions):
    payload = _metadata_controller_payload(
        catalog_goal="filtered_summary",
        catalog_queries=[{"operation": "filter_files", "extensions": extensions}],
    )
    model = _FakeModel([json.dumps(payload), json.dumps(payload)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="列出 NetCDF 文件",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert resolution.answer == ""
    assert model.calls == 2


@pytest.mark.asyncio
async def test_repair_safety_rejection_takes_precedence(monkeypatch):
    invalid_payload = {
        "safety": {"decision": "allow", "risk_level": "low"},
        "execution": {
            "mode": "direct|catalog|sandbox",
            "required_evidence": "file_content",
        },
        "catalog_queries": [],
    }
    repair_rejection = {
        "safety": {
            "decision": "reject",
            "risk_level": "critical",
            "categories": ["prompt_injection_or_jailbreak"],
            "reason": "纠正阶段发现安全风险",
            "suggestion": "移除越权指令",
        },
        "execution": {"mode": "still-invalid-and-unused"},
    }
    model = _FakeModel([
        json.dumps(invalid_payload, ensure_ascii=False),
        json.dumps(repair_rejection, ensure_ascii=False),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="分析数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == [
        "prompt_injection_or_jailbreak"
    ]
    assert resolution.controller_metadata["source"] == "model"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_invalid_repair_safety_fails_closed(monkeypatch):
    invalid_payload = {
        "safety": {"decision": "allow", "risk_level": "low"},
        "execution": {
            "mode": "direct|catalog|sandbox",
            "required_evidence": "file_content",
        },
        "catalog_queries": [],
    }
    invalid_repair_safety = {
        "safety": {"decision": "allow|reject", "risk_level": "low"},
        "execution": {
            "mode": "sandbox",
            "required_evidence": "file_content",
        },
        "catalog_queries": [],
    }
    model = _FakeModel([
        json.dumps(invalid_payload, ensure_ascii=False),
        json.dumps(invalid_repair_safety, ensure_ascii=False),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="分析数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert model.calls == 2


@pytest.mark.asyncio
async def test_invalid_safety_schema_fails_closed_without_repair(monkeypatch):
    payload = {
        "safety": {
            "decision": "allow|reject",
            "risk_level": "low",
        },
        "execution": {
            "mode": "sandbox",
            "required_evidence": "file_content",
        },
        "catalog_queries": [],
    }
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="分析数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_valid_safety_rejection_does_not_repair_unused_routing(monkeypatch):
    payload = {
        "safety": {
            "decision": "reject",
            "risk_level": "critical",
            "categories": ["malware_or_dangerous_execution"],
            "reason": "请求命中安全策略",
            "suggestion": "移除危险操作",
        },
        "execution": {
            "mode": "invalid-unused-mode",
            "required_evidence": "file_content",
        },
        "catalog_queries": [],
    }
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="危险请求",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == [
        "malware_or_dangerous_execution"
    ]
    assert resolution.controller_metadata["source"] == "model"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_semantically_invalid_decision_is_not_schema_repaired(monkeypatch):
    payload = {
        "safety": {"decision": "allow", "risk_level": "low"},
        "execution": {
            "mode": "direct",
            "required_evidence": "user_message",
        },
        "answer": "",
        "catalog_queries": [],
    }
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="直接回答",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_dataset_metadata_goal_swap_is_repaired_only_for_matching_catalog_query(monkeypatch):
    model = _FakeModel([
        json.dumps(_metadata_controller_payload(), ensure_ascii=False),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"description": "逐月气候观测数据"})

    resolution = await _resolver().resolve(
        question="这个数据集的简介是什么？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.decision.catalog_goal == "summary"
    assert [query.operation for query in resolution.decision.catalog_queries] == ["dataset_metadata"]
    assert resolution.controller_metadata["source"] == "catalog_executor"
    assert "逐月气候观测数据" in resolution.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_valid_summary_goal_with_dataset_metadata_query_is_unchanged(monkeypatch):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="展示数据集元数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.decision.catalog_goal == "summary"
    assert [query.operation for query in resolution.decision.catalog_queries] == ["dataset_metadata"]
    assert resolution.controller_metadata["source"] == "catalog_executor"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "coverage_field", "coverage"),
    [
        ("这个数据集的空间范围是多少？", "spatial_coverage", "73.5°E–135.1°E，18.2°N–53.6°N"),
        ("这个数据集的时间范围是多少？", "temporal_coverage", "2011-2020"),
        ("What is this dataset's spatial extent?", "spatial_coverage", "Qilian Mountains"),
        ("What is this dataset's temporal coverage?", "temporal_coverage", "2011-2020"),
    ],
    ids=["spatial-zh", "temporal-zh", "spatial-en", "temporal-en"],
)
async def test_registered_metadata_coverage_stays_in_catalog_and_is_rendered(
    monkeypatch,
    question,
    coverage_field,
    coverage,
):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={coverage_field: coverage})

    resolution = await _resolver().resolve(
        question=question,
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.controller_metadata["source"] == "catalog_executor"
    assert coverage in resolution.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_registered_coverage_preserves_other_catalog_queries(monkeypatch):
    payload = _metadata_controller_payload(
        catalog_goal="summary",
        catalog_queries=[
            {"operation": "inventory_summary", "limit": 50},
        ],
    )
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})

    resolution = await _resolver().resolve(
        question="这个数据集的空间范围和文件数量是多少？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert [
        query.operation for query in resolution.decision.catalog_queries
    ] == ["inventory_summary", "dataset_metadata"]
    assert "祁连山国家公园" in resolution.answer
    assert "共有 2 个文件" in resolution.answer


@pytest.mark.asyncio
async def test_registered_coverage_replaces_direct_answer_with_catalog_evidence(monkeypatch):
    payload = _metadata_controller_payload(
        catalog_goal="summary",
        mode="direct",
        answer="模型猜测的范围",
        catalog_queries=[],
    )
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})

    resolution = await _resolver().resolve(
        question="这个数据集的空间范围是多少？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.controller_metadata["source"] == "catalog_executor"
    assert "祁连山国家公园" in resolution.answer
    assert "模型猜测的范围" not in resolution.answer
    assert [
        query.operation for query in resolution.decision.catalog_queries
    ] == ["dataset_metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "这个数据集的空间范围是多少？",
        "这个数据集的时间范围是多少？",
        "What is this dataset's spatial extent?",
        "What is this dataset's temporal coverage?",
    ],
    ids=["spatial-zh", "temporal-zh", "spatial-en", "temporal-en"],
)
async def test_missing_requested_metadata_coverage_escalates_to_sandbox(monkeypatch, question):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question=question,
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.execution.required_evidence == "file_content"
    assert resolution.decision.catalog_queries == []
    assert resolution.answer == ""
    assert resolution.controller_metadata["source"] == "catalog_fallback"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_missing_coverage_does_not_escalate_unrelated_dataset_metadata(monkeypatch):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={
        "description": "逐月气候观测数据",
        "spatial_coverage": "祁连山国家公园",
        "temporal_coverage": "2011-2020",
    })

    resolution = await _resolver().resolve(
        question="这个数据集的简介是什么？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.controller_metadata["source"] == "catalog_executor"
    assert "逐月气候观测数据" in resolution.answer
    assert "祁连山国家公园" not in resolution.answer
    assert "2011-2020" not in resolution.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_exact_coordinate_bounds_always_use_file_evidence(monkeypatch):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})

    resolution = await _resolver().resolve(
        question="这个数据集的经纬度范围是多少？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.execution.required_evidence == "file_content"
    assert resolution.controller_metadata["source"] == "catalog_fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _metadata_controller_payload(
            catalog_goal="summary",
            mode="direct",
            answer="经纬度范围是 70 至 140 度。",
            catalog_queries=[],
        ),
        _metadata_controller_payload(
            catalog_goal="summary",
            catalog_queries=[{"operation": "inventory_summary", "limit": 50}],
        ),
    ],
    ids=["direct", "wrong-catalog-operation"],
)
async def test_exact_coordinate_bounds_cannot_bypass_file_evidence(monkeypatch, payload):
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})

    resolution = await _resolver().resolve(
        question="这个数据集的经纬度范围是多少？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.answer == ""
    assert resolution.decision.execution.required_evidence == "file_content"
    assert resolution.decision.catalog_queries == []
    assert resolution.controller_metadata["source"] == "catalog_fallback"


@pytest.mark.asyncio
async def test_coverage_fallback_preserves_validated_target_file(monkeypatch):
    payload = _metadata_controller_payload(catalog_goal="summary")
    payload["execution"]["target_files"] = ["monthly/rain_195301.nc"]
    model = _FakeModel([json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="rain_195301.nc 的空间范围是多少？",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.target_files == ["monthly/rain_195301.nc"]
    assert resolution.decision.execution.target_files == ["monthly/rain_195301.nc"]


@pytest.mark.asyncio
async def test_partial_coverage_across_datasets_uses_file_evidence(monkeypatch):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    complete = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})
    incomplete = _dataset().model_copy(update={
        "dataset_id": "dataset-2",
        "name": "Second climate dataset",
    })

    resolution = await _resolver().resolve(
        question="这两个数据集的空间范围分别是什么？",
        datasets=[complete, incomplete],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.controller_metadata["source"] == "catalog_fallback"


@pytest.mark.asyncio
async def test_any_missing_requested_coverage_field_uses_file_evidence(monkeypatch):
    model = _FakeModel([
        json.dumps(
            _metadata_controller_payload(catalog_goal="summary"),
            ensure_ascii=False,
        ),
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )
    dataset = _dataset().model_copy(update={"spatial_coverage": "祁连山国家公园"})

    resolution = await _resolver().resolve(
        question="这个数据集的空间范围和时间范围是什么？",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.controller_metadata["source"] == "catalog_fallback"


@pytest.mark.parametrize(
    "question",
    [
        "这个数据集占用多少存储空间？",
        "这次分析的运行时间是多少？",
        "请分析空间分布和时间趋势。",
        "How much storage space does this dataset use?",
        "What was the analysis runtime?",
    ],
)
def test_unrelated_space_and_time_phrases_are_not_coverage_requests(question):
    assert DatasetRequestResolver._requested_coverage_fields(question) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _metadata_controller_payload(mode="sandbox"),
        _metadata_controller_payload(
            catalog_queries=[{"operation": "inventory_summary", "limit": 50}],
        ),
        _metadata_controller_payload(answer="模型不应为 catalog 模式直接作答"),
        _metadata_controller_payload(
            catalog_queries=[
                {"operation": "dataset_metadata", "limit": 50},
                {"operation": "dataset_metadata", "limit": 50},
            ],
        ),
        _metadata_controller_payload(catalog_goal="unknown_goal"),
    ],
    ids=[
        "wrong-mode",
        "wrong-query-operation",
        "non-empty-answer",
        "multiple-queries",
        "unknown-goal",
    ],
)
async def test_dataset_metadata_goal_swap_mismatch_still_fails_closed(monkeypatch, payload):
    invalid_response = json.dumps(payload, ensure_ascii=False)
    model = _FakeModel([invalid_response, invalid_response])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="展示数据集元数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "reject"
    assert resolution.decision.safety.categories == ["front_controller_unavailable"]
    assert resolution.controller_metadata["source"] == "failure"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_dataset_metadata_goal_swap_does_not_mutate_parsed_payload(monkeypatch):
    payload = _metadata_controller_payload()
    original = copy.deepcopy(payload)
    model = _FakeModel(["ignored because the parser is controlled by this test"])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "parse_json_lenient", lambda _raw: payload)
    monkeypatch.setattr(
        resolver_module,
        "get_settings",
        lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1),
    )

    resolution = await _resolver().resolve(
        question="展示数据集元数据",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.decision.catalog_goal == "summary"
    assert payload == original
    assert payload["catalog_goal"] == "dataset_metadata"


def test_catalog_query_service_exposes_only_logical_catalog_metadata():
    evidence = DatasetCatalogQueryService().execute(
        [_dataset()],
        [CatalogQuery(operation="search_files", query="rain_195301.nc", limit=10)],
    )

    assert evidence[0]["matches"][0]["logical_path"] == "monthly/rain_195301.nc"
    assert evidence[0]["matches"][0]["extension"] == ".nc"
    assert "/home/" not in str(evidence)


def test_catalog_random_sample_returns_requested_unique_files():
    evidence = DatasetCatalogQueryService().execute(
        [_dataset()],
        [CatalogQuery(operation="sample_files", limit=2)],
    )

    matches = evidence[0]["matches"]
    assert len(matches) == 2
    assert len({item["logical_path"] for item in matches}) == 2
    assert evidence[0]["inventory_file_count"] == 2


def test_catalog_filter_files_counts_generic_size_predicate():
    dataset = _dataset()
    dataset.files.append(DatasetFile(path="monthly/large.nc", size=2048))
    evidence = DatasetCatalogQueryService().execute(
        [dataset],
        [CatalogQuery(operation="filter_files", size_greater_than_bytes=1024)],
    )

    assert evidence[0]["match_count"] == 1
    assert evidence[0]["matched_total_size_bytes"] == 2048
    assert evidence[0]["matches"][0]["filename"] == "large.nc"
    assert evidence[0]["filters"]["size_greater_than_bytes"] == 1024


def test_catalog_aggregate_files_computes_extrema_and_ranking_over_full_inventory():
    evidence = DatasetCatalogQueryService().execute(
        [_dataset()],
        [CatalogQuery(
            operation="aggregate_files",
            metrics=["min_size", "max_size", "average_size"],
            order_by="size_bytes",
            order_direction="desc",
            return_files=True,
            limit=1,
        )],
    )[0]

    assert evidence["min_size_bytes"] == 123
    assert evidence["max_size_bytes"] == 456
    assert evidence["average_size_bytes"] == 289.5
    assert evidence["matches"][0]["filename"] == "snow_195301.nc"
    assert evidence["largest_files"][0]["logical_path"] == "monthly/snow_195301.nc"


def test_catalog_inventory_summary_starts_with_direct_count_without_template_heading():
    evidence = DatasetCatalogQueryService().execute(
        [_dataset()],
        [CatalogQuery(operation="inventory_summary")],
    )
    answer = DatasetRequestResolver._render_catalog_answer(
        question="有多少个文件",
        evidence=evidence,
        artifacts=[],
    )

    assert answer.startswith("共有 2 个文件")
    assert "数据集目录统计" not in answer
    assert "**" not in answer


@pytest.mark.asyncio
async def test_resolver_answers_file_size_predicate_from_catalog(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"catalog_goal":"filtered_summary","answer":"",'
        '"catalog_queries":[{"operation":"filter_files","size_greater_than_bytes":1024,"limit":50}],'
        '"reason":"按逐文件大小过滤并计数"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    dataset = _dataset()
    dataset.files.append(DatasetFile(path="monthly/large.nc", size=2048))

    resolution = await _resolver().resolve(
        question="这个数据集里有几个超过1kb的文件",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert "文件数量为 1 个" in resolution.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_resolver_answers_largest_file_with_one_model_call(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"catalog_goal":"aggregate","answer":"",'
        '"catalog_queries":[{"operation":"aggregate_files","metrics":["max_size"],'
        '"order_by":"size_bytes","order_direction":"desc","return_files":true,"limit":1}],'
        '"reason":"在完整登记清单上计算最大文件"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="最大的文件是多大",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert "monthly/snow_195301.nc" in resolution.answer
    assert "456" in resolution.answer
    assert "无法" not in resolution.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_incomplete_filtered_inventory_escalates_to_sandbox(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"catalog_goal":"filtered_summary","answer":"",'
        '"catalog_queries":[{"operation":"filter_files","size_greater_than_bytes":1024,"limit":50}],'
        '"reason":"按逐文件大小过滤并计数"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    dataset = _dataset()
    dataset.metadata["inventory_complete"] = False

    resolution = await _resolver().resolve(
        question="这个数据集里有几个超过1kb的文件",
        datasets=[dataset],
        events=[],
    )

    assert resolution.mode == "sandbox"
    assert resolution.decision.execution.required_capabilities == ["recursive_file_inventory"]
    assert resolution.decision.catalog_queries == []
    assert resolution.controller_metadata["source"] == "catalog_fallback"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_catalog_goal_corrects_natural_language_search_to_random_sample(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":false},'
        '"catalog_goal":"random_sample","answer":"",'
        '"catalog_queries":[{"operation":"search_files","query":"随机挑选文件","limit":2}],"reason":"抽样"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="从数据集中随机挑选两个文件",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.mode == "catalog"
    assert resolution.decision.catalog_queries[0].operation == "sample_files"
    assert resolution.decision.catalog_queries[0].query == ""
    assert model.calls == 1


@pytest.mark.asyncio
async def test_complete_inventory_goal_generates_full_catalog_artifact(monkeypatch):
    model = _FakeModel([
        '{"safety":{"decision":"allow","risk_level":"low"},'
        '"execution":{"mode":"catalog","required_evidence":"catalog","required_capabilities":[],"requires_artifacts":true},'
        '"catalog_goal":"complete_export","answer":"",'
        '"catalog_queries":[{"operation":"search_files","query":"所有文件路径","limit":50}],"reason":"完整导出"}',
    ])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))

    resolution = await _resolver().resolve(
        question="枚举所有文件的路径",
        datasets=[_dataset()],
        events=[],
    )

    assert resolution.decision.catalog_queries[0].operation == "export_file_inventory"
    assert len(resolution.artifacts) == 1
    assert "monthly/rain_195301.nc" in resolution.artifacts[0].content
    assert "monthly/snow_195301.nc" in resolution.artifacts[0].content
    assert "dataset_file_paths.txt" in resolution.answer
    assert model.calls == 1


def test_catalog_query_constrains_model_limit_to_server_capability():
    query = CatalogQuery(operation="export_file_inventory", limit=4860)

    assert query.limit == 200


@pytest.mark.asyncio
async def test_lightweight_runner_uploads_catalog_artifact():
    uploaded = []

    class Storage:
        async def upload_file(self, file_data, filename, user_id, content_type=None, metadata=None):
            uploaded.append(file_data.read().decode("utf-8"))
            return SimpleNamespace(filename=filename, file_id="file-1")

    class Repository:
        async def add_file(self, _session_id, file_info):
            assert file_info.file_id == "file-1"

    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._file_storage = Storage()
    runner._session_repository = Repository()
    runner._resolution = _resolution()
    runner._resolution.artifacts = [CatalogArtifact("paths.tsv", "a.tif\nb.tif\n")]

    files = await runner._upload_catalog_artifacts()

    assert uploaded == ["a.tif\nb.tif\n"]
    assert files[0].filename == "paths.tsv"


@pytest.mark.asyncio
async def test_lightweight_runner_persists_answer_and_done_without_sandbox():
    class Queue:
        def __init__(self, item=None):
            self.item = item
            self.items = []

        async def pop(self):
            return "input-1", self.item

        async def put(self, payload):
            self.items.append(payload)
            return f"output-{len(self.items)}"

    class Task:
        def __init__(self, payload):
            self.input_stream = Queue(payload)
            self.output_stream = Queue()

    class Repository:
        def __init__(self):
            self.events = []
            self.status = None

        async def add_event(self, _session_id, event):
            self.events.append(event)

        async def update_latest_message(self, *_args):
            return None

        async def increment_unread_message_count(self, *_args):
            return None

        async def update_status(self, _session_id, status):
            self.status = status

    class Advice:
        def default_advice(self):
            return SimpleNamespace()

        def to_payload(self, _advice):
            return {"recommendations": [], "is_skill_candidate": False, "skill_reason": ""}

    repository = Repository()
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._resolution = _resolution()
    runner._session_repository = repository
    runner._completion_advice = Advice()
    runner._record_safety_audit = lambda _review: _async_none()
    task = Task(MessageEvent(role="user", message="example.nc 后缀是什么").model_dump_json())

    await runner.run(task)

    assert isinstance(repository.events[0], MessageEvent)
    assert repository.events[0].message == "文件后缀名是 `.nc`。"
    assert repository.events[0].metadata["execution_mode"] == "lightweight"
    assert isinstance(repository.events[1], DoneEvent)


@pytest.mark.asyncio
async def test_controller_failure_is_not_presented_as_a_safety_violation():
    class Queue:
        def __init__(self, item=None):
            self.item = item

        async def pop(self):
            return "input-1", self.item

        async def put(self, _payload):
            return "output-1"

    class Task:
        def __init__(self, payload):
            self.input_stream = Queue(payload)
            self.output_stream = Queue()

    class Repository:
        def __init__(self):
            self.events = []

        async def add_event(self, _session_id, event):
            self.events.append(event)

        async def update_latest_message(self, *_args):
            return None

        async def increment_unread_message_count(self, *_args):
            return None

        async def update_status(self, *_args):
            return None

    review = SafetyReview(
        decision="reject",
        risk_level="high",
        categories=["front_controller_unavailable"],
        reason="前置决策服务暂时不可用。",
    )
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._resolution = _resolution(safety=review)
    runner._session_repository = Repository()
    runner._completion_advice = SimpleNamespace(
        default_advice=lambda: SimpleNamespace(),
        to_payload=lambda _advice: {},
    )
    runner._record_safety_audit = lambda _review: _async_none()

    await runner.run(Task(MessageEvent(role="user", message="分析数据").model_dump_json()))

    metadata = runner._session_repository.events[0].metadata
    assert "front_controller_error" in metadata
    assert "safety_review" not in metadata
    assert metadata["analysis_outcome"]["status"] == "failed"
    assert metadata["analysis_outcome"]["reason_code"] == "front_controller_unavailable"
    assert metadata["execution_stage"] == "routing" and metadata["analysis_started"] is False
    assert runner._session_repository.events[-1].advice is None


async def _async_none():
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("controller_mode", "uses_lightweight"),
    [("direct", True), ("catalog", True), ("sandbox", False)],
)
async def test_agent_domain_selects_task_from_dataset_backed_controller_decision(
    controller_mode,
    uses_lightweight,
):
    class Repository:
        def __init__(self):
            self.events = []

        async def update_status(self, *_args):
            return None

        async def get_events(self, *_args):
            return []

        async def save(self, *_args):
            return None

        async def update_latest_message(self, *_args):
            return None

        async def add_event(self, _session_id, event):
            self.events.append(event)

    class DatasetService:
        async def get_dataset(self, *_args, **_kwargs):
            return _dataset()

    class Resolver:
        async def resolve(self, **_kwargs):
            return _resolution(mode=controller_mode)

    class Task:
        id = "light-task"
        done = False
        accepting_input = True

        def __init__(self):
            self.started = False

        async def enqueue_input(self, _payload):
            return "input-1"

        async def run(self):
            self.started = True

    repository = Repository()
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = repository
    service._dataset_service = DatasetService()
    service._dataset_request_resolver = Resolver()
    service._get_task = lambda _session: _async_value(None)
    lightweight_task = Task()
    sandbox_task = Task()
    service._create_lightweight_task = lambda *_args, **_kwargs: _async_value(lightweight_task)
    service._create_task = lambda *_args, **_kwargs: _async_value(sandbox_task)
    service._resolve_message_attachments = lambda *_args, **_kwargs: _async_value([])
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        dataset_ids=["dataset-1"],
    )

    selected = await service._bootstrap_chat_task_locked(
        session=session,
        user_id="user-1",
        message="rain_195301.nc 的后缀是什么？",
        timestamp=None,
        attachments=None,
        skills=["auto-enabled-analysis-skill"],
        mcp_servers=None,
        dataset_ids=["dataset-1"],
        mcp_access_all=True,
        client_message_id=None,
    )

    expected_task = lightweight_task if uses_lightweight else sandbox_task
    unexpected_task = sandbox_task if uses_lightweight else lightweight_task
    assert selected is expected_task
    assert expected_task.started is True
    assert unexpected_task.started is False
    assert isinstance(repository.events[0], MessageEvent)


async def _async_value(value):
    return value


async def _raise_sandbox_allocation():
    raise AssertionError("sandbox allocation must not run for a lightweight answer")
