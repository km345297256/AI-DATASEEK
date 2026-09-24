"""Dataset admission validates execution-readable views, not root-only access."""
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.errors.exceptions import BadRequestError
from app.application.services import data_center_dataset_service as service_module
from app.domain.models.dataset import DatasetMount, DatasetStorageType
from app.infrastructure.external.sandbox import dataset_mount_validator as validator
from app.infrastructure.external.sandbox import dataset_readability as module
from app.infrastructure.external.sandbox import docker_sandbox
from test_dataset_management import FakeDatasetDocument, _seed_payload, _service


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "managed"
    source = root / "sample"
    (source / "nested").mkdir(parents=True)
    (source / "data.bin").write_bytes(b"observed source bytes")
    (source / "nested" / "notes.txt").write_bytes(b"protected source notes")
    for path in [source, source / "nested", source / "data.bin", source / "nested" / "notes.txt"]:
        path.chmod(0o700)
    yield root, source
    # Published fixtures are immutable like production views. Restore only
    # these test-owned directory modes so pytest can remove its own temp tree.
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink() and path.is_dir():
            path.chmod(0o700)


def prepare(root):
    return module._dataset_view_operation("prepare", str(root), "sample")


def view_path(root, receipt):
    return root / module.VIEW_DIRECTORY / "sample" / receipt["fingerprint"]


def signature(path):
    details = path.stat()
    return details.st_ino, stat.S_IMODE(details.st_mode), details.st_mtime_ns, details.st_ctime_ns


def test_strict_umask_and_restricted_sources_produce_independent_read_only_bytes(managed):
    root, source = managed
    originals = {str(path.relative_to(source)): (signature(path), path.read_bytes()) for path in source.rglob("*") if path.is_file()}
    directory_modes = {str(path): signature(path) for path in [source, source / "nested"]}
    previous = os.umask(0o077)
    try:
        result = prepare(root)
    finally:
        os.umask(previous)
    assert result["ok"] and result["file_count"] == 2 and result["reused"] is False
    view = view_path(root, result)
    for relative, (identity, content) in originals.items():
        original, copied = source / relative, view / relative
        assert signature(original) == identity and original.read_bytes() == content
        assert copied.read_bytes() == content
        assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(content).digest()
        assert copied.stat().st_ino != original.stat().st_ino and copied.stat().st_nlink == 1
        assert stat.S_IMODE(copied.stat().st_mode) == 0o444
    assert all(signature(Path(path)) == details for path, details in directory_modes.items())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o555 for path in [view, view / "nested"])
    assert module._dataset_view_operation("probe", str(view)) == {"ok": True, "file_count": 2}


def test_unchanged_manifest_reuses_view_without_copying_or_modifying_cached_files(managed, monkeypatch):
    root, _ = managed
    first = prepare(root)
    view = view_path(root, first)
    before = {str(path): signature(path) for path in [view, *view.rglob("*")]}
    monkeypatch.setattr(os, "write", Mock(side_effect=AssertionError("Unchanged inputs were copied again")))
    second = prepare(root)
    assert second == {**first, "reused": True}
    assert {str(path): signature(path) for path in [view, *view.rglob("*")]} == before


def test_changed_source_gets_new_generation_without_mutating_previous_task_view(managed):
    root, source = managed
    first = prepare(root)
    old = view_path(root, first)
    old_bytes, old_identity = (old / "data.bin").read_bytes(), signature(old / "data.bin")
    (source / "data.bin").write_bytes(b"new bytes for a new task")
    second = prepare(root)
    assert second["ok"] and second["fingerprint"] != first["fingerprint"]
    assert (view_path(root, second) / "data.bin").read_bytes() == b"new bytes for a new task"
    assert (old / "data.bin").read_bytes() == old_bytes and signature(old / "data.bin") == old_identity


def test_concurrent_preparations_publish_one_complete_reusable_generation(managed):
    root, _ = managed
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: prepare(root), range(8)))
    assert all(result["ok"] for result in results), results
    assert len({result["fingerprint"] for result in results}) == 1
    parent = root / module.VIEW_DIRECTORY / "sample"
    assert len(list(parent.iterdir())) == 1
    assert module._dataset_view_operation("probe", str(view_path(root, results[0])))["file_count"] == 2


@pytest.mark.parametrize("fault", ["file_symlink", "directory_symlink", "hardlink", "fifo", "dataset_symlink"])
def test_unsafe_sources_never_copy_follow_links_or_publish_partial_view(managed, tmp_path, fault):
    root, source = managed
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "private.txt"
    protected.write_bytes(b"must not enter dataset view")
    before = signature(protected)
    if fault == "file_symlink":
        (source / "linked").symlink_to(protected)
    elif fault == "directory_symlink":
        (source / "linked").symlink_to(outside, target_is_directory=True)
    elif fault == "hardlink":
        os.link(protected, source / "linked")
        before = signature(protected)
    elif fault == "fifo":
        os.mkfifo(source / "pipe")
    else:
        source.rename(root / "retained-original")
        source.symlink_to(outside, target_is_directory=True)
    assert prepare(root) == {"ok": False, "code": "dataset_unsafe"}
    assert signature(protected) == before and protected.read_bytes() == b"must not enter dataset view"
    assert not (root / module.VIEW_DIRECTORY).exists()


@pytest.mark.parametrize("fault", ["file_writable", "directory_writable", "cache_writable", "file_symlink", "view_symlink"])
def test_cache_with_writable_files_or_links_is_rejected_not_fixed_in_place(managed, tmp_path, fault):
    root, _ = managed
    result = prepare(root)
    view = view_path(root, result)
    if fault == "file_writable":
        (view / "data.bin").chmod(0o644)
    elif fault == "directory_writable":
        (view / "nested").chmod(0o755)
    elif fault == "cache_writable":
        (root / module.VIEW_DIRECTORY).chmod(0o777)
    elif fault == "file_symlink":
        view.chmod(0o755)
        (view / "data.bin").unlink()
        (view / "data.bin").symlink_to(tmp_path / "absent-private")
        view.chmod(0o555)
    else:
        view.rename(view.with_name("retained-generation"))
        view.symlink_to(tmp_path, target_is_directory=True)
    assert prepare(root) == {"ok": False, "code": "dataset_unsafe"}


def test_source_mutation_during_copy_is_detected_and_unpublished_stage_is_removed(managed, monkeypatch):
    root, source = managed
    original = os.read
    mutated = False

    def changing_read(fd, size):
        nonlocal mutated
        content = original(fd, size)
        if size == 1024 * 1024 and not mutated:
            mutated = True
            (source / "data.bin").write_bytes(b"concurrent change")
        return content

    monkeypatch.setattr(os, "read", changing_read)
    result = prepare(root)
    assert result["ok"] is False and result["code"] == "dataset_changed"
    assert result["diagnostic"]["stage"] == "source_copy"
    assert list((root / module.VIEW_DIRECTORY / "sample").iterdir()) == []


def test_probe_opens_every_regular_file_but_only_reads_one_byte_each(managed, monkeypatch):
    _, source = managed
    (source / "large.bin").write_bytes(b"z" * (2 * 1024 * 1024))
    original, sizes = os.read, []

    def bounded_read(fd, size):
        sizes.append(size)
        return original(fd, size)

    monkeypatch.setattr(os, "read", bounded_read)
    assert module._dataset_view_operation("probe", str(source)) == {"ok": True, "file_count": 3}
    assert sizes == [1, 1, 1]


@pytest.mark.parametrize("fault", ["link", "fifo", "hardlink"])
def test_host_probe_rejects_unsafe_entries_without_materializing_a_copy(managed, fault):
    root, source = managed
    if fault == "link":
        (source / "link").symlink_to(source / "data.bin")
    elif fault == "fifo":
        os.mkfifo(source / "pipe")
    else:
        os.link(source / "data.bin", source / "hardlinked.bin")
    assert module._dataset_view_operation("probe", str(source))["code"] == "dataset_unsafe"
    assert not (root / module.VIEW_DIRECTORY).exists()


class DockerFixture:
    def __init__(self, *, probe=None, prepare=None):
        self.calls = []
        self.probe = probe if probe is not None else {"ok": True, "file_count": 2}
        self.prepared = prepare if prepare is not None else {"ok": True, "file_count": 2, "fingerprint": "a" * 64, "reused": True}
        self.volumes = SimpleNamespace(get=Mock(return_value=SimpleNamespace(attrs={"Mountpoint": "/var/lib/docker/volumes/approved/_data"})))
        self.containers = SimpleNamespace(run=self.run)
        self.close = Mock()

    def run(self, **arguments):
        self.calls.append(arguments)
        result = self.probe if arguments["user"] == "ubuntu" else self.prepared
        if isinstance(result, Exception):
            raise result
        return json.dumps(result).encode()


def test_docker_boundary_materializes_only_approved_managed_volume_and_checks_real_image_user():
    client = DockerFixture()
    path = module.prepare_managed_dataset_source(client, image="sandbox-image", volume="approved", dataset_id="sample")
    assert path == "/var/lib/docker/volumes/approved/_data/.analysis-views-v1/sample/" + "a" * 64
    assert len(client.calls) == 2
    create, probe = client.calls
    assert create["user"] == "0:0" and create["mounts"][0]["Type"] == "volume"
    assert create["mounts"][0]["Source"] == "approved" and create["mounts"][0]["ReadOnly"] is False
    assert probe["user"] == "ubuntu" and probe["mounts"][0]["Source"] == path
    assert probe["mounts"][0]["ReadOnly"] is True
    assert probe["cap_drop"] == ["ALL"]
    assert 'pwd.getpwnam("ubuntu").pw_uid' in probe["command"][1]
    for call in client.calls:
        assert call["image"] == "sandbox-image" and call["read_only"] and call["network_disabled"] and call["remove"]
        assert call["security_opt"] == ["no-new-privileges:true"]
        compile(call["command"][1], "dataset-helper", "exec")


@pytest.mark.parametrize("result", [
    {"ok": False, "code": "dataset_unreadable"}, {"ok": False, "code": "/Users/private/secret"},
    {"ok": True, "file_count": True}, {"ok": True, "file_count": 20001},
    RuntimeError("private Docker endpoint /Users/private/secret"),
])
def test_probe_failure_has_safe_fixed_error_and_never_enables_root_fallback(result):
    client = DockerFixture(probe=result)
    with pytest.raises(module.DatasetReadabilityError) as error:
        module.verify_dataset_readability(client, image="sandbox", source="/authorized/source")
    assert len(client.calls) == 1 and client.calls[0]["user"] == "ubuntu"
    assert "/Users/" not in str(error.value) and "endpoint" not in str(error.value)


@pytest.mark.parametrize("identifier", ["../outside", ".", "nested/dataset", "bad\\path", "", "/root", ".analysis-views-v1"])
def test_managed_identity_is_checked_before_touching_docker(identifier):
    client = DockerFixture()
    with pytest.raises(module.DatasetReadabilityError):
        module.prepare_managed_dataset_source(client, image="sandbox", volume="approved", dataset_id=identifier)
    assert client.calls == [] and client.volumes.get.call_count == 0


@pytest.mark.asyncio
async def test_seed_copy_does_not_inherit_restricted_source_mode_and_admits_before_registration(monkeypatch, tmp_path):
    seeds, storage = tmp_path / "seeds", tmp_path / "storage"
    source = seeds / "seed_climate"
    source.mkdir(parents=True)
    content = b"year,value\n2024,1\n"
    (source / "observations.csv").write_bytes(content)
    (source / "observations.csv").chmod(0o700)
    (source / "manifest.json").write_text(json.dumps(_seed_payload(content)))
    monkeypatch.setattr(FakeDatasetDocument, "records", {})
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(storage_root=storage, seed_root=seeds)
    before = signature(source / "observations.csv")

    async def admission(dataset_id):
        assert dataset_id == "seed_climate"
        assert FakeDatasetDocument.records == {}
        copied = storage / dataset_id / "observations.csv"
        assert copied.read_bytes() == content and stat.S_IMODE(copied.stat().st_mode) == 0o644

    service._prepare_managed_execution_view = AsyncMock(side_effect=admission)
    old = os.umask(0o077)
    try:
        await service.ensure_seed_data()
    finally:
        os.umask(old)
    assert signature(source / "observations.csv") == before
    service._prepare_managed_execution_view.assert_awaited_once_with("seed_climate")
    assert FakeDatasetDocument.records["seed_climate"].locations[0].verified


@pytest.mark.asyncio
async def test_failed_managed_admission_cannot_publish_verified_catalog(monkeypatch, tmp_path):
    source = tmp_path / "seed_climate"
    source.mkdir()
    content = b"year,value\n2024,1\n"
    (source / "observations.csv").write_bytes(content)
    monkeypatch.setattr(FakeDatasetDocument, "records", {})
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(storage_root=tmp_path)
    service._prepare_managed_execution_view = AsyncMock(side_effect=BadRequestError("Dataset is unreadable"))
    with pytest.raises(BadRequestError):
        await service.register_curated_managed_directory(_seed_payload(content))
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_service_uses_managed_volume_image_identity_and_sanitizes_admission_failure(monkeypatch):
    service = _service()
    service._settings.sandbox_image = "production-sandbox"
    service._settings.sandbox_docker_create_timeout_seconds = 37
    prepare_view = Mock(side_effect=module.DatasetReadabilityError("dataset_unreadable"))
    monkeypatch.setattr(service_module, "prepare_registered_managed_dataset", prepare_view)
    with pytest.raises(BadRequestError, match="analysis user"):
        await service_module.DataCenterDatasetService._prepare_managed_execution_view(service, "sample")
    prepare_view.assert_called_once_with(image="production-sandbox", volume="managed-volume", dataset_id="sample", timeout=37)


def test_host_registration_root_inventory_cannot_mask_unreadable_runtime_files(monkeypatch):
    from test_dataset_mount_validator import FakeDockerClient, _settings, _success_output
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(_success_output())
    real_run = client.containers.run

    def run(**arguments):
        if arguments["user"] == "ubuntu":
            client.containers.calls.append(arguments)
            return b'{"ok":false,"code":"dataset_unreadable"}'
        return real_run(**arguments)

    client.containers.run = run
    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory("/srv/datasets/center-a", docker_client=client,
                                                 helper_image="sandbox", configured_roots="/srv/datasets")
    assert error.value.code == "dataset_unreadable"
    assert [call["user"] for call in client.containers.calls] == ["0:0", "ubuntu"]
    assert all(call["mounts"][0]["ReadOnly"] for call in client.containers.calls)


def sandbox_fixture(monkeypatch):
    settings = SimpleNamespace(sandbox_image="sandbox", sandbox_name_prefix="sandbox", sandbox_docker_create_timeout_seconds=31,
        sandbox_ttl_minutes=90, sandbox_chrome_args="", sandbox_https_proxy="", sandbox_http_proxy="", sandbox_no_proxy="",
        sandbox_network="private", dataset_managed_volume="approved", dataset_host_path_allowlist="/approved", dataset_docker_host_root="")
    instance = SimpleNamespace(attrs={"NetworkSettings": {"IPAddress": "127.0.0.1"}, "Image": "sha256:test"}, reload=Mock())
    client = SimpleNamespace(containers=SimpleNamespace(run=Mock(return_value=instance)), close=Mock())
    monkeypatch.setattr(docker_sandbox, "get_settings", lambda: settings)
    monkeypatch.setattr(docker_sandbox.docker, "from_env", Mock(return_value=client))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_new_sandbox_prepares_or_probes_source_before_readonly_mount(monkeypatch, managed):
    client = sandbox_fixture(monkeypatch)
    prepare_view = Mock(return_value="/approved-volume/.analysis-views-v1/sample/fingerprint")
    probe = Mock()
    canonical = Mock(return_value="/approved/source")
    monkeypatch.setattr(docker_sandbox, "prepare_managed_dataset_source", prepare_view)
    monkeypatch.setattr(docker_sandbox, "verify_dataset_readability", probe)
    monkeypatch.setattr(docker_sandbox, "_canonical_host_source", canonical)
    location_id = "dsl_" + "a" * 16
    target = "/home/ubuntu/datasets/sample" + ("" if managed else f"/sources/{location_id}/data")
    mount = DatasetMount(dataset_id="sample", source_id=location_id, display_name="data", node_id="local-default",
        storage_type=DatasetStorageType.MANAGED_UPLOAD if managed else DatasetStorageType.HOST_PATH,
        source="approved" if managed else "/approved/source", target=target)
    sandbox = docker_sandbox.DockerSandbox._create_task(mounts=[mount])
    try:
        actual = client.containers.run.call_args.kwargs["mounts"][0]
        assert actual["Target"] == target and actual["ReadOnly"] is True
        if managed:
            prepare_view.assert_called_once_with(client, image="sandbox", volume="approved", dataset_id="sample")
            probe.assert_not_called()
            assert actual["Source"] == prepare_view.return_value
        else:
            prepare_view.assert_not_called()
            probe.assert_called_once_with(client, image="sandbox", source="/approved/source")
            assert actual["Source"] == "/approved/source"
    finally:
        await sandbox.client.aclose()


@pytest.mark.parametrize("managed", [True, False])
def test_failed_admission_stops_before_creating_any_task_container(monkeypatch, managed):
    client = sandbox_fixture(monkeypatch)
    failed = Mock(side_effect=module.DatasetReadabilityError("dataset_unreadable"))
    monkeypatch.setattr(docker_sandbox, "prepare_managed_dataset_source", failed)
    monkeypatch.setattr(docker_sandbox, "verify_dataset_readability", failed)
    monkeypatch.setattr(docker_sandbox, "_canonical_host_source", Mock(return_value="/approved/source"))
    location_id = "dsl_" + "a" * 16
    target = "/home/ubuntu/datasets/sample" + ("" if managed else f"/sources/{location_id}/data")
    mount = DatasetMount(dataset_id="sample", source_id=location_id, display_name="data", node_id="local-default",
        storage_type=DatasetStorageType.MANAGED_UPLOAD if managed else DatasetStorageType.HOST_PATH,
        source="approved" if managed else "/approved/source", target=target)
    with pytest.raises(module.DatasetReadabilityError, match="analysis user"):
        docker_sandbox.DockerSandbox._create_task(mounts=[mount])
    client.containers.run.assert_not_called()
