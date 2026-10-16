"""Host-backed notebook storage must preserve ownership and mount boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import docker
import pytest

from app.application.services import jupyter_service as module
from app.application.services.jupyter_service import JupyterService


class Volumes:
    def __init__(self, volume=None):
        self.volume = volume
        self.created = []

    def get(self, name):
        if self.volume is None:
            raise docker.errors.NotFound("missing")
        return self.volume

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.volume = SimpleNamespace(attrs={
            "Labels": kwargs["labels"], "Driver": kwargs.get("driver", "local"),
            "Options": kwargs.get("driver_opts"),
        }, remove=Mock())
        return self.volume


def service(tmp_path, *, host_root="/data2/state/jupyter", hostfs=""):
    result = JupyterService()
    result._settings = SimpleNamespace(
        jupyter_workspaces_host_root=host_root,
        jupyter_workspaces_storage_root=str(tmp_path.resolve()),
        dataset_docker_host_root=hostfs,
    )
    return result


def owned_volume(options, **labels):
    return SimpleNamespace(attrs={
        "Labels": {"ai-dataseek.session_id": "session-a", "ai-dataseek.kind": "jupyter-work", **labels},
        "Driver": "local", "Options": options,
    }, remove=Mock())


def test_new_host_workspace_is_private_and_snap_mapped(tmp_path):
    notebook = service(tmp_path, hostfs="/var/lib/snapd/hostfs")
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert (tmp_path / "session-a").is_dir()
    assert json.loads((tmp_path / ".owner-session-a.json").read_text()) == {
        "session_id": "session-a", "user_id": "user-a"}
    assert volumes.created[0]["driver_opts"] == {
        "type": "none", "o": "bind", "device": "/var/lib/snapd/hostfs/data2/state/jupyter/session-a"}
    assert volumes.created[0]["name"] == "ai-dataseek-jupyter-work-session-a"
    assert volumes.created[0]["labels"]["ai-dataseek.user_id"] == "user-a"


def test_default_mode_does_not_touch_host_storage(tmp_path):
    notebook = service(tmp_path, host_root="")
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert "driver_opts" not in volumes.created[0]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("session", ["../escape", "a/b", "/root", "..", "", "a\n"])
def test_invalid_session_cannot_make_directory_or_volume(tmp_path, session):
    volumes = Volumes()
    with pytest.raises(RuntimeError, match="session identity"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=volumes), session, "user-a")
    assert not volumes.created
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("options", [None, {"type": "none", "o": "bind", "device": "/old/session-a"}])
def test_existing_same_name_volume_must_actually_be_migrated(tmp_path, options):
    volumes = Volumes(owned_volume(options))
    with pytest.raises(RuntimeError, match="storage mismatch"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert list(tmp_path.iterdir()) == []
    assert not volumes.created


def test_valid_migrated_workspace_preserves_notebook_and_adopts_owner_record(tmp_path):
    notebook = service(tmp_path)
    directory = tmp_path / "session-a"
    directory.mkdir()
    (directory / "DataSeek.ipynb").write_text("saved notebook")
    volumes = Volumes(owned_volume(notebook._workspace_options("session-a")))
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert (directory / "DataSeek.ipynb").read_text() == "saved notebook"
    assert not volumes.created


def test_unowned_existing_directory_is_not_adopted(tmp_path):
    (tmp_path / "session-a").mkdir()
    with pytest.raises(RuntimeError, match="Unowned"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")


def test_archived_workspace_reopens_only_for_original_user(tmp_path):
    notebook = service(tmp_path)
    notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")
    saved = tmp_path / "session-a" / "saved.txt"
    saved.write_text("preserved")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-b")
    notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")
    assert saved.read_text() == "preserved"


@pytest.mark.parametrize("location", ["root", "session", "owner"])
def test_symlinks_cannot_escape_workspace_root_or_leak_paths(tmp_path, location):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "workspaces"
    if location == "root":
        root.symlink_to(external, target_is_directory=True)
    else:
        root.mkdir()
        if location == "session":
            (root / "session-a").symlink_to(external, target_is_directory=True)
        else:
            (external / "owner.json").write_text(json.dumps({"session_id": "session-a", "user_id": "user-a"}))
            (root / ".owner-session-a.json").symlink_to(external / "owner.json")
    notebook = service(root)
    # Preserve the requested symlink spelling to verify each ancestor is checked.
    notebook._settings.jupyter_workspaces_storage_root = str(root)
    volumes = Volumes(owned_volume(notebook._workspace_options("session-a")))
    with pytest.raises(RuntimeError) as error:
        notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert str(tmp_path) not in str(error.value)
    assert not (external / "session-a").exists()
    assert not volumes.created


def test_successful_create_does_not_adopt_racing_foreign_volume(tmp_path):
    volumes = Volumes()
    volumes.create = Mock(return_value=owned_volume(None, **{"ai-dataseek.session_id": "someone-else"}))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        service(tmp_path, host_root="")._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")


@pytest.mark.asyncio
async def test_cleanup_retains_host_archive_and_never_follows_child_symlink(tmp_path, monkeypatch):
    notebook = service(tmp_path)
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    archive = tmp_path / "session-a" / "DataSeek.ipynb"
    archive.write_text("saved")
    other = tmp_path / "unrelated"
    other.mkdir()
    (tmp_path / "session-a" / "link").symlink_to(other, target_is_directory=True)
    document = SimpleNamespace(container_name="jupyter-a", work_volume=notebook._volume("session-a"), delete=AsyncMock())
    monkeypatch.setattr(module, "JupyterSessionDocument", SimpleNamespace(
        session_id="session_id", user_id="user_id", find_one=AsyncMock(return_value=document)))
    client = SimpleNamespace(volumes=volumes, containers=SimpleNamespace(get=Mock(side_effect=docker.errors.NotFound("missing"))), close=Mock())
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    await notebook.delete(session_id="session-a", user_id="user-a")
    volumes.volume.remove.assert_called_once_with(force=True)
    assert archive.read_text() == "saved"
    assert other.is_dir()
    assert (tmp_path / ".owner-session-a.json").is_file()
