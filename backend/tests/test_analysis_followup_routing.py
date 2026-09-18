"""Historical analysis prose must not bypass the publication evidence gate."""
import json
from types import SimpleNamespace

import pytest

from app.application.services import dataset_request_resolver as resolver_module
from app.domain.models.event import MessageEvent
from test_dataset_request_resolver import _dataset, _FakeModel, _resolver


def prior_analysis(status="partial"):
    return MessageEvent(role="assistant", message="The unreviewed mean was 9000.", metadata={
        "analysis_outcome": {"status": status, "reason_code": "answer_validation_unavailable",
                             "missing": [], "issues": [], "can_resume": False},
    })


async def resolve(monkeypatch, *, history, evidence="conversation", safety="allow",
                  question="只在回答中给出刚才统计的 JSON，不要生成任何文件。", answer='{"mean":9000}'):
    model = _FakeModel([json.dumps({
        "safety": {"decision": safety, "risk_level": "low"},
        "execution": {"mode": "direct", "required_evidence": evidence,
                      "requires_artifacts": False, "deliverables": []},
        "answer": answer,
    })])
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: SimpleNamespace(
        dataset_request_resolver_timeout_seconds=1))
    result = await _resolver().resolve(question=question,
                                      datasets=[_dataset()], events=history)
    return result, model


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "partial", "succeeded"])
async def test_analysis_followup_uses_existing_reviewed_execution_path(monkeypatch, status):
    result, model = await resolve(monkeypatch, history=[prior_analysis(status)])
    assert result.mode == "sandbox"
    assert result.answer == ""
    assert result.decision.answer == ""
    assert result.decision.execution.required_evidence == "file_content"
    assert result.decision.execution.requires_artifacts is False
    assert result.decision.execution.deliverables == []
    assert result.artifacts == []
    assert result.controller_metadata["source"] == "analysis_followup"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_ordinary_user_message_stays_direct_in_analysis_session(monkeypatch):
    result, _ = await resolve(monkeypatch, history=[prior_analysis()], evidence="user_message",
                              question="你好", answer="你好")
    assert result.mode == "direct"
    assert result.answer == "你好"


@pytest.mark.asyncio
async def test_ordinary_conversation_without_analysis_stays_direct(monkeypatch):
    result, _ = await resolve(monkeypatch, history=[MessageEvent(role="assistant", message="Hello.")],
                              question="把你刚才的问候翻译成中文。", answer="你好")
    assert result.mode == "direct"


@pytest.mark.asyncio
async def test_user_supplied_analysis_marker_cannot_classify_history(monkeypatch):
    event = prior_analysis().model_copy(update={"role": "user"})
    result, _ = await resolve(monkeypatch, history=[event])
    assert result.mode == "direct"


@pytest.mark.asyncio
async def test_analysis_followup_never_overrides_safety_rejection(monkeypatch):
    result, _ = await resolve(monkeypatch, history=[prior_analysis()], safety="reject")
    assert result.mode == "reject"
    assert result.answer == ""


@pytest.mark.asyncio
async def test_controller_context_preserves_bounded_analysis_status_not_private_metadata(monkeypatch):
    event = prior_analysis()
    event.metadata["private_debug"] = {"source_path": "/private/secret.csv"}
    result, model = await resolve(monkeypatch, history=[event])
    context = json.loads(model.requests[0][-1].content)
    prior = context["recent_conversation"][0]
    assert prior["analysis_outcome"] == {"status": "partial", "reason_code": "answer_validation_unavailable"}
    assert "private_debug" not in prior
    assert "/private/secret.csv" not in json.dumps(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [{"status": ["partial"]}, {"status": "unknown"}, "partial", None])
async def test_malformed_historical_metadata_does_not_break_routing(monkeypatch, outcome):
    event = MessageEvent(role="assistant", message="Hello.", metadata={"analysis_outcome": outcome})
    result, model = await resolve(monkeypatch, history=[event])
    assert result.mode == "direct"
    prior = json.loads(model.requests[0][-1].content)["recent_conversation"][0]
    assert "analysis_outcome" not in prior


def test_controller_contract_retains_conditional_named_outputs():
    prompt = resolver_module.DECISION_PROMPT
    assert "conditional" in prompt
    assert "unavailable" in prompt
    assert "summary.csv" in prompt
    assert "missing_summary.csv" in prompt
    assert "objective" in prompt and "output_paths" in prompt
