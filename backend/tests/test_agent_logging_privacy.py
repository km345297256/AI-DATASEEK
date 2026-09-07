import json
import logging
from pathlib import Path

import pytest

from app.domain.models.event import ErrorEvent, MessageEvent, PlanEvent, StepEvent
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services import agent_domain_service as domain_service_module
from app.domain.services import agent_task_runner as task_runner_module
from app.domain.services import lightweight_task_runner as lightweight_module
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents import planner as planner_module
from app.domain.services.agents import vision as vision_module
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.vision import VisionAgent
from app.domain.services.lightweight_task_runner import LightweightTaskRunner


def _module_logs(caplog, module) -> str:
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == module.__name__
    )


def _assert_no_traceback(caplog, module) -> None:
    assert all(
        record.exc_info is None
        for record in caplog.records
        if record.name == module.__name__
    )


@pytest.mark.asyncio
async def test_planner_logs_model_output_length_without_model_text(caplog):
    raw_model_output = json.dumps({
        "goal": "model-output-secret /Users/alice/private.csv",
        "language": "en",
        "steps": [],
    })
    planner = object.__new__(PlannerAgent)

    async def reset_context():
        return None

    async def execute(_prompt):
        yield MessageEvent(role="assistant", message=raw_model_output)

    async def parse_json(value):
        return json.loads(value)

    planner.reset_context = reset_context
    planner.execute = execute
    planner._parse_json = parse_json

    events = []
    with caplog.at_level(logging.DEBUG, logger=planner_module.__name__):
        async for event in planner.create_plan(Message(message="private user prompt")):
            events.append(event)

    assert any(isinstance(event, PlanEvent) for event in events)
    logs = _module_logs(caplog, planner_module)
    assert f"chars={len(raw_model_output)}" in logs
    assert "model-output-secret" not in logs
    assert "/Users/alice" not in logs
    _assert_no_traceback(caplog, planner_module)


@pytest.mark.asyncio
async def test_vision_failure_log_uses_type_while_step_event_behavior_is_preserved(caplog):
    raw_error = "vision-provider-secret /Users/alice/image.png"
    agent = object.__new__(VisionAgent)

    async def fail_build(_message, _sandbox):
        raise RuntimeError(raw_error)

    agent._build_image_blocks = fail_build
    step = Step(description="inspect image", agent="vision")
    events = []

    with caplog.at_level(logging.ERROR, logger=vision_module.__name__):
        async for event in agent.analyze_step(
            Plan(goal="inspect"),
            step,
            Message(message="private prompt"),
            object(),
        ):
            events.append(event)

    assert isinstance(events[-1], StepEvent)
    assert "vision-provider-secret" in events[-1].step.error
    assert "/Users/alice" not in events[-1].step.error
    assert "[redacted path]" in events[-1].step.error
    logs = _module_logs(caplog, vision_module)
    assert "error_type=RuntimeError" in logs
    assert "vision-provider-secret" not in logs
    assert "/Users/alice" not in logs
    _assert_no_traceback(caplog, vision_module)


@pytest.mark.asyncio
async def test_chat_bootstrap_log_hides_session_and_exception_values(caplog):
    captured_events = []

    class Repository:
        async def add_event(self, _session_id, event):
            captured_events.append(event)

        async def update_status(self, _session_id, _status):
            return None

    service = object.__new__(AgentDomainService)
    service._session_repository = Repository()
    raw_session = "session-private-value"
    raw_error = "password=bootstrap-secret /Users/alice/data.nc"

    with caplog.at_level(logging.ERROR, logger=domain_service_module.__name__):
        await service._handle_chat_bootstrap_error(
            raw_session,
            RuntimeError(raw_error),
        )

    assert len(captured_events) == 1
    assert isinstance(captured_events[0], ErrorEvent)
    logs = _module_logs(caplog, domain_service_module)
    assert "session=session:sha256:" in logs
    assert "error_type=RuntimeError" in logs
    assert raw_session not in logs
    assert "bootstrap-secret" not in logs
    assert "/Users/alice" not in logs
    _assert_no_traceback(caplog, domain_service_module)


@pytest.mark.asyncio
async def test_lightweight_failure_log_is_private_and_still_publishes_error(caplog):
    published = []

    class Task:
        async def pop_input_or_close(self):
            raise RuntimeError("token=lightweight-secret /Users/alice/input.csv")

    class Repository:
        async def update_status(self, _session_id, _status):
            return None

    runner = object.__new__(LightweightTaskRunner)
    runner._session_id = "lightweight-private-session"
    runner._session_repository = Repository()

    async def publish(_task, event):
        published.append(event)

    runner._publish = publish

    with caplog.at_level(logging.ERROR, logger=lightweight_module.__name__):
        await runner.run(Task())

    assert len(published) == 1
    assert isinstance(published[0], ErrorEvent)
    logs = _module_logs(caplog, lightweight_module)
    assert "session=session:sha256:" in logs
    assert "error_type=RuntimeError" in logs
    assert "lightweight-private-session" not in logs
    assert "lightweight-secret" not in logs
    assert "/Users/alice" not in logs
    _assert_no_traceback(caplog, lightweight_module)


@pytest.mark.asyncio
async def test_artifact_failure_log_hashes_agent_and_path(caplog):
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent-private-value"

    async def fail_read(_file_path):
        raise RuntimeError("artifact-secret /Users/alice/output.csv")

    runner._read_artifact_with_fingerprint = fail_read
    raw_path = "/Users/alice/private/output.csv"

    with caplog.at_level(logging.ERROR, logger=task_runner_module.__name__):
        result = await runner._sync_file_to_storage(raw_path)

    assert result is None
    logs = _module_logs(caplog, task_runner_module)
    assert "agent=agent:sha256:" in logs
    assert "file=file:sha256:" in logs
    assert "error_type=RuntimeError" in logs
    assert "agent-private-value" not in logs
    assert raw_path not in logs
    assert "artifact-secret" not in logs
    _assert_no_traceback(caplog, task_runner_module)


def test_agent_execution_modules_do_not_emit_exception_tracebacks():
    modules = (
        domain_service_module,
        task_runner_module,
        lightweight_module,
        planner_module,
        vision_module,
    )

    for module in modules:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "logger.exception(" not in source, module.__name__
        assert "message[:50]" not in source, module.__name__
        assert "logger.info(event.message)" not in source, module.__name__
