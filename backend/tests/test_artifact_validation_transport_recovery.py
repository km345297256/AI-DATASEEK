"""Transient verification outages must not discard already computed results."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from test_analysis_repair_flow import collect, output, scenario, terminal_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", 408, 429, 500, 502, 503, 504])
async def test_real_flow_recovers_verification_without_reexecuting_analysis(failure, monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr("app.infrastructure.external.sandbox.docker_sandbox.asyncio.sleep", pause)
    table, image = output("summary.csv", "table"), output("chart.png", "image")
    runner, _, step, message, state = scenario([[table, image]], [{"kind": "table"}, {"kind": "image"}])
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("private diagnostic")
            if failure == "network":
                raise httpx.ConnectError("private diagnostic")
            return httpx.Response(failure, headers={"Retry-After": "0"}, text="private diagnostic")
        records = {record["path"]: record for record, _ in [table, image]}
        batch = json.loads(request.content)["items"]
        return httpx.Response(200, json={"success": True, "data": {
            "version": 1, "files": [records[item["path"]] for item in batch]}})

    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        runner._sandbox.validate_artifacts = adapter.validate_artifacts
        events = await collect(runner, message)
    assert len(calls) == 2 and calls[0].content == calls[1].content
    assert {call.url.path for call in calls} == {"/api/v1/file/validate-artifacts"}
    assert len(state["prompts"]) == 1  # Never regenerate either completed file.
    assert step.success and step.outcome.status == "succeeded"
    assert {item.file_id for event in terminal_messages(events) for item in event.attachments or []} == {
        table[1].file_id, image[1].file_id}
    assert "private diagnostic" not in step.result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [400, 401, 403, 404, "malformed", "invalid_content", "retry_after", "boolean_version"])
async def test_nontransient_failure_or_invalid_content_is_not_retried(failure, monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr("app.infrastructure.external.sandbox.docker_sandbox.asyncio.sleep", pause)
    record, _ = output("chart.png", "image", valid=False)
    calls = []

    def respond(request):
        calls.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure)
        if failure == "retry_after":
            return httpx.Response(503, headers={"Retry-After": "600"})
        if failure == "malformed":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"success": True, "data": {
            "version": True if failure == "boolean_version" else 1, "files": [record]}})

    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.validate_artifacts([{"path": record["path"], "kind": "image"}])
    assert len(calls) == 1
    pause.assert_not_awaited()
    if failure == "invalid_content":
        assert result.success and result.data["files"][0]["valid"] is False
    else:
        assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["request", "backoff"])
async def test_cancellation_never_becomes_a_validation_failure_or_more_retries(phase, monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if phase == "request":
            raise asyncio.CancelledError()
        raise httpx.ReadTimeout("private diagnostic")

    monkeypatch.setattr("app.infrastructure.external.sandbox.docker_sandbox.asyncio.sleep",
                        AsyncMock(side_effect=asyncio.CancelledError()))
    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        with pytest.raises(asyncio.CancelledError):
            await adapter.validate_artifacts([{"path": "/home/ubuntu/output/chart.png", "kind": "image"}])
    assert len(calls) == 1
