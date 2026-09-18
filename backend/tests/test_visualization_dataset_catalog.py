"""Portable integrity checks for the ten reviewed visualization datasets.

These tests inspect repository metadata only: they never read the host Data
directory, download a source, execute scientific files, or register datasets.
The installer/importer separately verify the bytes through the host's validated
read-only bind; here every declaration is checked against its pinned descriptor.
"""
from collections import Counter
from datetime import date
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from app.domain.models.dataset import CuratedDatasetSeed
from app.domain.models.visualization import VisualizationPlugin
from app.domain.services.domain_presets import get_domain_preset


BACKEND = Path(__file__).resolve().parents[1]
RESOURCES = BACKEND / "app/resources"
CATALOG = RESOURCES / "visualization-datasets/open-science"
DESCRIPTORS = RESOURCES / "visualization-descriptors/open-science.json"
PLUGINS = BACKEND.parent / "plugin-host/visualizations"
VIEW_KINDS = {
    "image", "map", "series", "table", "text", "structure", "document",
    "tree", "media", "graph",
}
SHA256 = re.compile(r"[0-9a-f]{64}")
SECRET_QUERY_TERMS = (
    "token", "signature", "credential", "secret", "access_key", "api_key", "password",
)
HOST_METADATA_KEYS = {
    "storage_directory", "source_path", "host_path", "host_root", "data_root",
    "staging_root", "inspection_directory", "inspection_root", "mount_source",
    "absolute_path", "local_path",
}


@pytest.fixture(scope="module")
def catalog():
    paths = sorted(CATALOG.glob("*.json"))
    assert len(paths) == 10, "All ten reviewed manifests must be installed together"
    return [(path, CuratedDatasetSeed.model_validate_json(path.read_text(encoding="utf-8")))
            for path in paths]


@pytest.fixture(scope="module")
def descriptors():
    items = json.loads(DESCRIPTORS.read_text(encoding="utf-8"))
    assert isinstance(items, list) and len(items) == 10
    assert all(isinstance(item, dict) for item in items)
    assert len({item["dataset_id"] for item in items}) == len(items)
    return {item["dataset_id"]: item for item in items}


def _relative_file(value):
    assert isinstance(value, str) and value
    path = PurePosixPath(value)
    assert not path.is_absolute() and str(path) == value
    assert not any(part in {".", ".."} for part in path.parts)
    assert "\\" not in value and not any(ord(char) < 32 for char in value)
    return path


def _public_https(value):
    assert isinstance(value, str) and value
    parsed = urlsplit(value)
    assert parsed.scheme == "https" and parsed.hostname
    assert parsed.username is None and parsed.password is None
    assert parsed.port is None and not parsed.fragment
    hostname = parsed.hostname.lower().rstrip(".")
    assert hostname != "localhost" and not hostname.endswith((".localhost", ".local"))
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        assert "." in hostname
    else:
        assert address.is_global
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        key = unquote(key).casefold()
        assert not any(term in key for term in SECRET_QUERY_TERMS)


def _no_host_paths(value):
    if isinstance(value, dict):
        for key, item in value.items():
            # Normalize camelCase as well as underscore spellings.
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).casefold()
            assert normalized not in HOST_METADATA_KEYS, key
            _no_host_paths(item)
    elif isinstance(value, list):
        for item in value:
            _no_host_paths(item)
    elif isinstance(value, str):
        decoded = unquote(unquote(value))
        assert not decoded.startswith(("/", "~/", "\\\\"))
        assert not re.search(r"(?:^|[\s\"'(])[A-Za-z]:[\\/]", decoded)
        assert "file://" not in decoded.casefold()
        assert not re.search(r"/(?:Users|home|private|Volumes|workspace|tmp|var/folders)/", decoded)


def _verify_derivation_graph(provenance):
    """Every bounded derivation chain must terminate at a preserved URL source."""
    assert 1 <= len(provenance) <= 32
    verified = set()

    def visit(path, ancestors):
        _relative_file(path)
        assert path != "SOURCE.md" and path in provenance, path
        assert path not in ancestors, "Cyclic or self-referencing derivation"
        assert len(ancestors) < 32, "Derivation depth exceeds the file inventory limit"
        if path in verified:
            return
        file = provenance[path]
        if "derived_from" in file:
            assert not file.get("url"), "A generated output must not claim a fictitious download URL"
            parents = file["derived_from"]
            assert isinstance(parents, list) and parents
            assert all(isinstance(parent, str) for parent in parents)
            assert len(set(parents)) == len(parents)
            for parent in parents:
                visit(parent, ancestors | {path})
        else:
            assert file.get("url"), "Derivation must terminate at a preserved upstream source"
            _public_https(file["url"])
        verified.add(path)

    for path in provenance:
        visit(path, set())


def test_catalog_covers_each_current_view_kind_once_with_complete_inventory(catalog):
    assert Counter(seed.metadata["visualization_view_kind"] for _, seed in catalog) == {
        kind: 1 for kind in VIEW_KINDS
    }
    ids = set()
    for path, seed in catalog:
        assert seed.dataset_id == path.stem and seed.dataset_id not in ids
        assert re.fullmatch(r"viz-[a-z0-9][a-z0-9-]{1,110}[a-z0-9]", seed.dataset_id)
        ids.add(seed.dataset_id)
        assert seed.data_center_id == "reviewed-open-science-catalog"
        assert seed.name.strip() and seed.description.strip()
        assert get_domain_preset(seed.domain) is not None
        metadata = seed.metadata
        assert metadata["source_catalog"] == path.parent.name == "open-science"
        assert metadata["curated"] is True and metadata["inventory_complete"] is True
        assert type(metadata["catalog_version"]) is int and metadata["catalog_version"] == 1
        assert date.fromisoformat(metadata["download_date"]).isoformat() == metadata["download_date"]
        assert metadata["recursive_file_count"] == len(seed.files)
        assert metadata["total_size_bytes"] == sum(file.size for file in seed.files)
        assert 0 < metadata["total_size_bytes"] <= 128 * 1024**2
        assert len({file.path.casefold() for file in seed.files}) == len(seed.files)
        inventory = {file.path: file for file in seed.files}
        source_note = inventory["SOURCE.md"]
        assert source_note.role == "documentation" and source_note.content_type == "text/markdown"
        entry = inventory[metadata["visualization_entry_file"]]
        assert entry.role == "data" and entry.path != "SOURCE.md"
        for file in seed.files:
            relative = _relative_file(file.path)
            assert file.path == "SOURCE.md" or relative.parts[0].casefold() != "source.md"
            assert type(file.size) is int and file.size > 0
            assert isinstance(file.sha256, str) and SHA256.fullmatch(file.sha256)
            assert file.role in {"data", "documentation"} and file.content_type


def test_canonical_descriptors_match_manifests_and_every_file_declaration(catalog, descriptors):
    assert set(descriptors) == {seed.dataset_id for _, seed in catalog}
    for _, seed in catalog:
        item = descriptors[seed.dataset_id]
        digest = hashlib.sha256(json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        metadata = seed.metadata
        assert metadata["source_descriptor_sha256"] == digest
        for key in ("name", "description", "domain"):
            assert getattr(seed, key) == item[key]
        assert seed.external_id == item.get("doi", seed.dataset_id)
        for item_key, metadata_key in (
            ("view_kind", "visualization_view_kind"),
            ("plugin_id", "visualization_plugin_id"),
            ("entry_file", "visualization_entry_file"),
            ("sample_scope", "sample_note"),
        ):
            assert item[item_key] == metadata[metadata_key]
        for key in ("publisher", "source_url", "license", "license_url", "license_details",
                    "authors", "source_version", "sample_scope", "doi", "transformation",
                    "suggested_questions"):
            if key in item:
                assert metadata[key] == item[key]
        assert metadata["provenance"] == item["files"]
        inventory = {file.path: file for file in seed.files}
        assert len({file["path"] for file in item["files"]}) == len(item["files"])
        assert set(inventory) == {file["path"] for file in item["files"]} | {"SOURCE.md"}
        for declaration in item["files"]:
            file = inventory[declaration["path"]]
            assert type(declaration["size"]) is int
            assert (file.size, file.sha256, file.role) == (
                declaration["size"], declaration["sha256"], declaration.get("role", "data"),
            )


def test_upstream_sources_have_explicit_public_data_licenses_and_scope(catalog):
    for _, seed in catalog:
        metadata = seed.metadata
        for key in ("publisher", "license", "license_details", "sample_scope", "source_version"):
            assert isinstance(metadata[key], str) and metadata[key].strip(), (seed.dataset_id, key)
        _public_https(metadata["source_url"])
        _public_https(metadata["license_url"])
        assert isinstance(metadata["authors"], list)
        assert all(isinstance(author, str) and author.strip() for author in metadata["authors"])
        for file in metadata["provenance"]:
            if file.get("url"):
                _public_https(file["url"])
            else:
                assert file.get("derived_from"), (seed.dataset_id, file["path"])


def test_derived_files_reference_preserved_originals_in_the_same_dataset(catalog):
    for _, seed in catalog:
        provenance = {file["path"]: file for file in seed.metadata["provenance"]}
        if any("derived_from" in file for file in provenance.values()):
            assert seed.metadata.get("transformation"), seed.dataset_id
        _verify_derivation_graph(provenance)


def test_derivation_validation_accepts_archive_extraction_then_quoted_tree():
    _verify_derivation_graph({
        "source.zip": {"url": "https://example.org/source.zip"},
        "source.nwk": {"derived_from": ["source.zip"]},
        "quoted.nwk": {"derived_from": ["source.nwk"]},
    })


@pytest.mark.parametrize("provenance", [
    {"a.nwk": {"derived_from": ["a.nwk"]}},
    {"a.nwk": {"derived_from": ["b.nwk"]}, "b.nwk": {"derived_from": ["a.nwk"]}},
    {"a.nwk": {"derived_from": ["missing.nwk"]}},
    {"a.nwk": {"derived_from": ["b.nwk"]}, "b.nwk": {}},
    {"a.nwk": {"derived_from": []}},
])
def test_derivation_validation_rejects_cycles_missing_sources_and_empty_chains(provenance):
    with pytest.raises(AssertionError):
        _verify_derivation_graph(provenance)


def test_preview_entry_matches_a_shipped_cordis_plugin_kind_and_input_limit(catalog):
    plugins = {}
    for path in PLUGINS.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["id"] not in plugins
        plugins[raw["id"]] = raw
    for _, seed in catalog:
        metadata = seed.metadata
        plugin = VisualizationPlugin.model_validate(plugins[metadata["visualization_plugin_id"]])
        assert plugin.view_kind == metadata["visualization_view_kind"]
        entry = next(file for file in seed.files if file.path == metadata["visualization_entry_file"])
        assert plugin.matches_filename(PurePosixPath(entry.path).name), (seed.dataset_id, plugin.id)
        assert entry.size <= plugin.limits.max_input_bytes, (seed.dataset_id, plugin.id)


def test_public_descriptors_and_manifests_contain_no_host_paths(catalog, descriptors):
    for item in descriptors.values():
        _no_host_paths(item)
    for _, seed in catalog:
        _no_host_paths(seed.model_dump())


def test_visualization_dataset_ids_do_not_replace_any_of_the_existing_fifty(catalog):
    old_paths = sorted((RESOURCES / "datasets").glob("*/manifest.json"))
    old_paths += sorted((RESOURCES / "external-datasets").glob("*/*.json"))
    assert len(old_paths) == 50
    old_ids = [json.loads(path.read_text(encoding="utf-8"))["dataset_id"] for path in old_paths]
    assert len(set(old_ids)) == 50
    assert not ({seed.dataset_id for _, seed in catalog} & set(old_ids))
