"""Two-reader evidence and source-based Git review, isolated from deployments."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.domain.utils.persistence_contracts import canonical, check_history, review_catalogs

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/persistence_review.py"
SPEC = importlib.util.spec_from_file_location("dataseek_persistence_review", SCRIPT)
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)


def catalog(values=("pending", "done"), *, version=1):
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": list(values)}}}
    body = {"writer_version": version, "storage": "durable", "read_schema": schema, "write_schema": deepcopy(schema)}
    return {"format_version": 1, "roots": {"Synthetic": {**body, "sha256": hashlib.sha256(canonical(body).encode()).hexdigest()}}}


def history(tmp_path, *, readers, filename="new.json", bump=False):
    directory = tmp_path / "docs/persistence"
    directory.mkdir(parents=True)
    test = tmp_path / "backend/tests/test_migration.py"
    test.parent.mkdir(parents=True)
    test.write_text("# synthetic behavior-test placeholder\n")
    before = catalog()
    after = catalog(("pending",) if bump else ("pending", "done", "interrupted"), version=2 if bump else 1)
    (directory / "old.json").write_text(json.dumps(before))
    (directory / filename).write_text(json.dumps(after))
    ack = {"reason": "synthetic transition", "compatibility": "synthetic evidence",
           "test": "backend/tests/test_migration.py"}
    if readers is not None:
        ack["readers"] = readers
    (directory / "history.json").write_text(json.dumps({"format_version": 1, "snapshots": [
        {"file": "old.json"}, {"file": filename, "changes": {"Synthetic": ack}}]}))
    return before, after


@pytest.mark.parametrize("readers", [None, {}, [], "safe", {"new_reads_old": "tested"},
    {"new_reads_old": "tested", "old_reads_new": " "},
    {"new_reads_old": True, "old_reads_new": "refuses"}])
def test_new_ack_requires_both_nonempty_reader_explanations(tmp_path, readers):
    _, after = history(tmp_path, readers=readers)
    with pytest.raises(ValueError, match="both reader directions"):
        check_history(tmp_path, after)


def test_legacy_filename_cannot_exempt_a_new_acknowledgement(tmp_path):
    _, after = history(tmp_path, readers=None, filename="2026-09-30-program-preparation.json")
    with pytest.raises(ValueError, match="both reader directions"):
        check_history(tmp_path, after)


@pytest.mark.parametrize("bump", [False, True])
def test_complete_two_reader_ack_and_existing_writer_gate(tmp_path, bump):
    before, after = history(tmp_path, readers={"new_reads_old": "old records preserved or migrated",
        "old_reads_new": "unsupported versions refused before mutation"}, bump=bump)
    assert check_history(tmp_path, after) == []
    report = review_catalogs(before, after)
    assert report["changes"]  # Acknowledgement does not hide the transition.
    assert report["semantic_compatibility_verified"] is False


def test_enum_expansion_reports_reverse_reader_hazard():
    change = review_catalogs(catalog(), catalog(("pending", "done", "interrupted")))["changes"][0]
    assert change["classification"] == "compatible"
    assert change["old_reader_structural_classification"] == "breaking"
    assert change["reader_evidence_required"] == ["new_reads_old", "old_reads_new"]


def test_new_root_requires_reader_evidence_and_equal_catalog_has_no_changes():
    before = catalog()
    after = deepcopy(before)
    after["roots"]["NewRoot"] = deepcopy(after["roots"]["Synthetic"])
    assert review_catalogs(before, after)["changes"][0]["old_reader_structural_classification"] == "breaking"
    assert review_catalogs(before, before)["changes"] == []


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    source = repo / "backend/app/domain/utils/persistence_contracts.py"
    source.parent.mkdir(parents=True)

    def commit(values):
        # These checks execute in the isolated extraction process.
        source.write_text("import os,pathlib\n"
            "assert 'DATASEEK_TEST_SECRET' not in os.environ\n"
            "assert not pathlib.Path('.env').exists()\n"
            f"def current_catalog(): return {catalog(values)!r}\n")
        git(repo, "add", "backend/app")
        git(repo, "-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid",
            "commit", "-q", "-m", "synthetic schema fixture")
        return git(repo, "rev-parse", "HEAD")

    return repo, source, commit


def test_ref_review_extracts_source_not_acknowledgement_or_worktree(source_repo, monkeypatch):
    repo, source, commit = source_repo
    before = commit(("pending", "done"))
    after = commit(("pending", "done", "interrupted"))
    source.write_text("raise RuntimeError('working tree must not execute')\n")
    (repo / ".env").write_text("DATASEEK_TEST_SECRET=must-not-load\n")
    monkeypatch.setenv("DATASEEK_TEST_SECRET", "must-not-inherit")
    status = git(repo, "status", "--porcelain")
    before_id, before_catalog = REVIEW.catalog_at_ref(repo, before)
    after_id, after_catalog = REVIEW.catalog_at_ref(repo, "HEAD")
    assert (before_id, after_id) == (before, after)
    assert review_catalogs(before_catalog, after_catalog)["changes"]
    assert git(repo, "status", "--porcelain") == status
    assert git(repo, "rev-parse", "HEAD") == after


@pytest.mark.parametrize("ref", ["--help", "missing-ref", "", "HEAD\x00bad"])
def test_invalid_refs_fail_closed_without_checkout(source_repo, ref):
    repo, _, commit = source_repo
    head = commit(("pending", "done"))
    with pytest.raises(ValueError):
        REVIEW.catalog_at_ref(repo, ref)
    assert git(repo, "rev-parse", "HEAD") == head


def test_source_failure_is_not_replaced_by_acknowledged_snapshot(source_repo):
    repo, source, commit = source_repo
    commit(("pending", "done"))
    source.write_text("raise RuntimeError('synthetic private diagnostics')\n")
    git(repo, "add", "backend/app")
    git(repo, "-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid",
        "commit", "-q", "-m", "synthetic invalid schema fixture")
    with pytest.raises(ValueError, match="Schema extraction failed") as raised:
        REVIEW.catalog_at_ref(repo, "HEAD")
    assert "private diagnostics" not in str(raised.value)


def test_source_archive_rejects_symlinks(source_repo):
    repo, source, commit = source_repo
    commit(("pending", "done"))
    (source.parent / "linked.py").symlink_to("/private/tmp/outside-schema.py")
    git(repo, "add", "backend/app")
    git(repo, "-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid",
        "commit", "-q", "-m", "synthetic symlink fixture")
    with pytest.raises(ValueError, match="Unsupported entry"):
        REVIEW.catalog_at_ref(repo, "HEAD")
