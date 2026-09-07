"""Phase 6-8 runtime compatibility and local-efficiency regression coverage.

This module deliberately stops at the ModelDriver binding boundary.  The
reported token counts use the versioned local UTF-8 estimator; timings cover
only in-process tool collection/schema normalization and are neither provider
billing tokens nor end-to-end task latency.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.messages import HumanMessage, SystemMessage

from app.core.config import Settings
from app.domain.models.agent_profile import AgentProfile
from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.models.session import SessionStatus
from app.domain.models.tool_approval import ToolApprovalStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.context_budget import (
    TOKEN_COUNTS_ARE_ESTIMATES,
    TOKEN_ESTIMATOR_VERSION,
    estimate_context_tokens,
    estimate_tool_tokens,
    prepare_context,
)
from app.domain.services.domain_presets import list_domain_presets
from app.domain.services.flows import plan_act
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.analysis_job import AnalysisJobInterceptor
from app.domain.services.tools.authorization import (
    ToolCallAdmissionInterceptor,
    ToolCallAuthorizationInterceptor,
)
from app.domain.services.tools.interceptors import (
    StructuredToolTraceInterceptor,
    ToolConcurrencyInterceptor,
    ToolTimeoutInterceptor,
)
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.spill import SpillArtifactInterceptor
from app.domain.services.tools.spill_projection import spill_notice_from_result
from test_analysis_job_service import InMemoryAnalysisJobRepository
from test_spill_artifact_store import CapturingStore
from test_tool_authorization_integration import services
from test_tool_runtime_flow import MemoryRepository, flow_factory


_RESPONSE_FORMAT = {"type": "json_object"}
_BIND_ROUNDS = 25
_REPORT_ENV = "DATASEEK_RUNTIME_EFFICIENCY_REPORT"


def _schema_name(schema: dict[str, Any]) -> str:
    return str(schema["function"]["name"])


def _canonical_schema_bytes(schemas: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        schemas,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bind_schemas(agent) -> list[dict[str, Any]]:
    """Mirror BaseAgent's per-request ModelDriver binding without invoking it."""

    runnable = agent._model.bind(
        response_format=_RESPONSE_FORMAT,
        tool_choice=agent.tool_choice,
    )
    return list(runnable.bind_tools(agent.get_tools()).kwargs["tools"])


def _percentile_95(samples: list[int]) -> int:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _binding_microbenchmark(agent, *, rounds: int = _BIND_ROUNDS) -> dict[str, Any]:
    """Measure only local get-tools/bind/normalization/serialization work."""

    _bind_schemas(agent)  # Warm provider/Pydantic caches outside the sample.
    elapsed_ns: list[int] = []
    fingerprints: set[str] = set()
    serialized_bytes: set[int] = set()
    tool_tokens: set[int] = set()
    for _ in range(rounds):
        started = time.perf_counter_ns()
        schemas = _bind_schemas(agent)
        payload = _canonical_schema_bytes(schemas)
        elapsed_ns.append(time.perf_counter_ns() - started)
        fingerprints.add(hashlib.sha256(payload).hexdigest())
        serialized_bytes.add(len(payload))
        tool_tokens.add(estimate_tool_tokens(schemas))

    # Rebinding must be stable; one task cannot see a changing catalog
    # generation halfway through its model calls.
    assert len(fingerprints) == 1
    assert len(serialized_bytes) == 1
    assert len(tool_tokens) == 1
    ordered = sorted(elapsed_ns)
    return {
        "rounds": rounds,
        "schema_count": len(schemas),
        "serialized_bytes": serialized_bytes.pop(),
        "estimated_tool_tokens": tool_tokens.pop(),
        "local_bind_schema_us_median": round(ordered[len(ordered) // 2] / 1_000, 3),
        "local_bind_schema_us_p95": round(_percentile_95(elapsed_ns) / 1_000, 3),
        "schema_sha256": fingerprints.pop(),
    }


def _profile_metrics(flow) -> dict[str, Any]:
    schemas = _bind_schemas(flow.executor)
    names = [_schema_name(schema) for schema in schemas]
    assert len(names) == len(set(names))
    serialized = _canonical_schema_bytes(schemas)
    messages = [
        SystemMessage(content=flow.executor.system_prompt),
        SystemMessage(content=flow._dynamic_system_prompt()),
        HumanMessage(content="Inspect the mounted dataset and report evidence."),
    ]
    total_tokens, tool_tokens = estimate_context_tokens(
        messages,
        tool_schemas=schemas,
        response_format=_RESPONSE_FORMAT,
    )
    prepared = prepare_context(
        messages,
        tool_schemas=schemas,
        response_format=_RESPONSE_FORMAT,
        capacity_tokens=131_072,
        max_output_tokens=4_096,
        safety_tokens=2_048,
    )
    assert prepared.input_tokens_before == total_tokens
    assert prepared.input_tokens_after == total_tokens
    assert prepared.tool_tokens == tool_tokens
    assert prepared.input_limit == 124_928
    assert not prepared.records and prepared.is_estimate
    selection = flow._plugin_view.selection_snapshot()
    return {
        "preset_id": selection["preset_id"],
        "selection_mode": selection["selection_mode"],
        "available_plugin_tools": selection["available_tool_count"],
        "loaded_plugin_tools": selection["loaded_tool_count"],
        "model_schema_count": len(schemas),
        "serialized_schema_bytes": len(serialized),
        "estimated_tool_tokens": tool_tokens,
        "estimated_total_input_tokens": total_tokens,
        "input_limit_after_reserves": prepared.input_limit,
    }


def _write_report(payload: dict[str, Any]) -> None:
    destination = os.environ.get(_REPORT_ENV)
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
async def complete_flow_factory(flow_factory, monkeypatch):
    """Build the production model-facing set, including optional search/spill."""

    seed = flow_factory()
    settings = plan_act.get_settings().model_copy(update={"spill_enabled": True})
    monkeypatch.setattr(plan_act, "get_settings", lambda: settings)
    flows = []

    def factory(profile=None):
        ordinal = len(flows)
        flow = PlanActFlow(
            agent_id=f"efficiency-agent-{ordinal}",
            user_id="offline-user",
            agent_repository=MemoryRepository(),
            session_id=f"offline-session-{ordinal}",
            session_repository=SimpleNamespace(
                find_by_id=AsyncMock(return_value=SimpleNamespace(status=SessionStatus.PENDING)),
                update_status=AsyncMock(),
                get_events=AsyncMock(return_value=[]),
            ),
            sandbox=seed._sandbox,
            browser=seed._browser,
            mcp_tool=MCPToolkit(),
            search_engine=SimpleNamespace(search=AsyncMock()),
            plugin_runtime=seed.plugin_toolkit.plugin_runtime,
            spill_artifact_store=CapturingStore(),
            llm_overrides=(
                {"agent_profile": profile}
                if profile is not None
                else None
            ),
        )
        flows.append(flow)
        return flow

    try:
        yield factory
    finally:
        for flow in flows:
            if flow._tool_execution_disposer:
                flow._tool_execution_disposer()
            await flow.drain_spill_saves()


@pytest.mark.asyncio
async def test_real_cordis_profiles_model_driver_budget_and_rebinding(
    complete_flow_factory,
):
    """Exercise the actual catalog through PlanActFlow and ModelDriver."""

    # Deployment defaults apply only when no saved profile is selected.
    assert Settings.model_fields["model_context_capacity_tokens"].default == 131_072
    assert Settings.model_fields["model_context_safety_tokens"].default == 2_048
    assert Settings.model_fields["model_task_token_budget"].default == 1_000_000
    assert Settings.model_fields["model_task_call_budget"].default == 128
    assert Settings.model_fields["tool_selection_mode"].default == "on_demand"
    assert Settings.model_fields["tool_preset_id"].default == "general"
    assert Settings.model_fields["code_mode_enabled"].default is False
    assert Settings.model_fields["domain_subagents_enabled"].default is False

    # A document persisted before tool_runtime existed retains the legacy full
    # catalog and never opts into either Phase-8 experiment.
    legacy_document = AgentProfile.model_validate({
        "id": "legacy-profile",
        "name": "Legacy profile",
    })
    assert legacy_document.tool_runtime.model_dump() == {
        "preset_id": "general",
        "selection_mode": "all",
        "code_mode_enabled": False,
        "domain_subagents_enabled": False,
    }

    default_flow = complete_flow_factory()
    legacy_flow = complete_flow_factory({"name": "Saved before tool_runtime"})
    assert default_flow.plugin_toolkit.catalog_snapshot.engine == "cordis"
    assert default_flow.executor._model._llm_type == "dataseek-model-driver"
    assert default_flow.tool_runtime.selection_mode == "on_demand"
    assert legacy_flow.tool_runtime == legacy_document.tool_runtime
    assert default_flow.plugin_toolkit.catalog_revision == legacy_flow.plugin_toolkit.catalog_revision

    preset_flows = {"general": default_flow}
    for preset in list_domain_presets():
        if preset.id == "general":
            continue
        preset_flows[preset.id] = complete_flow_factory({
            "name": f"{preset.id} on-demand",
            "tool_runtime": {
                "preset_id": preset.id,
                "selection_mode": "on_demand",
            },
        })
    assert set(preset_flows) == {preset.id for preset in list_domain_presets()}

    snapshot = default_flow.plugin_toolkit.catalog_snapshot
    catalog_names = {tool.name for tool in snapshot.tools}
    assert catalog_names
    profile_results: dict[str, dict[str, Any]] = {}
    for preset in list_domain_presets():
        flow = preset_flows[preset.id]
        selection = flow._plugin_view.selection_snapshot()
        available = {
            tool.name
            for tool in snapshot.tools
            if preset.id == "general" or tool.plugin in preset.plugin_ids
        }
        assert selection["available_tool_count"] == len(available)
        assert selection["loaded_tool_count"] == len(
            available.intersection(preset.initial_tools)
        )
        assert set(selection["loaded_tool_names"]).issubset(catalog_names)
        assert flow.executor._model._llm_type == "dataseek-model-driver"
        profile_results[preset.id] = _profile_metrics(flow)

    legacy_metrics = _profile_metrics(legacy_flow)
    default_metrics = profile_results["general"]
    assert {"info_search_web", "spill_artifact_read"} <= {
        _schema_name(schema) for schema in _bind_schemas(default_flow.executor)
    }
    assert legacy_metrics["available_plugin_tools"] == len(snapshot.tools)
    assert legacy_metrics["loaded_plugin_tools"] == len(snapshot.tools)
    assert default_metrics["loaded_plugin_tools"] < legacy_metrics["loaded_plugin_tools"]
    assert default_metrics["model_schema_count"] < legacy_metrics["model_schema_count"]
    assert default_metrics["estimated_tool_tokens"] < legacy_metrics["estimated_tool_tokens"] / 3
    assert all(
        item["estimated_tool_tokens"] < legacy_metrics["estimated_tool_tokens"]
        for item in profile_results.values()
    )

    # Dataset fast-path filtering occurs before the same ModelDriver boundary.
    normal_schemas = _bind_schemas(default_flow.executor)
    default_flow.executor._dataset_fast_path_mode = True
    try:
        fast_schemas = _bind_schemas(default_flow.executor)
    finally:
        default_flow.executor._dataset_fast_path_mode = False
    normal_names = {_schema_name(schema) for schema in normal_schemas}
    fast_names = {_schema_name(schema) for schema in fast_schemas}
    assert fast_names < normal_names
    assert {"tool_catalog_search", "tool_catalog_load"} <= fast_names
    assert fast_names.issubset(default_flow.executor._dataset_fast_path_tool_names())
    assert estimate_tool_tokens(fast_schemas) < estimate_tool_tokens(normal_schemas)

    # Repeated model bindings are local and deterministic: they must not issue
    # another JSON-RPC request to the pinned Cordis host.
    runtime = default_flow.plugin_toolkit.plugin_runtime
    request_id_before = runtime._next_request_id
    default_binding = _binding_microbenchmark(default_flow.executor)
    legacy_binding = _binding_microbenchmark(legacy_flow.executor)
    assert runtime._next_request_id == request_id_before
    assert default_binding["serialized_bytes"] < legacy_binding["serialized_bytes"]
    assert default_binding["estimated_tool_tokens"] < legacy_binding["estimated_tool_tokens"]

    _write_report({
        "schema_version": 1,
        "scope": "offline_process_only",
        "catalog": {
            "engine": snapshot.engine,
            "plugin_count": len(snapshot.plugins),
            "tool_count": len(snapshot.tools),
            "catalog_revision": snapshot.revision,
        },
        "estimator": {
            "version": TOKEN_ESTIMATOR_VERSION,
            "counts_are_estimates": TOKEN_COUNTS_ARE_ESTIMATES,
            "capacity_tokens": 131_072,
            "reserved_output_tokens": 4_096,
            "safety_tokens": 2_048,
        },
        "profiles": profile_results,
        "legacy_general_all": legacy_metrics,
        "default_vs_legacy_reduction_percent": {
            "model_schema_count": round(
                100 * (
                    1
                    - default_metrics["model_schema_count"]
                    / legacy_metrics["model_schema_count"]
                ),
                3,
            ),
            "serialized_schema_bytes": round(
                100 * (
                    1
                    - default_metrics["serialized_schema_bytes"]
                    / legacy_metrics["serialized_schema_bytes"]
                ),
                3,
            ),
            "estimated_tool_tokens": round(
                100 * (
                    1
                    - default_metrics["estimated_tool_tokens"]
                    / legacy_metrics["estimated_tool_tokens"]
                ),
                3,
            ),
        },
        "fast_path": {
            "normal_schema_count": len(normal_schemas),
            "fast_schema_count": len(fast_schemas),
            "normal_estimated_tool_tokens": estimate_tool_tokens(normal_schemas),
            "fast_estimated_tool_tokens": estimate_tool_tokens(fast_schemas),
        },
        "repeated_binding": {
            "default_general_on_demand": default_binding,
            "legacy_general_all": legacy_binding,
            "cordis_rpc_requests_during_samples": runtime._next_request_id - request_id_before,
        },
        "limitations": [
            "Token figures are conservative local UTF-8 estimates, not provider billing usage.",
            "Timing covers in-process get_tools, schema normalization and serialization only.",
            "No model request, database write or sandbox execution occurs in this benchmark.",
        ],
    })


class _TraceSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_production_pipeline_reconfiguration_and_approval_job_spill_stack(
    flow_factory,
    monkeypatch,
):
    """One wrapper call retains every Phase 3-5 governance layer exactly once."""

    settings = plan_act.get_settings().model_copy(update={
        "analysis_jobs_enabled": True,
        "spill_enabled": True,
    })
    monkeypatch.setattr(plan_act, "get_settings", lambda: settings)

    flow = flow_factory()
    jobs = InMemoryAnalysisJobRepository()
    approvals, credentials, approval_repository = services()
    spill_store = CapturingStore()
    trace_sink = _TraceSink()
    flow._analysis_job_service = AnalysisJobService(jobs)
    flow._tool_approval_service = approvals
    flow._credential_service = credentials
    flow._spill_artifact_store = spill_store
    flow._analysis_job_identity_provider = lambda: {
        "task_id": "runtime-regression-task",
        "execution_snapshot_id": "runtime-regression-snapshot",
        "catalog_revision": flow.plugin_toolkit.catalog_revision,
    }

    async def approve(view, _context) -> None:
        if view.status == ToolApprovalStatus.PENDING:
            await approvals.decide(
                "offline-user",
                "offline-session",
                view.approval_id,
                "approved",
                view.revision,
            )

    flow._tool_approval_event_sink = approve
    expected_order = [
        StructuredToolTraceInterceptor,
        ToolCallAuthorizationInterceptor,
        AnalysisJobInterceptor,
        ToolTimeoutInterceptor,
        ToolConcurrencyInterceptor,
        ToolCallAdmissionInterceptor,
        SpillArtifactInterceptor,
    ]
    flow.configure_tool_execution(trace_sink=trace_sink)
    assert [
        type(item) for item in flow.plugin_toolkit.tool_execution_pipeline.interceptors
    ] == expected_order
    flow.configure_tool_execution(trace_sink=trace_sink)
    assert [
        type(item) for item in flow.plugin_toolkit.tool_execution_pipeline.interceptors
    ] == expected_order

    # The catalog's data_format_inspect contract is read-only.  Elevate only
    # this ephemeral wrapper's classification so the test also traverses the
    # call-specific approval path; the immutable catalog is not mutated.
    tool = flow.executor.get_tool("data_format_inspect")
    assert tool is not None
    tool._resolved.execution_contract = {
        **tool._resolved.execution_contract,
        "effects": ["network"],
    }
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(
        success=True,
        data={"output": "x" * (settings.spill_max_inline_bytes + 2_000)},
    ))

    result = await tool.ainvoke({
        "id": "stacked-call",
        "name": "data_format_inspect",
        "args": {"input_paths": []},
    })
    notice = spill_notice_from_result(result)
    assert notice is not None and notice.status == "stored"
    assert notice.omitted_bytes > 0
    assert len(result.content.encode("utf-8")) <= settings.spill_max_inline_bytes
    assert len(spill_store.requests) == 1
    flow.plugin_toolkit.call_tool.assert_awaited_once()

    approval_records = list(approval_repository.records.values())
    assert len(approval_records) == 1
    assert approval_records[0].status == ToolApprovalStatus.CONSUMED
    assert approval_records[0].effects == ["network"]
    job_records = list(jobs.records.values())
    assert len(job_records) == 1
    assert job_records[0].status == AnalysisJobStatus.SUCCEEDED
    assert job_records[0].result_spill is not None
    assert job_records[0].result_spill.locator == notice.reference.locator
    assert [event.phase.value for event in trace_sink.events] == ["started", "succeeded"]
