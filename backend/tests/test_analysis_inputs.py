import io
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.models.analysis_input import (
    AnalysisInputContext, assign_upload_namespace, build_analysis_inputs,
    render_upload_context, upload_catalog_views, upload_runtime_path,
)
from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_input_selection import (
    input_snapshot, select_input_files, session_input_state, snapshot_files,
)
from app.domain.services.flows.plan_act import PlanActFlow
from app.interfaces.schemas.session import ChatRequest


def file(file_id="first", name="measurements.csv", size=12):
    return FileInfo(file_id=file_id, filename=name, size=size, content_type="text/csv")


def accepted(files, selected=None):
    selected = files if selected is None else selected
    return MessageEvent(role="user", message="分析资料", attachments=files,
        metadata={"analysis_input_files": input_snapshot(selected),
                  "analysis_input_file_ids": [item.file_id for item in selected]})


def upload_message(files, question="分析数据并可视化"):
    infos = assign_upload_namespace(files)
    for info in infos:
        info.file_path = upload_runtime_path(info)
    return Message(message=question, attachment_file_infos=infos,
        attachment_file_ids=[item.file_id for item in infos], attachments=[item.file_path for item in infos],
        analysis_inputs=build_analysis_inputs([], infos))


def flow():
    instance = PlanActFlow.__new__(PlanActFlow)
    instance.enabled_subagents = {"execution": SimpleNamespace(enabled=True, handler_type="execution")}
    return instance


def test_same_name_inputs_never_overwrite_and_sidecars_share_namespace():
    batch = assign_upload_namespace([file("a", "same.csv"), file("b", "same.csv"),
                                     file("shp", "roads.shp"), file("dbf", "roads.dbf")])
    paths = [upload_runtime_path(item) for item in batch]
    assert len(set(paths)) == 4
    assert len({str(PurePosixPath(path).parent) for path in paths}) == 1
    assert paths[2].endswith("/roads.shp") and paths[3].endswith("/roads.dbf")
    later = assign_upload_namespace([file("c", "same.csv")])
    assert upload_runtime_path(later[0]) not in paths


@pytest.mark.parametrize("name", ["../../etc/passwd", "/Users/person/data.csv", r"C:\data\table.csv", "..", "a\n.csv"])
def test_uploaded_name_cannot_escape_input_namespace(name):
    path = upload_runtime_path(assign_upload_namespace([file(name=name)])[0])
    assert path.startswith("/home/ubuntu/inputs/")
    assert len(PurePosixPath(path).parts) == 6
    assert ".." not in PurePosixPath(path).parts and "\n" not in path


def test_upload_metadata_cannot_supply_its_own_namespace():
    info = file()
    info.metadata = {"analysis_input_namespace": "../output", "analysis_input_filename": "../../evil"}
    safe = assign_upload_namespace([info])[0]
    assert upload_runtime_path(safe).endswith("/measurements.csv")
    assert "../" not in upload_runtime_path(safe)


def test_followup_inherits_user_inputs_not_assistant_outputs_and_explicit_clear_is_sticky():
    initial = assign_upload_namespace([file()])
    history = [accepted(initial), MessageEvent(role="assistant", message="完成",
        attachments=[file("result", "output.png")])]
    chosen, submitted = select_input_files(history, [], None)
    assert [item.file_id for item in chosen] == ["first"] and submitted == []
    with pytest.raises(ValueError, match="本会话"):
        select_input_files(history, [], ["result"])
    cleared, _ = select_input_files(history, [], [])
    assert cleared == []
    history.append(accepted([], selected=[]))
    assert select_input_files(history, [], None)[0] == []
    assert session_input_state(history)[1] == []
    assert [item.file_id for item in session_input_state(history)[0]] == ["first"]


def test_append_does_not_mutate_prior_snapshot_or_rename_existing_inputs():
    original = assign_upload_namespace([file()])
    event = accepted(original)
    before = event.model_dump(mode="json")
    selected, submitted = select_input_files([event], [file("second", "measurements.csv")], None)
    assert [item.file_id for item in selected] == ["first", "second"]
    assert len({upload_runtime_path(item) for item in selected}) == 2
    assert submitted[0].file_id == "second"
    assert event.model_dump(mode="json") == before
    assert upload_runtime_path(selected[0]) == upload_runtime_path(original[0])


def test_uploaded_catalog_is_an_unregistered_source_view_with_truthful_context():
    message = upload_message([file()])
    assert message.datasets == []
    view = upload_catalog_views(message.attachment_file_infos)[0]
    assert view.locations == [] and view.metadata["source_kind"] == "upload"
    assert "not published datasets" in render_upload_context(message.analysis_inputs)
    assert view.sandbox_path == "/home/ubuntu/inputs"
    assert message.analysis_inputs.upload_file_ids == ["first"]
    assert message.analysis_inputs.source_paths == message.attachments
    assert AnalysisInputContext.model_validate_json(message.analysis_inputs.model_dump_json()) == message.analysis_inputs


def test_manifest_is_immutable_and_changes_when_selection_changes():
    first = upload_message([file()]).analysis_inputs
    second = upload_message([file("second")]).analysis_inputs
    assert first.manifest_digest != second.manifest_digest
    with pytest.raises(ValidationError):
        first.sources = ()


def test_uploaded_tabular_analysis_uses_same_fast_path_and_exact_targets():
    message = upload_message([file()], "请分析 measurements.csv 并绘图")
    assert flow()._should_use_dataset_fast_path(message)
    plan = flow()._create_dataset_fast_path_plan(message)
    assert plan.steps[0].inputs["execution_mode"] == "dataset_fast_path"
    assert plan.steps[0].inputs["target_file"].endswith("/measurements.csv")
    assert plan.steps[0].inputs["require_evidence"] is True
    assert plan.steps[0].inputs["artifact_policy"] == "required"


@pytest.mark.parametrize("special", ["skill", "mcp", "image"])
def test_special_capabilities_keep_their_existing_orchestration(special):
    message = upload_message([file()])
    if special == "skill":
        message.skills = ["statistics"]
    elif special == "mcp":
        message.mcp_servers = ["external"]
    else:
        image = file("image", "sample.png")
        image.content_type = "image/png"
        message = upload_message([image], "解释图片")
    assert not flow()._should_use_dataset_fast_path(message)


def test_mixed_sources_preserve_filename_ambiguity_instead_of_selecting_first():
    message = upload_message([file()], "分析 measurements.csv")
    message.datasets = [MountedDataset(dataset_id="data", data_center_id="center", data_center_name="center",
        name="Dataset", sandbox_path="/home/ubuntu/datasets/data", files=[DatasetFile(path="measurements.csv")])]
    message.analysis_inputs = build_analysis_inputs(message.datasets, message.attachment_file_infos)
    assert flow()._resolve_dataset_file_references(message) == []
    upload_reference = upload_catalog_views(message.attachment_file_infos)[0].files[0].path
    message.controller_target_files = [upload_reference]
    selected = flow()._resolve_dataset_file_references(message)
    assert len(selected) == 1 and selected[0].path == upload_reference


def test_selection_wire_contract_distinguishes_omit_and_clear():
    assert ChatRequest(message="继续").input_file_ids is None
    assert ChatRequest(message="继续", input_file_ids=[]).input_file_ids == []
    with pytest.raises(ValidationError):
        ChatRequest(resume_from="a" * 32, client_message_id="resume", input_file_ids=[])


@pytest.mark.asyncio
async def test_materialization_uses_input_identity_and_preserves_original_filenames():
    from app.domain.services.agent_task_runner import AgentTaskRunner
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._user_id = "owner"
    runner._agent_id = "agent"
    runner._file_storage = SimpleNamespace(download_file=AsyncMock(side_effect=[
        (io.BytesIO(b"first"), file("a", "same.csv")), (io.BytesIO(b"second"), file("b", "same.csv"))]))
    runner._sandbox = SimpleNamespace(file_upload=AsyncMock(return_value=ToolResult(success=True)))
    assigned = assign_upload_namespace([file("a", "same.csv"), file("b", "same.csv")])
    first = await runner._sync_file_to_sandbox("a", input_info=assigned[0])
    second = await runner._sync_file_to_sandbox("b", input_info=assigned[1])
    assert first.filename == second.filename == "same.csv"
    assert first.file_path != second.file_path
    assert first.file_path == upload_runtime_path(assigned[0])


@pytest.mark.asyncio
async def test_admission_reauthorizes_inherited_files_and_rejects_outputs():
    from app.domain.services.agent_domain_service import AgentDomainService
    original = assign_upload_namespace([file()])
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = SimpleNamespace(get_events=AsyncMock(return_value=[accepted(original)]))
    service._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=file()))
    selected, submitted = await service._prepare_input_selection("session", "owner", None, None)
    assert selected[0].file_id == "first" and submitted == []
    assert service._file_storage.get_file_info.await_args.args == ("first", "owner")
    service._file_storage.get_file_info.return_value = None
    with pytest.raises(ValueError, match="无权访问"):
        await service._prepare_input_selection("session", "owner", None, None)
    with pytest.raises(ValueError, match="本会话"):
        await service._prepare_input_selection("session", "owner", None, ["unsubmitted-output"])


def test_queued_snapshot_is_self_contained_and_has_no_filesystem_paths():
    assigned = assign_upload_namespace([file()])
    payload = input_snapshot(assigned)
    assert "/home/" not in str(payload) and "/Users/" not in str(payload)
    reconstructed = snapshot_files({"analysis_input_files": payload})
    assert upload_runtime_path(reconstructed[0]) == upload_runtime_path(assigned[0])


@pytest.mark.asyncio
async def test_resume_never_restages_inputs_before_fingerprint_verification():
    from app.domain.services.agent_task_runner import AgentTaskRunner
    selected = assign_upload_namespace([file()])
    event = accepted([], selected=selected)
    event.metadata["resume_from"] = "a" * 32
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id = "session"
    runner._sync_file_to_sandbox = AsyncMock(side_effect=AssertionError("must not overwrite input bytes"))
    runner._session_repository = SimpleNamespace(add_file=AsyncMock())
    result = await runner._sync_analysis_inputs_to_sandbox(event)
    assert result[0].file_path == upload_runtime_path(selected[0])
    runner._sync_file_to_sandbox.assert_not_awaited()


def test_only_server_admission_snapshot_can_authorize_namespace_metadata():
    malicious = file()
    malicious.metadata = {"analysis_input_namespace": "a" * 24, "analysis_input_filename": "other.csv"}
    history = [MessageEvent(role="user", message="prior upload", attachments=[malicious])]
    chosen, _ = select_input_files(history, [], None)
    assert upload_runtime_path(chosen[0]).endswith("/measurements.csv")
    assert (chosen[0].metadata or {})["analysis_input_namespace"] != "a" * 24


@pytest.mark.asyncio
async def test_input_lifecycle_admission_resolver_and_runner_share_exact_selection():
    from app.domain.services.agent_domain_service import AgentDomainService
    from app.domain.services.agent_task_runner import AgentTaskRunner
    history = []
    stored = {item.file_id: item for item in [file("a", "a.csv"), file("b", "b.csv"), file("result", "result.csv")]}
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = SimpleNamespace(get_events=AsyncMock(side_effect=lambda _: list(history)))
    service._file_storage = SimpleNamespace(get_file_info=AsyncMock(side_effect=lambda fid, owner: stored.get(fid)))
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id = "session"
    runner._session_repository = SimpleNamespace(add_file=AsyncMock())

    async def materialize(fid, *, input_info):
        return input_info.model_copy(deep=True, update={"file_path": upload_runtime_path(input_info)})

    runner._sync_file_to_sandbox = AsyncMock(side_effect=materialize)
    cases = [([{"file_id": "a"}, {"file_id": "b"}], None, ["a", "b"]),
             (None, None, ["a", "b"]), (None, [], []),
             (None, None, []), (None, ["b"], ["b"])]
    first_snapshot = None
    for incoming, requested, expected in cases:
        selected, submitted = await service._prepare_input_selection("session", "owner", incoming, requested)
        event = accepted(submitted, selected=selected)
        before = event.model_dump(mode="json")
        # The resolver sees exactly the accepted file snapshot, before staging.
        views = upload_catalog_views(snapshot_files(event.metadata))
        assert sum(len(view.files) for view in views) == len(expected)
        actual = await runner._sync_analysis_inputs_to_sandbox(event)
        context = build_analysis_inputs([], actual)
        assert context.upload_file_ids == expected
        assert [item.file_id for item in snapshot_files(event.metadata)] == expected
        assert event.metadata == before["metadata"]
        history.extend([event, MessageEvent(role="assistant", message="结果", attachments=[stored["result"]])])
        if first_snapshot is None:
            first_snapshot = history[0].model_dump(mode="json")
        assert history[0].model_dump(mode="json") == first_snapshot
    assert session_input_state(history)[1] == ["b"]
    assert set(item.file_id for item in session_input_state(history)[0]) == {"a", "b"}


@pytest.mark.asyncio
async def test_public_file_and_event_projection_hide_private_input_snapshot(monkeypatch):
    from app.interfaces.schemas.file import FileInfoResponse
    from app.interfaces.schemas.event import EventMapper
    from app.interfaces import dependencies
    monkeypatch.setattr(dependencies, "get_file_service", lambda: SimpleNamespace(
        create_signed_url=AsyncMock(return_value="https://local.test/files/first")))
    selected = assign_upload_namespace([file()])
    selected[0].file_path = upload_runtime_path(selected[0])
    public = await FileInfoResponse.from_file_info(selected[0])
    assert "analysis_input_namespace" not in str(public.model_dump())
    assert "/home/ubuntu/inputs" not in str(public.model_dump())
    event = accepted(selected)
    projected = await EventMapper.event_to_sse_event(event)
    assert "analysis_input_files" not in str(projected.model_dump())
    assert "analysis_input_namespace" not in str(projected.model_dump())


@pytest.mark.asyncio
async def test_upload_preview_reuses_exact_source_copy_with_manifest_authorization():
    import shlex
    from langchain_core.messages import ToolMessage
    from app.domain.models.plan import Plan
    from test_execution_context_budget import _preview_agent, _preview_step
    message = upload_message([file(name="data.csv")], "预览 data.csv")
    target = upload_catalog_views(message.attachment_file_infos)[0].files[0].path
    agent = _preview_agent(ToolMessage(tool_call_id="", name="shell_run", content="completed",
        artifact=ToolResult(success=True, data={"status": "completed", "returncode": 0, "output": ""})))
    step = _preview_step(target)
    _ = [event async for event in agent.execute_step(Plan(language="zh", steps=[step]), step, message)]
    assert step.success is True
    command = agent.invoke_tool.await_args.args[1]["args"]["command"]
    assert f"source_path={shlex.quote(message.attachments[0])}" in command
    assert '[ "$resolved_source" = "$source_path" ]' in command
    assert step.attachments[0].startswith("/home/ubuntu/output/file-preview-")


@pytest.mark.asyncio
async def test_upload_preview_rejects_unbound_or_replaced_runtime_file():
    from app.domain.models.plan import Plan
    from test_execution_context_budget import _preview_agent, _preview_step
    message = upload_message([file(name="data.csv")], "预览 data.csv")
    target = upload_catalog_views(message.attachment_file_infos)[0].files[0].path
    message.attachments = ["/home/ubuntu/inputs/" + "a" * 24 + "/unselected.csv"]
    agent = _preview_agent(None)
    step = _preview_step(target)
    _ = [event async for event in agent.execute_step(Plan(language="zh", steps=[step]), step, message)]
    assert step.success is False
    agent.invoke_tool.assert_not_awaited()
