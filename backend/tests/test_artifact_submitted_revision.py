"""Submitted source revisions stay distinct from current-file freshness."""
import hashlib
from types import SimpleNamespace

import pytest

from app.domain.services.artifact_write_progress import ArtifactWriteProgress


@pytest.fixture(autouse=True)
def synthetic_identity_key(monkeypatch):
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: SimpleNamespace(
        execution_snapshot_identity_key="synthetic-submitted-revision-key"))


PATH = "/home/ubuntu/output/analysis.R"


def write(body, **kwargs):
    return {"name": "file_write", "args": {"file": PATH, "content": body, **kwargs}}


def patch(old, new):
    return {"name": "file_str_replace", "args": {"file": PATH, "old_str": old, "new_str": new}}


def test_same_full_source_after_execution_is_a_real_write_but_not_a_new_revision():
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("old source"))
    policy.invalidate_content()
    policy.record_observation("actual terminal failure", target=PATH)
    assert policy.before_call(write("old source")).action == "allow"
    assert policy.record_success(write("old source")) is False
    assert policy.before_call(write("old source")).action == "reuse"


def test_new_full_source_after_failure_is_a_new_revision_and_is_consumed():
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("version one"))
    policy.invalidate_content()
    policy.record_observation("actual terminal failure", target=PATH)
    assert policy.record_success(write("version two"))
    policy.invalidate_content()
    policy.record_observation("next terminal failure", target=PATH)
    assert policy.record_success(write("version two")) is False
    assert policy.record_success(write("version three"))


def test_identical_newline_semantics_and_alias_do_not_create_new_revision():
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("source", leading_newline=True, trailing_newline=True))
    policy.invalidate_content()
    alias = write("\nsource\n")
    alias["args"]["file"] = "/home/ubuntu/output/./analysis.R"
    assert policy.record_success(alias) is False


@pytest.mark.parametrize("operation", [patch("old", "new"), write("extra", append=True)])
def test_same_incremental_submission_is_not_new_feedback_by_itself(operation):
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("original"))
    assert policy.record_success(operation)
    policy.invalidate_content()
    policy.record_observation("terminal failure", target=PATH)
    assert policy.record_success(operation) is False


def test_observed_changed_whole_bytes_can_establish_incremental_progress():
    policy = ArtifactWriteProgress()
    digest = lambda body: hashlib.sha256(body.encode()).hexdigest()
    assert policy.record_success(write("x"))
    operation = write("x", append=True)
    assert policy.record_success(operation, current_digest=digest("xx"))
    assert policy.record_success(operation, current_digest=digest("xxx"))
    assert policy.record_success(operation, current_digest=digest("xxx")) is False


def test_incremental_edit_invalidates_old_full_submission_identity_without_claiming_bytes():
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("first source"))
    assert policy.record_success(patch("first", "second"))
    # Reverting after a real patch changes the authored source, unlike simply
    # resubmitting the same complete source after a failed runtime.
    assert policy.record_success(write("first source"))


@pytest.mark.parametrize("operation", [write("", append=True), patch("x", "x")])
def test_empty_incremental_operations_cannot_establish_source_change(operation):
    policy = ArtifactWriteProgress()
    assert policy.record_success(write("original"))
    assert policy.record_success(operation) is False
    assert policy.record_success(write("original")) is False


def test_unsupported_mutation_does_not_claim_a_changed_source():
    assert ArtifactWriteProgress().record_success({"name": "plugin_write", "args": {}}) is False
