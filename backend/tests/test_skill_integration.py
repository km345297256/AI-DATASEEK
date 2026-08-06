import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from app.domain.services.skills import SkillRegistry, SkillSelector, SkillRenderer
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.skill import SkillToolkit
from app.domain.models.approval import ApprovalRequest, ApprovalStatus
from app.domain.models.mcp_config import MCPConfig, MCPRiskLevel, MCPServerConfig, MCPTransport
from app.domain.services.approval_service import ApprovalService
from app.domain.services.token_usage_service import TokenUsageService
from langchain.messages import AIMessage
from app.domain.models.event import DoneEvent, MessageEvent, StepEvent, StepStatus
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.event import FileToolContent
from app.domain.models.session import Session
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.flows.plan_act import AgentStatus
from app.application.services.agent_service import AgentService
from app.application.services.file_service import FileService
from app.domain.models.tool_result import ToolResult
from app.domain.models.session import SessionStatus
from app.domain.models.file import FileInfo
from app.domain.external.sandbox_runtime import SandboxNotFoundError
from app.domain.models.plan import Step
from app.domain.models.execution_node import ExecutionNodeAuthType, ExecutionNodeCapacity, ExecutionNodeHealth, ExecutionNodeStatus, ExecutionNodeType, SandboxAllocationStatus
from app.domain.models.user import User, UserRole
from app.infrastructure.external.sandbox import runtime as sandbox_runtime_module
from app.infrastructure.external.sandbox import node_health as node_health_module
from app.infrastructure.external.sandbox import node_monitor as node_monitor_module
from app.infrastructure.external.sandbox.node_health import execution_node_auth_headers
from app.infrastructure.external.sandbox.runtime import WorkerAgentSandbox
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool
from app.interfaces.schemas.event import EventMapper
from app.domain.services.skills.session_skill_creator import (
    _derive_skill_metadata,
    build_reference_files,
    build_script_files,
    build_skill_content_from_events,
)
from app.domain.models.event import PlanEvent, PlanStatus
from app.domain.models.plan import Plan
from app.domain.utils.robust_json_parser import parse_json_lenient
from app.interfaces.api.renderer_routes import _validate_request
from app.interfaces.api import file_routes
from app.interfaces.schemas.file import LargeUploadCompleteRequest, LargeUploadInitRequest, LargeUploadPart
from app.interfaces.schemas.renderer import RendererRequest
from app.interfaces.dependencies import get_optional_current_user


def test_skill_registry_selector_and_renderer(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "metadata.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "description": "Demo skill for testing.",
                "triggers": ["demo trigger"],
                "priority": 1,
                "max_context_chars": 20,
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Use this demo skill content.", encoding="utf-8")

    registry = SkillRegistry(str(tmp_path / "skills"))
    selected = SkillSelector(registry).select("please use demo trigger")
    rendered = SkillRenderer.render(selected)

    assert [skill.name for skill in registry.list_skills()] == ["demo"]
    assert [skill.name for skill in selected] == ["demo"]
    assert "<active_skills>" in rendered
    assert 'name="demo"' in rendered
    assert "Demo skill for testing." in rendered
    assert "Skill loading is internal preparation" in rendered
    assert "Use this demo skill" not in rendered


@pytest.mark.asyncio
async def test_skill_toolkit_lists_and_reads_skills(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Demo instructions.", encoding="utf-8")

    toolkit = SkillToolkit(SkillRegistry(str(tmp_path / "skills")))
    list_tool = toolkit.get_tool("skill_list")
    read_tool = toolkit.get_tool("skill_read")

    listed = (await list_tool.ainvoke({"id": "1", "args": {}})).artifact
    read = (await read_tool.ainvoke({"id": "2", "args": {"name": "demo"}})).artifact
    missing = (await read_tool.ainvoke({"id": "3", "args": {"name": "missing"}})).artifact

    assert listed.success is True
    assert listed.data[0]["name"] == "demo"
    assert read.success is True
    assert read.data["content"] == "Demo instructions."
    assert missing.success is False


@pytest.mark.asyncio
async def test_skill_registry_uploads_markdown_skill(tmp_path):
    registry = SkillRegistry(str(tmp_path / "skills"))

    skill = await registry.save_markdown_skill("My Skill.md", b"# My Skill\n\nInstructions.", user_id="user-1")

    assert skill.name == "my-skill"
    assert (tmp_path / "skills" / "users" / "user-1" / "my-skill" / "SKILL.md").exists()


def test_skill_registry_reads_skill_frontmatter(tmp_path):
    skill_dir = tmp_path / "skills" / "research-data-extractor"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: research-data-extractor
description: Extract research data from project PDFs.
triggers: [PDF, research data]
priority: 2
---

# Research Data Extractor

Instructions.
""",
        encoding="utf-8",
    )

    registry = SkillRegistry(str(tmp_path / "skills"))
    skill = registry.get_skill("research-data-extractor")

    assert skill is not None
    assert skill.description == "Extract research data from project PDFs."
    assert skill.triggers == ["PDF", "research data"]
    assert skill.priority == 2
    assert skill.content.startswith("# Research Data Extractor")


@pytest.mark.asyncio
async def test_skill_registry_uploads_zip_skills(tmp_path):
    zip_path = tmp_path / "skills.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("alpha/SKILL.md", "Alpha instructions.")
        archive.writestr("alpha/metadata.json", json.dumps({"name": "alpha", "triggers": ["alpha"]}))
        archive.writestr("alpha/scripts/helper.py", "print('helper')")
        archive.writestr("beta/SKILL.md", "Beta instructions.")
        archive.writestr("ignored.txt", "ignored")

    registry = SkillRegistry(str(tmp_path / "skills"))
    with zip_path.open("rb") as fileobj:
        skills = await registry.save_zip_skills(fileobj, user_id="user-1")

    assert [skill.name for skill in skills] == ["alpha", "beta"]
    assert (tmp_path / "skills" / "users" / "user-1" / "alpha" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "users" / "user-1" / "alpha" / "scripts" / "helper.py").exists()
    assert (tmp_path / "skills" / "users" / "user-1" / "beta" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_skill_registry_uploads_zip_skill_with_yaml_block_metadata(tmp_path):
    zip_path = tmp_path / "alphanumeric_filter.zip"
    skill_md = """---
name: alphanumeric_filter
description: |
  字母数字比例过滤器。
  当用户提到文本过滤、比例筛选时使用此skill。
name_zh: 字母数字比例过滤器算子
input_params:
  - name: input
    type: string
    required: true
    description: 输入JSON文件路径
output_params:
  - name: output
    type: json_file
    description: 过滤后的JSON文件
tag: 过滤与筛选
---

# Alphanumeric Filter
Use this skill to filter text by alphanumeric ratio.
"""
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("alphanumeric_filter/SKILL.md", skill_md)
        archive.writestr("__MACOSX/alphanumeric_filter/._SKILL.md", "ignored")
        archive.writestr("alphanumeric_filter/scripts/run.py", "print('ok')")

    registry = SkillRegistry(str(tmp_path / "skills"), user_id="user-1")
    with zip_path.open("rb") as fileobj:
        skills = await registry.save_zip_skills(fileobj, user_id="user-1")

    assert len(skills) == 1
    assert skills[0].name == "alphanumeric_filter"
    assert "字母数字比例过滤器" in skills[0].description
    assert (tmp_path / "skills" / "users" / "user-1" / "alphanumeric_filter" / "scripts" / "run.py").exists()


@pytest.mark.asyncio
async def test_skill_registry_rejects_zip_when_skill_cannot_be_loaded(tmp_path):
    zip_path = tmp_path / "broken.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("broken/SKILL.md", "---\nname: broken\npriority: invalid\n---\nBroken")

    registry = SkillRegistry(str(tmp_path / "skills"), user_id="user-1")
    with zip_path.open("rb") as fileobj:
        with pytest.raises(ValueError, match="No valid skills were loaded"):
            await registry.save_zip_skills(fileobj, user_id="user-1")


@pytest.mark.asyncio
async def test_skill_registry_rejects_unsafe_zip_paths(tmp_path):
    zip_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../evil/SKILL.md", "bad")

    registry = SkillRegistry(str(tmp_path / "skills"))
    with zip_path.open("rb") as fileobj:
        with pytest.raises(ValueError, match="Unsafe zip path"):
            await registry.save_zip_skills(fileobj, user_id="user-1")


def test_skill_registry_loads_global_and_current_user_only(tmp_path):
    global_dir = tmp_path / "skills" / "global" / "global-skill"
    global_dir.mkdir(parents=True)
    (global_dir / "SKILL.md").write_text("---\nname: global-skill\n---\nGlobal.", encoding="utf-8")
    user_dir = tmp_path / "skills" / "users" / "user-a" / "private-skill"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text("---\nname: private-skill\n---\nPrivate.", encoding="utf-8")
    other_dir = tmp_path / "skills" / "users" / "user-b" / "other-skill"
    other_dir.mkdir(parents=True)
    (other_dir / "SKILL.md").write_text("---\nname: other-skill\n---\nOther.", encoding="utf-8")

    registry = SkillRegistry(str(tmp_path / "skills"), user_id="user-a")

    assert [skill.name for skill in registry.list_skills()] == ["global-skill", "private-skill"]


def test_parse_json_lenient_repairs_unescaped_quotes_in_markdown_json():
    raw = '''```json
[
  {
    "排名": 1,
    "原排名": 2,
    "标题": "3亿北斗工程现"脆皮底座"",
    "热度": 780.9,
    "分析": "这是当前百度热搜榜上热度最高的非文旅类话题。"3亿"指投资金额高达3亿元，"脆皮底座"是一句网络流行语。"
  }
]
```'''

    parsed = parse_json_lenient(raw)

    assert parsed[0]["标题"] == '3亿北斗工程现"脆皮底座"'
    assert '"3亿"' in parsed[0]["分析"]
    assert '"脆皮底座"' in parsed[0]["分析"]


def test_renderer_request_validation_normalizes_extensions():
    normalized = _validate_request(
        RendererRequest(
            name="PNG API",
            kind="api",
            extensions=[".PNG", "png", " jpeg "],
            api_url="http://renderer.local/render",
        )
    )

    assert normalized == ["png", "jpeg"]


def test_renderer_request_validation_requires_runtime_entrypoint():
    with pytest.raises(HTTPException, match="api_url"):
        _validate_request(RendererRequest(name="CSV", kind="api", extensions=["csv"]))

    with pytest.raises(HTTPException, match="entry"):
        _validate_request(RendererRequest(name="HTML", kind="component", extensions=["html"]))


def test_renderer_request_validation_rejects_builtin_management():
    with pytest.raises(HTTPException, match="Builtin"):
        _validate_request(RendererRequest(name="PNG", kind="builtin", extensions=["png"]))


@pytest.mark.asyncio
async def test_file_find_tool_event_preserves_search_results():
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"

    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="file",
        function_name="file_find_by_name",
        function_args={"path": "/home/ubuntu", "glob_pattern": "*.obj"},
        status=ToolStatus.CALLED,
        function_result=ToolResult(success=True, data={"path": "/home/ubuntu", "files": ["/home/ubuntu/model.obj"]}),
    )

    await runner._handle_tool_event(event)

    assert isinstance(event.tool_content, FileToolContent)
    assert "(No Content)" not in event.tool_content.content
    assert "/home/ubuntu/model.obj" in event.tool_content.content


@pytest.mark.asyncio
async def test_shell_exec_tool_event_renders_only_current_command_console():
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"

    class FakeSandbox:
        async def view_shell(self, session_id, console=False):
            assert session_id == "shell-1"
            assert console is True
            return ToolResult(
                success=True,
                data={
                    "console": [
                        {"ps1": "$", "command": "mkdir -p /home/ubuntu/brightness_temperature", "output": ""},
                        {"ps1": "$", "command": "python generate_csv.py", "output": "csv ok\n"},
                        {"ps1": "$", "command": "python plot_png.py", "output": "png ok\n"},
                    ]
                },
            )

    runner._sandbox = FakeSandbox()
    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="shell",
        function_name="shell_exec",
        function_args={"id": "shell-1", "exec_dir": "/home/ubuntu", "command": "python plot_png.py"},
        status=ToolStatus.CALLED,
    )

    await runner._handle_tool_event(event)

    assert event.tool_content.console == [
        {"ps1": "$", "command": "python plot_png.py", "output": "png ok\n"}
    ]


@pytest.mark.asyncio
async def test_completed_step_discovers_and_syncs_generated_png_artifact():
    class FakeSessionRepository:
        def __init__(self):
            self.files_by_path = {}
            self.added_files = []

        async def get_file_by_path(self, session_id, file_path):
            return self.files_by_path.get(file_path)

        async def add_file(self, session_id, file_info):
            self.files_by_path[file_info.file_path] = file_info
            self.added_files.append(file_info)

        async def remove_file(self, session_id, file_id):
            self.files_by_path = {
                path: file_info
                for path, file_info in self.files_by_path.items()
                if file_info.file_id != file_id
            }

    class FakeSandbox:
        async def file_find(self, path, glob_pattern):
            assert path == "/home/ubuntu/output"
            assert glob_pattern == "**/*"
            return ToolResult(
                success=True,
                data={"path": path, "files": ["/home/ubuntu/output/brightness_map.png"]},
            )

        async def file_download(self, file_path):
            assert file_path == "/home/ubuntu/output/brightness_map.png"
            return SimpleNamespace(read=lambda: b"png-data")

    class FakeFileStorage:
        async def upload_file(self, file_data, file_name, user_id):
            assert file_name == "brightness_map.png"
            assert user_id == "user-1"
            return FileInfo(file_id="gridfs-png", filename=file_name, size=8)

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._session_repository = FakeSessionRepository()
    runner._sandbox = FakeSandbox()
    runner._file_storage = FakeFileStorage()
    runner._generated_files = []
    runner._artifact_baseline_paths = set()

    event = StepEvent(status=StepStatus.COMPLETED, step=Step())

    await runner._sync_step_attachments_to_storage(event)
    await runner._sync_discovered_artifacts_to_storage()

    message_event = MessageEvent(message="任务完成")
    await runner._sync_message_attachments_to_storage(message_event)
    if not message_event.attachments and runner._generated_files:
        message_event.attachments = runner._generated_files

    assert runner._session_repository.added_files[0].file_path == "/home/ubuntu/output/brightness_map.png"
    assert message_event.attachments[0].file_id == "gridfs-png"


@pytest.mark.asyncio
async def test_artifact_discovery_syncs_data_files_beyond_old_suffix_and_count_limit():
    class FakeSessionRepository:
        def __init__(self):
            self.files_by_path = {}
            self.added_files = []

        async def get_file_by_path(self, session_id, file_path):
            return self.files_by_path.get(file_path)

        async def add_file(self, session_id, file_info):
            self.files_by_path[file_info.file_path] = file_info
            self.added_files.append(file_info)

        async def remove_file(self, session_id, file_id):
            return None

    generated_paths = [
        f"/home/ubuntu/output/brightness_temperature/data_{index:02d}.csv"
        for index in range(35)
    ] + [
        "/home/ubuntu/output/brightness_temperature/array.npy",
        "/home/ubuntu/output/brightness_temperature/report.html",
        "/home/ubuntu/output/.cache/ignored.csv",
        "/home/ubuntu/output/unpacked/copied-source.tif",
        "/home/ubuntu/output/unpacked/unpack-manifest.json",
        "/home/ubuntu/output/unpacked_archives/nested-source.xml",
        "/home/ubuntu/output/brightness_temperature/raw.bin",
        "/home/ubuntu/outside-output.csv",
    ]

    class FakeSandbox:
        async def file_find(self, path, glob_pattern):
            assert path == "/home/ubuntu/output"
            assert glob_pattern == "**/*"
            return ToolResult(success=True, data={"path": path, "files": generated_paths})

        async def file_download(self, file_path):
            return SimpleNamespace(read=lambda: b"data")

    class FakeFileStorage:
        async def upload_file(self, file_data, file_name, user_id):
            return FileInfo(file_id=f"gridfs-{file_name}", filename=file_name, size=4)

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._session_repository = FakeSessionRepository()
    runner._sandbox = FakeSandbox()
    runner._file_storage = FakeFileStorage()
    runner._generated_files = []
    runner._artifact_baseline_paths = {"/home/ubuntu/output/brightness_temperature/data_00.csv"}
    runner._private_artifact_roots = {
        "/home/ubuntu/output/unpacked",
        "/home/ubuntu/output/unpacked_archives",
    }

    await runner._sync_discovered_artifacts_to_storage()

    synced_paths = {file.file_path for file in runner._session_repository.added_files}
    assert "/home/ubuntu/output/brightness_temperature/data_00.csv" not in synced_paths
    assert "/home/ubuntu/output/brightness_temperature/data_34.csv" in synced_paths
    assert "/home/ubuntu/output/brightness_temperature/array.npy" in synced_paths
    assert "/home/ubuntu/output/brightness_temperature/report.html" in synced_paths
    assert "/home/ubuntu/output/.cache/ignored.csv" not in synced_paths
    assert "/home/ubuntu/output/unpacked/copied-source.tif" not in synced_paths
    assert "/home/ubuntu/output/unpacked/unpack-manifest.json" not in synced_paths
    assert "/home/ubuntu/output/unpacked_archives/nested-source.xml" not in synced_paths
    assert "/home/ubuntu/output/brightness_temperature/raw.bin" not in synced_paths
    assert "/home/ubuntu/outside-output.csv" not in synced_paths


@pytest.mark.asyncio
async def test_artifact_discovery_uploads_only_new_or_changed_content():
    artifact_path = "/home/ubuntu/output/chart.png"

    class FakeSessionRepository:
        def __init__(self):
            self.files_by_path = {}
            self.added_files = []
            self.removed_file_ids = []

        async def get_file_by_path(self, session_id, file_path):
            return self.files_by_path.get(file_path)

        async def add_file(self, session_id, file_info):
            self.files_by_path[file_info.file_path] = file_info
            self.added_files.append(file_info)

        async def remove_file(self, session_id, file_id):
            self.removed_file_ids.append(file_id)
            self.files_by_path = {
                path: info
                for path, info in self.files_by_path.items()
                if info.file_id != file_id
            }

    class FakeSandbox:
        def __init__(self):
            self.content = b"first-render"
            self.search_roots = []

        async def file_find(self, path, glob_pattern):
            self.search_roots.append(path)
            return ToolResult(success=True, data={"path": path, "files": [artifact_path]})

        async def file_download(self, file_path):
            assert file_path == artifact_path
            return io.BytesIO(self.content)

    class FakeFileStorage:
        def __init__(self):
            self.uploads = []
            self.deleted_file_ids = []

        async def upload_file(self, file_data, file_name, user_id, metadata=None):
            payload = file_data.read()
            self.uploads.append((file_name, payload, metadata))
            return FileInfo(
                file_id=f"file-{len(self.uploads)}",
                filename=file_name,
                size=len(payload),
                metadata=metadata,
            )

        async def delete_file(self, file_id, user_id):
            self.deleted_file_ids.append(file_id)
            return True

    repository = FakeSessionRepository()
    sandbox = FakeSandbox()
    storage = FakeFileStorage()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._session_repository = repository
    runner._sandbox = sandbox
    runner._file_storage = storage
    runner._generated_files = []
    runner._artifact_baseline_paths = set()
    runner._artifact_fingerprints = {}

    first = await runner._sync_discovered_artifacts_to_storage()
    unchanged = await runner._sync_discovered_artifacts_to_storage()
    first_fingerprint = runner._artifact_fingerprints[artifact_path]

    sandbox.content = b"second-render"
    changed = await runner._sync_discovered_artifacts_to_storage()

    assert sandbox.search_roots == ["/home/ubuntu/output"] * 3
    assert len(first) == 1
    assert unchanged == []
    assert len(changed) == 1
    assert [payload for _, payload, _ in storage.uploads] == [b"first-render", b"second-render"]
    assert repository.removed_file_ids == ["file-1"]
    assert storage.deleted_file_ids == ["file-1"]
    assert artifact_path in runner._artifact_baseline_paths
    assert runner._artifact_fingerprints[artifact_path] != first_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_metadata",
    [
        {"source": "user_upload", "session_id": "session-1"},
        {"source": "sandbox_artifact", "session_id": "another-session"},
    ],
)
async def test_replacing_artifact_does_not_delete_unmanaged_or_other_session_uploads(
    existing_metadata,
):
    artifact_path = "/home/ubuntu/output/report.csv"

    class FakeSessionRepository:
        def __init__(self):
            self.file_info = FileInfo(
                file_id="user-upload",
                filename="report.csv",
                file_path=artifact_path,
                metadata=existing_metadata,
            )

        async def get_file_by_path(self, session_id, file_path):
            return self.file_info

        async def add_file(self, session_id, file_info):
            self.file_info = file_info

        async def remove_file(self, session_id, file_id):
            return None

    class FakeSandbox:
        async def file_download(self, file_path):
            return io.BytesIO(b"new report")

    class FakeFileStorage:
        def __init__(self):
            self.deleted_file_ids = []

        async def upload_file(self, file_data, file_name, user_id, metadata=None):
            return FileInfo(file_id="generated-report", filename=file_name, metadata=metadata)

        async def delete_file(self, file_id, user_id):
            self.deleted_file_ids.append(file_id)
            return True

    storage = FakeFileStorage()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._session_repository = FakeSessionRepository()
    runner._sandbox = FakeSandbox()
    runner._file_storage = storage
    runner._artifact_baseline_paths = set()
    runner._artifact_fingerprints = {}

    synced = await runner._sync_file_to_storage(artifact_path)

    assert synced.file_id == "generated-report"
    assert storage.deleted_file_ids == []


@pytest.mark.asyncio
async def test_explicit_attachment_outside_output_is_preserved_and_deduplicated():
    attachment_path = "/home/ubuntu/work/report.csv"

    class FakeSessionRepository:
        def __init__(self):
            self.file_info = None

        async def get_file_by_path(self, session_id, file_path):
            return self.file_info

        async def add_file(self, session_id, file_info):
            self.file_info = file_info

        async def remove_file(self, session_id, file_id):
            self.file_info = None

    class FakeSandbox:
        async def file_download(self, file_path):
            assert file_path == attachment_path
            return io.BytesIO(b"a,b\n1,2\n")

    class FakeFileStorage:
        def __init__(self):
            self.upload_count = 0

        async def upload_file(self, file_data, file_name, user_id, metadata=None):
            self.upload_count += 1
            return FileInfo(
                file_id=f"file-{self.upload_count}",
                filename=file_name,
                metadata=metadata,
            )

    repository = FakeSessionRepository()
    storage = FakeFileStorage()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._user_id = "user-1"
    runner._session_repository = repository
    runner._sandbox = FakeSandbox()
    runner._file_storage = storage
    runner._generated_files = []
    runner._artifact_baseline_paths = set()
    runner._artifact_fingerprints = {}

    first = await runner._sync_explicit_paths_to_storage([attachment_path])
    repeated = await runner._sync_explicit_paths_to_storage([attachment_path])

    assert first[0].file_id == "file-1"
    assert repeated[0].file_id == "file-1"
    assert storage.upload_count == 1


@pytest.mark.asyncio
async def test_file_read_tool_event_does_not_upload_the_file_directly():
    class FakeSandbox:
        async def file_read(self, file_path):
            return ToolResult(success=True, data={"content": "print('ready')"})

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._sandbox = FakeSandbox()
    sync_calls = []

    async def record_sync(file_path):
        sync_calls.append(file_path)

    runner._sync_file_to_storage = record_sync
    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="file",
        function_name="file_read",
        function_args={"file": "/home/ubuntu/output/plot.py"},
        status=ToolStatus.CALLED,
    )

    await runner._handle_tool_event(event)

    assert event.tool_content.content == "print('ready')"
    assert sync_calls == []


@pytest.mark.asyncio
async def test_flow_does_not_rescan_unchanged_artifacts_at_summary_and_done():
    class FakeSafetyReviewer:
        async def review(self, message, excerpts):
            return SimpleNamespace(
                allowed=True,
                decision="allow",
                risk_level="low",
                categories=[],
                reason="",
                suggestion="",
            )

    class FakeFlow:
        status = AgentStatus.EXECUTING

        async def run(self, message):
            yield ToolEvent(
                tool_call_id="tool-1",
                tool_name="message",
                function_name="message_notify",
                function_args={},
                status=ToolStatus.CALLED,
            )
            yield StepEvent(status=StepStatus.COMPLETED, step=Step())
            self.status = AgentStatus.SUMMARIZING
            yield MessageEvent(message="summary")
            yield DoneEvent()

    class FakeSessionRepository:
        async def find_by_id(self, session_id):
            return None

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._flow = FakeFlow()
    runner._safety_reviewer = FakeSafetyReviewer()
    runner._session_repository = FakeSessionRepository()
    runner._generated_files = []
    discovery_calls = []

    async def noop(*args, **kwargs):
        return None

    async def record_discovery():
        discovery_calls.append(True)
        return []

    runner._record_safety_audit = noop
    runner._initialize_mcp_tool = noop
    runner._handle_tool_event = noop
    runner._sync_discovered_artifacts_to_storage = record_discovery

    message = SimpleNamespace(
        message="make a chart",
        attachment_file_infos=[],
        mcp_servers=[],
        mcp_access_all=False,
    )
    events = [event async for event in runner._run_flow(message)]

    assert len(discovery_calls) == 1
    assert [type(event) for event in events] == [ToolEvent, StepEvent, MessageEvent, DoneEvent]


@pytest.mark.asyncio
async def test_generated_files_attach_only_to_summary_message():
    class FakeSessionRepository:
        async def get_file_by_path(self, session_id, file_path):
            return None

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._session_repository = FakeSessionRepository()
    runner._generated_files = [
        FileInfo(file_id="gridfs-csv", filename="data.csv", file_path="/home/ubuntu/data.csv")
    ]
    runner._flow = SimpleNamespace(status=AgentStatus.EXECUTING)

    step_message = MessageEvent(message="步骤中间结果")
    await runner._sync_message_attachments_to_storage(step_message)
    if (
        not step_message.attachments
        and runner._generated_files
        and runner._should_attach_generated_files_to_message()
    ):
        step_message.attachments = runner._generated_files

    runner._flow.status = AgentStatus.SUMMARIZING
    summary_message = MessageEvent(message="最终总结")
    await runner._sync_message_attachments_to_storage(summary_message)
    if (
        not summary_message.attachments
        and runner._generated_files
        and runner._should_attach_generated_files_to_message()
    ):
        summary_message.attachments = runner._generated_files

    assert step_message.attachments == []
    assert summary_message.attachments == runner._generated_files


@pytest.mark.asyncio
async def test_chat_recreates_missing_running_task(monkeypatch):
    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def put(self, message):
            self.messages.append(message)
            return f"msg-{len(self.messages)}"

        async def get(self, start_id=None, block_ms=None):
            return None, None

    class FakeTask:
        created = []

        def __init__(self):
            self.id = f"task-{len(self.created) + 1}"
            self.done = True
            self.input_stream = FakeQueue()
            self.output_stream = FakeQueue()
            self.run_called = False

        async def run(self):
            self.run_called = True

        @classmethod
        def get(cls, task_id):
            return None

        @classmethod
        def create(cls, runner):
            task = cls()
            cls.created.append(task)
            return task

    class FakeSessionRepository:
        def __init__(self):
            self.session = Session(
                id="session-1",
                user_id="user-1",
                agent_id="agent-1",
                sandbox_id="sandbox-1",
                task_id="missing-task",
                status=SessionStatus.RUNNING,
            )
            self.events = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return self.session

        async def save(self, session):
            self.session = session

        async def update_latest_message(self, session_id, message, timestamp):
            self.session.latest_message = message

        async def add_event(self, session_id, event):
            self.events.append(event)

        async def update_status(self, session_id, status):
            self.session.status = status

        async def update_unread_message_count(self, session_id, count):
            self.session.unread_message_count = count

    class FakeSandbox:
        id = "sandbox-1"

        @classmethod
        async def get(cls, sandbox_id):
            return cls()

        async def get_browser(self):
            return object()

    async def fake_assign_to_session(container_name, session_id, task_id):
        return None

    async def fake_ensure_user_can_run_task(self, user_id):
        return None

    monkeypatch.setattr(
        "app.domain.services.token_quota_service.TokenQuotaService.ensure_user_can_run_task",
        fake_ensure_user_can_run_task,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.DockerSandbox.assign_to_session",
        fake_assign_to_session,
    )

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=FakeSandbox,
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
    )

    events = [
        event async for event in service.chat(
            session_id="session-1",
            user_id="user-1",
            message="hello",
        )
    ]

    assert events == []
    assert len(FakeTask.created) == 1
    assert FakeTask.created[0].run_called is True
    assert FakeTask.created[0].input_stream.messages


@pytest.mark.asyncio
async def test_chat_bootstrap_survives_sse_cancellation(monkeypatch):
    import asyncio

    async def fake_ensure_user_can_run_task(self, user_id):
        return None

    monkeypatch.setattr(
        "app.domain.services.token_quota_service.TokenQuotaService.ensure_user_can_run_task",
        fake_ensure_user_can_run_task,
    )

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def put(self, message):
            self.messages.append(message)
            return f"msg-{len(self.messages)}"

        async def get(self, start_id=None, block_ms=None):
            return None, None

    class FakeTask:
        created = []

        def __init__(self):
            self.id = f"task-{len(self.created) + 1}"
            self.done = True
            self.input_stream = FakeQueue()
            self.output_stream = FakeQueue()
            self.run_called = False

        async def run(self):
            self.run_called = True

        @classmethod
        def get(cls, task_id):
            return None

        @classmethod
        def create(cls, runner):
            task = cls()
            cls.created.append(task)
            return task

    class FakeSessionRepository:
        def __init__(self):
            self.session = Session(id="session-1", user_id="user-1", agent_id="agent-1")
            self.events = []
            self.statuses = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return self.session

        async def save(self, session):
            self.session = session

        async def update_status(self, session_id, status):
            self.session.status = status
            self.statuses.append(status)

        async def update_latest_message(self, session_id, message, timestamp):
            self.session.latest_message = message

        async def add_event(self, session_id, event):
            self.events.append(event)

        async def update_unread_message_count(self, session_id, count):
            self.session.unread_message_count = count

    repository = FakeSessionRepository()
    release_create_task = asyncio.Event()

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=repository,
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
    )

    async def slow_create_task(session):
        await release_create_task.wait()
        task = FakeTask.create(None)
        session.task_id = task.id
        await repository.save(session)
        return task

    service._create_task = slow_create_task

    generator = service.chat(session_id="session-1", user_id="user-1", message="hello")
    consumer_task = asyncio.create_task(generator.__anext__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    consumer_task.cancel()
    await asyncio.gather(consumer_task, return_exceptions=True)

    release_create_task.set()
    await asyncio.wait_for(asyncio.gather(*service._chat_bootstrap_tasks), timeout=1)

    assert repository.session.status == SessionStatus.RUNNING
    assert repository.events
    assert len(FakeTask.created) == 1
    assert FakeTask.created[0].run_called is True
    assert FakeTask.created[0].input_stream.messages


@pytest.mark.asyncio
async def test_chat_bootstrap_failure_after_sse_cancellation_is_persisted(monkeypatch):
    import asyncio

    async def fake_ensure_user_can_run_task(self, user_id):
        return None

    monkeypatch.setattr(
        "app.domain.services.token_quota_service.TokenQuotaService.ensure_user_can_run_task",
        fake_ensure_user_can_run_task,
    )

    class FakeTask:
        @classmethod
        def get(cls, task_id):
            return None

    class FakeSessionRepository:
        def __init__(self):
            self.session = Session(id="session-1", user_id="user-1", agent_id="agent-1")
            self.events = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return self.session

        async def update_status(self, session_id, status):
            self.session.status = status

        async def add_event(self, session_id, event):
            self.events.append(event)

        async def update_unread_message_count(self, session_id, count):
            self.session.unread_message_count = count

    repository = FakeSessionRepository()
    release_create_task = asyncio.Event()

    service = AgentDomainService(
        agent_repository=object(),
        session_repository=repository,
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
    )

    async def failing_create_task(session):
        await release_create_task.wait()
        raise RuntimeError("sandbox failed")

    service._create_task = failing_create_task

    generator = service.chat(session_id="session-1", user_id="user-1", message="hello")
    consumer_task = asyncio.create_task(generator.__anext__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    consumer_task.cancel()
    await asyncio.gather(consumer_task, return_exceptions=True)

    release_create_task.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert repository.session.status == SessionStatus.COMPLETED
    assert repository.events
    assert repository.events[-1].error == "sandbox failed"


@pytest.mark.asyncio
async def test_agent_service_shell_view_restores_sandbox_via_runtime():
    class ExplodingSandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            raise AssertionError("shell_view must not restore sandbox through sandbox_cls.get")

    class FakeSandbox:
        vnc_url = "ws://worker/vnc"

        async def view_shell(self, shell_session_id, console=False):
            assert shell_session_id == "shell-1"
            assert console is True
            return ToolResult(
                success=True,
                data={"output": "ok", "session_id": "shell-1", "console": []},
            )

    class FakeSandboxRuntime:
        def __init__(self):
            self.restored = []

        async def allocate(self, session=None):
            return FakeSandbox()

        async def restore(self, sandbox_id):
            self.restored.append(sandbox_id)
            return FakeSandbox()

        async def assign(self, sandbox, session, task_id=None):
            return None

    class FakeSessionRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return Session(
                id=session_id,
                user_id=user_id,
                agent_id="agent-1",
                sandbox_id="sandbox-remote",
            )

    runtime = FakeSandboxRuntime()
    service = AgentService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=ExplodingSandboxClass,
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=runtime,
    )

    response = await service.shell_view("session-1", "shell-1", "user-1")

    assert runtime.restored == ["sandbox-remote"]
    assert response.session_id == "shell-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_status", [SessionStatus.COMPLETED, SessionStatus.WAITING])
async def test_agent_service_file_view_uses_storage_for_inactive_session(session_status):
    class ExplodingRuntime:
        async def restore(self, sandbox_id):
            raise AssertionError("inactive file preview must not resume sandbox")

    class FakeFileStorage:
        def __init__(self):
            self.downloaded = []

        async def download_file(self, file_id, user_id):
            self.downloaded.append((file_id, user_id))
            return io.BytesIO("结果内容".encode("utf-8")), FileInfo(file_id=file_id, filename="result.txt")

    class FakeSessionRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return Session(
                id=session_id,
                user_id=user_id,
                agent_id="agent-1",
                sandbox_id="sandbox-paused",
                status=session_status,
                files=[
                    FileInfo(
                        file_id="minio:file-1",
                        filename="result.txt",
                        file_path="/home/ubuntu/result.txt",
                    )
                ],
            )

    storage = FakeFileStorage()
    service = AgentService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=storage,
        mcp_repository=object(),
        sandbox_runtime=ExplodingRuntime(),
    )

    response = await service.file_view("session-1", "/home/ubuntu/result.txt", "user-1")

    assert response.content == "结果内容"
    assert storage.downloaded == [("minio:file-1", "user-1")]


@pytest.mark.asyncio
async def test_agent_domain_service_resumes_paused_session_sandbox_before_reuse():
    class FakeSandbox:
        id = "sandbox-paused"

        def __init__(self):
            self.calls = []

        async def is_paused(self):
            self.calls.append("is_paused")
            return True

        async def resume(self):
            self.calls.append("resume")
            return True

        async def is_available(self):
            self.calls.append("is_available")
            return True

        async def get_browser(self):
            self.calls.append("get_browser")
            return object()

    class FakeRuntime:
        def __init__(self, sandbox):
            self.sandbox = sandbox
            self.assigned = []

        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-paused"
            return self.sandbox

        async def allocate(self, session=None):
            raise AssertionError("existing paused sandbox should be reused")

        async def assign(self, sandbox, session, task_id=None):
            self.assigned.append((sandbox.id, session.id, task_id))

    class FakeTask:
        id = "task-1"
        done = False

        @classmethod
        def create(cls, runner):
            return cls()

    class FakeSessionRepository:
        def __init__(self):
            self.saved = []

        async def save(self, session):
            self.saved.append(session.task_id)

    sandbox = FakeSandbox()
    runtime = FakeRuntime(sandbox)
    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=runtime,
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-paused",
    )

    task = await service._create_task(session)

    assert task.id == "task-1"
    assert sandbox.calls[:3] == ["is_paused", "resume", "is_available"]
    assert "get_browser" in sandbox.calls
    assert runtime.assigned == [("sandbox-paused", "session-1", "task-1")]


@pytest.mark.asyncio
async def test_agent_domain_service_hydrates_replacement_sandbox_from_session_files():
    class FakeSandbox:
        id = "sandbox-new"

        def __init__(self):
            self.uploads = []

        async def get_browser(self):
            return object()

        async def file_upload(self, file_data, path, filename=None):
            self.uploads.append((path, filename, file_data.read()))
            return SimpleNamespace(success=True, message="OK")

    class FakeRuntime:
        def __init__(self, sandbox):
            self.sandbox = sandbox
            self.assigned = []

        async def restore(self, sandbox_id):
            assert sandbox_id == "sandbox-old"
            raise SandboxNotFoundError("No such container")

        async def allocate(self, session=None):
            return self.sandbox

        async def assign(self, sandbox, session, task_id=None):
            self.assigned.append((sandbox.id, session.id, task_id))

    class FakeTask:
        id = "task-1"
        done = False

        @classmethod
        def create(cls, runner):
            return cls()

    class FakeSessionRepository:
        async def save(self, session):
            return None

    class FakeFileStorage:
        async def download_file(self, file_id, user_id=None):
            assert file_id == "minio:file-1"
            assert user_id == "user-1"
            return io.BytesIO(b"restored-data"), FileInfo(
                file_id=file_id,
                filename="AT-CORPUS-B006-test.zip",
                file_path="/home/ubuntu/upload/AT-CORPUS-B006-test.zip",
                content_type="application/zip",
            )

    sandbox = FakeSandbox()
    runtime = FakeRuntime(sandbox)
    service = AgentDomainService(
        agent_repository=object(),
        session_repository=FakeSessionRepository(),
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=FakeFileStorage(),
        mcp_repository=object(),
        sandbox_runtime=runtime,
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-old",
        files=[
            FileInfo(
                file_id="minio:file-1",
                filename="AT-CORPUS-B006-test.zip",
                file_path="/home/ubuntu/upload/AT-CORPUS-B006-test.zip",
                content_type="application/zip",
            )
        ],
    )

    task = await service._create_task(session)

    assert task.id == "task-1"
    assert session.sandbox_id == "sandbox-new"
    assert sandbox.uploads == [
        ("/home/ubuntu/upload/AT-CORPUS-B006-test.zip", "AT-CORPUS-B006-test.zip", b"restored-data")
    ]
    assert runtime.assigned == [("sandbox-new", "session-1", "task-1")]


@pytest.mark.asyncio
async def test_agent_task_runner_pauses_sandbox_on_done_after_browser_cleanup():
    class FakeBrowser:
        def __init__(self):
            self.cleaned = False

        async def cleanup(self):
            self.cleaned = True

    class FakeSandbox:
        id = "sandbox-1"

        def __init__(self):
            self.paused = False

        async def pause(self):
            self.paused = True
            return True

    browser = FakeBrowser()
    sandbox = FakeSandbox()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._browser = browser
    runner._sandbox = sandbox

    await runner.on_done(object())

    assert browser.cleaned is True
    assert sandbox.paused is True


@pytest.mark.asyncio
async def test_agent_service_updates_session_collaborators_owner_only():
    class FakeSessionRepository:
        def __init__(self):
            self.collaborators = []

        async def find_owned_by_id_and_user_id(self, session_id, user_id):
            if user_id != "owner-1":
                return None
            return Session(id=session_id, user_id="owner-1", agent_id="agent-1")

        async def update_collaborators(self, session_id, collaborator_user_ids):
            self.collaborators = collaborator_user_ids

    repository = FakeSessionRepository()
    service = AgentService(
        agent_repository=object(),
        session_repository=repository,
        sandbox_cls=object(),
        task_cls=object(),
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=object(),
    )

    collaborators = await service.update_session_collaborators(
        "session-1",
        "owner-1",
        ["user-2", "owner-1", "user-2", "user-3"],
    )

    assert collaborators == ["user-2", "user-3"]
    assert repository.collaborators == ["user-2", "user-3"]

    with pytest.raises(RuntimeError, match="Session not found"):
        await service.update_session_collaborators("session-1", "user-2", ["user-3"])


@pytest.mark.asyncio
async def test_agent_task_runner_syncs_non_seekable_storage_stream_to_sandbox():
    class NonSeekableStream(io.BytesIO):
        def seek(self, *args, **kwargs):
            raise io.UnsupportedOperation("seek")

    class FakeFileStorage:
        async def download_file(self, file_id, user_id):
            return NonSeekableStream(b"hello"), FileInfo(
                file_id=file_id,
                filename="数据文件.md",
                user_id=user_id,
            )

    class FakeSandbox:
        def __init__(self):
            self.uploaded = None

        async def file_upload(self, file_data, file_path, filename=None):
            self.uploaded = (file_data.read(), file_path, filename)
            return ToolResult(success=True)

    sandbox = FakeSandbox()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._file_storage = FakeFileStorage()
    runner._sandbox = sandbox
    runner._user_id = "user-1"
    runner._agent_id = "agent-1"

    file_info = await runner._sync_file_to_sandbox("minio:file-1")

    assert file_info is not None
    assert file_info.file_path == "/home/ubuntu/upload/数据文件.md"
    assert sandbox.uploaded == (b"hello", "/home/ubuntu/upload/数据文件.md", "数据文件.md")


@pytest.mark.asyncio
async def test_optional_current_user_uses_system_identity():
    user = await get_optional_current_user()

    assert user.id == "anonymous"
    assert user.role == UserRole.ADMIN


@pytest.mark.asyncio
async def test_runtime_restore_uses_allocation_urls_without_local_docker(monkeypatch):
    allocation = SimpleNamespace(
        sandbox_id="sandbox-worker",
        node_id="missing-worker-node",
        status=SandboxAllocationStatus.RUNNING,
        api_url="http://10.0.82.237:49152",
        vnc_url="ws://10.0.82.237:49153",
        cdp_url="http://10.0.82.237:49154",
    )

    class FakeAllocationDocument:
        sandbox_id = "sandbox_id"
        status = "status"

        @classmethod
        async def find_one(cls, *args):
            return allocation

    class FakeExecutionNodeDocument:
        node_id = "node_id"

        @classmethod
        async def find_one(cls, *args):
            return None

    async def explode_local_get(sandbox_id):
        raise AssertionError("restore must not fall back to local Docker when allocation URLs exist")

    monkeypatch.setattr(sandbox_runtime_module, "SandboxAllocationDocument", FakeAllocationDocument)
    monkeypatch.setattr(sandbox_runtime_module, "ExecutionNodeDocument", FakeExecutionNodeDocument)
    monkeypatch.setattr(sandbox_runtime_module.DockerSandbox, "get", explode_local_get)

    sandbox = await sandbox_runtime_module.LocalDockerRuntime().restore("sandbox-worker")

    assert sandbox.id == "sandbox-worker"
    assert sandbox.base_url == "http://10.0.82.237:49152"
    assert sandbox.vnc_url == "ws://10.0.82.237:49153"


@pytest.mark.asyncio
async def test_runtime_restore_discovers_worker_sandbox_when_allocation_missing(monkeypatch):
    worker_node = SimpleNamespace(
        node_id="worker-1",
        type=ExecutionNodeType.WORKER_AGENT,
        enabled=True,
        base_url="http://10.0.82.237:8088",
        credential_ref=None,
        runtime_config={},
    )
    worker_sandbox = sandbox_runtime_module.WorkerAgentSandbox(
        sandbox_id="sandbox-worker",
        api_url="http://10.0.82.237:49152",
        vnc_url="ws://10.0.82.237:49153",
        cdp_url="http://10.0.82.237:49154",
        worker_url="http://10.0.82.237:8088",
    )
    upserts = []

    class FakeAllocationDocument:
        sandbox_id = "sandbox_id"
        status = "status"

        @classmethod
        async def find_one(cls, *args):
            return None

    class FakeQuery:
        async def to_list(self):
            return [worker_node]

    class FakeExecutionNodeDocument:
        enabled = True
        type = "type"

        @classmethod
        def find(cls, *args):
            return FakeQuery()

    async def fake_get_worker_sandbox(node, sandbox_id):
        assert node is worker_node
        assert sandbox_id == "sandbox-worker"
        return worker_sandbox

    async def fake_upsert_allocation(**kwargs):
        upserts.append(kwargs)

    async def explode_local_get(sandbox_id):
        raise AssertionError("restore must not fall back to local Docker when worker has the sandbox")

    monkeypatch.setattr(sandbox_runtime_module, "SandboxAllocationDocument", FakeAllocationDocument)
    monkeypatch.setattr(sandbox_runtime_module, "ExecutionNodeDocument", FakeExecutionNodeDocument)
    monkeypatch.setattr(sandbox_runtime_module, "_get_worker_sandbox", fake_get_worker_sandbox)
    monkeypatch.setattr(sandbox_runtime_module, "_upsert_allocation", fake_upsert_allocation)
    monkeypatch.setattr(sandbox_runtime_module.DockerSandbox, "get", explode_local_get)

    sandbox = await sandbox_runtime_module.LocalDockerRuntime().restore("sandbox-worker")

    assert sandbox is worker_sandbox
    assert upserts[0]["node_id"] == "worker-1"
    assert upserts[0]["status"] == SandboxAllocationStatus.RUNNING


@pytest.mark.asyncio
async def test_runtime_assign_preserves_allocated_worker_node_id(monkeypatch):
    worker_node = SimpleNamespace(
        node_id="worker-237",
        type=ExecutionNodeType.WORKER_AGENT,
        base_url="http://10.0.82.237:8088",
        runtime_config={},
    )
    worker_sandbox = sandbox_runtime_module.WorkerAgentSandbox(
        sandbox_id="sandbox-worker",
        api_url="http://10.0.82.237:49152",
        vnc_url="ws://10.0.82.237:49153",
        cdp_url="http://10.0.82.237:49154",
        worker_url="http://10.0.82.237:8088",
    )
    upserts = []

    async def fake_list_candidates():
        return [worker_node]

    async def fake_create_worker_sandbox(node, session):
        assert node is worker_node
        return worker_sandbox

    async def fake_assign_worker_sandbox(sandbox, session, task_id):
        assert sandbox is worker_sandbox
        return sandbox

    async def fake_upsert_allocation(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(sandbox_runtime_module, "_list_execution_node_candidates", fake_list_candidates)
    monkeypatch.setattr(sandbox_runtime_module, "_create_worker_sandbox", fake_create_worker_sandbox)
    monkeypatch.setattr(sandbox_runtime_module, "_assign_worker_sandbox", fake_assign_worker_sandbox)
    monkeypatch.setattr(sandbox_runtime_module, "_upsert_allocation", fake_upsert_allocation)

    runtime = sandbox_runtime_module.LocalDockerRuntime()
    session = Session(id="session-1", user_id="user-1", agent_id="agent-1")
    sandbox = await runtime.allocate(session)
    await runtime.assign(sandbox, session, task_id="task-1")

    assert upserts[0]["node_id"] == "worker-237"
    assert upserts[1]["node_id"] == "worker-237"
    assert upserts[1]["status"] == SandboxAllocationStatus.RUNNING


@pytest.mark.asyncio
async def test_sandbox_pool_discards_unavailable_warm_sandbox():
    class FakeSandbox:
        def __init__(self, sandbox_id: str, available: bool):
            self._id = sandbox_id
            self.available = available
            self.destroyed = False

        @property
        def id(self):
            return self._id

        async def is_available(self):
            return self.available

        async def destroy(self):
            self.destroyed = True

    pool = SandboxPool(pool_size=1)
    stale = FakeSandbox("stale", False)
    fresh = FakeSandbox("fresh", True)
    await pool._pool.put(stale)

    async def create_and_warm():
        return fresh

    pool._create_and_warm = create_and_warm

    acquired = await pool.acquire()

    assert stale.destroyed is True
    assert acquired is fresh


@pytest.mark.asyncio
async def test_sandbox_pool_replenish_creates_one_sandbox_per_run():
    class FakeSandbox:
        pass

    pool = SandboxPool(pool_size=2)
    attempts = 0
    scheduled = 0

    async def create_and_warm():
        nonlocal attempts
        attempts += 1
        return FakeSandbox()

    def schedule_replenish():
        nonlocal scheduled
        scheduled += 1

    pool._create_and_warm = create_and_warm
    pool.schedule_replenish = schedule_replenish

    await pool._replenish()

    assert attempts == 1
    assert pool._pool.qsize() == 1
    assert scheduled == 1


@pytest.mark.asyncio
async def test_sandbox_pool_replenish_schedules_retry_after_failure():
    pool = SandboxPool(pool_size=2)
    pool._replenish_retry_seconds = 0
    attempts = 0
    scheduled = 0

    async def create_and_warm():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("node temporarily unavailable")

    def schedule_replenish():
        nonlocal scheduled
        scheduled += 1

    pool._create_and_warm = create_and_warm
    pool.schedule_replenish = schedule_replenish

    await pool._replenish()

    assert attempts == 1
    assert pool._pool.qsize() == 0
    assert scheduled == 1


@pytest.mark.asyncio
async def test_sandbox_pool_does_not_start_replenish_when_pool_empty(monkeypatch):
    class FakeSandbox:
        async def is_available(self):
            return True

    pool = SandboxPool(pool_size=1)
    tasks = []

    async def create_and_warm():
        return FakeSandbox()

    def track_background_task(coro):
        tasks.append(coro)
        return SimpleNamespace()

    pool._create_and_warm = create_and_warm
    monkeypatch.setattr(pool, "_track_background_task", track_background_task)

    acquired = await pool.acquire()

    assert isinstance(acquired, FakeSandbox)
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_sandbox_pool_schedule_replenish_deduplicates_active_task():
    pool = SandboxPool(pool_size=2)
    started = 0
    release = asyncio.Event()

    async def replenish():
        nonlocal started
        started += 1
        await release.wait()

    pool._replenish = replenish

    pool.schedule_replenish()
    pool.schedule_replenish()

    await asyncio.sleep(0)

    assert started == 1
    assert len(pool._background_tasks) == 1

    release.set()
    await asyncio.gather(*list(pool._background_tasks))


def test_docker_sandbox_adopts_container_created_before_client_timeout(monkeypatch):
    from app.infrastructure.external.sandbox import docker_sandbox as docker_sandbox_module

    class FakeContainer:
        status = "running"
        attrs = {
            "NetworkSettings": {
                "IPAddress": "",
                "Networks": {
                    "manus-network": {
                        "IPAddress": "172.18.0.99",
                    }
                },
            }
        }

        def reload(self):
            return None

    class FakeContainers:
        def __init__(self):
            self.run_called = False
            self.requested_name = None

        def run(self, **kwargs):
            self.run_called = True
            self.requested_name = kwargs["name"]
            raise TimeoutError("read timed out")

        def get(self, name):
            assert name == self.requested_name
            return FakeContainer()

    class FakeDockerClient:
        def __init__(self):
            self.containers = FakeContainers()
            self.closed = False

        def close(self):
            self.closed = True

    fake_client = FakeDockerClient()
    captured_timeout = None

    def fake_from_env(timeout=None):
        nonlocal captured_timeout
        captured_timeout = timeout
        return fake_client

    monkeypatch.setattr(docker_sandbox_module.docker, "from_env", fake_from_env)
    monkeypatch.setattr(
        docker_sandbox_module,
        "get_settings",
        lambda: SimpleNamespace(
            sandbox_image="simpleyyt/manus-sandbox",
            sandbox_name_prefix="sandbox",
            sandbox_ttl_minutes=30,
            sandbox_chrome_args="",
            sandbox_https_proxy=None,
            sandbox_http_proxy=None,
            sandbox_no_proxy=None,
            sandbox_network="manus-network",
            sandbox_docker_create_timeout_seconds=75,
        ),
    )
    monkeypatch.setattr(docker_sandbox_module.uuid, "uuid4", lambda: "12345678-timeout-test")

    sandbox = docker_sandbox_module.DockerSandbox._create_task()

    assert sandbox.id == "sandbox-12345678"
    assert sandbox.ip == "172.18.0.99"
    assert captured_timeout == 75
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_local_node_health_separates_warm_running_from_paused_sandboxes(monkeypatch):
    records = [
        SimpleNamespace(container_name="sandbox-warm", status="warm"),
        SimpleNamespace(container_name="sandbox-assigned", status="assigned"),
        SimpleNamespace(container_name="sandbox-paused-record", status="paused"),
        SimpleNamespace(container_name="sandbox-paused-docker", status="assigned"),
    ]

    class FakeQuery:
        async def to_list(self):
            return records

    class FakeSandboxRecordDocument:
        @classmethod
        def find(cls, *args):
            return FakeQuery()

    def fake_container_states():
        return {
            "sandbox-warm": "running",
            "sandbox-assigned": "running",
            "sandbox-paused-record": "paused",
            "sandbox-paused-docker": "paused",
        }

    monkeypatch.setattr(node_health_module, "SandboxRecordDocument", FakeSandboxRecordDocument)
    monkeypatch.setattr(node_health_module, "_local_sandbox_container_states", fake_container_states)
    async def no_op_reconcile(**_kwargs):
        return None

    monkeypatch.setattr(
        node_health_module,
        "_reconcile_local_sandbox_lifecycle",
        no_op_reconcile,
    )
    monkeypatch.setattr(
        node_health_module,
        "_host_metrics",
        lambda: {
            "cpu_percent": 1.0,
            "cpu_cores": 8,
            "memory_used_bytes": 1024,
            "disk_used_bytes": 2048,
            "memory_total_bytes": 4096,
            "memory_available_bytes": 3072,
            "disk_total_bytes": 8192,
            "disk_free_bytes": 6144,
            "load_average": (0.1, 0.2, 0.3),
        },
    )
    doc = SimpleNamespace(
        node_id="local-default",
        capacity=ExecutionNodeCapacity(max_sandboxes=10),
        enabled=True,
        status=ExecutionNodeStatus.UNKNOWN,
        last_heartbeat_at=None,
        health=None,
    )

    await node_health_module._check_local_docker_node(doc)

    # Capacity follows Docker reality, including warm or temporarily untracked
    # running containers, instead of trusting only assigned database records.
    assert doc.health.running_sandboxes == 2
    assert doc.health.warm_sandboxes == 1
    assert doc.health.assigned_sandboxes == 1
    assert doc.health.paused_sandboxes == 2


@pytest.mark.asyncio
async def test_node_monitor_destroys_expired_paused_sandboxes(monkeypatch):
    now = datetime.now(UTC)

    class FakeRecord:
        def __init__(self, name, last_used_at):
            self.container_name = name
            self.status = "paused"
            self.last_used_at = last_used_at
            self.paused_at = last_used_at
            self.assigned_at = None
            self.created_at = last_used_at
            self.destroyed_at = None
            self.saved = False

        async def save(self):
            self.saved = True

    # MongoDB may deserialize legacy UTC values without tzinfo.  The monitor
    # must still compare and retire them instead of aborting the entire tick.
    expired = FakeRecord(
        "sandbox-expired",
        (now - timedelta(minutes=31)).replace(tzinfo=None),
    )
    fresh = FakeRecord("sandbox-fresh", now - timedelta(minutes=5))

    class FakeQuery:
        async def to_list(self):
            return [expired, fresh]

    class FakeSandboxRecordDocument:
        status = "status"

        @classmethod
        def find(cls, *args):
            return FakeQuery()

    class FakeAllocation:
        def __init__(self, sandbox_id):
            self.sandbox_id = sandbox_id
            self.status = SandboxAllocationStatus.PAUSED
            self.updated_at = now
            self.saved = False

        async def save(self):
            self.saved = True

    expired_allocation = FakeAllocation("sandbox-expired")
    fresh_allocation = FakeAllocation("sandbox-fresh")

    class AllocationQuery:
        async def to_list(self):
            return [expired_allocation, fresh_allocation]

    class FakeSandboxAllocationDocument:
        node_id = "node_id"
        sandbox_id = "sandbox_id"
        status = "status"

        @classmethod
        def find(cls, *args):
            return AllocationQuery()

    class FakeContainer:
        def __init__(self, name):
            self.name = name
            self.removed = False

        def remove(self, force=False):
            self.removed = force

    removed = {}

    class FakeContainers:
        def get(self, name):
            container = FakeContainer(name)
            removed[name] = container
            return container

    class FakeDockerClient:
        containers = FakeContainers()

        def close(self):
            return None

    monkeypatch.setattr(node_monitor_module, "SandboxRecordDocument", FakeSandboxRecordDocument)
    monkeypatch.setattr(
        node_monitor_module,
        "SandboxAllocationDocument",
        FakeSandboxAllocationDocument,
    )
    monkeypatch.setattr(node_monitor_module.docker, "from_env", lambda: FakeDockerClient())
    cleared = []

    async def fake_clear(sandbox_ids, **_kwargs):
        cleared.extend(sandbox_ids)

    monkeypatch.setattr(
        node_monitor_module,
        "clear_session_sandbox_references",
        fake_clear,
    )

    monitor = node_monitor_module.ExecutionNodeMonitor()
    node = SimpleNamespace(
        type=ExecutionNodeType.LOCAL_DOCKER,
        runtime_config={"paused_sandbox_destroy_after_minutes": 30},
    )

    await monitor._destroy_expired_paused_sandboxes(node)

    assert expired.status == "destroyed"
    assert expired.destroyed_at is not None
    assert expired.saved is True
    assert removed["sandbox-expired"].removed is True
    assert expired_allocation.status == SandboxAllocationStatus.RELEASED
    assert expired_allocation.saved is True
    assert cleared == ["sandbox-expired"]
    assert fresh.status == "paused"
    assert "sandbox-fresh" not in removed


@pytest.mark.asyncio
async def test_execution_node_selector_prefers_available_remote_node(monkeypatch):
    local_full = SimpleNamespace(
        node_id="local-default",
        type=ExecutionNodeType.LOCAL_DOCKER,
        status=ExecutionNodeStatus.HEALTHY,
        enabled=True,
        capacity=ExecutionNodeCapacity(max_sandboxes=1),
        health=ExecutionNodeHealth(running_sandboxes=1),
    )
    remote_available = SimpleNamespace(
        node_id="remote-1",
        type=ExecutionNodeType.REMOTE_DOCKER,
        status=ExecutionNodeStatus.HEALTHY,
        enabled=True,
        capacity=ExecutionNodeCapacity(max_sandboxes=3),
        health=ExecutionNodeHealth(running_sandboxes=1),
    )

    class FakeQuery:
        async def to_list(self):
            return [local_full, remote_available]

    class FakeExecutionNodeDocument:
        enabled = True

        @classmethod
        def find(cls, *args):
            return FakeQuery()

    async def no_op():
        return None

    async def no_op_check(node):
        return None

    monkeypatch.setattr(sandbox_runtime_module, "_ensure_local_default_node", no_op)
    monkeypatch.setattr(sandbox_runtime_module, "check_execution_node", no_op_check)
    monkeypatch.setattr(sandbox_runtime_module, "ExecutionNodeDocument", FakeExecutionNodeDocument)

    selected = await sandbox_runtime_module._select_execution_node()

    assert selected.node_id == "remote-1"


def test_agent_message_content_normalization_handles_none_and_non_string():
    normalize = BaseAgent._message_content_to_text

    assert normalize(None, None) == ""
    assert normalize(None, "skills") == "skills"
    assert normalize(None, [{"type": "text", "text": "skills"}]) == "[{'type': 'text', 'text': 'skills'}]"


def test_mcp_toolkit_exposes_builtin_list_tool_without_selected_servers():
    toolkit = MCPToolkit()

    tool_names = [tool.name for tool in toolkit.get_tools()]
    mcp_list_tool = toolkit.get_tool("mcp_list_tools")
    skill_toolkit = SkillToolkit(SkillRegistry("missing-skills-dir"))
    skill_list_tool = skill_toolkit.get_tool("skill_list")

    assert "mcp_list_tools" in tool_names
    assert mcp_list_tool is not None
    assert "not MCP servers and not MCP tools" in skill_list_tool.description


@pytest.mark.asyncio
async def test_worker_credential_ref_resolves_from_current_process_env(monkeypatch):
    monkeypatch.setenv("WORKER_TEST_NODE_TOKEN", "secret-token")
    async def no_stored_credential(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.node_health.NodeCredentialDocument.find_one",
        no_stored_credential,
    )
    node = SimpleNamespace(
        auth_type=ExecutionNodeAuthType.BEARER,
        credential_ref="WORKER_TEST_NODE_TOKEN",
    )

    assert await execution_node_auth_headers(node) == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_worker_credential_ref_prefers_persisted_node_credential(monkeypatch):
    monkeypatch.setenv("WORKER_TEST_NODE_TOKEN", "env-token")

    async def stored_credential(*args, **kwargs):
        return SimpleNamespace(secret_value="stored-token")

    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.node_health.NodeCredentialDocument.find_one",
        stored_credential,
    )
    node = SimpleNamespace(
        auth_type=ExecutionNodeAuthType.BEARER,
        credential_ref="WORKER_TEST_NODE_TOKEN",
    )

    assert await execution_node_auth_headers(node) == {"Authorization": "Bearer stored-token"}


@pytest.mark.asyncio
async def test_worker_health_updates_usage_and_capacity(monkeypatch):
    async def fake_headers(doc):
        return {"Authorization": "Bearer token"}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status": "ok",
                "usage": {
                    "running_sandboxes": 2,
                    "cpu_percent": 37.5,
                    "cpu_cores": 8,
                    "memory_used_bytes": 3_221_225_472,
                    "memory_total_bytes": 17_179_869_184,
                    "disk_used_bytes": 42_949_672_960,
                    "disk_total_bytes": 274_877_906_944,
                },
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            assert url == "http://worker:8088/health"
            assert headers == {"Authorization": "Bearer token"}
            return FakeResponse()

    monkeypatch.setattr(node_health_module, "execution_node_auth_headers", fake_headers)
    monkeypatch.setattr(node_health_module.httpx, "AsyncClient", FakeAsyncClient)
    node = SimpleNamespace(
        base_url="http://worker:8088",
        auth_type=ExecutionNodeAuthType.BEARER,
        enabled=True,
        capacity=ExecutionNodeCapacity(max_sandboxes=4),
    )

    await node_health_module._check_worker_agent_node(node)

    assert node.status == ExecutionNodeStatus.HEALTHY
    assert node.health.running_sandboxes == 2
    assert node.health.cpu_percent == 37.5
    assert node.health.memory_used_bytes == 3_221_225_472
    assert node.health.disk_used_bytes == 42_949_672_960
    assert node.capacity.cpu_cores == 8
    assert node.capacity.memory_bytes == 17_179_869_184
    assert node.capacity.disk_bytes == 274_877_906_944


def test_software_role_default_token_quota_is_unlimited():
    from app.domain.services.token_quota_service import DEFAULT_ROLE_TOKEN_QUOTAS

    assert DEFAULT_ROLE_TOKEN_QUOTAS[UserRole.SOFTWARE]["initial_tokens"] is None
    assert DEFAULT_ROLE_TOKEN_QUOTAS[UserRole.SOFTWARE]["daily_refill_tokens"] is None

@pytest.mark.asyncio
async def test_worker_assign_falls_back_when_worker_lacks_assign_endpoint(monkeypatch):
    sandbox = WorkerAgentSandbox(
        sandbox_id="sandbox-old-worker",
        api_url="http://10.0.82.237:49152",
        vnc_url="ws://10.0.82.237:49153",
        cdp_url="http://10.0.82.237:49154",
        worker_url="http://worker:8088",
        headers={"Authorization": "Bearer token"},
    )
    session = SimpleNamespace(id="session-1")

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("raise_for_status should not be called for fallback response")

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            assert url == "http://worker:8088/sandboxes/sandbox-old-worker/assign"
            return FakeResponse(404, {"detail": "not found"})

        async def get(self, url, headers):
            assert url == "http://worker:8088/sandboxes/sandbox-old-worker"
            return FakeResponse(
                200,
                {
                    "id": "sandbox-old-worker",
                    "ip": "172.18.0.10",
                    "api_url": "http://10.0.82.237:49152",
                    "vnc_url": "ws://10.0.82.237:49153",
                    "cdp_url": "http://10.0.82.237:49154",
                    "status": "running",
                    "session_id": None,
                    "task_id": None,
                    "created_at": "2026-06-17T08:00:00+00:00",
                },
            )

    monkeypatch.setattr(sandbox_runtime_module.httpx, "AsyncClient", FakeAsyncClient)

    assigned = await sandbox_runtime_module._assign_worker_sandbox(sandbox, session, "task-1")

    assert assigned.id == "sandbox-old-worker"
    assert assigned.base_url == "http://10.0.82.237:49152"


@pytest.mark.asyncio
async def test_mcp_list_tools_reports_no_servers_when_none_selected():
    toolkit = MCPToolkit()
    available_config = MCPConfig(
        mcpServers={
            "demo": MCPServerConfig(
                transport=MCPTransport.STDIO,
                command="demo-command",
                description="Demo MCP server",
            )
        }
    )
    await toolkit.initialized(MCPConfig(mcpServers={}), available_config=available_config)

    result = (await toolkit.get_tool("mcp_list_tools").ainvoke({"id": "1", "args": {}})).artifact

    assert result.success is True
    assert result.data["servers"] == []
    assert result.data["tools"] == []


@pytest.mark.asyncio
async def test_mcp_list_tools_reports_only_selected_servers():
    toolkit = MCPToolkit()
    selected_config = MCPConfig(
        mcpServers={
            "selected": MCPServerConfig(
                transport=MCPTransport.STDIO,
                command="selected-command",
                description="Selected MCP server",
            )
        }
    )
    available_config = MCPConfig(
        mcpServers={
            "selected": selected_config.mcpServers["selected"],
            "unselected": MCPServerConfig(
                transport=MCPTransport.STDIO,
                command="unselected-command",
                description="Unselected MCP server",
            ),
        }
    )
    await toolkit.initialized(selected_config, available_config=available_config)

    result = (await toolkit.get_tool("mcp_list_tools").ainvoke({"id": "1", "args": {}})).artifact

    assert result.success is True
    assert [server["name"] for server in result.data["servers"]] == ["selected"]
    assert result.data["tools"] == []


@pytest.mark.asyncio
async def test_message_event_metadata_survives_sse_mapping():
    event = MessageEvent(
        message="test",
        metadata={"skills": ["demo"], "mcp_servers": ["mcp_demo"]},
    )

    sse_event = await EventMapper.event_to_sse_event(event)

    assert sse_event.event == "message"
    assert sse_event.data.metadata["skills"] == ["demo"]
    assert sse_event.data.metadata["mcp_servers"] == ["mcp_demo"]


def test_session_skill_generation_creates_real_referenced_resources():
    events = [
        MessageEvent(role="user", message="Analyze CSV revenue"),
        ToolEvent(
            tool_call_id="1",
            tool_name="shell",
            function_name="shell_exec",
            function_args={"command": "python analyze.py revenue.csv"},
            status=ToolStatus.CALLED,
        ),
        MessageEvent(role="assistant", message="Revenue analysis complete."),
    ]

    references = build_reference_files(events)
    scripts = build_script_files(events)
    skill_content = build_skill_content_from_events(events, references, scripts)

    assert "references/session-summary.md" in references
    assert "references/final-output.md" in references
    assert "scripts/replay-commands.sh" in scripts
    assert "`references/session-summary.md`" in skill_content
    assert "`scripts/replay-commands.sh`" in skill_content


def test_session_skill_name_uses_task_plan_not_skill_create_request():
    events = [
        MessageEvent(role="user", message="分析 Landsat 8 OLI 数据并计算 NDVI"),
        PlanEvent(
            status=PlanStatus.COMPLETED,
            plan=Plan(
                title="Landsat 8 OLI NDVI分析",
                goal="分析用户上传的Landsat 8 OLI数据产品，计算并分析NDVI数值",
            ),
        ),
        MessageEvent(role="user", message="使用/skill-create将这个流程替保存为可复用的技能"),
        PlanEvent(
            status=PlanStatus.COMPLETED,
            plan=Plan(
                title="Save Task Skill",
                goal="Create a skill from the current session",
            ),
        ),
    ]

    name, description, triggers = _derive_skill_metadata(events)

    assert name != "save-task-skill"
    assert name == "landsat-8-oli-ndvi"
    assert "NDVI" in description
    assert "Landsat" in triggers


@pytest.mark.asyncio
async def test_skill_toolkit_creates_private_skill_from_current_session(tmp_path):
    class FakeSessionRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return Session(id=session_id, user_id=user_id, agent_id="agent-1")

        async def get_events(self, session_id):
            return [
                MessageEvent(role="user", message="Analyze CSV revenue"),
                PlanEvent(
                    status=PlanStatus.COMPLETED,
                    plan=Plan(
                        title="CSV Revenue Analysis",
                        goal="Analyze CSV revenue and produce a reusable report",
                    ),
                ),
                ToolEvent(
                    tool_call_id="1",
                    tool_name="shell",
                    function_name="shell_exec",
                    function_args={"command": "python analyze.py revenue.csv"},
                    status=ToolStatus.CALLED,
                ),
                MessageEvent(role="assistant", message="Revenue analysis complete."),
                MessageEvent(role="user", message="/skill-create save this workflow"),
            ]

    registry = SkillRegistry(str(tmp_path / "skills"), user_id="user-1")
    toolkit = SkillToolkit(
        registry,
        session_id="session-1",
        user_id="user-1",
        session_repository=FakeSessionRepository(),
    )

    result = (await toolkit.get_tool("skill_create_from_session").ainvoke({"id": "1", "args": {}})).artifact

    assert result.success is True
    assert result.data["scope"] == "user"
    assert result.data["user_id"] == "user-1"
    assert result.data["created_from_session_id"] == "session-1"
    assert result.data["name"] == "csv-revenue-analysis"
    skill_dir = tmp_path / "skills" / "users" / "user-1" / result.data["name"]
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "references" / "session-summary.md").exists()
    assert (skill_dir / "scripts" / "replay-commands.sh").exists()


@pytest.mark.asyncio
async def test_approval_decision_enables_and_disables_mcp_server(monkeypatch):
    saved_configs = []

    class FakeMCPRepository:
        async def get_mcp_config(self):
            return MCPConfig(
                mcpServers={
                    "sensitive-db": MCPServerConfig(
                        transport=MCPTransport.SSE,
                        url="http://mcp.local/sse",
                        enabled=False,
                        risk_level=MCPRiskLevel.SENSITIVE,
                    )
                }
            )

        async def save_mcp_config(self, config):
            saved_configs.append(config)

    monkeypatch.setattr(
        "app.domain.services.approval_service.MongoMCPRepository",
        FakeMCPRepository,
    )

    service = ApprovalService()
    approval = ApprovalRequest(
        requester_user_id="user-1",
        resource_type="mcp_server",
        resource_id="sensitive-db",
        requested_permissions=["mcp.use"],
        status=ApprovalStatus.APPROVED,
    )

    await service._apply_decision_effects(approval)

    assert saved_configs[-1].mcpServers["sensitive-db"].enabled is True

    approval.status = ApprovalStatus.REJECTED
    await service._apply_decision_effects(approval)

    assert saved_configs[-1].mcpServers["sensitive-db"].enabled is False


def test_token_usage_service_extracts_usage_without_touching_message_content():
    message = AIMessage(
        content="hello",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    )

    usage = TokenUsageService().extract_usage(message)

    assert message.content == "hello"
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_large_upload_routes_use_independent_upload_flow(monkeypatch):
    class FakeLargeUploadService:
        def __init__(self):
            self.completed_parts = None

        async def init_large_upload(self, filename, size, user_id, content_type=None, metadata=None):
            assert user_id == "user-1"
            return SimpleNamespace(
                upload_id="upload-1",
                file_id="minio:file-1",
                filename=filename,
                size=size,
                part_size=16,
                status="initiated",
                expires_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def upload_large_upload_part(self, upload_id, part_number, user_id, data):
            assert upload_id == "upload-1"
            assert part_number == 2
            assert user_id == "user-1"
            assert data == b"part-data"
            return "etag-2"

        async def complete_large_upload(self, upload_id, parts, user_id):
            self.completed_parts = parts
            return FileInfo(
                file_id="minio:file-1",
                filename="big.bin",
                content_type="application/octet-stream",
                size=32,
                upload_date=datetime(2026, 1, 1, tzinfo=UTC),
                metadata={"uploadMode": "multipart"},
            )

        async def create_signed_url(self, file_id, user_id=None, expire_minutes=30):
            return f"/signed/{file_id}"

    service = FakeLargeUploadService()
    monkeypatch.setattr("app.interfaces.dependencies.get_file_service", lambda: service)
    user = User(id="user-1", email="user@example.com", fullname="User One", role=UserRole.USER)

    init_response = await file_routes.init_large_upload(
        LargeUploadInitRequest(filename="big.bin", size=32, content_type="application/octet-stream"),
        file_service=service,
        current_user=user,
    )
    async def receive_part_data():
        return {"type": "http.request", "body": b"part-data", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/files/large-uploads/upload-1/parts/2",
            "headers": [],
        },
        receive=receive_part_data,
    )
    part_response = await file_routes.upload_large_upload_part(
        "upload-1",
        2,
        request,
        file_service=service,
        current_user=user,
    )
    complete_response = await file_routes.complete_large_upload(
        "upload-1",
        LargeUploadCompleteRequest(parts=[LargeUploadPart(part_number=2, etag='"etag-2"', size=16)]),
        file_service=service,
        current_user=user,
    )

    assert init_response.data.upload_id == "upload-1"
    assert init_response.data.part_size == 16
    assert part_response.data.etag == "etag-2"
    assert part_response.data.size == len(b"part-data")
    assert complete_response.data.file_id == "minio:file-1"
    assert service.completed_parts == [{"part_number": 2, "etag": '"etag-2"', "size": 16}]


@pytest.mark.asyncio
async def test_file_service_rejects_large_upload_without_minio_capable_storage():
    service = FileService(file_storage=object())

    with pytest.raises(RuntimeError, match="Large file upload requires MinIO"):
        await service.init_large_upload("big.bin", 1024, "user-1")


@pytest.mark.asyncio
async def test_large_upload_accepts_naive_expiration_datetime(monkeypatch):
    class FakeLargeUploadStorage:
        async def init_large_upload(self, *args, **kwargs):
            raise NotImplementedError

        async def upload_large_upload_part(self, session, part_number, data):
            assert session.upload_id == "upload-naive"
            assert part_number == 1
            assert data == b"hello"
            return "etag-naive"

        async def complete_large_upload(self, *args, **kwargs):
            raise NotImplementedError

        async def abort_large_upload(self, *args, **kwargs):
            raise NotImplementedError

    session = SimpleNamespace(
        upload_id="upload-naive",
        user_id="user-1",
        status="initiated",
        expires_at=datetime(2099, 1, 1),
        part_size=16,
        updated_at=None,
        save=lambda: None,
    )

    async def fake_find_one(*args, **kwargs):
        return session

    async def fake_save():
        return None

    class FakeField:
        def __eq__(self, other):
            return ("upload_id", other)

    class FakeUploadSessionDocument:
        upload_id = FakeField()

        @staticmethod
        async def find_one(*args, **kwargs):
            return await fake_find_one(*args, **kwargs)

    session.save = fake_save
    monkeypatch.setattr(
        "app.application.services.file_service.FileUploadSessionDocument",
        FakeUploadSessionDocument,
    )

    service = FileService(file_storage=FakeLargeUploadStorage())
    etag = await service.upload_large_upload_part("upload-naive", 1, "user-1", b"hello")

    assert etag == "etag-naive"
    assert session.status == "uploading"
