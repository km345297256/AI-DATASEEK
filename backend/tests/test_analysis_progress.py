import hashlib
import json

import pytest

from app.domain.services.analysis_progress import AnalysisProgressGuard, call_identity, program_identity


def call(name="file_write", **args):
    return {"name": name, "args": args, "id": "not-progress"}


def test_repeated_script_drafts_are_stopped_without_executing_them():
    guard = AnalysisProgressGuard()
    for source in ("first", "second"):
        item = call(file="/home/ubuntu/output/test.py", content=source)
        assert guard.before_call(item) is None
        guard.record(item, succeeded=True)
    assert "Execute" in guard.instruction()
    assert "NOT executed" in guard.before_call(call(file="/home/ubuntu/output/test.py", content="third"))
    assert guard.stalled and not guard.evidence


def test_only_bound_program_execution_allows_revision_not_plain_read_or_shell():
    guard = AnalysisProgressGuard()
    item = call(file="/home/ubuntu/output/test.py", content="draft")
    guard.record(item, succeeded=True)
    guard.record(item, succeeded=True)
    guard.record(call("file_read", file="/home/ubuntu/output/test.py"), succeeded=True)
    assert guard.before_call(item)
    guard.record(call("shell_run", command="bounded-analysis"), succeeded=True, confirmed_execution=True)
    assert guard.before_call(item)
    guard.record_program_execution(path="/home/ubuntu/output/test.py", operation_id="actual-program",
                                   source_digest="saved-version", returncode=0)
    assert guard.before_call(item) is None
    assert not guard.stalled


def test_append_is_not_a_repeated_whole_program_draft():
    guard = AnalysisProgressGuard()
    item = call(file="/home/ubuntu/output/test.py", content="line", append=True)
    for _ in range(5):
        assert guard.before_call(item) is None
        guard.record(item, succeeded=True)


def test_identical_failures_cannot_evade_guard_by_changing_shell_identity():
    guard = AnalysisProgressGuard()
    for shell in ("one", "two"):
        item = call("dataset_quicklook", id=shell, input_path="/home/ubuntu/datasets/data.xlsx")
        guard.record(item, succeeded=False)
    repeat = call("dataset_quicklook", id="three", input_path="/home/ubuntu/datasets/data.xlsx")
    assert guard.before_call(repeat)
    assert guard.before_call(call("dataset_quicklook", input_path="/home/ubuntu/datasets/other.xlsx")) is None


def test_only_unique_confirmed_read_evidence_counts_as_progress():
    guard = AnalysisProgressGuard()
    item = call("file_read", file="/home/ubuntu/datasets/data.csv")
    for _ in range(2):
        guard.record(item, succeeded=True, read_only=True, result_digest="first")
    assert len(guard.evidence) == 1
    assert guard.before_call(item)
    guard.record(item, succeeded=True, read_only=True, result_digest="changed")
    assert len(guard.evidence) == 2
    assert not guard.stalled and guard.before_call(item) is None
    guard.record(call("plugin_untrusted"), succeeded=True, result_digest="arbitrary")
    assert len(guard.evidence) == 2


def test_progress_guard_keeps_only_bounded_private_identities():
    guard = AnalysisProgressGuard()
    for index in range(100):
        guard.record(call("file_read", file=f"/Users/private/{index}"), succeeded=True,
                     read_only=True, result_digest=str(index))
        guard.record(call("failing", secret=str(index)), succeeded=False)
    assert len(guard.evidence) <= 64 and len(guard.failures) <= 64
    assert "/Users" not in repr(guard.__dict__)
    assert len(guard.evidence_digest()) == 64
    assert call_identity(call("shell_run", id="first", command="a")) == call_identity(call("shell_run", id="second", command="a"))


def test_own_script_reads_are_not_source_progress_and_failed_writes_are_not_replayed():
    guard = AnalysisProgressGuard()
    guard.read_scope_paths = frozenset({"/home/ubuntu/datasets/data.csv"})
    guard.record(call("file_read", file="/home/ubuntu/output/analysis.py"), succeeded=True,
                 read_only=True, result_digest="code")
    assert not guard.evidence
    guard.record(call("file_read", file="/home/ubuntu/datasets/data.csv"), succeeded=True,
                 read_only=True, result_digest="source")
    assert len(guard.evidence) == 1
    write = call("shell_run", command="an operation")
    guard.record(write, succeeded=False, confirmed_execution=True)
    assert guard.before_call(write)


def test_blocked_dispatches_stop_only_until_verified_progress_resumes():
    guard = AnalysisProgressGuard()
    guard.record_blocked(call("unsafe", command="one"), "Cannot dispatch")
    assert not guard.should_stop
    guard.record_blocked(call("unsafe", command="two"), "Cannot dispatch")
    assert guard.should_stop
    guard.record(call("file_read", file="/data/input.csv"), succeeded=True,
                 read_only=True, result_digest="new-evidence")
    assert not guard.should_stop
    guard.record_blocked(call("unsafe", command="three"), "Cannot dispatch")
    guard.record_observation()
    assert not guard.should_stop and not guard.stalled


def test_new_verified_reads_keep_progress_after_private_cache_capacity():
    guard = AnalysisProgressGuard()
    for index in range(200):
        guard.record_blocked(call("unsafe", command="same"), "Cannot dispatch")
        guard.record(call("file_read", file=f"/data/{index}.csv"), succeeded=True,
                     read_only=True, result_digest=str(index))
        assert not guard.should_stop
    assert len(guard.evidence) <= guard.MAX_RECORDS


def test_confirmed_execution_invalidates_read_counts_before_the_next_inspection():
    guard = AnalysisProgressGuard()
    read = call("file_read", file="/home/ubuntu/output/result.csv")
    for _ in range(2):
        assert guard.before_call(read) is None
        guard.record(read, succeeded=True, read_only=True, result_digest="previous")
    assert guard.before_call(read)
    previous_evidence = set(guard.evidence)
    guard.record(call("shell_run", command="update results"), succeeded=True, confirmed_execution=True)
    assert guard.before_call(read) is None
    assert previous_evidence <= guard.evidence
    guard.record(read, succeeded=True, read_only=True, result_digest="updated")
    assert not guard.should_stop and not guard.stalled


def test_successful_content_write_invalidates_reads_without_resetting_draft_guard():
    guard = AnalysisProgressGuard()
    read = call("file_read", file="/home/ubuntu/output/result.py")
    write = call("file_write", file="/home/ubuntu/output/result.py", content="updated source")
    for _ in range(2):
        assert guard.before_call(read) is None
        guard.record(read, succeeded=True, read_only=True, result_digest="previous")
    assert guard.before_call(read)
    previous_evidence = set(guard.evidence)

    assert guard.before_call(write) is None
    guard.record(write, succeeded=True, read_only=False, confirmed_execution=False)
    assert guard.before_call(read) is None
    assert previous_evidence <= guard.evidence
    guard.record(write, succeeded=True, read_only=False, confirmed_execution=False)
    assert guard.before_call(write)  # Success is not confirmation of execution.


def test_failed_unconfirmed_content_write_does_not_invalidate_read_evidence():
    guard = AnalysisProgressGuard()
    read = call("file_read", file="/home/ubuntu/output/result.csv")
    for _ in range(2):
        guard.record(read, succeeded=True, read_only=True, result_digest="previous")
    guard.record(call("file_write", file="/home/ubuntu/output/result.csv", content="new"),
                 succeeded=False, read_only=False, confirmed_execution=False)
    assert guard.before_call(read)


PROGRAM = "/home/ubuntu/output/analysis.py"
SOURCE = "/home/ubuntu/datasets/input.csv"


def record_program(guard, index, *, path=PROGRAM, error="ValueError: invalid numeric field", returncode=1):
    guard.record_program_execution(path=path, operation_id=f"operation-{index}",
                                   source_digest=f"version-{index}", returncode=returncode,
                                   failure_fingerprint=error)


def test_write_replace_and_append_share_one_revision_history():
    guard = AnalysisProgressGuard()
    for item in (
        call(file=PROGRAM, content="first"),
        call("file_str_replace", file=PROGRAM, old_str="first", new_str="second"),
        call(file=PROGRAM, content="third", append=True),
    ):
        assert guard.before_call(item) is None
        guard.record(item, succeeded=True)
    state = guard.programs[program_identity(PROGRAM)]
    assert state.revision == 3
    record_program(guard, 1, returncode=0)
    assert state.executed_revision == 3
    assert state.executed_source and "version-1" not in repr(state)


def test_repeated_failure_survives_changed_code_and_mutation_tool_channel():
    guard = AnalysisProgressGuard()
    guard.read_scope_paths = frozenset({SOURCE})
    for index, item in enumerate((
        call(file=PROGRAM, content="first draft"),
        call("file_str_replace", file=PROGRAM, old_str="first", new_str="second"),
    )):
        assert guard.before_call(item) is None
        guard.record(item, succeeded=True)
        record_program(guard, index)
    assert "same failure" in guard.instruction()
    for item in (
        call(file=PROGRAM, content="third draft"),
        call(file=PROGRAM, content="more", append=True),
        call("file_str_replace", file=PROGRAM, old_str="second", new_str="third"),
    ):
        assert "original input" in guard.before_call(item)
    assert guard.before_call(call("program_run", script_path=PROGRAM), program_path=PROGRAM)
    assert not guard.should_stop  # A repeated error prompts diagnosis, not a task quota.


def test_own_modified_program_read_does_not_erase_diagnostic_gate_or_blocked_count():
    guard = AnalysisProgressGuard()
    guard.read_scope_paths = frozenset({SOURCE})
    for index in range(2):
        record_program(guard, index)
    guard.record_blocked(call(file=PROGRAM, content="blind rewrite"), "diagnose")
    guard.record(call("file_read", file=PROGRAM), succeeded=True, read_only=True,
                 result_digest="newly changed source body")
    assert not guard.evidence
    assert guard.blocked_without_progress == 1
    assert guard.before_call(call("file_str_replace", file=PROGRAM))


def test_changed_source_evidence_allows_new_strategy_without_task_limit():
    guard = AnalysisProgressGuard()
    guard.read_scope_paths = frozenset({SOURCE})
    for iteration in range(30):
        for offset in range(2):
            record_program(guard, iteration * 2 + offset)
        assert guard.before_call(call(file=PROGRAM, content="correction"))
        guard.record(call("file_read", file=SOURCE, start_line=iteration), succeeded=True,
                     read_only=True, result_digest=f"new source detail {iteration}")
        assert guard.before_call(call(file=PROGRAM, content="evidence-backed correction")) is None
        guard.record(call(file=PROGRAM, content=f"correction-{iteration}"), succeeded=True)
        assert not guard.should_stop


def test_unrelated_shell_ls_does_not_reset_failed_program_or_other_drafts():
    guard = AnalysisProgressGuard()
    other = "/home/ubuntu/output/other.py"
    for _ in range(2):
        guard.record(call(file=other, content="draft"), succeeded=True)
    for index in range(2):
        record_program(guard, index)
    guard.record_blocked(call(file=PROGRAM, content="blind rewrite"), "diagnose")
    guard.record(call("shell_run", command="ls /home/ubuntu/output"), succeeded=True, confirmed_execution=True)
    assert guard.before_call(call(file=PROGRAM, content="still blind"))
    assert guard.before_call(call(file=other, content="third draft"))
    assert guard.blocked_without_progress == 1


def test_legitimate_new_failure_feedback_and_successful_programs_can_continue():
    guard = AnalysisProgressGuard()
    for index, error in enumerate(("missing header", "missing delimiter", "bad dtype", "bad unit")):
        item = call("file_str_replace", file=PROGRAM, old_str=str(index), new_str=str(index + 1))
        assert guard.before_call(item) is None
        guard.record(item, succeeded=True)
        record_program(guard, index, error=error)
        assert guard.before_call(call("program_run", script_path=PROGRAM), program_path=PROGRAM) is None
    record_program(guard, 100, returncode=0)
    assert not guard.programs[program_identity(PROGRAM)].failures
    assert not guard.stalled


def test_repeated_error_cycle_is_detected_despite_intervening_other_error():
    guard = AnalysisProgressGuard()
    for index, error in enumerate(("bad delimiter", "bad dtype", "bad delimiter")):
        record_program(guard, index, error=error)
    assert guard.before_call(call("program_run", script_path=PROGRAM), program_path=PROGRAM)


def test_failed_program_does_not_block_unrelated_healthy_program():
    guard = AnalysisProgressGuard()
    for index in range(2):
        record_program(guard, index)
    other = "/home/ubuntu/output/another_analysis.py"
    assert guard.before_call(call(file=other, content="new program")) is None
    assert guard.before_call(call("program_run", script_path=other), program_path=other) is None
    record_program(guard, 10, path=other, returncode=0)
    assert guard.before_call(call(file=PROGRAM, content="same blind correction"))


def test_duplicate_receipt_is_not_another_execution_failure():
    guard = AnalysisProgressGuard()
    record_program(guard, 0)
    record_program(guard, 0)
    assert not guard.programs[program_identity(PROGRAM)].diagnostic_required


def test_normalized_program_path_prevents_dot_segment_target_evasion():
    guard = AnalysisProgressGuard()
    for index in range(2):
        record_program(guard, index)
    assert guard.before_call(call(file="/home/ubuntu/output/./analysis.py", content="same target"))


def test_program_failure_after_revision_is_not_blocked_by_unchanged_launch_arguments():
    guard = AnalysisProgressGuard()
    launch = call("program_run", script_path=PROGRAM)
    guard.record(launch, succeeded=False, confirmed_execution=True)
    guard.record_program_execution(path=PROGRAM, operation_id="op", source_digest="a" * 64,
                                   returncode=1, failure_fingerprint="error", call=launch)
    guard.record(call("file_str_replace", file=PROGRAM, old_str="bad", new_str="fixed"), succeeded=True)
    assert guard.before_call(launch, program_path=PROGRAM) is None


def test_old_program_receipt_does_not_forgive_later_missing_source_failure():
    guard = AnalysisProgressGuard()
    launch = call("program_run", script_path=PROGRAM)
    guard.record_program_execution(path=PROGRAM, operation_id="old", source_digest="a" * 64,
                                   returncode=0, call=launch)
    # The latest invocation never produced a validated program receipt (e.g.
    # source removed or unreadable). It must follow raw failure/replay rules.
    guard.record(launch, succeeded=False, confirmed_execution=False)
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_program_execution(path=PROGRAM, operation_id="old", source_digest="a" * 64,
                                   returncode=0, call=launch)
    assert guard.before_call(launch, program_path=PROGRAM)  # Replayed old receipt cannot clear it.


@pytest.mark.parametrize("suffix,runtime", [(".r", "Rscript"), (".js", "node"), (".sh", "bash")])
def test_non_python_programs_are_not_forced_through_python_only_runner(suffix, runtime):
    guard = AnalysisProgressGuard()
    path = f"/home/ubuntu/output/analysis{suffix}"
    for iteration in range(8):
        write = call(file=path, content=f"revision-{iteration}")
        assert guard.before_call(write) is None
        guard.record(write, succeeded=True)
        launch = call("shell_run", command=f"{runtime} {path}")
        assert guard.before_call(launch) is None
        guard.record(launch, succeeded=True, confirmed_execution=True)
    assert "supported language runtime" in guard.instruction()
    assert "program_run" not in guard.instruction()


def code_failure(guard, index, *, path=PROGRAM, exception="SyntaxError"):
    guard.record_program_execution(path=path, operation_id=f"code-op-{index}", source_digest=str(index),
                                   returncode=1, failure_fingerprint="same-error",
                                   diagnostic={"exception_type": exception, "line": 3})


@pytest.mark.parametrize("exception", ["SyntaxError", "IndentationError", "TabError", "NameError", "UnboundLocalError"])
def test_code_diagnostic_allows_one_trial_without_dataset_progress(exception):
    guard = AnalysisProgressGuard()
    for index in range(2):
        code_failure(guard, index, exception=exception)
    assert "code error" in guard.before_call(call("file_str_replace", file=PROGRAM))
    guard.record_blocked(call("file_str_replace", file=PROGRAM), "code diagnosis needed")
    guard.record_program_diagnostic(path=PROGRAM, content_digest="actual-code-section")
    assert guard.before_call(call("file_str_replace", file=PROGRAM)) is None
    assert not guard.evidence
    assert guard.blocked_without_progress == 0
    code_failure(guard, 2, exception=exception)
    assert guard.before_call(call("file_str_replace", file=PROGRAM))
    guard.record_program_diagnostic(path=PROGRAM, content_digest="actual-code-section")
    assert guard.before_call(call("file_str_replace", file=PROGRAM))  # Repeated contents are not new diagnosis.


@pytest.mark.parametrize("exception", ["ValueError", "KeyError", "AttributeError", "TypeError"])
def test_generated_source_read_does_not_relieve_data_error(exception):
    guard = AnalysisProgressGuard()
    for index in range(2):
        code_failure(guard, index, exception=exception)
    guard.record_program_diagnostic(path=PROGRAM, content_digest="freshly edited own code")
    assert guard.before_call(call("file_str_replace", file=PROGRAM))
    assert not guard.evidence


def test_code_diagnostic_does_not_clear_other_program_or_blocked_target():
    guard = AnalysisProgressGuard()
    other = "/home/ubuntu/output/other.py"
    for index in range(2):
        code_failure(guard, index, path=PROGRAM)
        code_failure(guard, index, path=other)
    guard.record_blocked(call("file_str_replace", file=other), "other program is blocked")
    guard.record_program_diagnostic(path=PROGRAM, content_digest="first program diagnostic")
    assert guard.before_call(call("file_str_replace", file=PROGRAM)) is None
    assert guard.before_call(call("file_str_replace", file=other))
    assert guard.blocked_without_progress == 1
    assert not guard.evidence


def test_code_diagnostics_are_not_a_hidden_task_wide_limit():
    guard = AnalysisProgressGuard()
    code_failure(guard, 0)
    for index in range(1, 90):
        code_failure(guard, index)
        guard.record_program_diagnostic(path=PROGRAM, content_digest=f"diagnostic-{index}")
        assert guard.before_call(call("file_str_replace", file=PROGRAM)) is None
        assert not guard.should_stop
    assert not guard.evidence


def test_code_diagnostic_does_not_clear_an_unrelated_non_program_block():
    guard = AnalysisProgressGuard()
    code_failure(guard, 0)
    code_failure(guard, 1)
    guard.record_blocked(call("unavailable_tool"), "unrelated tool unavailable")
    guard.record_program_diagnostic(path=PROGRAM, content_digest="new diagnostic code")
    assert guard.before_call(call("file_str_replace", file=PROGRAM)) is None
    assert guard.blocked_without_progress == 1 and guard.stalled
    assert guard.instruction() == "unrelated tool unavailable"


def program_launch(identifier="failed-launch", **args):
    item = call("program_run", script_path=PROGRAM, exec_dir="/home/ubuntu/output", **args)
    item["id"] = identifier
    return item


def prelaunch_failure(guard, launch, *, operation_id="never-started", confirm=True, observe_missing=True):
    if observe_missing and program_identity(PROGRAM) not in guard.programs:
        # A known-missing prerequisite has a real semantic digest. An absent
        # observation means unknown and must never serve as a repair baseline.
        guard.record_program_prerequisites(path=PROGRAM,
            prerequisite_digest=prerequisite_signature(cwd="missing", source="missing"), ready=False)
    guard.record(launch, succeeded=False, program_path=PROGRAM)
    if confirm:
        guard.record_program_prelaunch_failure(call=launch, path=PROGRAM, operation_id=operation_id)


def test_confirmed_not_started_can_launch_after_actual_script_creation():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    prelaunch_failure(guard, launch)
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_blocked(launch, "missing prerequisite", program_path=PROGRAM)
    guard.record(call(file=PROGRAM, content="created"), succeeded=True)
    assert guard.before_call(launch, program_path=PROGRAM)  # Claimed write success is not a snapshot.
    guard.record_program_source(path=PROGRAM, content_digest="actual-created-source")
    assert guard.before_call(launch, program_path=PROGRAM) is None
    assert not guard.stalled and not guard.should_stop
    assert guard.blocked_without_progress == 0
    assert not guard.evidence  # Program availability is not dataset evidence.


def test_late_not_started_receipt_uses_source_snapshot_from_failed_invocation():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    prelaunch_failure(guard, launch, confirm=False)
    guard.record_program_source(path=PROGRAM, content_digest="created-after-failure")
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_program_prelaunch_failure(call=launch, path=PROGRAM, operation_id="later-confirmed")
    assert guard.before_call(launch, program_path=PROGRAM) is None


def test_same_source_rewrite_or_unrelated_work_cannot_retry_prelaunch_failure():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    guard.record_program_source(path=PROGRAM, content_digest="same-source")
    prelaunch_failure(guard, launch)
    guard.record(call(file=PROGRAM, content="same bytes"), succeeded=True)
    guard.record_program_source(path=PROGRAM, content_digest="same-source")
    guard.record_program_source(path="/home/ubuntu/output/other.py", content_digest="different")
    guard.record(call("shell_run", command="ls"), succeeded=True, confirmed_execution=True)
    assert guard.before_call(launch, program_path=PROGRAM)


def test_source_correction_opens_only_one_not_started_retry_without_another_change():
    guard = AnalysisProgressGuard()
    initial = program_launch()
    guard.record_program_source(path=PROGRAM, content_digest="initial")
    prelaunch_failure(guard, initial)
    guard.record_program_source(path=PROGRAM, content_digest="corrected")
    retry = program_launch("retry")
    assert guard.before_call(retry, program_path=PROGRAM) is None
    prelaunch_failure(guard, retry, operation_id="retry-never-started")
    assert guard.before_call(program_launch("retry-again"), program_path=PROGRAM)
    guard.record_program_source(path=PROGRAM, content_digest="corrected")
    assert guard.before_call(program_launch("retry-again"), program_path=PROGRAM)


@pytest.mark.parametrize("newer_has_resolved_path", [False, True])
def test_old_not_started_receipt_cannot_forgive_newer_unknown_attempt(newer_has_resolved_path):
    guard = AnalysisProgressGuard()
    first = program_launch()
    prelaunch_failure(guard, first)
    guard.record_program_source(path=PROGRAM, content_digest="version-one")
    newer = program_launch("newer-unconfirmed")
    assert guard.before_call(newer, program_path=PROGRAM) is None
    guard.record(newer, succeeded=False, program_path=PROGRAM if newer_has_resolved_path else None)
    guard.record_program_source(path=PROGRAM, content_digest="version-two")
    guard.record_program_prelaunch_failure(call=first, path=PROGRAM, operation_id="never-started")
    assert guard.before_call(program_launch("third"), program_path=PROGRAM)


def test_not_started_receipt_must_match_failed_invocation_and_target():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    prelaunch_failure(guard, launch, confirm=False)
    guard.record_program_source(path=PROGRAM, content_digest="available-source")
    guard.record_program_prelaunch_failure(call=program_launch("wrong-call"), path=PROGRAM, operation_id="op")
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_program_prelaunch_failure(call=launch, path="/home/ubuntu/output/other.py", operation_id="op")
    assert guard.before_call(launch, program_path=PROGRAM)


def test_prelaunch_source_repair_does_not_reset_runtime_error_or_other_blocks():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    for index in range(2):
        record_program(guard, index)
    prelaunch_failure(guard, launch)
    guard.record_blocked(call("unavailable_tool"), "other operation blocked")
    guard.record_program_source(path=PROGRAM, content_digest="repaired-source")
    assert "original input" in guard.before_call(launch, program_path=PROGRAM)
    assert guard.blocked_without_progress == 1
    assert guard.programs[program_identity(PROGRAM)].diagnostic_required


def test_successful_program_feedback_clears_only_current_launch_failure():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    prelaunch_failure(guard, launch)
    guard.record_program_source(path=PROGRAM, content_digest="available-source")
    guard.record_program_execution(path=PROGRAM, operation_id="actually-ran", source_digest="source", returncode=0, call=launch)
    assert call_identity(launch) not in guard.program_launch_failures
    assert call_identity(launch) not in guard.unsafe_failed_calls


@pytest.mark.parametrize("argv", [None, []])
def test_program_launch_identity_normalizes_default_argv_and_dot_segments(argv):
    original = program_launch()
    variant = program_launch("different-call", argv=argv)
    variant["args"]["script_path"] = "/home/ubuntu/output/./analysis.py"
    variant["args"]["exec_dir"] = "/home/ubuntu/output/."
    assert call_identity(original) == call_identity(variant)


def test_program_launch_failure_history_stays_bounded_and_private():
    guard = AnalysisProgressGuard()
    for index in range(90):
        path = f"/home/ubuntu/output/private-{index}.py"
        launch = program_launch(f"private-call-{index}")
        launch["args"]["script_path"] = path
        guard.record_program_source(path=path, content_digest=f"private-source-{index}")
        guard.record(launch, succeeded=False, program_path=path)
        guard.record_program_prelaunch_failure(call=launch, path=path, operation_id=f"private-op-{index}")
    assert len(guard.program_launch_failures) == guard.MAX_RECORDS
    assert len(guard.programs) == guard.MAX_RECORDS
    assert "private-" not in repr(guard.__dict__)


def prerequisite_signature(*, cwd="ready", source="ready", source_digest="a" * 64):
    value = {"version": 1, "cwd": {"state": cwd},
             "source": {"state": source, "source_digest": source_digest if source == "ready" else None}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_changed_but_still_blocked_prerequisites_cannot_authorize_retry_or_clear_stall():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="missing"), ready=False)
    prelaunch_failure(guard, launch)
    guard.record_blocked(launch, "cwd missing", program_path=PROGRAM)
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="not_accessible"), ready=False)
    assert guard.before_call(launch, program_path=PROGRAM)
    assert guard.blocked_without_progress == 1 and guard.stalled
    assert not guard.evidence


@pytest.mark.parametrize("receipt_before_invalidation", [False, True])
def test_unavailable_fresh_probe_cannot_borrow_previous_ready_snapshot(receipt_before_invalidation):
    guard = AnalysisProgressGuard()
    launch = program_launch()
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="missing"), ready=False)
    prelaunch_failure(guard, launch, confirm=receipt_before_invalidation)
    ready_digest = prerequisite_signature()
    guard.record_program_prerequisites(path=PROGRAM, prerequisite_digest=ready_digest, ready=True)
    prior_identity = guard.programs[program_identity(PROGRAM)].confirmed_prerequisites
    guard.invalidate_program_prerequisites(path=PROGRAM)
    if not receipt_before_invalidation:
        guard.record_program_prelaunch_failure(call=launch, path=PROGRAM, operation_id="never-started")
    state = guard.programs[program_identity(PROGRAM)]
    assert state.confirmed_prerequisites == prior_identity  # Unknown is not a fabricated new state.
    assert state.prerequisites_ready is False
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_program_prerequisites(path=PROGRAM, prerequisite_digest=ready_digest, ready=True)
    assert guard.before_call(launch, program_path=PROGRAM) is None  # A new actual observation restores freshness.


@pytest.mark.parametrize("cwd,source", [
    ("missing", "ready"), ("not_accessible", "ready"), ("ready", "not_readable"),
])
def test_directory_or_permission_repair_allows_retry_without_code_revision(cwd, source):
    guard = AnalysisProgressGuard()
    launch = program_launch()
    original_source = "a" * 64
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd=cwd, source=source, source_digest=original_source), ready=False)
    prelaunch_failure(guard, launch)
    assert guard.before_call(launch, program_path=PROGRAM)
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(source_digest=original_source), ready=True)
    assert guard.before_call(program_launch("corrected-launch"), program_path=PROGRAM) is None
    state = guard.programs[program_identity(PROGRAM)]
    assert state.revision == 0 and state.executed_revision == -1
    assert not guard.writes and not guard.evidence


def test_repaired_prerequisites_and_old_not_started_cannot_erase_later_unknown_launch():
    guard = AnalysisProgressGuard()
    initial = program_launch()
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="missing"), ready=False)
    prelaunch_failure(guard, initial)
    guard.record_program_prerequisites(path=PROGRAM, prerequisite_digest=prerequisite_signature(), ready=True)
    unknown = program_launch("later-unknown-launch")
    assert guard.before_call(unknown, program_path=PROGRAM) is None
    guard.record(unknown, succeeded=False, program_path=PROGRAM)
    guard.invalidate_program_prerequisites(path=PROGRAM)
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(source_digest="b" * 64), ready=True)
    guard.record_program_prelaunch_failure(call=initial, path=PROGRAM, operation_id="never-started")
    failure = guard.program_launch_failures[call_identity(unknown)]
    assert failure.not_started_operation is None
    assert guard.before_call(program_launch("unsafe-replay"), program_path=PROGRAM)


def test_unavailable_probe_invalidates_only_its_own_program():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="missing"), ready=False)
    prelaunch_failure(guard, launch)
    guard.record_program_prerequisites(path=PROGRAM, prerequisite_digest=prerequisite_signature(), ready=True)
    guard.invalidate_program_prerequisites(path="/home/ubuntu/output/another.py")
    assert guard.before_call(launch, program_path=PROGRAM) is None
    assert program_identity("/home/ubuntu/output/another.py") not in guard.programs


def test_unknown_initial_prerequisites_are_not_a_proven_missing_baseline():
    guard = AnalysisProgressGuard()
    launch = program_launch()
    prelaunch_failure(guard, launch, observe_missing=False)
    guard.record_program_prerequisites(path=PROGRAM, prerequisite_digest=prerequisite_signature(), ready=True)
    assert guard.program_launch_failures[call_identity(launch)].prerequisites is None
    assert guard.before_call(program_launch("unjustified-retry"), program_path=PROGRAM)


@pytest.mark.parametrize("old_was_ready", [False, True])
def test_failed_launch_after_unavailable_probe_does_not_snapshot_stale_prerequisites(old_was_ready):
    guard = AnalysisProgressGuard()
    launch = program_launch()
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(cwd="ready" if old_was_ready else "missing"), ready=old_was_ready)
    old_identity = guard.programs[program_identity(PROGRAM)].confirmed_prerequisites
    guard.invalidate_program_prerequisites(path=PROGRAM)
    prelaunch_failure(guard, launch)
    failure = guard.program_launch_failures[call_identity(launch)]
    assert failure.prerequisites is None
    assert guard.programs[program_identity(PROGRAM)].confirmed_prerequisites == old_identity
    guard.record_program_prerequisites(path=PROGRAM,
        prerequisite_digest=prerequisite_signature(source_digest="b" * 64), ready=True)
    assert guard.before_call(program_launch("unjustified-retry"), program_path=PROGRAM)
