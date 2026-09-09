from app.domain.services.analysis_progress import AnalysisProgressGuard, call_identity


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


def test_confirmed_execution_allows_revision_but_plain_read_does_not():
    guard = AnalysisProgressGuard()
    item = call(file="/home/ubuntu/output/test.py", content="draft")
    guard.record(item, succeeded=True)
    guard.record(item, succeeded=True)
    guard.record(call("file_read", file="/home/ubuntu/output/test.py"), succeeded=True)
    assert guard.before_call(item)
    guard.record(call("shell_run", command="bounded-analysis"), succeeded=True, confirmed_execution=True)
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
