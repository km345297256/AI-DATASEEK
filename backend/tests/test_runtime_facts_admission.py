"""Volatile facts refresh at every physical request without altering evidence."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langchain_classic.output_parsers.retry import RetryWithErrorOutputParser
from langchain_core.output_parsers import JsonOutputParser

from app.domain.models.memory import Memory
from app.domain.services.agents.base import BaseAgent
from app.domain.utils.robust_json_parser import RobustJsonParser
from test_model_driver import driver, FakeClient
from test_model_retry_policy import failure


def ticking_provider():
    values = []
    def current():
        values.append(len(values) + 1)
        return f"runtime-facts={values[-1]}"
    return current, values


@pytest.mark.asyncio
async def test_agent_retry_refreshes_only_runtime_facts_not_accepted_context():
    model, client, middleware = driver()
    facts, evaluations = ticking_provider()
    model.set_runtime_facts_provider(facts)
    client._responses = [failure("0"), AIMessage(content="done")]
    agent = object.__new__(BaseAgent)
    agent.name = "fixture"
    agent.max_retries = agent._llm_retry_attempts = 3
    agent.retry_interval = agent._llm_retry_base_seconds = 0
    agent._llm_retry_max_seconds = 8
    agent.bind_tools = False
    agent.tool_choice = None
    agent._model = model
    agent._add_to_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    agent.memory = Memory(messages=[SystemMessage(content="stable system"), HumanMessage(content="user")])
    before = deepcopy(agent.memory.messages)
    static_calls = []
    def frozen():
        static_calls.append(1)
        return "frozen dataset/skill/tool selection"
    agent.dynamic_system_prompt_provider = frozen
    agent.dynamic_user_context_provider = lambda: '{"historical":"data"}'
    await agent.ask_with_messages([], allow_tools=False)
    assert evaluations == [1, 2] and static_calls == [1]
    assert agent.memory.messages == before
    for index, request in enumerate(middleware.calls, 1):
        systems = [message.content for message in request["messages"] if isinstance(message, SystemMessage)]
        assert systems == ["stable system", "frozen dataset/skill/tool selection", f"runtime-facts={index}"]
    assert all("runtime-facts" not in str(call.args) for call in agent._add_to_memory.await_args_list)


@pytest.mark.asyncio
async def test_both_parser_repair_paths_refresh_at_driver_boundary():
    model, client, middleware = driver()
    facts, evaluations = ticking_provider()
    model.set_runtime_facts_provider(facts)
    client._responses = [AIMessage(content='{"first":true}'), AIMessage(content='{"second":true}')]
    assert await RobustJsonParser.from_llm(model)._stage3_output_fixing("not JSON") == {"first": True}
    agent = object.__new__(BaseAgent)
    agent._json_output_parser = RetryWithErrorOutputParser.from_llm(parser=JsonOutputParser(), llm=model, max_retries=1)
    assert await agent._parse_json("not JSON") == {"second": True}
    assert evaluations == [1, 2]
    for index, call in enumerate(middleware.calls, 1):
        assert call["messages"][0].content == f"runtime-facts={index}"
        assert len([item for item in call["messages"] if isinstance(item, SystemMessage)]) == 1


@pytest.mark.asyncio
async def test_strict_binding_clone_keeps_facts_and_sizing_sees_them():
    class CloneClient(FakeClient):
        def bind_tools(self, tools, **kwargs):
            return self.model_copy().bind(tools=list(tools), **kwargs)
    model, _, middleware = driver()
    model._client = CloneClient(cache=False)
    facts, evaluations = ticking_provider()
    model.set_runtime_facts_provider(facts)
    original = [SystemMessage(content="one"), SystemMessage(content="two"), HumanMessage(content="question")]
    await model.bind_tools([], strict=True).ainvoke(original)
    assert [item.content for item in middleware.calls[0]["messages"][:3]] == ["one", "two", "runtime-facts=1"]
    assert len(original) == 3 and evaluations == [1]


@pytest.mark.asyncio
async def test_facts_cancellation_cannot_dispatch_and_request_scope_does_not_leak():
    first, client, _ = driver()
    second, _, second_middleware = driver()
    def cancelled():
        raise asyncio.CancelledError()
    first.set_runtime_facts_provider(cancelled)
    with pytest.raises(asyncio.CancelledError):
        await first.ainvoke("one")
    assert client._requests == []
    await second.ainvoke("two")
    assert not any(isinstance(item, SystemMessage) for item in second_middleware.calls[0]["messages"])
