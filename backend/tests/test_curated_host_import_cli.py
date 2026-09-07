"""A host checksum import must not trust an arbitrary same-sized local tree."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import import_curated_host_datasets as cli


@pytest.fixture
def checked_view(tmp_path, monkeypatch):
    root = tmp_path / "inspection"
    root.mkdir()
    args = SimpleNamespace(host_root="/srv/datasets/open-catalog", inspection_root=root)
    mount = {"Type": "bind", "Source": args.host_root, "Destination": str(root), "RW": False}
    client = Mock()
    client.containers.get.return_value.attrs = {"Mounts": [mount]}
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "calling-container-id")
    return args, mount, client


def test_exact_read_only_host_bind_is_verified(checked_view):
    args, _, client = checked_view
    cli.verify_inspection_mount(args, docker_client=client)
    client.containers.get.assert_called_once_with("calling-container-id")
    client.close.assert_not_called()


@pytest.mark.parametrize("change", [
    {"RW": True},
    {"RW": None},
    {"Source": "/srv/datasets/other-same-sized-tree"},
    {"Type": "volume"},
    {"Source": None},
])
def test_wrong_or_writable_data_bind_fails_closed(checked_view, change):
    args, mount, client = checked_view
    mount.update(change)
    with pytest.raises(ValueError, match="read-only bind of the exact host-root"):
        cli.verify_inspection_mount(args, docker_client=client)


def test_parent_mount_does_not_prove_exact_source_mapping(checked_view):
    args, mount, client = checked_view
    mount["Destination"] = str(args.inspection_root.parent)
    with pytest.raises(ValueError, match="exact container bind mount target"):
        cli.verify_inspection_mount(args, docker_client=client)


def test_nested_mount_cannot_shadow_checked_host_files(checked_view):
    args, _, client = checked_view
    client.containers.get.return_value.attrs["Mounts"].append({
        "Type": "bind", "Source": "/other", "Destination": str(args.inspection_root / "scidb"), "RW": False,
    })
    with pytest.raises(ValueError, match="overriding nested mounts"):
        cli.verify_inspection_mount(args, docker_client=client)


def test_duplicate_mount_targets_are_rejected(checked_view):
    args, mount, client = checked_view
    client.containers.get.return_value.attrs["Mounts"].append(dict(mount))
    with pytest.raises(ValueError, match="exact container bind mount target"):
        cli.verify_inspection_mount(args, docker_client=client)


@pytest.mark.parametrize("mounts", [None, {}, [{"Type": "bind"}]])
def test_missing_or_malformed_mount_information_is_rejected(checked_view, mounts):
    args, _, client = checked_view
    client.containers.get.return_value.attrs = {"Mounts": mounts}
    with pytest.raises(ValueError, match="mount information"):
        cli.verify_inspection_mount(args, docker_client=client)


def test_symlinked_inspection_root_is_rejected_before_docker_lookup(checked_view):
    args, _, client = checked_view
    alias = args.inspection_root.parent / "alias"
    alias.symlink_to(args.inspection_root, target_is_directory=True)
    args.inspection_root = alias
    with pytest.raises(ValueError, match="non-symlink"):
        cli.verify_inspection_mount(args, docker_client=client)
    client.containers.get.assert_not_called()


def test_owned_docker_client_is_closed_on_inspection_failure(checked_view, monkeypatch):
    args, _, client = checked_view
    client.containers.get.side_effect = RuntimeError("private daemon details")
    factory = Mock(return_value=client)
    monkeypatch.setattr(cli.docker, "from_env", factory)
    with pytest.raises(ValueError, match="Unable to verify") as error:
        cli.verify_inspection_mount(args)
    assert "private daemon details" not in str(error.value)
    factory.assert_called_once_with(timeout=20)
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_mount_verification_precedes_any_database_access(checked_view, monkeypatch, apply):
    args, _, _ = checked_view
    args.apply = apply
    verify = Mock(side_effect=ValueError("wrong mount"))
    mongo = Mock()
    monkeypatch.setattr(cli, "verify_inspection_mount", verify)
    monkeypatch.setattr(cli, "get_mongodb", mongo)
    with pytest.raises(ValueError, match="wrong mount"):
        await cli.run(args)
    verify.assert_called_once_with(args)
    mongo.assert_not_called()
