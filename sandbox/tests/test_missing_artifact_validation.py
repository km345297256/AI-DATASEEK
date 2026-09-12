"""Absence is repairable only after a read-only, no-follow ancestry proof."""
import json
import os

import pytest

from app.services import artifact_manifest as manifest
from app.services import artifact_validation as validation


def receipt(path, root, kind="table"):
    return validation.validate_artifacts([{"path": str(path), "kind": kind}], root=root)["files"][0]


@pytest.mark.parametrize("name,kind", [
    ("arbitrary plot.png", "image"), ("任意统计.json", "report"),
    ("measurements.csv", "table"), ("nested/deeper/new-table.csv", "table"),
])
def test_safe_missing_file_or_descendant_is_reported_without_creating_anything(tmp_path, name, kind):
    root = tmp_path / "output"
    root.mkdir()
    result = receipt(root / name, root, kind)
    assert result["valid"] is False and result["reason"] == "missing_artifact"
    assert result["sha256"] is None and result["size"] is None
    assert result["diagnostics"] == {} and result["metadata"] == {}
    assert list(root.iterdir()) == []


def test_output_root_may_be_missing_only_below_a_verified_existing_parent(tmp_path):
    root = tmp_path / "output"
    assert receipt(root / "table.csv", root)["reason"] == "missing_artifact"
    assert not root.exists()
    unavailable_root = tmp_path / "absent-runtime-parent" / "output"
    result = receipt(unavailable_root / "table.csv", unavailable_root)
    assert result["reason"] == "unavailable_or_unsafe_path"
    assert not unavailable_root.parent.exists()


@pytest.mark.parametrize("position", ["ancestor", "root", "descendant", "leaf"])
def test_existing_or_dangling_symlinks_never_become_repairable_missing(tmp_path, position):
    actual = tmp_path / "actual"
    actual.mkdir()
    root = tmp_path / "output"
    if position == "ancestor":
        linked = tmp_path / "linked-parent"
        linked.symlink_to(actual, target_is_directory=True)
        root = linked / "output"
    elif position == "root":
        root.symlink_to(actual, target_is_directory=True)
    else:
        root.mkdir()
        if position == "descendant":
            (root / "nested").symlink_to(actual, target_is_directory=True)
        else:
            (root / "table.csv").symlink_to(actual / "absent.csv")
    path = root / "nested/table.csv" if position == "descendant" else root / "table.csv"
    result = receipt(path, root)
    assert not result["valid"] and result["reason"] == "unavailable_or_unsafe_path"
    assert result["sha256"] is None and result["size"] is None
    assert list(actual.iterdir()) == []


def test_permissions_and_directory_targets_remain_nonrepairable(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    path = root / "directory.csv"
    path.mkdir()
    assert receipt(path, root)["reason"] == "unavailable_or_unsafe_path"
    original_open = os.open

    def denied(path, *args, **kwargs):
        if path == "denied.csv":
            raise PermissionError("private OS diagnostic must not leave validator")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(manifest.os, "open", denied)
    result = receipt(root / "denied.csv", root)
    assert result["reason"] == "unavailable_or_unsafe_path"
    assert "private OS" not in json.dumps(result)


def test_replaced_output_directory_cannot_supply_a_missing_proof(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    original_open = os.open
    changed = False

    def replace_parent(path, *args, **kwargs):
        nonlocal changed
        if path == "table.csv" and not changed:
            changed = True
            root.rename(tmp_path / "old-output")
            root.mkdir()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(manifest.os, "open", replace_parent)
    assert receipt(root / "table.csv", root)["reason"] == "unavailable_or_unsafe_path"


def test_file_appearing_between_absence_checks_is_not_reported_missing(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    path = root / "table.csv"
    original_open = manifest._open_output_once
    changed = False

    def appears(*args):
        nonlocal changed
        try:
            return original_open(*args)
        except manifest._MissingOutputEntry:
            if not changed:
                changed = True
                path.write_text("one,two\n1,2\n")
            raise

    monkeypatch.setattr(manifest, "_open_output_once", appears)
    assert receipt(path, root)["reason"] == "unavailable_or_unsafe_path"


def test_disappearance_after_reading_started_is_changed_not_missing(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    path = root / "table.csv"
    path.write_text("one,two\n1,2\n")
    original_open = validation._open_output

    class DisappearingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            content = self.stream.read(size)
            path.unlink(missing_ok=True)
            return content

    def open_once(*args):
        return DisappearingReader(original_open(*args))

    monkeypatch.setattr(validation, "_open_output", open_once)
    result = receipt(path, root)
    assert result["reason"] == "changed_during_read"
    assert result["sha256"] is None and result["size"] is None


@pytest.mark.parametrize("relative", ["../private.csv", "nested/../../private.csv"])
def test_path_escape_is_not_absence_even_when_target_is_missing(tmp_path, relative):
    root = tmp_path / "output"
    root.mkdir()
    assert receipt(root / relative, root)["reason"] == "unavailable_or_unsafe_path"


def test_non_directory_parent_is_unsafe_and_malformed_image_is_still_content_failure(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    (root / "file-parent").write_text("not a directory")
    assert receipt(root / "file-parent/table.csv", root)["reason"] == "unavailable_or_unsafe_path"
    image = root / "chart.png"
    image.write_bytes(b"not an actual image")
    result = receipt(image, root, "image")
    assert result["reason"] == "invalid_content"
    assert result["sha256"] is not None and result["size"] == image.stat().st_size


def test_invalid_path_on_read_back_is_changed_not_missing_or_content_failure(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    path = root / "table.csv"
    path.write_text("one,two\n1,2\n")
    original_open = validation._open_output
    calls = 0

    def changed_path(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("unsafe replacement")
        return original_open(*args)

    monkeypatch.setattr(validation, "_open_output", changed_path)
    assert receipt(path, root)["reason"] == "changed_during_read"


def test_manifest_keeps_its_existing_generic_error_contract(tmp_path):
    root = tmp_path / "output"
    path = str(root / "table.csv")
    assert manifest.fingerprint_artifacts([path], root=root) == {
        "version": 1, "files": [], "errors": [{"path": path, "code": "unavailable_or_changed"}],
    }
