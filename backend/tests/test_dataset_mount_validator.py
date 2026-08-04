import json
from types import SimpleNamespace

import pytest

from app.infrastructure.external.sandbox import dataset_mount_validator as validator


class FakeContainers:
    def __init__(self, output: bytes | str):
        self.output = output
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


class FakeDockerClient:
    def __init__(self, output: bytes | str):
        self.containers = FakeContainers(output)
        self.closed = False

    def close(self):
        self.closed = True


def _settings(**overrides):
    values = {
        "sandbox_image": "test-sandbox-image",
        "sandbox_docker_create_timeout_seconds": 17,
        "dataset_host_path_allowlist": "/legacy-datasets",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _success_output(
    *,
    source: str = "/srv/datasets/center-a",
    roots: list[str] | None = None,
    files: list[dict] | None = None,
) -> bytes:
    return json.dumps(
        {
            "ok": True,
            "canonical_source_directory": source,
            "canonical_allowed_roots": roots or ["/srv/datasets"],
            "files": files
            if files is not None
            else [
                {"relative_path": "rasters/tile-01.tif", "size": 1024},
                {"relative_path": "metadata.json", "size": 42},
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_host_path_candidates_require_an_absolute_lexical_allowlist_match():
    assert validator.host_path_candidates(
        "/srv/datasets/center-a",
        "/srv/datasets,/data",
    ) == ["/srv/datasets"]
    assert validator.host_path_candidates("/srv/datasets-escape/a", "/srv/datasets") == []
    assert validator.host_path_candidates("/srv/datasets/../secrets", "/srv/datasets") == []
    assert validator.host_path_candidates("relative/path", "/srv/datasets") == []


def test_inspect_local_directory_returns_canonical_safe_manifest_and_uses_read_only_helper(
    monkeypatch,
):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(_success_output())

    inventory = validator.inspect_local_dataset_directory(
        "/srv/datasets/center-a",
        docker_client=client,
        helper_image="helper-image",
        configured_roots=["/srv/datasets", "/data"],
    )

    assert inventory.canonical_source_directory == "/srv/datasets/center-a"
    assert [(item.relative_path, item.size) for item in inventory.files] == [
        ("metadata.json", 42),
        ("rasters/tile-01.tif", 1024),
    ]
    assert inventory.total_size == 1066

    assert len(client.containers.calls) == 1
    call = client.containers.calls[0]
    assert call["image"] == "helper-image"
    assert call["entrypoint"] == "python3"
    assert call["command"][2] == "/srv/datasets/center-a"
    assert json.loads(call["command"][3]) == ["/srv/datasets"]
    assert call["command"][6:] == ["20000", "100000"]
    assert call["network_disabled"] is True
    assert call["read_only"] is True
    assert call["remove"] is True
    assert call["user"] == "0:0"
    assert len(call["mounts"]) == 1
    assert call["mounts"][0]["Source"] == "/"
    assert call["mounts"][0]["Target"] == "/host"
    assert call["mounts"][0]["ReadOnly"] is True

    helper_script = call["command"][1]
    assert "follow_symlinks=False" in helper_script
    assert "os.O_NOFOLLOW" in helper_script
    assert "stat.S_ISREG" in helper_script
    assert "with os.scandir(directory_fd) as entries" in helper_script
    assert "sorted(iterator" not in helper_script


def test_inspect_maps_logical_path_into_configured_docker_host_root(monkeypatch):
    physical_source = "/var/lib/snapd/hostfs/data/A1"
    physical_root = "/var/lib/snapd/hostfs/data"
    client = FakeDockerClient(
        _success_output(
            source=physical_source,
            roots=[physical_root],
            files=[],
        )
    )
    monkeypatch.setattr(
        validator,
        "get_settings",
        lambda: _settings(dataset_docker_host_root="/var/lib/snapd/hostfs"),
    )

    inventory = validator.inspect_local_dataset_directory(
        "/data/A1",
        configured_roots="/data",
        docker_client=client,
    )

    call = client.containers.calls[0]
    assert inventory.canonical_source_directory == physical_source
    assert call["command"][2] == physical_source
    assert json.loads(call["command"][3]) == [physical_root]


def test_mount_validation_accepts_scanned_physical_path_under_mapped_allowlist():
    source, candidates = validator.docker_host_source_and_candidates(
        "/var/lib/snapd/hostfs/data/A1",
        "/data,/mnt",
        "/var/lib/snapd/hostfs",
    )

    assert source == "/var/lib/snapd/hostfs/data/A1"
    assert candidates == ["/var/lib/snapd/hostfs/data"]


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        ("relative/dataset", "invalid_source_directory"),
        ("/srv/datasets/../secret", "invalid_source_directory"),
        ("/outside/dataset", "source_outside_allowlist"),
    ],
)
def test_inspect_rejects_invalid_or_unapproved_paths_before_starting_helper(
    monkeypatch,
    source,
    expected_code,
):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(_success_output())

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            source,
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
        )

    assert error.value.code == expected_code
    assert client.containers.calls == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("source_not_found", "Dataset source directory does not exist"),
        ("source_not_directory", "Dataset source path is not a directory"),
        ("too_many_files", "Dataset directory contains more files than allowed"),
        ("too_many_directories", "Dataset directory contains more directories than allowed"),
        ("too_many_entries", "Dataset directory contains more entries than allowed"),
        ("output_too_large", "Dataset directory inventory exceeds its output size limit"),
    ],
)
def test_inspect_surfaces_clear_helper_limit_and_source_errors(monkeypatch, code, message):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    output = json.dumps({"ok": False, "code": code, "message": message}).encode()
    client = FakeDockerClient(output)

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            "/srv/datasets/center-a",
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
        )

    assert error.value.code == code
    assert message in str(error.value)


@pytest.mark.parametrize(
    "unsafe_file",
    [
        {"relative_path": "../secret.txt", "size": 1},
        {"relative_path": "/absolute.txt", "size": 1},
        {"relative_path": "folder\\escape.txt", "size": 1},
        {"relative_path": "folder/unsafe\x7f.txt", "size": 1},
        {"relative_path": "valid.txt", "size": -1},
        {"relative_path": "valid.txt", "size": True},
    ],
)
def test_inspect_rejects_unsafe_file_entries_from_helper(monkeypatch, unsafe_file):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(_success_output(files=[unsafe_file]))

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            "/srv/datasets/center-a",
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
        )

    assert error.value.code == "invalid_helper_response"


def test_inspect_rechecks_canonical_allowlist_boundary_from_helper(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(
        _success_output(source="/etc/private", roots=["/srv/datasets"], files=[])
    )

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            "/srv/datasets/link",
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
        )

    assert error.value.code == "source_outside_allowlist"


def test_inspect_enforces_file_count_even_for_a_malformed_helper(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(
        _success_output(
            files=[
                {"relative_path": "one.txt", "size": 1},
                {"relative_path": "two.txt", "size": 2},
            ]
        )
    )

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            "/srv/datasets/center-a",
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
            max_files=1,
        )

    assert error.value.code == "too_many_files"


def test_directory_traversal_limits_bound_non_file_entries():
    assert validator._directory_traversal_limits(1) == (1_024, 10_000)
    assert validator._directory_traversal_limits(10_001) == (20_002, 100_010)


def test_inspect_enforces_raw_helper_output_limit(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    client = FakeDockerClient(b"x" * 513)

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory(
            "/srv/datasets/center-a",
            docker_client=client,
            helper_image="helper-image",
            configured_roots="/srv/datasets",
            max_output_bytes=512,
        )

    assert error.value.code == "output_too_large"


def test_inspect_prefers_dataset_local_path_allowlist_environment(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    monkeypatch.setenv("DATASET_LOCAL_PATH_ALLOWLIST", "/local-datasets")
    client = FakeDockerClient(
        _success_output(
            source="/local-datasets/center-a",
            roots=["/local-datasets"],
            files=[],
        )
    )

    validator.inspect_local_dataset_directory(
        "/local-datasets/center-a",
        docker_client=client,
        helper_image="helper-image",
    )

    assert json.loads(client.containers.calls[0]["command"][3]) == ["/local-datasets"]


def test_inspect_closes_only_the_docker_client_it_creates(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())
    created_client = FakeDockerClient(
        _success_output(source="/legacy-datasets/a", roots=["/legacy-datasets"], files=[])
    )
    from_env_calls: list[int] = []

    def fake_from_env(*, timeout):
        from_env_calls.append(timeout)
        return created_client

    monkeypatch.setattr(validator.docker, "from_env", fake_from_env)

    validator.inspect_local_dataset_directory("/legacy-datasets/a")

    assert from_env_calls == [17]
    assert created_client.closed is True


def test_inspect_wraps_docker_client_connection_failures(monkeypatch):
    monkeypatch.setattr(validator, "get_settings", lambda: _settings())

    def fail_from_env(*, timeout):
        raise OSError(f"Docker unavailable after {timeout} seconds")

    monkeypatch.setattr(validator.docker, "from_env", fail_from_env)

    with pytest.raises(validator.DatasetDirectoryInspectionError) as error:
        validator.inspect_local_dataset_directory("/legacy-datasets/a")

    assert error.value.code == "helper_failed"
    assert "Docker unavailable" not in str(error.value)


def test_canonical_host_source_keeps_the_shared_host_mount_read_only():
    client = FakeDockerClient(b"/srv/datasets/a\tdirectory\n/srv/datasets\tdirectory\n")

    result = validator.canonical_host_source(
        client,
        image="helper-image",
        source="/srv/datasets/a",
        candidate_roots=["/srv/datasets"],
    )

    assert result == "/srv/datasets/a"
    call = client.containers.calls[0]
    assert call["read_only"] is True
    assert call["mounts"][0]["ReadOnly"] is True
