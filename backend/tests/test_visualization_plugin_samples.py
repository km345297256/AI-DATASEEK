"""Portable checks of the per-plugin sample inventory (no host files/network)."""
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath

from app.domain.models.dataset import CuratedDatasetSeed
from app.domain.models.visualization import VisualizationPlugin
from app.domain.services.domain_presets import get_domain_preset


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
RESOURCES = BACKEND / "app/resources"
KINDS = {"scientific_original", "scientific_derived", "official_fixture", "project_fixture"}
spec = importlib.util.spec_from_file_location("plugin_sample_preparer", BACKEND / "scripts/prepare_visualization_datasets.py")
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def catalogs():
    paths = sorted((RESOURCES / "datasets").glob("*/manifest.json"))
    paths += sorted((RESOURCES / "external-datasets").glob("*/*.json"))
    paths += sorted((RESOURCES / "visualization-datasets").glob("*/*.json"))
    paths += sorted((RESOURCES / "visualization-plugin-datasets").glob("*/*.json"))
    seeds = [CuratedDatasetSeed.model_validate(load(p)) for p in paths]
    assert len({s.dataset_id for s in seeds}) == len(seeds)
    return {s.dataset_id: s for s in seeds}


def plugins():
    return {p["id"]: p for p in map(load, sorted((ROOT / "plugin-host/visualizations").glob("*.json")))}


def descriptors():
    return [item for path in sorted((RESOURCES / "visualization-descriptors").glob("plugin-tests-*.json")) for item in load(path)]


def no_host_paths(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in {"storage_directory", "host_path", "source_path", "staging_root", "inspection_root"}
            no_host_paths(item)
    elif isinstance(value, list):
        for item in value:
            no_host_paths(item)
    elif isinstance(value, str):
        assert not any(term in value for term in ("/Users/", "/private/", "file://", "/home/", "/workspace/"))


def test_matrix_covers_every_shipped_plugin_once_with_a_pinned_entry():
    rows = load(RESOURCES / "visualization-plugin-test-matrix.json")
    current, seeds = plugins(), catalogs()
    assert len(rows) == len({row["plugin_id"] for row in rows}) == len(current)
    assert {row["plugin_id"] for row in rows} == set(current)
    for row in rows:
        plugin = VisualizationPlugin.model_validate(current[row["plugin_id"]])
        seed = seeds[row["dataset_id"]]
        file = next(f for f in seed.files if f.path == row["entry_file"])
        assert file.role == "data"
        assert plugin.matches_filename(PurePosixPath(file.path).name)
        assert row["expected_file_sha256"] == file.sha256
        assert row["expected_file_bytes"] == file.size
        assert row["sample_kind"] in KINDS
        assert row["test_steps"] and all(isinstance(s, str) and s.strip() for s in row["test_steps"])
        assert row["expected"].strip()
        if plugin.capabilities.input_mode == "whole":
            assert file.size <= plugin.limits.max_input_bytes
        no_host_paths(row)


def test_new_manifests_match_frozen_descriptors_and_complete_inventory():
    items = descriptors()
    paths = sorted((RESOURCES / "visualization-plugin-datasets/plugin-tests").glob("*.json"))
    assert len(items) == len({i["dataset_id"] for i in items}) == len(paths)
    seeds = {p.stem: CuratedDatasetSeed.model_validate(load(p)) for p in paths}
    assert {i["dataset_id"] for i in items} == set(seeds)
    for item in items:
        seed = seeds[item["dataset_id"]]
        metadata = seed.metadata
        assert seed.dataset_id == seed.external_id or seed.external_id == item.get("doi")
        assert metadata["source_catalog"] == "plugin-tests"
        assert metadata["sample_kind"] == item["sample_kind"] in KINDS
        digest = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert metadata["source_descriptor_sha256"] == digest
        assert metadata["provenance"] == item["files"]
        assert metadata["inventory_complete"] is True
        assert metadata["recursive_file_count"] == len(seed.files)
        assert metadata["total_size_bytes"] == sum(f.size for f in seed.files)
        assert metadata["total_size_bytes"] <= 4 * 1024**3
        assert get_domain_preset(seed.domain) is not None
        inventory = {f.path: f for f in seed.files}
        assert len(inventory) == len(seed.files)
        assert set(inventory) == {f["path"] for f in item["files"]} | {"SOURCE.md"}
        assert inventory["SOURCE.md"].role == "documentation"
        for declaration in item["files"]:
            file = inventory[declaration["path"]]
            assert (file.size, file.sha256, file.role) == (declaration["size"], declaration["sha256"], declaration.get("role", "data"))
            preparer.relative(file.path)
        no_host_paths(item)
        no_host_paths(seed.model_dump())


def test_provenance_terminates_at_preserved_sources_or_explicit_fixtures():
    for item in descriptors():
        preparer.public_url(item["source_url"])
        preparer.public_url(item["license_url"])
        assert all(item.get(key) for key in ("publisher", "license", "license_details", "authors", "sample_scope", "source_version"))
        preparer.validate_derivations(item["files"], profile="plugin-tests")
        for file in item["files"]:
            if "url" in file:
                preparer.public_url(file["url"])
            if "origin" in file:
                preparer.fixture_origin(file)
        if any("derived_from" in file for file in item["files"]):
            assert item.get("transformation"), item["dataset_id"]


def test_gigabyte_sample_uses_window_plugin_without_raising_runtime_limits():
    rows = load(RESOURCES / "visualization-plugin-test-matrix.json")
    row = next(r for r in rows if r["plugin_id"] == "viz-array-window")
    plugin = plugins()[row["plugin_id"]]
    assert row["expected_file_bytes"] > 1_000_000_000
    assert plugin["capabilities"]["input_mode"] == "window"
    assert plugin["limits"]["max_input_bytes"] == 8 * 1024**2
