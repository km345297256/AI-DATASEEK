"""Routing schema repair must retain scientific text and safe diagnostics."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application.services import dataset_request_resolver as routing
from app.domain.models.analysis_outcome import DeliverableRequirement
from test_dataset_request_resolver import _FakeModel, _resolver


@pytest.mark.parametrize("objective", ["比较 SASA/Rg", "计算SASA/Rg", "B-factor/occupancy", "均值/标准差", "浓度 mg/L", "浓度mg/L", "比较 a / b", "面积 Å²"])
def test_objective_accepts_scientific_ratios(objective):
    assert DeliverableRequirement(kind="report", objective=objective).objective == objective


@pytest.mark.parametrize("objective,code", [
    ("读取 /Users/private/source.csv", "objective_filesystem_path"),
    ("读取/home/ubuntu/private", "objective_filesystem_path"),
    ("写入 results/file.csv", "objective_filesystem_path"),
    ("读取 ./secret", "objective_filesystem_path"),
    ("读取 ../secret", "objective_filesystem_path"),
    (r"读取 C:\private\secret", "objective_filesystem_path"),
    (r"读取 \\host\secret", "objective_filesystem_path"),
    ("读取 ~/secret", "objective_filesystem_path"),
    ("读取：/secret", "objective_filesystem_path"),
    ("路径【/secret】", "objective_filesystem_path"),
    ("分析/secret", "objective_filesystem_path"),
    ("读取/unlisted-data", "objective_filesystem_path"),
    ("summary\nnew line", "objective_non_plain_text"),
])
def test_objective_still_rejects_paths_and_control_characters(objective, code):
    with pytest.raises(ValidationError) as captured:
        DeliverableRequirement(kind="report", objective=objective)
    assert captured.value.errors()[0]["type"] == code


def payload(objective):
    data = json.loads(json.dumps(routing.FRONT_CONTROLLER_PROMPT_EXAMPLE))
    data["execution"]["deliverables"] = [{"kind": "report", "objective": objective}]
    data["execution"]["requires_artifacts"] = True
    return json.dumps(data)


@pytest.mark.asyncio
async def test_valid_ratio_does_not_spend_a_schema_repair_call(monkeypatch):
    model = _FakeModel([payload("比较 SASA/Rg 与 B-factor/occupancy")])
    monkeypatch.setattr(routing, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(routing, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    result = await _resolver().resolve(question="比较指标", datasets=[], events=[])
    assert result.mode == "sandbox" and model.calls == 1


@pytest.mark.asyncio
async def test_rejected_objective_reports_stable_rule_without_private_value(monkeypatch, caplog):
    private = "/Users/secret-routing-name/source.csv"
    model = _FakeModel([payload(private), payload(private)])
    monkeypatch.setattr(routing, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(routing, "get_settings", lambda: SimpleNamespace(dataset_request_resolver_timeout_seconds=1))
    result = await _resolver().resolve(question="导出分析", datasets=[], events=[])
    assert result.mode == "reject" and model.calls == 2
    assert result.controller_metadata["failure_code"] == "invalid_routing"
    failures = result.controller_metadata["validation_failures"]
    assert [item["stage"] for item in failures] == ["initial", "repair"]
    assert all(item["fields"][0]["type"] == "objective_filesystem_path" for item in failures)
    assert private not in json.dumps(result.controller_metadata) + caplog.text
    assert "objective_filesystem_path" in model.requests[1][-1].content


def test_extra_field_names_cannot_leak_into_schema_diagnostics():
    data = json.loads(payload("统计分析"))
    data["execution"]["deliverables"][0]["/Users/private/secret"] = "never public"
    with pytest.raises(ValidationError) as captured:
        routing.RequestDecision.model_validate(data)
    fields = routing.DatasetRequestResolver._routing_validation_fields(captured.value)
    assert "/Users" not in json.dumps(fields)
    assert any(item["loc"].endswith(".unknown_field") for item in fields)
