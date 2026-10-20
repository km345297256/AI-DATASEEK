"""No sandbox, model, or filesystem writes: exercise phase decisions only."""
import hashlib

import pytest

from app.domain.services.artifact_write_progress import ArtifactWriteProgress


def write(content="complete source", path="/home/ubuntu/scripts/analysis.py", **kwargs):
    return {"name": "file_write", "id": "irrelevant-call-id",
            "args": {"file": path, "content": content, **kwargs}}


def patch(old="before", new="after", path="/home/ubuntu/scripts/analysis.py"):
    return {"name": "file_str_replace", "id": "irrelevant-call-id",
            "args": {"file": path, "old_str": old, "new_str": new}}


def saved(policy, call, **kwargs):
    assert policy.before_call(call).action == "allow"
    policy.record_success(call, **kwargs)


@pytest.mark.parametrize("suffix", [".py", ".PY", ".r", ".R", ".js", ".ts", ".sh", ".ipynb"])
def test_complete_program_moves_to_execution_instead_of_redrafting(suffix):
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis" + suffix
    saved(policy, write("first saved program", path))
    decision = policy.before_call(write("another whole draft", path))
    assert decision.action == "execute_saved"
    assert decision.path == path and decision.kind == "program"
    assert decision.reason == "saved_content_ready"


@pytest.mark.parametrize("suffix", [".md", ".MD", ".txt", ".csv", ".json", ".svg", ".html"])
def test_saved_user_artifacts_move_to_delivery_not_failure(suffix):
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/output/result" + suffix
    saved(policy, write("already saved result", path))
    for i in range(20):
        decision = policy.before_call(write(f"cosmetic new wording {i}", path))
        assert decision.action == "finalize_saved"
        assert decision.path == path and decision.kind == "report"
    assert not hasattr(policy, "should_stop")


def test_unchanged_complete_bytes_are_reused_despite_call_id_and_path_spelling():
    policy = ArtifactWriteProgress()
    saved(policy, write("first\n"))
    call = write("first", "/home/ubuntu/scripts/./analysis.py", trailing_newline=True, append=None)
    call["id"] = "another-call"
    assert policy.before_call(call).action == "reuse"


def test_empty_file_preparation_does_not_claim_content_ready():
    policy = ArtifactWriteProgress()
    saved(policy, write(""))
    saved(policy, write("now complete"))
    assert policy.before_call(write("redraft")).action == "execute_saved"


def test_arbitrarily_many_distinct_incremental_chunks_are_not_a_write_quota():
    policy = ArtifactWriteProgress()
    saved(policy, write("initial section"))
    for i in range(200):
        saved(policy, write(f"\nsection {i}\n", append=True))
        saved(policy, patch(f"placeholder-{i}", f"filled-{i}"))
    assert policy.before_call(write("whole draft again")).action == "execute_saved"


@pytest.mark.parametrize("path,action", [
    ("/home/ubuntu/scripts/analysis.py", "inspect_saved"),
    ("/home/ubuntu/output/report.md", "finalize_saved"),
])
@pytest.mark.parametrize("operation", ["append", "replace"])
def test_incremental_replay_is_not_an_unlimited_bypass(path, action, operation):
    policy = ArtifactWriteProgress()
    saved(policy, write("existing content", path))
    item = write("same section", path, append=True) if operation == "append" else patch("old", "new", path)
    saved(policy, item)
    assert policy.before_call(item).action == action
    assert policy.before_call(item).reason == "repeated_incremental_edit"


def test_inverse_patch_without_new_feedback_changes_phase():
    policy = ArtifactWriteProgress()
    saved(policy, write("before"))
    saved(policy, patch("before", "after"))
    decision = policy.before_call(patch("after", "before"))
    assert decision.action == "inspect_saved" and decision.reason == "reversed_incremental_edit"


def test_new_saved_inspection_allows_one_targeted_edit_not_a_whole_redraft():
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    saved(policy, write("nested structural blocks", path))
    chunk = write("}\n", path, append=True)
    saved(policy, chunk)
    assert policy.before_call(chunk).action == "inspect_saved"
    assert policy.record_saved_inspection(path, "newly observed current code section")
    assert policy.before_call(write("unnecessary full rewrite", path)).action == "execute_saved"
    saved(policy, chunk)
    assert policy.before_call(chunk).action == "inspect_saved"
    assert not policy.record_saved_inspection(path, "newly observed current code section")
    assert policy.before_call(chunk).action == "inspect_saved"
    assert policy.record_saved_inspection(path, "next observed code section")
    saved(policy, chunk)


def test_empty_append_and_identity_patch_reuse_saved_content():
    policy = ArtifactWriteProgress()
    saved(policy, write("saved"))
    assert policy.before_call(write("", append=True)).action == "reuse"
    assert policy.before_call(patch("same", "same")).action == "reuse"


def test_known_whole_file_hash_after_append_can_detect_equal_overwrite():
    policy = ArtifactWriteProgress()
    saved(policy, write("a"))
    saved(policy, write("b", append=True), current_digest=hashlib.sha256(b"ab").hexdigest())
    assert policy.before_call(write("ab")).action == "reuse"


def test_unobserved_incremental_bytes_never_claim_equal_whole_content():
    policy = ArtifactWriteProgress()
    saved(policy, write("a"))
    saved(policy, write("b", append=True))
    assert policy.before_call(write("a")).action == "execute_saved"
    assert policy.before_call(write("a")).reason == "saved_version_cycle"
    assert policy.before_call(write("ab")).action == "execute_saved"


@pytest.mark.parametrize("outcome", ["succeeded", "failed", 0, 1])
def test_confirmed_execution_opens_a_new_revision_phase(outcome):
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    saved(policy, write("first program", path))
    assert policy.record_execution(path, outcome=outcome, source_digest="a" * 64, operation_id="run-1")
    saved(policy, write("feedback-based correction", path))
    assert policy.before_call(write("unjustified redraft", path)).action == "execute_saved"
    assert not policy.record_execution(path, outcome=outcome, source_digest="a" * 64, operation_id="run-1")
    assert policy.before_call(write("unjustified redraft", path)).action == "execute_saved"


@pytest.mark.parametrize("outcome", ["pending", "unknown", "not_started", True, None, {}])
def test_unconfirmed_execution_cannot_grant_rewrite(outcome):
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    saved(policy, write("saved", path))
    assert not policy.record_execution(path, outcome=outcome, source_digest="a" * 64)
    assert policy.before_call(write("redraft", path)).action == "execute_saved"


def test_new_input_evidence_is_consumed_and_same_observation_cannot_reset_it():
    policy = ArtifactWriteProgress()
    saved(policy, write("first"))
    assert policy.record_observation("trusted new source bytes")
    saved(policy, write("updated with those input bytes"))
    assert not policy.record_observation("trusted new source bytes")
    assert policy.before_call(write("more changes without new input")).action == "execute_saved"


def test_target_bound_execution_or_diagnosis_cannot_reopen_another_artifact():
    policy = ArtifactWriteProgress()
    first, second = "/home/ubuntu/scripts/one.py", "/home/ubuntu/scripts/two.R"
    saved(policy, write("first", first))
    saved(policy, write("second", second))
    assert policy.record_diagnostic(first, "accepted diagnosis")
    saved(policy, write("fix only first", first))
    assert policy.before_call(write("redraft second", second)).action == "execute_saved"


def test_reverting_a_version_is_allowed_after_actual_new_execution_feedback():
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    saved(policy, write("version A", path))
    policy.record_execution(path, outcome="failed", source_digest="a" * 64, operation_id="one")
    saved(policy, write("version B", path))
    assert policy.before_call(write("version A", path)).reason == "saved_version_cycle"
    policy.record_execution(path, outcome="failed", source_digest="b" * 64, operation_id="two")
    saved(policy, write("version A", path))


def test_successful_phases_do_not_accumulate_a_total_task_limit():
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    for i in range(100):
        saved(policy, write(f"phase {i}", path))
        assert policy.record_execution(path, outcome="succeeded", source_digest=hashlib.sha256(str(i).encode()).hexdigest())


def test_failed_or_merely_proposed_write_is_not_recorded():
    policy = ArtifactWriteProgress()
    item = write()
    for _ in range(20):
        assert policy.before_call(item).action == "allow"


@pytest.mark.parametrize("path", ["/home/ubuntu/config/settings.json", "relative.py", "~/code.py", "/bad\x00name.py"])
def test_policy_does_not_invent_a_target_or_a_report_contract(path):
    policy = ArtifactWriteProgress()
    policy.record_success(write("one", path))
    assert policy.before_call(write("two", path)).action == "allow"


def test_operation_history_retains_no_source_or_replacement_bodies():
    policy = ArtifactWriteProgress()
    saved(policy, write("PRIVATE_FULL_SOURCE"))
    saved(policy, patch("PRIVATE_OLD", "PRIVATE_NEW"))
    assert "PRIVATE_" not in repr(policy.__dict__)


def test_untracked_mutation_invalidates_reuse_without_erasing_phase_history():
    policy = ArtifactWriteProgress()
    saved(policy, write("old"))
    assert policy.before_call(write("old")).action == "reuse"
    policy.invalidate_content()
    assert policy.before_call(write("old")).action == "execute_saved"
    assert policy.before_call(write("old")).reason == "saved_version_cycle"


def test_program_execution_does_not_claim_current_source_or_other_artifact_bytes():
    policy = ArtifactWriteProgress()
    path, report = "/home/ubuntu/scripts/analysis.py", "/home/ubuntu/output/result.md"
    saved(policy, write("source", path))
    saved(policy, write("report", report))
    policy.record_execution(path, outcome=0, source_digest="a" * 64)
    # Same source may have been deleted/replaced during execution: allow the
    # new authorized preparation, never synthesize a reuse receipt for it.
    assert policy.before_call(write("source", path)).action == "allow"
    assert policy.before_call(write("report", report)).action == "finalize_saved"


def test_invalid_observations_do_not_grant_revision_authority():
    policy = ArtifactWriteProgress()
    path = "/home/ubuntu/scripts/analysis.py"
    saved(policy, write("source", path))
    assert not policy.record_diagnostic(path, "")
    assert not policy.record_diagnostic(path, None)
    assert not policy.record_observation("evidence", target="relative.py")
    assert not policy.record_execution(path, outcome=0, source_digest="invalid")
    assert policy.before_call(write("draft", path)).action == "execute_saved"
