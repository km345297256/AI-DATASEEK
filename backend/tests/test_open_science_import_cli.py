"""The separate visualization catalog uses the existing reviewed import boundary.

All filesystem fixtures are temporary. Mongo, Docker and service calls are mocks;
these tests never register data or inspect the operator's actual host datasets.
"""
import hashlib
import json
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import import_curated_host_datasets as cli


def _manifest(catalog, *, source="open-science", dataset_id="open-science-example", folder=None):
    payload = b"time,value\n0,1\n"
    target = catalog / (folder or source) / f"{dataset_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "dataset_id": dataset_id,
        "data_center_id": "reviewed-visualization-data",
        "data_center_name": "Reviewed open science",
        "name": "Open science example",
        "domain": "general",
        "metadata": {"curated": True, "source_catalog": source},
        "files": [{"path": "observations.csv", "size": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}],
    }
    target.write_text(json.dumps(seed), encoding="utf-8")
    return target


@pytest.fixture
def importer(tmp_path, monkeypatch):
    catalog = tmp_path / "visualization-datasets"
    catalog.mkdir()
    inspection = tmp_path / "inspection"
    inspection.mkdir()
    args = SimpleNamespace(catalog=catalog, source="open-science", apply=False,
                           host_root="/srv/datasets/visualization-catalog", inspection_root=inspection)
    mount = {"Type": "bind", "Source": args.host_root, "Destination": str(inspection), "RW": False}
    docker_client = Mock()
    docker_client.containers.get.return_value.attrs = {"Mounts": [mount]}
    monkeypatch.setattr(cli.docker, "from_env", Mock(return_value=docker_client))
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "test-import-container")
    database = object()
    mongo = SimpleNamespace(initialize=AsyncMock(), shutdown=AsyncMock(), client={"test_catalog": database})
    mongo_factory = Mock(return_value=mongo)
    monkeypatch.setattr(cli, "get_mongodb", mongo_factory)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(mongodb_database="test_catalog"))
    initialize_documents = AsyncMock()
    monkeypatch.setattr(cli, "init_beanie", initialize_documents)

    async def register(seed, **_kwargs):
        return SimpleNamespace(dataset_id=seed.dataset_id, files=seed.files, enabled=True)

    service = SimpleNamespace(register_curated_host_directory=AsyncMock(side_effect=register))
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(cli, "DataCenterDatasetService", service_factory)
    return SimpleNamespace(args=args, mount=mount, docker=docker_client, mongo=mongo,
                           mongo_factory=mongo_factory, service=service, service_factory=service_factory,
                           init_beanie=initialize_documents)


def _cli_argv(args, source):
    return ["import_curated_host_datasets.py", "--catalog", str(args.catalog), "--source", source,
            "--host-root", args.host_root, "--inspection-root", str(args.inspection_root)]


@pytest.mark.parametrize("source", ["open-science", "plugin-tests", "scidb", "tpdc", "chemdc", "ngdc"])
def test_cli_accepts_only_explicit_reviewed_sources(importer, monkeypatch, source):
    monkeypatch.setattr(cli.sys, "argv", _cli_argv(importer.args, source))
    result = cli.parse_args()
    assert result.source == source and result.apply is False


@pytest.mark.parametrize("source", ["unreviewed", "open_science", "../open-science"])
def test_cli_rejects_unknown_source_options(importer, monkeypatch, source):
    monkeypatch.setattr(cli.sys, "argv", _cli_argv(importer.args, source))
    with pytest.raises(SystemExit) as error:
        cli.parse_args()
    assert error.value.code == 2
    importer.mongo_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_separate_open_science_catalog_keeps_two_phase_validation(importer, capsys, apply):
    args = importer.args
    for suffix in ("alpha", "beta"):
        _manifest(args.catalog, dataset_id="open-science-" + suffix)
    args.apply = apply
    await cli.run(args)
    calls = importer.service.register_curated_host_directory.await_args_list
    assert len(calls) == (4 if apply else 2)
    assert [call.args[0].dataset_id for call in calls[:2]] == ["open-science-alpha", "open-science-beta"]
    assert all(call.kwargs["dry_run"] is True for call in calls[:2])
    if apply:
        assert all("dry_run" not in call.kwargs for call in calls[2:])
    for call in calls:
        seed = call.args[0]
        assert seed.metadata["source_catalog"] == "open-science"
        assert call.kwargs["storage_directory"] == str(PurePosixPath(args.host_root) / "open-science" / seed.dataset_id)
        assert call.kwargs["inspection_directory"] == args.inspection_root / "open-science" / seed.dataset_id
    importer.docker.containers.get.assert_called_once_with("test-import-container")
    importer.docker.close.assert_called_once()
    importer.mongo.initialize.assert_awaited_once()
    importer.mongo.shutdown.assert_awaited_once()
    assert importer.init_beanie.await_args.kwargs["skip_indexes"] is True
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["validated"] == 2 and result["apply"] is apply
    assert len(result["datasets"]) == (2 if apply else 0)
    assert args.host_root not in output and str(args.inspection_root) not in output


@pytest.mark.asyncio
async def test_legacy_source_selection_is_unchanged(importer, capsys):
    _manifest(importer.args.catalog)
    _manifest(importer.args.catalog, source="tpdc", dataset_id="tpdc-existing")
    importer.args.source = "tpdc"
    await cli.run(importer.args)
    calls = importer.service.register_curated_host_directory.await_args_list
    assert len(calls) == 1 and calls[0].args[0].dataset_id == "tpdc-existing"
    assert json.loads(capsys.readouterr().out)["validated"] == 1


@pytest.mark.asyncio
async def test_unknown_manifest_source_is_rejected_even_when_filtered(importer):
    _manifest(importer.args.catalog)
    _manifest(importer.args.catalog, source="unreviewed", dataset_id="unknown-example")
    with pytest.raises(ValueError, match="Unknown reviewed catalog source"):
        await cli.run(importer.args)
    importer.mongo_factory.assert_not_called()
    importer.service_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source_folder", "filename"])
async def test_source_and_manifest_identity_must_still_match(importer, mutation):
    path = _manifest(importer.args.catalog, folder="wrong-source" if mutation == "source_folder" else None)
    if mutation == "filename":
        path.rename(path.with_name("different-id.json"))
    with pytest.raises(ValueError, match="Manifest name must match"):
        await cli.run(importer.args)
    importer.mongo_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mount_change", [
    {"RW": True}, {"Source": "/srv/datasets/unrelated"}, {"Type": "volume"},
])
async def test_open_science_does_not_bypass_read_only_bind_proof(importer, mount_change):
    _manifest(importer.args.catalog)
    importer.mount.update(mount_change)
    with pytest.raises(ValueError, match="read-only bind of the exact host-root"):
        await cli.run(importer.args)
    importer.mongo_factory.assert_not_called()
    importer.service_factory.assert_not_called()


@pytest.mark.asyncio
async def test_failed_checksum_prevalidation_cannot_apply_any_item(importer):
    for suffix in ("alpha", "beta"):
        _manifest(importer.args.catalog, dataset_id="open-science-" + suffix)
    importer.args.apply = True

    async def fail_second(seed, **kwargs):
        assert kwargs.get("dry_run") is True
        if seed.dataset_id.endswith("beta"):
            raise ValueError("Curated dataset file checksum did not match its manifest")

    importer.service.register_curated_host_directory.side_effect = fail_second
    with pytest.raises(ValueError, match="checksum"):
        await cli.run(importer.args)
    calls = importer.service.register_curated_host_directory.await_args_list
    assert len(calls) == 2 and all(call.kwargs.get("dry_run") is True for call in calls)
    importer.mongo.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_visualization_catalog_does_not_touch_database(importer):
    with pytest.raises(ValueError, match="No reviewed manifests selected"):
        await cli.run(importer.args)
    importer.mongo_factory.assert_not_called()
