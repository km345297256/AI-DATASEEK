"""Offline sample installation must preserve originals and fail closed."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("prepare_visualization_datasets", Path(__file__).resolve().parents[1] / "scripts/prepare_visualization_datasets.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def setup_args(tmp_path):
    tmp_path = tmp_path.resolve()
    staging = tmp_path / "staging"
    source = staging / "viz-test-sample"
    source.mkdir(parents=True)
    (source / "sample.txt").write_bytes(b"one scientific observation\n")
    data = (source / "sample.txt").read_bytes()
    item = {"dataset_id": "viz-test-sample", "name": "Test", "description": "Scientific fixture",
        "domain": "general", "view_kind": "text", "plugin_id": "text", "entry_file": "sample.txt",
        "publisher": "Test publisher", "source_url": "https://example.org/data", "license": "CC0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/", "license_details": "Fixture only",
        "sample_scope": "One test fixture", "files": [{"path": "sample.txt", "url": "https://example.org/sample.txt",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}
    descriptor = tmp_path / "descriptors.json"
    descriptor.write_text(json.dumps([item]))
    return SimpleNamespace(staging_root=staging, data_root=tmp_path / "data", catalog_root=tmp_path / "catalog",
        descriptors=descriptor, download_date="2026-09-18", apply=False), item


def test_validation_only_then_install_and_idempotent(tmp_path):
    args, item = setup_args(tmp_path)
    assert module.run(args)[0]["status"] == "validated"
    assert not args.data_root.exists() and not args.catalog_root.exists()
    args.apply = True
    assert module.run(args)[0]["status"] == "prepared"
    assert module.run(args)[0]["status"] == "existing_verified"
    directory = args.data_root / "open-science" / item["dataset_id"]
    assert (directory / "sample.txt").read_bytes() == (args.staging_root / item["dataset_id"] / "sample.txt").read_bytes()
    manifest = json.loads(next(args.catalog_root.rglob("*.json")).read_text())
    assert manifest["metadata"]["source_catalog"] == "open-science"
    assert len(manifest["files"]) == 2
    assert str(tmp_path) not in json.dumps(manifest)
    (directory / "sample.txt").write_text("user change")
    with pytest.raises(ValueError, match="differs"):
        module.run(args)
    assert (directory / "sample.txt").read_text() == "user change"


def test_preflight_checks_whole_batch_before_any_write(tmp_path):
    args, item = setup_args(tmp_path)
    second = {**item, "dataset_id": "viz-second-sample"}
    args.descriptors.write_text(json.dumps([item, second]))
    args.apply = True
    with pytest.raises(ValueError, match="Missing"):
        module.run(args)
    assert not args.data_root.exists() and not args.catalog_root.exists()


@pytest.mark.parametrize("name", ["../secret", "/absolute", "a/../secret", "a\\b", "a//b"])
def test_reject_unsafe_paths(name):
    with pytest.raises(ValueError):
        module.relative(name)


def test_symlinked_source_and_root_are_rejected(tmp_path):
    args, item = setup_args(tmp_path)
    source = args.staging_root / item["dataset_id"] / "sample.txt"
    other = source.with_name("other.txt")
    source.rename(other)
    source.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        module.run(args)
    alias = tmp_path / "alias"
    alias.symlink_to(args.staging_root, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        module.safe_root(alias)


@pytest.mark.parametrize("url", ["http://example.org/data", "https://user:secret@example.org/data", "https://example.org/data?token=secret", "https://example.org/data?%74oken=secret", "https://example.org/data#token=secret", "file:///data"])
def test_no_secret_or_nonpublic_provenance(url):
    with pytest.raises(ValueError):
        module.public_url(url)


@pytest.mark.parametrize("reserved", ["SOURCE.md/file.txt", "source.md", "Source.MD/data"])
def test_reserved_documentation_path_rejected_before_writes(tmp_path, reserved):
    args, item = setup_args(tmp_path)
    item["files"][0]["path"] = reserved
    args.descriptors.write_text(json.dumps([item]))
    args.apply = True
    with pytest.raises(ValueError, match="reserved"):
        module.run(args)
    assert not args.data_root.exists()


def test_full_manifest_contract_is_validated_before_writes(tmp_path):
    args, item = setup_args(tmp_path)
    item["name"] = "x" * 301
    args.descriptors.write_text(json.dumps([item]))
    args.apply = True
    with pytest.raises(ValueError):
        module.run(args)
    assert not args.data_root.exists()


def plugin_args(tmp_path):
    args, item = setup_args(tmp_path)
    args.profile = "plugin-tests"
    item.update(sample_kind="project_fixture", test_steps=["Read text without executing it"])
    args.descriptors.write_text(json.dumps([item]))
    return args, item


def test_plugin_profile_has_distinct_classification_without_changing_old_profile(tmp_path):
    args, item = plugin_args(tmp_path)
    args.apply = True
    module.run(args)
    manifest = json.loads(next(args.catalog_root.rglob("*.json")).read_text())
    assert manifest["metadata"]["source_catalog"] == "plugin-tests"
    assert manifest["metadata"]["sample_kind"] == "project_fixture"
    assert "开放科学" not in manifest["tags"]
    assert "插件测试样例" in manifest["tags"]
    assert module.run(args)[0]["status"] == "existing_verified"


@pytest.mark.parametrize("field,value", [("sample_kind", "unknown"), ("test_steps", []), ("plugin_id", "unregistered")])
def test_plugin_profile_rejects_missing_classification_steps_and_unknown_plugin(tmp_path, field, value):
    args, item = plugin_args(tmp_path)
    item[field] = value
    args.descriptors.write_text(json.dumps([item])); args.apply = True
    with pytest.raises(ValueError): module.run(args)
    assert not args.data_root.exists()


def test_extended_offline_budget_requires_explicit_profile(tmp_path):
    args, _ = setup_args(tmp_path)
    args.max_dataset_bytes = 2 * 1024**3
    with pytest.raises(ValueError, match="explicit"): module.run(args)


def test_whole_file_plugin_limit_is_not_relaxed_by_offline_budget(tmp_path, monkeypatch):
    args, item = plugin_args(tmp_path)
    # Shrink a test-only plugin manifest's limit, not a real file or live plugin.
    repo = tmp_path / "repo"; manifests = repo / "plugin-host/visualizations"
    manifests.mkdir(parents=True)
    (manifests / "text.json").write_text(json.dumps({"id": "text", "view_kind": "text", "capabilities": {"input_mode": "whole"}, "limits": {"max_input_bytes": 1}}))
    monkeypatch.setattr(module, "REPOSITORY", repo)
    args.max_dataset_bytes = 2 * 1024**3
    with pytest.raises(ValueError, match="unchanged plugin budget"): module.run(args)


def test_generated_fixture_origin_requires_reviewed_generator_hash(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; script = repo / "backend/scripts/generate.py"
    script.parent.mkdir(parents=True); script.write_text("# reviewed generator, not executed\n")
    monkeypatch.setattr(module, "REPOSITORY", repo)
    spec = {"sha256": "a"*64, "origin": {"kind": "generated_fixture", "generator": "backend/scripts/generate.py", "generator_sha256": module.sha256(script), "description": "Synthetic signals, not observations"}}
    module.fixture_origin(spec)
    spec["origin"]["generator_sha256"] = "0"*64
    with pytest.raises(ValueError, match="checksum"): module.fixture_origin(spec)
    spec["origin"]["generator"] = "../secret"
    with pytest.raises(ValueError): module.fixture_origin(spec)


def test_original_fixture_must_match_repository_bytes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; original = repo / "sandbox/tests/fixtures/test.bin"
    original.parent.mkdir(parents=True); original.write_bytes(b"synthetic")
    monkeypatch.setattr(module, "REPOSITORY", repo)
    digest = module.sha256(original)
    spec = {"sha256": digest, "origin": {"kind": "project_fixture", "repository_file": "sandbox/tests/fixtures/test.bin", "sha256": digest}}
    module.fixture_origin(spec)
    spec["sha256"] = "0"*64
    with pytest.raises(ValueError, match="checksum"): module.fixture_origin(spec)
