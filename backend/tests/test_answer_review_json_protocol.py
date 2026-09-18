"""Actual review transport must request the provider's JSON mode on every pass.

The driver and ExecutionAgent are real; only the credential-free provider is an
offline double. User data never supplies the protocol's required JSON keyword.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import BadRequestError
from pydantic import PrivateAttr
import pytest

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.services import model_runtime as runtime
from app.domain.services.agents import execution as execution_module
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_answer_review import AnswerEvidence
from app.infrastructure.external.llm.chat_model import LangChainModelDriver
from test_analysis_answer_review import file, paragraph, response, tool
from test_answer_review_transport_timeout import TraceStore


GOOD = "Observed mean is 3 mg."
PRIVATE_BODY = "PRIVATE_PROVIDER_BODY /Users/private/credentials secret-provider-key"


class JsonModeProvider(BaseChatModel):
    _requests: list = PrivateAttr(default_factory=list)
    _responses: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-json-mode-provider"

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._requests.append({"messages": messages, **kwargs})
        # Enforce the provider's rule against the actual outbound request,
        # not a constant inspected separately from ExecutionAgent.
        assert kwargs["response_format"] == {"type": "json_object"}
        assert any(message.type == "system" and "json" in message.content.lower() for message in messages)
        assert not any(key in kwargs for key in ("tools", "functions", "tool_choice", "function_call"))
        value = self._responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return ChatResult(generations=[ChatGeneration(message=value)])

    def _generate(self, *args, **kwargs):
        raise AssertionError("No synchronous or external model calls")


class RecordingDriver(LangChainModelDriver):
    _bindings: list = PrivateAttr(default_factory=list)

    def bind(self, **kwargs):
        self._bindings.append(dict(kwargs))
        return super().bind(**kwargs)


@pytest.fixture
def setup(monkeypatch):
    settings = Settings(_env_file=None, api_key="unit-test-protocol-identity", llm_retry_attempts=2,
                        llm_retry_base_seconds=0, llm_retry_max_seconds=0)
    monkeypatch.setattr(execution_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    provider = JsonModeProvider(cache=False)
    driver = RecordingDriver(client=provider,
        identity=ModelIdentity(provider="deepseek", model_name="offline-json-protocol"),
        capabilities=ModelCapabilities(tool_calling="adapter", json_object="adapter"), max_output_tokens=4096)
    agent = object.__new__(ExecutionAgent)
    agent._model = driver
    agent.execute = AsyncMock(side_effect=AssertionError("Review cannot execute analysis"))
    agent._parse_json = AsyncMock(side_effect=AssertionError("No hidden JSON model repair"))
    return agent, driver, provider


def conversation(kind):
    evidence = AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe_context({"name": "Archive"})
    evidence.observe(tool())
    evidence.observe(tool(call="optional", success=False, data={"message": "Optional read failed"}))
    initial = response(paragraph(GOOD), paragraph("Rejected optional paragraph", kind=kind,
                       source="catalog_0001", quote="Archive"))
    repaired_text = "The observed mean is 3 mg." if kind == "analysis" else "The optional read failed."
    repair = AIMessage(content=json.dumps({"answer_complete": True,
        "paragraph_corrections": [{"index": 1, "paragraph": {"text": repaired_text, "kind": kind,
            "evidence": ["tool_0001_result:excerpt_0001" if kind == "analysis" else "tool_0002_result:excerpt_0001"]}}],
        "requirement_corrections": []}))
    return evidence, initial, repair, repaired_text


def assert_protocol(requests, bindings):
    assert bindings == [{"response_format": {"type": "json_object"}}] * len(requests)
    for request in requests:
        messages = request["messages"]
        assert len(messages) == 2 and [message.type for message in messages] == ["system", "human"]
        system, human = messages
        assert "json" in system.content.lower()
        assert "json" not in human.content.lower(), "Protocol must not accidentally depend on user/evidence text"
        assert all(not getattr(message, "tool_calls", None) for message in messages)
        assert request["response_format"] == {"type": "json_object"}
        assert not any(key in request for key in ("tools", "functions", "tool_choice", "function_call"))
        is_initial = "failed_paragraphs" not in json.loads(human.content)
        fields = ("unsupported_claims", "paragraphs", "requirement_checks") if is_initial else (
            "answer_complete", "paragraph_corrections", "requirement_corrections")
        assert all('"' + field + '"' in system.content for field in fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["analysis", "limitation"])
@pytest.mark.parametrize("filename", [None, "chart.png", "measurements.csv"])
async def test_initial_and_scoped_repair_both_have_explicit_json_mode_and_schema(setup, kind, filename):
    agent, driver, provider = setup
    evidence, initial, repair, repaired_text = conversation(kind)
    provider._responses = [initial, repair]
    result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
        files=[] if filename is None else [file("/home/ubuntu/output/" + filename)],
        evidence=evidence, requirements=[], language="en")
    assert result.status == "corrected" and result.text.startswith(GOOD) and repaired_text in result.text
    assert len(provider._requests) == 2 and not provider._responses
    assert_protocol(provider._requests, driver._bindings)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


def transcript(request):
    """Compare the actual frozen wire messages, excluding generated request IDs."""
    return [(message.type, message.content) for message in request["messages"]]


def assert_private_result(result, store):
    public = result.text + json.dumps(result.metadata) + "".join(record.model_dump_json() for record in store.records.values())
    assert all(secret not in public for secret in ("PRIVATE_PROVIDER_BODY", "/Users/private/", "secret-provider-key", "provider.invalid"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
@pytest.mark.parametrize("bad_content", ["", " \n\t ", '{"private":"' + PRIVATE_BODY])
async def test_empty_or_truncated_json_retries_the_same_frozen_request_then_succeeds(setup, failure_phase, bad_content):
    agent, driver, provider = setup
    evidence, initial, repair, repaired_text = conversation("analysis")
    bad = AIMessage(content=bad_content)
    provider._responses = [bad, initial, repair] if failure_phase == "initial" else [initial, bad, repair]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="syntax-retry", store=store):
        result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
            files=[], evidence=evidence, requirements=[], language="en")
    assert result.status == "corrected" and GOOD in result.text and repaired_text in result.text
    assert len(provider._requests) == 3 and not provider._responses
    retry_start = 0 if failure_phase == "initial" else 1
    assert transcript(provider._requests[retry_start]) == transcript(provider._requests[retry_start + 1])
    assert_protocol(provider._requests, driver._bindings)
    assert_private_result(result, store)
    assert all(record.role == "answer_review" for record in store.records.values())
    assert all(PRIVATE_BODY not in message.content for request in provider._requests for message in request["messages"])
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
@pytest.mark.parametrize("bad_content", ["", '{"private":"' + PRIVATE_BODY])
async def test_repeated_malformed_json_exhausts_only_the_bounded_review_retry_without_false_success(setup, failure_phase, bad_content):
    agent, driver, provider = setup
    evidence, initial, repair, _ = conversation("analysis")
    bad = AIMessage(content=bad_content)
    # The final valid response is deliberately unreachable after the attempt cap.
    provider._responses = [bad, bad, initial] if failure_phase == "initial" else [initial, bad, bad, repair]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="syntax-exhausted", store=store):
        result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
            files=[], evidence=evidence, requirements=[], language="en")
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert len(provider._requests) == (2 if failure_phase == "initial" else 3)
    assert len(provider._responses) == 1
    assert transcript(provider._requests[-2]) == transcript(provider._requests[-1])
    diagnostics = result.metadata["citation_diagnostics"]
    if failure_phase == "initial":
        assert diagnostics["review"] == "invalid_json"
    else:
        assert diagnostics["correction"] == {"invalid_json": 1}
        assert GOOD in result.text
    assert_protocol(provider._requests, driver._bindings)
    assert_private_result(result, store)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
@pytest.mark.parametrize("malformed_before_cancel", [False, True])
async def test_cancellation_during_review_or_syntax_retry_propagates_without_another_attempt(setup, failure_phase, malformed_before_cancel):
    agent, driver, provider = setup
    evidence, initial, repair, _ = conversation("analysis")
    queue = [AIMessage(content="")] if malformed_before_cancel else []
    provider._responses = ([] if failure_phase == "initial" else [initial]) + queue + [asyncio.CancelledError(), repair]
    expected_calls = 1 + int(failure_phase == "repair") + int(malformed_before_cancel)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="syntax-cancel", store=store):
        with pytest.raises(asyncio.CancelledError):
            await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
                files=[], evidence=evidence, requirements=[], language="en")
    assert len(provider._requests) == expected_calls and len(provider._responses) == 1
    assert list(store.records.values())[-1].status == "cancelled"
    assert list(store.records.values())[-1].scheduled_retry is None
    assert_protocol(provider._requests, driver._bindings)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
@pytest.mark.parametrize("failure_kind", ["array", "scalar", "duplicate_key", "nonfinite", "tool_call"])
async def test_valid_but_wrong_schema_and_forbidden_tools_never_trigger_syntax_retry(setup, failure_phase, failure_kind):
    agent, driver, provider = setup
    evidence, initial, repair, _ = conversation("analysis")
    content = {
        "object": json.dumps({"unexpected": PRIVATE_BODY}), "array": "[]", "scalar": "true",
        "duplicate_key": '{"duplicate":0,"duplicate":1}', "nonfinite": '{"number":NaN}',
        "tool_call": "{}",
    }[failure_kind]
    invalid = AIMessage(content=content,
        tool_calls=[{"id": "forbidden", "name": "shell_run", "args": {"command": PRIVATE_BODY}}] if failure_kind == "tool_call" else [])
    provider._responses = [invalid, initial] if failure_phase == "initial" else [initial, invalid, repair]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="invalid-schema", store=store):
        result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
            files=[], evidence=evidence, requirements=[], language="en")
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert len(provider._requests) == (1 if failure_phase == "initial" else 2) and len(provider._responses) == 1
    if failure_phase == "repair":
        assert GOOD in result.text
    assert_protocol(provider._requests, driver._bindings)
    assert_private_result(result, store)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
async def test_structural_object_recovery_is_initial_only_and_never_reopens_accepted_paragraphs(setup, failure_phase):
    agent, driver, provider = setup
    evidence, initial, repair, _ = conversation("analysis")
    invalid = AIMessage(content=json.dumps({"unexpected": PRIVATE_BODY}))
    covered = json.loads(initial.content)
    covered["answer_complete"] = True
    provider._responses = ([invalid, AIMessage(content=json.dumps(covered)), repair] if failure_phase == "initial"
                           else [initial, invalid, repair])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="object-shape", store=store):
        result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
            files=[], evidence=evidence, requirements=[], language="en")
    assert GOOD in result.text and result.missing_requirement_indices == ()
    if failure_phase == "initial":
        assert result.status == "corrected" and len(provider._requests) == 3
        assert result.metadata["review_schema_repair_status"] == "corrected"
        assert transcript(provider._requests[0])[1] == transcript(provider._requests[1])[1]
        assert not provider._responses
    else:
        assert result.status == "unavailable" and len(provider._requests) == 2
        assert "review_schema_repair_attempted" not in result.metadata
        assert len(provider._responses) == 1
    assert_protocol(provider._requests, driver._bindings)
    assert_private_result(result, store)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["initial", "repair"])
async def test_http_400_diagnostic_is_classified_without_publishing_provider_body_or_running_tools(setup, failure_phase):
    agent, driver, provider = setup
    evidence, initial, _, _ = conversation("analysis")
    request = httpx.Request("POST", "https://provider.invalid/review")
    error = BadRequestError(PRIVATE_BODY, response=httpx.Response(400, request=request),
                            body={"error": {"message": PRIVATE_BODY, "code": "invalid_request_error"}})
    provider._responses = [error] if failure_phase == "initial" else [initial, error]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="fixture", session_id="fixture-session", task_id="review", store=store):
        result = await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
            files=[], evidence=evidence, requirements=[], language="en")
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    diagnostics = result.metadata["citation_diagnostics"]
    if failure_phase == "initial":
        assert diagnostics["review"] == "provider_http_400"
    else:
        assert diagnostics["correction"] == {"provider_http_400": 1}
        assert GOOD in result.text
    assert len(provider._requests) == (1 if failure_phase == "initial" else 2), "Bad requests must not be retried"
    assert_protocol(provider._requests, driver._bindings)
    public = result.text + json.dumps(result.metadata) + "".join(record.model_dump_json() for record in store.records.values())
    assert all(secret not in public for secret in ("PRIVATE_PROVIDER_BODY", "/Users/private/", "secret-provider-key", "provider.invalid"))
    assert all(record.role == "answer_review" and record.scheduled_retry is None for record in store.records.values())
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()
