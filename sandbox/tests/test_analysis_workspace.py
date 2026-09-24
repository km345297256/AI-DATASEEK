"""Platform initialization is independent of a program's dataset and paths."""
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BadRequestException
from app.services import analysis_workspace
from app.services.analysis_workspace import prepare_analysis_workspace
from app.services.shell import ShellService
from conftest import BASE_URL


def test_creates_only_output_and_preserves_existing_results(tmp_path):
    root = tmp_path.resolve()
    prepare_analysis_workspace(workspace_root=root)
    assert set(root.iterdir()) == {root / "output"}
    result = root / "output" / "existing.csv"
    result.write_text("x\n1\n")
    before = result.stat()
    prepare_analysis_workspace(workspace_root=root)
    assert result.read_text() == "x\n1\n"
    assert result.stat().st_mtime_ns == before.st_mtime_ns
    assert list((root / "output").iterdir()) == [result]


@pytest.mark.parametrize("obstacle", ["symlink", "dangling", "file", "fifo"])
def test_output_cannot_redirect_creation_into_dataset(tmp_path, obstacle):
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    dataset = tmp_path.resolve() / "dataset"
    dataset.mkdir()
    source = dataset / "observations.csv"
    source.write_text("source content")
    output = root / "output"
    if obstacle == "symlink":
        output.symlink_to(dataset, target_is_directory=True)
    elif obstacle == "dangling":
        output.symlink_to(dataset / "absent", target_is_directory=True)
    elif obstacle == "file":
        output.write_text("keep this file")
    else:
        os.mkfifo(output)
    with pytest.raises(BadRequestException, match="workspace is not safely writable"):
        prepare_analysis_workspace(workspace_root=root)
    assert source.read_text() == "source content"
    assert list(dataset.iterdir()) == [source]
    if obstacle == "file":
        assert output.read_text() == "keep this file"


def test_workspace_ancestors_are_not_created_or_followed(tmp_path):
    root = tmp_path.resolve()
    missing = root / "missing" / "nested"
    with pytest.raises(BadRequestException):
        prepare_analysis_workspace(workspace_root=missing)
    assert not (root / "missing").exists()
    target = root / "data"
    target.mkdir()
    linked = root / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(BadRequestException):
        prepare_analysis_workspace(workspace_root=linked)
    assert not (target / "output").exists()


@pytest.mark.parametrize("root", [Path("/"), Path("relative"), Path("/home/ubuntu/../datasets")])
def test_invalid_workspace_fails_without_creating_anything(root):
    with pytest.raises(BadRequestException):
        prepare_analysis_workspace(workspace_root=root)


def test_failed_write_probe_is_cleaned_up_without_exposing_paths(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    def write(*args):
        raise OSError("sensitive host details")
    monkeypatch.setattr(analysis_workspace.os, "write", write)
    with pytest.raises(BadRequestException) as failure:
        prepare_analysis_workspace(workspace_root=root)
    assert "sensitive" not in str(failure.value)
    assert list((root / "output").iterdir()) == []


@pytest.mark.asyncio
async def test_program_can_write_output_on_first_launch(isolated_analysis_workspace, tmp_path):
    destination = isolated_analysis_workspace / "output" / "summary.txt"
    script = tmp_path / "extract.py"
    script.write_text(f"from pathlib import Path\nPath({str(destination)!r}).write_text('extracted')\n")
    service = ShellService()
    process = await service._create_program_process(str(tmp_path), str(script), [])
    try:
        await process.communicate()
        assert process.returncode == 0
        assert destination.read_text() == "extracted"
    finally:
        process._dataseek_program_diagnostics.close()


@pytest.mark.asyncio
async def test_shell_launch_also_prepares_output(isolated_analysis_workspace, tmp_path):
    destination = isolated_analysis_workspace / "output" / "from-shell.txt"
    service = ShellService()
    process = await service._create_process(f"printf ready > '{destination}'", str(tmp_path))
    await process.communicate()
    assert process.returncode == 0
    assert destination.read_text() == "ready"


@pytest.mark.asyncio
async def test_program_missing_input_is_not_auto_created(isolated_analysis_workspace, tmp_path):
    source = tmp_path / "dataset" / "missing.csv"
    script = tmp_path / "read.py"
    script.write_text(f"from pathlib import Path\nPath({str(source)!r}).read_text()\n")
    service = ShellService()
    process = await service._create_program_process(str(tmp_path), str(script), [])
    try:
        await process.communicate()
        assert process.returncode != 0
        assert not source.parent.exists()
        assert (isolated_analysis_workspace / "output").is_dir()
    finally:
        process._dataseek_program_diagnostics.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["program", "shell"])
async def test_unsafe_workspace_rejected_before_process_creation(isolated_analysis_workspace, tmp_path, monkeypatch, kind):
    import app.services.shell as shell
    output = isolated_analysis_workspace / "output"
    output.symlink_to(tmp_path.resolve(), target_is_directory=True)
    spawn = AsyncMock()
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", spawn)
    service = ShellService()
    with pytest.raises(BadRequestException):
        if kind == "program":
            await service._create_program_process(str(tmp_path), str(tmp_path / "absent.py"), [])
        else:
            await service._create_process("true", str(tmp_path))
    spawn.assert_not_called()


@pytest.mark.parametrize("kind", ["program", "shell"])
def test_first_api_execution_prepares_output_before_cwd_check(client, isolated_analysis_workspace, tmp_path, kind):
    output = isolated_analysis_workspace / "output"
    script = tmp_path / "first.py"
    script.write_text("from pathlib import Path\nPath('summary.txt').write_text('ready')\n")
    identity = uuid.uuid4().hex
    payload = {"id": identity, "exec_dir": str(output), "operation_id": identity}
    if kind == "program":
        endpoint = "program"
        payload.update(script_path=str(script), args=[])
    else:
        endpoint = "exec"
        payload["command"] = "printf ready > summary.txt"
    assert not output.exists()
    result = client.post(f"{BASE_URL}/api/v1/shell/{endpoint}", json=payload)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["success"] is True
    assert body["data"]["returncode"] == 0
    assert (output / "summary.txt").read_text() == "ready"


def test_missing_arbitrary_api_cwd_is_not_created(client, isolated_analysis_workspace, tmp_path):
    cwd = tmp_path / "missing-input" / "nested"
    identity = uuid.uuid4().hex
    result = client.post(f"{BASE_URL}/api/v1/shell/exec", json={
        "id": identity, "exec_dir": str(cwd), "command": "true", "operation_id": identity,
    })
    assert result.status_code == 400
    assert not cwd.parent.exists()
    assert (isolated_analysis_workspace / "output").is_dir()
    from app.services.shell import shell_service
    record = shell_service._execution_operations[identity]
    assert record.state == "not_started" and not record.creation_attempted


def test_workspace_preparation_failure_records_no_process_start(client, isolated_analysis_workspace, tmp_path):
    (isolated_analysis_workspace / "output").symlink_to(tmp_path, target_is_directory=True)
    identity = uuid.uuid4().hex
    result = client.post(f"{BASE_URL}/api/v1/shell/exec", json={
        "id": identity, "exec_dir": str(tmp_path), "command": "true", "operation_id": identity,
    })
    assert result.status_code == 400
    from app.services.shell import shell_service
    record = shell_service._execution_operations[identity]
    assert record.state == "not_started" and not record.creation_attempted
