import io
from types import SimpleNamespace

import pytest

from app.domain.models.event import (
    DoneEvent,
    ErrorEvent,
    MessageEvent,
    StepEvent,
    StepStatus,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.file import FileInfo
from app.domain.models.plan import ExecutionStatus, Step
from app.domain.services.agent_task_runner import (
    ARTIFACT_HASH_METADATA_KEY,
    ARTIFACT_SIZE_METADATA_KEY,
    AgentTaskRunner,
)
from app.domain.services.completion_advice_service import CompletionAdvice
from app.domain.services.flows.plan_act import AgentStatus


class _AllowSafetyReviewer:
    async def review(self, message, excerpts):
        return SimpleNamespace(
            allowed=True,
            decision="allow",
            risk_level="low",
            categories=[],
            reason="",
            suggestion="",
        )


async def _noop(*args, **kwargs):
    return None


def _message(text: str = "生成一张图"):
    return SimpleNamespace(
        message=text,
        attachment_file_infos=[],
        mcp_servers=[],
        mcp_access_all=False,
    )


def test_dataset_unpack_working_root_is_excluded_from_artifact_delivery():
    runner = object.__new__(AgentTaskRunner)
    runner._private_artifact_roots = set()
    event = ToolEvent(
        status=ToolStatus.CALLING,
        tool_call_id="unpack-call",
        tool_name="shell",
        function_name="dataset_unpack",
        function_args={
            "output_dir": "/home/ubuntu/output/dataset-42_unpacked",
        },
    )

    runner._remember_private_tool_output(event)

    assert runner._private_artifact_roots == {
        "/home/ubuntu/output/dataset-42_unpacked"
    }
    assert runner._is_syncable_artifact(
        "/home/ubuntu/output/dataset-42_unpacked/data/source.tif"
    ) is False
    assert runner._is_syncable_artifact(
        "/home/ubuntu/output/dataset-42_unpacked/unpack_manifest.json"
    ) is False
    assert runner._is_syncable_artifact(
        "/home/ubuntu/output/generated-report.csv"
    ) is True


@pytest.mark.asyncio
async def test_completed_step_delivers_artifact_once_before_duplicate_messages():
    artifact_path = "/home/ubuntu/output/chart.png"
    artifact = FileInfo(
        file_id="chart-file",
        filename="chart.png",
        file_path=artifact_path,
    )
    step = Step(
        description="绘制图表",
        status=ExecutionStatus.COMPLETED,
        success=True,
        result="图表已生成。",
        attachments=[artifact_path],
    )

    class _Flow:
        status = AgentStatus.EXECUTING

        async def run(self, message):
            yield ToolEvent(
                tool_call_id="tool-1",
                tool_name="message",
                function_name="message_notify",
                function_args={},
                status=ToolStatus.CALLED,
            )
            yield StepEvent(status=StepStatus.COMPLETED, step=step)
            # ExecutionAgent normally repeats step.result here.
            yield MessageEvent(message="图表已生成。")
            self.status = AgentStatus.SUMMARIZING
            # A one-step summary is another rendition of the same answer.
            yield MessageEvent(message="图表已经生成，请查看附件。")
            yield DoneEvent()

    class _Advice:
        def analyze_fast(self, events):
            return CompletionAdvice([], False, "")

        def to_payload(self, advice):
            return {"recommendations": [], "is_skill_candidate": False, "skill_reason": ""}

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._flow = _Flow()
    runner._safety_reviewer = _AllowSafetyReviewer()
    runner._generated_files = []
    runner._artifact_fingerprints = {artifact_path: (10, "hash")}
    runner._completion_advice_service = _Advice()
    runner._record_safety_audit = _noop
    runner._initialize_mcp_tool = _noop
    runner._handle_tool_event = _noop
    runner._sync_message_attachments_to_storage = _noop

    discovery_skip_paths = []

    async def _sync_step(event):
        runner._remember_generated_file(artifact)
        return [artifact]

    async def _discover(*, skip_paths=None):
        discovery_skip_paths.append(skip_paths)
        return []

    runner._sync_step_attachments_to_storage = _sync_step
    runner._sync_discovered_artifacts_to_storage = _discover

    events = [event async for event in runner._run_flow(_message())]

    assert [type(event) for event in events] == [
        ToolEvent,
        StepEvent,
        MessageEvent,
        DoneEvent,
    ]
    delivery = events[2]
    assert delivery.message == "图表已生成。"
    assert delivery.attachments == [artifact]
    assert delivery.metadata == {"artifact_delivery": True, "step_id": step.id}
    assert discovery_skip_paths == [{artifact_path}]
    assert events[-1].advice is not None


@pytest.mark.asyncio
async def test_done_uses_fast_advice_without_loading_session_history_or_model():
    class _Flow:
        status = AgentStatus.EXECUTING

        async def run(self, message):
            yield DoneEvent()

    class _Advice:
        analyze_called = False

        async def analyze(self, events):
            self.analyze_called = True
            raise AssertionError("model-backed advice must not run on the Done path")

        def analyze_fast(self, events):
            assert any(
                isinstance(event, MessageEvent) and event.role == "user"
                for event in events
            )
            return CompletionAdvice(["继续分析", "导出结果", "解释结论"], False, "")

        def to_payload(self, advice):
            return {
                "recommendations": advice.recommendations,
                "is_skill_candidate": advice.is_skill_candidate,
                "skill_reason": advice.skill_reason,
            }

    advice = _Advice()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._flow = _Flow()
    runner._safety_reviewer = _AllowSafetyReviewer()
    runner._generated_files = []
    runner._completion_advice_service = advice
    runner._record_safety_audit = _noop
    runner._initialize_mcp_tool = _noop

    discovery_calls = 0

    async def _discover(*, skip_paths=None):
        nonlocal discovery_calls
        discovery_calls += 1
        return []

    runner._sync_discovered_artifacts_to_storage = _discover

    events = [event async for event in runner._run_flow(_message("分析数据"))]

    assert len(events) == 1
    assert events[0].advice["recommendations"][0] == "继续分析"
    assert advice.analyze_called is False
    assert discovery_calls == 1


@pytest.mark.asyncio
async def test_late_discovered_artifact_is_emitted_before_done():
    artifact = FileInfo(
        file_id="late-file",
        filename="late.csv",
        file_path="/home/ubuntu/output/late.csv",
    )

    class _Flow:
        status = AgentStatus.EXECUTING

        async def run(self, message):
            yield DoneEvent()

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._flow = _Flow()
    runner._safety_reviewer = _AllowSafetyReviewer()
    runner._generated_files = [artifact]
    runner._record_safety_audit = _noop
    runner._initialize_mcp_tool = _noop

    async def _discover(*, skip_paths=None):
        return [artifact]

    runner._sync_discovered_artifacts_to_storage = _discover

    events = [event async for event in runner._run_flow(_message())]

    assert [type(event) for event in events] == [MessageEvent, DoneEvent]
    assert events[0].attachments == [artifact]
    assert events[0].metadata == {"artifact_delivery": True}


@pytest.mark.asyncio
async def test_partial_artifact_is_emitted_before_terminal_error():
    artifact = FileInfo(
        file_id="partial-file",
        filename="partial.csv",
        file_path="/home/ubuntu/output/partial.csv",
    )
    failed_step = Step(
        description="生成可视化",
        status=ExecutionStatus.FAILED,
        success=False,
        error="iteration budget reached",
    )

    class _Flow:
        status = AgentStatus.EXECUTING

        async def run(self, message):
            yield ToolEvent(
                tool_call_id="tool-1",
                tool_name="message",
                function_name="message_notify",
                function_args={},
                status=ToolStatus.CALLED,
            )
            yield StepEvent(status=StepStatus.FAILED, step=failed_step)
            yield ErrorEvent(error="iteration budget reached")
            yield DoneEvent()

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._flow = _Flow()
    runner._safety_reviewer = _AllowSafetyReviewer()
    runner._generated_files = [artifact]
    runner._record_safety_audit = _noop
    runner._initialize_mcp_tool = _noop
    runner._handle_tool_event = _noop

    async def _sync_step(event):
        return []

    runner._sync_step_attachments_to_storage = _sync_step

    discovery_calls = 0

    async def _discover(*, skip_paths=None):
        nonlocal discovery_calls
        discovery_calls += 1
        return [artifact]

    runner._sync_discovered_artifacts_to_storage = _discover

    events = [event async for event in runner._run_flow(_message())]

    assert [type(event) for event in events] == [
        ToolEvent,
        StepEvent,
        MessageEvent,
        ErrorEvent,
        DoneEvent,
    ]
    assert events[2].attachments == [artifact]
    assert events[2].metadata == {
        "artifact_delivery": True,
        "partial": True,
    }
    assert discovery_calls == 1


@pytest.mark.asyncio
async def test_artifact_baseline_uses_persisted_fingerprint_metadata():
    artifact_path = "/home/ubuntu/output/existing.png"
    existing = FileInfo(
        file_id="existing-file",
        filename="existing.png",
        file_path=artifact_path,
        metadata={
            ARTIFACT_SIZE_METADATA_KEY: 123,
            ARTIFACT_HASH_METADATA_KEY: "persisted-sha",
        },
    )

    class _Sandbox:
        async def file_find(self, path, glob_pattern):
            return SimpleNamespace(
                success=True,
                data={"files": [artifact_path]},
            )

        async def file_download(self, path):
            raise AssertionError("baseline must not download artifact bodies")

    class _Repository:
        async def find_by_id(self, session_id):
            return SimpleNamespace(files=[existing])

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._sandbox = _Sandbox()
    runner._session_repository = _Repository()
    runner._protected_dataset_paths = set()
    runner._protected_dataset_roots = set()

    await runner._capture_artifact_baseline()

    assert runner._artifact_baseline_paths == {artifact_path}
    assert runner._artifact_fingerprints == {artifact_path: (123, "persisted-sha")}


@pytest.mark.asyncio
async def test_legacy_artifact_without_fingerprint_is_delivered_after_overwrite():
    artifact_path = "/home/ubuntu/output/legacy.csv"
    legacy = FileInfo(
        file_id="legacy-file",
        filename="legacy.csv",
        file_path=artifact_path,
        metadata=None,
    )

    class _Sandbox:
        body = b"old contents"
        download_count = 0

        async def file_find(self, path, glob_pattern):
            return SimpleNamespace(success=True, data={"files": [artifact_path]})

        async def file_download(self, path):
            self.download_count += 1
            return io.BytesIO(self.body)

    class _Repository:
        async def find_by_id(self, session_id):
            return SimpleNamespace(files=[legacy])

    sandbox = _Sandbox()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-legacy"
    runner._session_id = "session-legacy"
    runner._sandbox = sandbox
    runner._session_repository = _Repository()
    runner._protected_dataset_paths = set()
    runner._protected_dataset_roots = set()
    runner._generated_files = []

    await runner._capture_artifact_baseline()
    old_fingerprint = runner._artifact_fingerprints[artifact_path]
    assert sandbox.download_count == 1

    delivered = FileInfo(
        file_id="updated-file",
        filename="legacy.csv",
        file_path=artifact_path,
    )
    sync_calls = []

    async def _sync(file_path, *, file_data, fingerprint):
        sync_calls.append((file_path, file_data.read(), fingerprint))
        return delivered

    runner._sync_file_to_storage = _sync
    sandbox.body = b"new contents"

    attachments = await runner._sync_discovered_artifacts_to_storage()

    assert attachments == [delivered]
    assert sandbox.download_count == 2
    assert len(sync_calls) == 1
    assert sync_calls[0][0] == artifact_path
    assert sync_calls[0][1] == b"new contents"
    assert sync_calls[0][2] != old_fingerprint


@pytest.mark.asyncio
async def test_summary_explicit_attachment_reuses_file_synced_in_same_turn():
    artifact_path = "/home/ubuntu/output/chart.png"
    synced = FileInfo(
        file_id="chart-file",
        filename="chart.png",
        file_path=artifact_path,
    )

    class _Sandbox:
        async def file_download(self, path):
            raise AssertionError("same-turn explicit attachment must not be downloaded twice")

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._sandbox = _Sandbox()
    runner._generated_files = [synced]
    runner._artifact_fingerprints = {artifact_path: (10, "hash")}

    event = MessageEvent(
        message="最终结果",
        attachments=[FileInfo(file_path=artifact_path)],
    )
    await runner._sync_message_attachments_to_storage(event)

    assert event.attachments == [synced]
