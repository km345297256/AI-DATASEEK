from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.models.analysis_input import assign_upload_namespace, build_analysis_inputs, upload_runtime_path
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session import Session
from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_checkpoint import (
    _checkpoint_digest, configuration_digest, fingerprints, prepare_continuation, save_checkpoint, source_paths,
)

SOURCE = "/home/ubuntu/datasets/dataset-a/input.nc"
OUTPUT = "/home/ubuntu/output/progress.csv"


class Repository:
    def __init__(self):
        self.session = Session(id="session-a", user_id="owner-a", agent_id="agent-a", sandbox_id="sandbox-a", llm_overrides={"model_name": "synthetic"})
        self.checkpoint = None
        self.current_checks = []
        self.current_results = [True, True]

    async def find_by_id_and_user_id(self, session_id, user_id):
        return self.session

    async def save_analysis_checkpoint(self, session_id, checkpoint):
        self.checkpoint = deepcopy(checkpoint)

    async def get_analysis_checkpoint(self, session_id, token):
        return deepcopy(self.checkpoint)

    async def is_analysis_checkpoint_current(self, session_id, token, user_id, client_message_id, *, source_seq, resume_event_seq):
        self.current_checks.append((session_id, token, user_id, client_message_id, source_seq, resume_event_seq))
        return self.current_results.pop(0)


class Sandbox:
    id = "sandbox-a"

    def __init__(self):
        self.calls = []
        self.upload_authorizations = []
        self.records = {
            SOURCE: {"path": SOURCE, "size": 16, "sha256": "a" * 64},
            OUTPUT: {"path": OUTPUT, "size": 32, "sha256": "b" * 64},
        }
        self.response = None

    async def analysis_fingerprints(self, paths, *, approved_upload_paths=None):
        self.calls.append(list(paths))
        self.upload_authorizations.append(list(approved_upload_paths or []))
        return self.response or ToolResult(success=True, data={"version": 1, "files": [deepcopy(self.records[path]) for path in paths], "errors": []})


def input_message():
    dataset = MountedDataset(dataset_id="dataset-a", data_center_id="dc", data_center_name="test", name="Synthetic", sandbox_path="/home/ubuntu/datasets/dataset-a", files=[DatasetFile(path="input.nc")])
    return Message(message="Original analysis goal", datasets=[dataset], controller_target_files=[SOURCE], skills=["analysis"], mcp_servers=["reader"])


async def checkpoint_fixture(**save_options):
    repository, sandbox, message = Repository(), Sandbox(), input_message()
    plan = Plan(steps=[Step(id="inspect", status=ExecutionStatus.COMPLETED, success=True), Step(id="plot", status=ExecutionStatus.FAILED, success=False, result="Need a chart")])
    record = {**sandbox.records[OUTPUT], "valid": True, "kind": "table"}
    delivered = FileInfo(file_id="progress-file", file_path=OUTPUT, size=32, metadata={"artifact_sha256": "b" * 64})
    options = {"source_fingerprints": [sandbox.records[SOURCE]], "records": [record], "delivered": [delivered], "reason_code": "artifacts_missing", "source_seq": 1, **save_options}
    token = await save_checkpoint(repository, sandbox, "session-a", "owner-a", message, plan, **options)
    return repository, sandbox, message, token


async def ready_checkpoint():
    repository, sandbox, message, token = await checkpoint_fixture()
    assert token and repository.checkpoint
    repository.checkpoint["claimed_by"] = "client-a"
    message.resume_from = token
    message.client_message_id = "client-a"
    message._accepted_event_seq = 10
    message.message = ""
    return repository, sandbox, message


@pytest.mark.asyncio
async def test_verified_continuation_restores_original_goal_and_saved_progress():
    repository, sandbox, message = await ready_checkpoint()
    checkpoint = await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert message.message == "Original analysis goal"
    assert message._resume_checkpoint == checkpoint
    assert checkpoint["progress"]["completed_step_ids"] == ["inspect"]
    assert checkpoint["progress"]["verified_files"] == [OUTPUT]
    assert len(repository.current_checks) == 2
    assert repository.current_checks[0][-2:] == (1, 10)


@pytest.mark.asyncio
async def test_mongo_millisecond_and_naive_datetime_roundtrip_preserves_digest():
    repository, sandbox, message = await ready_checkpoint()
    expiry = repository.checkpoint["expires_at"]
    repository.checkpoint["expires_at"] = expiry.replace(tzinfo=None, microsecond=(expiry.microsecond // 1000) * 1000)
    assert await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("owner_id", "other"), ("sandbox_id", "other"), ("configuration_digest", "other"),
    ("version", 2), ("version", True), ("claimed_by", None), ("claimed_by", "other-input"),
    ("expires_at", datetime.now(UTC) - timedelta(days=1)), ("expires_at", "tomorrow"),
])
async def test_identity_version_expiry_and_exact_claim_are_required_before_reading_bytes(field, value):
    repository, sandbox, message = await ready_checkpoint()
    repository.checkpoint[field] = value
    calls = len(sandbox.calls)
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert len(sandbox.calls) == calls
    assert message.message == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "sandbox", "model", "skills", "mcp", "permission", "targets", "attachment"])
async def test_changed_execution_identity_and_capabilities_block_continuation(change):
    repository, sandbox, message = await ready_checkpoint()
    if change == "owner": repository.session.user_id = "other"
    elif change == "sandbox": repository.session.sandbox_id = "other"
    elif change == "model": repository.session.llm_overrides = {"model_name": "changed"}
    elif change == "skills": message.skills = ["different"]
    elif change == "mcp": message.mcp_servers = ["different"]
    elif change == "permission": message.mcp_access_all = True
    elif change == "targets": message.controller_target_files = []
    else: message.attachment_file_ids = ["new-input"]
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SOURCE, OUTPUT])
@pytest.mark.parametrize("field,value", [("sha256", "c" * 64), ("size", 999)])
async def test_both_source_and_progress_bytes_must_match_saved_proof(path, field, value):
    repository, sandbox, message = await ready_checkpoint()
    sandbox.records[path][field] = value
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["goal", "plan", "fingerprints", "source_paths", "progress", "delivered", "checkpoint_digest"])
async def test_incomplete_or_modified_checkpoint_cannot_resume(field):
    repository, sandbox, message = await ready_checkpoint()
    repository.checkpoint.pop(field)
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.mark.asyncio
async def test_even_resealed_checkpoint_requires_complete_source_and_progress_proof():
    repository, sandbox, message = await ready_checkpoint()
    repository.checkpoint["fingerprints"] = [repository.checkpoint["fingerprints"][0]]
    repository.checkpoint["checkpoint_digest"] = _checkpoint_digest(repository.checkpoint)
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.mark.asyncio
async def test_save_does_not_enable_resume_without_source_proof():
    repository, _, _, token = await checkpoint_fixture(source_fingerprints=[])
    assert token is None and repository.checkpoint is None


@pytest.mark.asyncio
async def test_save_rechecks_sources_against_pre_execution_snapshot():
    _, sandbox, _, _ = await checkpoint_fixture()
    stale = {**sandbox.records[SOURCE], "sha256": "d" * 64}
    repository, _, _, token = await checkpoint_fixture(source_fingerprints=[stale])
    assert token is None and repository.checkpoint is None


@pytest.mark.asyncio
async def test_save_requires_progress_receipt_to_match_current_bytes():
    record = {"path": OUTPUT, "size": 32, "sha256": "d" * 64, "valid": True}
    repository, _, _, token = await checkpoint_fixture(records=[record])
    assert token is None and repository.checkpoint is None


@pytest.mark.asyncio
async def test_save_requires_delivered_digest_to_match_verified_progress():
    file = FileInfo(file_id="file", file_path=OUTPUT, size=32, metadata={"artifact_sha256": "c" * 64})
    repository, _, _, token = await checkpoint_fixture(delivered=[file])
    assert token is None and repository.checkpoint is None


@pytest.mark.asyncio
async def test_save_never_enables_attachment_sources_without_fingerprint_proof():
    repository, sandbox, message, _ = await checkpoint_fixture()
    repository.checkpoint = None
    message.attachment_file_ids = ["input-file"]
    token = await save_checkpoint(repository, sandbox, "session-a", "owner-a", message,
                                  Plan(steps=[Step(id="work")]), source_fingerprints=[sandbox.records[SOURCE]],
                                  records=[], delivered=[], reason_code="execution_failed", source_seq=1)
    assert token is None and repository.checkpoint is None


@pytest.mark.parametrize("relative", ["/Users/private/data", "../secret", "./input.nc", "folder//input.nc", "folder/../../secret", "folder\\secret"])
def test_sources_must_be_canonical_registered_dataset_paths(relative):
    message = input_message()
    message.datasets[0].files[0].path = relative
    assert source_paths(message) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"version": 2, "files": [], "errors": []}, {"version": 1, "files": [], "errors": []},
    {"version": 1, "files": [{"path": SOURCE, "size": 1, "sha256": "x"}], "errors": []},
    {"version": 1, "files": [{"path": SOURCE, "size": True, "sha256": "a" * 64}], "errors": []},
    {"version": 1, "files": [{"path": SOURCE, "size": 1, "sha256": "a" * 64}] * 2, "errors": []},
    {"version": 1, "files": [{"path": SOURCE, "size": 1, "sha256": "a" * 64}], "errors": [{"code": "changed"}]},
])
async def test_malformed_incomplete_or_duplicate_snapshot_fails_closed(response):
    sandbox = Sandbox()
    sandbox.response = ToolResult(success=True, data=response)
    assert await fingerprints(sandbox, [SOURCE]) is None


@pytest.mark.asyncio
async def test_unsafe_fingerprint_paths_are_rejected_before_sandbox_call():
    sandbox = Sandbox()
    for paths in [["/Users/private/data"], ["/home/ubuntu/output/../private"], [SOURCE, SOURCE]]:
        assert await fingerprints(sandbox, paths) is None
    assert sandbox.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [[False], [True, False]])
async def test_superseded_input_before_or_during_fingerprints_cannot_resume(current):
    repository, sandbox, message = await ready_checkpoint()
    repository.current_results = current
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert message.message == ""
    assert message._resume_checkpoint is None


@pytest.mark.asyncio
async def test_legacy_repository_without_current_checkpoint_proof_fails_closed():
    repository, sandbox, message = await ready_checkpoint()
    repository.is_analysis_checkpoint_current = None
    calls = len(sandbox.calls)
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert len(sandbox.calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("seq", [None, 0, True, 1])
async def test_resume_requires_its_own_newer_accepted_user_event(seq):
    repository, sandbox, message = await ready_checkpoint()
    message._accepted_event_seq = seq
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)


@pytest.fixture
def synthetic_model_settings(monkeypatch):
    settings = SimpleNamespace(api_key="synthetic-private-key", api_base="https://model.invalid/v1",
                               model_provider="deepseek", model_name="model-a", temperature=0.2,
                               max_tokens=2000, execution_max_tokens=4096, extra_headers=None,
                               execution_snapshot_identity_key="synthetic-stable-identity-key")
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("field,value", [
    ("api_key", "changed-private-key"), ("api_base", "https://changed.invalid/v1"),
    ("model_provider", "openai"), ("model_name", "model-b"), ("temperature", 0.3),
    ("max_tokens", 3000), ("execution_max_tokens", 8192), ("extra_headers", {"X-Provider": "new"}),
])
def test_effective_global_model_changes_invalidate_unprofiled_checkpoint(synthetic_model_settings, field, value):
    session = Repository().session
    session.llm_overrides = None
    original = configuration_digest(session)
    setattr(synthetic_model_settings, field, value)
    assert configuration_digest(session) != original


def test_unused_global_defaults_do_not_change_fully_overridden_effective_model(synthetic_model_settings):
    session = Repository().session
    session.llm_overrides = {"api_key": "profile-key", "api_base": "https://profile.invalid/v1",
                            "model_provider": "openai", "model_name": "profile-model",
                            "temperature": 0.0, "max_tokens": 6000}
    original = configuration_digest(session)
    for key, value in {"api_key": "unused-new-key", "api_base": "https://unused.invalid", "model_provider": "anthropic", "model_name": "unused-model", "temperature": 1.0, "max_tokens": 1000}.items():
        setattr(synthetic_model_settings, key, value)
    assert configuration_digest(session) == original
    assert len(original) == 64
    assert "profile-key" not in original and "profile.invalid" not in original


async def upload_checkpoint_fixture(*, ready=False, requires_artifacts=None):
    repository, sandbox = Repository(), Sandbox()
    infos = assign_upload_namespace([FileInfo(file_id="input-a", filename="table.csv", size=16)])
    infos[0].file_path = upload_runtime_path(infos[0])
    source = infos[0].file_path
    sandbox.records[source] = {"path": source, "size": 16, "sha256": "c" * 64}
    message = Message(message="Visualize the submitted data", attachments=[source],
        attachment_file_ids=["input-a"], attachment_file_infos=infos,
        analysis_inputs=build_analysis_inputs([], infos), controller_requires_artifacts=requires_artifacts)
    token = await save_checkpoint(repository, sandbox, "session-a", "owner-a", message,
        Plan(steps=[Step(id="plot", success=False)]),
        source_fingerprints=[sandbox.records[source]], records=[], delivered=[],
        reason_code="artifacts_missing", source_seq=1)
    if ready:
        repository.checkpoint["claimed_by"] = "client-a"
        message.resume_from = token
        message.client_message_id = "client-a"
        message._accepted_event_seq = 10
        message.message = ""
    return repository, sandbox, message, token


@pytest.mark.asyncio
async def test_upload_continuation_binds_exact_manifest_and_fingerprints():
    repository, sandbox, message, token = await upload_checkpoint_fixture(ready=True)
    assert token
    assert repository.checkpoint["analysis_input_manifest"] == message.analysis_inputs.model_dump(mode="json")
    assert source_paths(message) == message.attachments
    assert sandbox.upload_authorizations == [message.attachments]
    checkpoint = await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert checkpoint and message.message == "Visualize the submitted data"
    assert sandbox.upload_authorizations == [message.attachments, message.attachments]


@pytest.mark.asyncio
async def test_continuation_restores_the_sealed_original_output_contract():
    repository, sandbox, message, _ = await upload_checkpoint_fixture(ready=True, requires_artifacts=True)
    assert repository.checkpoint["controller_requires_artifacts"] is True
    # A fresh advisory routing decision cannot weaken the original goal during
    # a verified continuation. A genuinely new turn uses its own contract.
    message.controller_requires_artifacts = False
    await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert message.controller_requires_artifacts is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "path", "size", "manifest", "missing_info", "extra_path", "duplicate"])
async def test_changed_upload_membership_or_identity_never_resumes(change):
    repository, sandbox, message, _ = await upload_checkpoint_fixture(ready=True)
    if change == "identity":
        message.attachment_file_ids = ["another-file"]
    elif change == "path":
        message.attachment_file_infos[0].file_path = "/home/ubuntu/output/impostor.csv"
    elif change == "size":
        message.attachment_file_infos[0].size = 17
    elif change == "manifest":
        source = message.analysis_inputs.sources[0]
        changed = source.files[0].model_copy(update={"logical_path": "renamed.csv"})
        message.analysis_inputs = message.analysis_inputs.model_copy(update={
            "sources": (source.model_copy(update={"files": (changed,)}),),
        })
    elif change == "missing_info":
        message.attachment_file_infos = []
    elif change == "extra_path":
        message.attachments.append("/home/ubuntu/inputs/" + "a" * 24 + "/extra.csv")
    elif change == "duplicate":
        message.attachment_file_infos.append(message.attachment_file_infos[0])
    calls = len(sandbox.calls)
    with pytest.raises(ValueError):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert len(sandbox.calls) == calls
    assert message._resume_checkpoint is None


@pytest.mark.asyncio
async def test_same_size_upload_replacement_is_detected_by_content_hash():
    repository, sandbox, message, _ = await upload_checkpoint_fixture(ready=True)
    sandbox.records[message.attachments[0]]["sha256"] = "d" * 64
    with pytest.raises(ValueError, match="源数据"):
        await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
    assert message._resume_checkpoint is None


@pytest.mark.asyncio
async def test_upload_snapshot_never_authorizes_an_unlisted_file_or_arbitrary_root():
    _, sandbox, message, _ = await upload_checkpoint_fixture()
    path = message.attachments[0]
    calls = len(sandbox.calls)
    assert await fingerprints(sandbox, [path]) is None
    assert await fingerprints(sandbox, [path], approved_upload_paths=["/home/ubuntu/private"]) is None
    assert await fingerprints(sandbox, [path], approved_upload_paths=[path.replace("table.csv", "other.csv")]) is None
    assert len(sandbox.calls) == calls
    assert await fingerprints(sandbox, [path], approved_upload_paths=[path])


@pytest.mark.asyncio
async def test_changed_upload_bytes_cannot_create_new_checkpoint():
    repository, sandbox, message, _ = await upload_checkpoint_fixture()
    repository.checkpoint = None
    original = deepcopy(sandbox.records[message.attachments[0]])
    sandbox.records[message.attachments[0]]["sha256"] = "e" * 64
    token = await save_checkpoint(repository, sandbox, "session-a", "owner-a", message,
        Plan(steps=[Step(id="plot", success=False)]), source_fingerprints=[original],
        records=[], delivered=[], reason_code="artifacts_missing", source_seq=2)
    assert token is None and repository.checkpoint is None


@pytest.mark.parametrize("target", ["input.nc", "dataset-a/input.nc", SOURCE])
def test_controller_catalog_names_resolve_only_inside_registered_scope(target):
    message = input_message()
    message.controller_target_files = [target]
    assert source_paths(message) == [SOURCE]


def test_ambiguous_controller_target_does_not_pick_first_file():
    message = input_message()
    message.datasets[0].files.extend([DatasetFile(path="another/input.nc")])
    message.controller_target_files = ["input.nc"]
    assert source_paths(message) == []


def test_upload_target_always_snapshots_the_whole_authorized_group():
    infos = assign_upload_namespace([
        FileInfo(file_id="shape", filename="map.shp", size=16),
        FileInfo(file_id="attributes", filename="map.dbf", size=32),
    ])
    for info in infos:
        info.file_path = upload_runtime_path(info)
    message = Message(attachments=[item.file_path for item in infos],
        attachment_file_ids=[item.file_id for item in infos], attachment_file_infos=infos,
        analysis_inputs=build_analysis_inputs([], infos))
    for target in ["map.shp", message.analysis_inputs.files[0].logical_path,
                   "/".join(infos[0].file_path.split("/")[-2:])]:
        message.controller_target_files = [target]
        assert source_paths(message) == sorted(message.attachments)
