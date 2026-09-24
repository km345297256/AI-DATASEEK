"""Upload-only recovery retains measured facts without executing analysis again."""
import json
import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.models.event import MessageEvent, ToolEvent, ToolStatus, StepEvent, StepStatus, DoneEvent
from app.domain.services.analysis_answer_review import AnswerEvidence, review_answer
from app.domain.services.analysis_checkpoint import _checkpoint_digest
from test_analysis_checkpoint import Repository, Sandbox, SOURCE, checkpoint_fixture, input_message
from test_analysis_repair_flow import collect, output, scenario, terminal_messages
from test_analysis_answer_review import tool
from test_answer_review_execution_evidence import bound_program
from test_answer_scientific_scope_routing import with_answer_scope_checks

FACT = "Observed 12 measurements; group mean = 4.5 mm. The requested chart was generated."
UNVERIFIED_DRAFT = "UNVERIFIED_PRIVATE_DRAFT_SENTINEL: mean = 999 mm."


async def delivery_recovery_fixture():
    generated = output("group_means.png", "image", uploaded=False)
    runner, flow, step, message, state = scenario([[generated]], [{
        "kind": "image", "formats": ["png"], "output_paths": [generated[0]["path"]],
        "objective": "Compute group means and plot them",
    }])
    original = flow.executor._execute_with_tool_scope
    runner._handle_tool_event = AsyncMock()
    runner._remember_private_tool_output = Mock()
    # Authentic checkpoint fixtures retain both raw observations and the exact
    # successful method receipt. Public program success alone is not such proof.
    raw_values = "value\n" + "4\n5\n" * 6
    script_path = "/home/ubuntu/group_means.py"
    script = ("import csv\nfrom statistics import mean\n"
              f"with open({SOURCE!r}) as handle:\n"
              "    values = [float(row['value']) for row in csv.DictReader(handle)]\n"
              "import matplotlib.pyplot as plt\nplt.bar(['group'], [mean(values)])\n"
              f"plt.savefig({generated[0]['path']!r})\n"
              "print(f'Observed {len(values)} measurements; group mean = {mean(values)} mm. "
              "The requested chart was generated.')\n")
    call = {"id": "original-analysis", "name": "program_run", "args": {
        "script_path": script_path, "exec_dir": "/home/ubuntu", "id": "original-shell", "argv": []}}
    receipt = {"version": 1, "script_path": script_path,
               "source_digest": hashlib.sha256(script.encode()).hexdigest(), "returncode": 0}
    core, ledger = bound_program(call, receipt)
    flow.executor.get_tool = Mock(return_value=core)

    async def execute(prompt, **kwargs):
        async for event in original(prompt, **kwargs):
            if isinstance(event, MessageEvent):
                flow.executor._tool_execution_ledger = ledger
                yield tool("file_read", call="original-read", args={"file": SOURCE}, data={"content": raw_values})
                yield tool("file_write", call="original-method", args={"file": script_path, "content": script})
                yield ToolEvent(tool_call_id=call["id"], tool_name=core.toolkit.name, function_name="program_run",
                    function_args=call["args"], status=ToolStatus.CALLED,
                    function_result={"success": True, "data": {"status": "completed", "returncode": 0,
                        "output": FACT, "program_execution": receipt}})
                payload = json.loads(event.message)
                payload["result"] = UNVERIFIED_DRAFT
                event = event.model_copy(update={"message": json.dumps(payload)})
            yield event

    flow.executor._execute_with_tool_scope = execute
    authority, disk = Repository(), Sandbox()
    authority.session.id, authority.session.user_id = runner._session_id, runner._user_id
    authority.session.sandbox_id = disk.id
    for name in ("find_by_id_and_user_id", "save_analysis_checkpoint", "get_analysis_checkpoint", "is_analysis_checkpoint_current"):
        setattr(runner._session_repository, name, getattr(authority, name))
    disk.records[generated[0]["path"]] = {key: generated[0][key] for key in ("path", "size", "sha256")}
    runner._sandbox.id = disk.id
    runner._sandbox.analysis_fingerprints = disk.analysis_fingerprints
    runner._delivery_audit_store = SimpleNamespace(record=AsyncMock())
    source = input_message()
    message.datasets, message.controller_target_files = source.datasets, source.controller_target_files
    # The first unavailable upload can now receive a read-only partial-answer
    # review. Keep that provider unavailable here so this fixture still proves
    # the later continuation does not trust or preserve its false draft.
    async def unavailable_initial_review(**arguments):
        return await review_answer(ask=AsyncMock(side_effect=TimeoutError("initial reviewer unavailable")), **arguments)
    flow.executor.review_delivery_answer = AsyncMock(side_effect=unavailable_initial_review)
    first = [event.model_copy(deep=True) async for event in runner._run_flow(message, trigger_event_seq=1)]
    assert step.outcome.reason_code == "delivery_failed" and step.outcome.can_resume
    assert authority.checkpoint
    assert authority.checkpoint["session_id"] == runner._session_id
    assert "answer_evidence" in authority.checkpoint
    assert UNVERIFIED_DRAFT not in json.dumps(authority.checkpoint["answer_evidence"])
    assert UNVERIFIED_DRAFT not in json.dumps(authority.checkpoint, default=str)
    assert not any(UNVERIFIED_DRAFT in event.message for event in terminal_messages(first))

    resume = message.model_copy(deep=True)
    resume.resume_from = authority.checkpoint["id"]
    resume.client_message_id = "recovery-input"
    resume._accepted_event_seq = 2
    authority.checkpoint["claimed_by"] = resume.client_message_id
    uploaded = generated[1].model_copy(update={"file_id": "recovered-chart"})
    state["current"] = [(generated[0], uploaded)]
    runner._sync_file_to_storage.return_value = uploaded
    requests = []

    async def review_provider(messages):
        payload = json.loads(messages[-1].content)
        requests.append(payload)
        fact = next((item for item in payload["sources"]
                     if item["kind"] == "tool_result" and FACT in item["text"]), None)
        inventory = next(item for item in payload["sources"] if item["source_id"] == "verified_files")
        return json.dumps(with_answer_scope_checks(payload, {"unsupported_claims": False, "paragraphs": [{
            "text": FACT if fact else "The requested chart is available in the attachments.",
            "kind": "analysis" if fact else "delivery",
            "evidence": [{"source_id": fact["source_id"] if fact else "verified_files",
                          "quote": FACT if fact else inventory["text"]}],
        }], "requirement_checks": [{"index": 0, "status": "met" if fact else "unclear",
            "evidence": [{"source_id": fact["source_id"], "quote": FACT}] if fact else []}]},
            status="verified" if fact else "unclear",
            evidence=[{"source_id": fact["source_id"], "quote": FACT}] if fact else [],
            scope="complete" if fact else "unclear"))

    async def real_review(**arguments):
        return await review_answer(ask=review_provider, **arguments)

    flow.executor.review_delivery_answer = AsyncMock(side_effect=real_review)
    return runner, flow, state, authority, resume, requests, generated, uploaded


@pytest.mark.asyncio
async def test_upload_only_continuation_uses_frozen_execution_evidence_without_recomputing():
    runner, flow, state, authority, resume, requests, generated, uploaded = await delivery_recovery_fixture()
    events = await collect(runner, resume)
    recovered = flow.plan.steps[0]
    assert len(state["prompts"]) == 1, "Upload recovery must never run the analysis again"
    assert recovered.success and recovered.outcome.status == "succeeded"
    assert recovered.result == FACT
    assert requests[0]["current_step_id"] == recovered.id
    original_fact = next(item for item in requests[0]["sources"] if item["kind"] == "tool_result" and FACT in item["text"])
    assert original_fact["step_id"] == recovered.id
    assert original_fact["executed_source_coverage"] == "full"
    assert original_fact["executed_source_id"] in {item["source_id"] for item in requests[0]["sources"]}
    assert recovered.outputs["answer_review"]["answer_scientific_review"]["status"] == "verified"
    assert UNVERIFIED_DRAFT not in json.dumps(requests)
    assert uploaded.file_id in {info.file_id for event in terminal_messages(events) for info in event.attachments or []}
    assert "answer_evidence" not in json.dumps([event.model_dump(mode="json") for event in events])


@pytest.mark.asyncio
async def test_legacy_delivery_checkpoint_without_frozen_evidence_stays_partial_without_replay():
    runner, flow, state, authority, resume, requests, _, uploaded = await delivery_recovery_fixture()
    authority.checkpoint.pop("answer_evidence")
    authority.checkpoint.pop("session_id")  # Compatibility with a genuine old checkpoint.
    authority.checkpoint["checkpoint_digest"] = _checkpoint_digest(authority.checkpoint)
    events = await collect(runner, resume)
    assert len(state["prompts"]) == 1
    recovered = flow.plan.steps[0]
    assert not recovered.success and recovered.outcome.status == "partial"
    assert recovered.outputs["answer_review"]["reason"] == "requirements_unverified"
    assert not any(item["kind"] == "tool_result" for item in requests[0]["sources"])
    assert uploaded.file_id in {info.file_id for event in terminal_messages(events) for info in event.attachments or []}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unsealed_text", "different_session", "wrong_step", "source_changed", "output_changed"])
async def test_frozen_execution_evidence_cannot_outlive_its_authenticated_scope_or_bytes(fault):
    runner, _, state, authority, resume, requests, generated, _ = await delivery_recovery_fixture()
    checkpoint = authority.checkpoint
    if fault == "unsealed_text":
        checkpoint["answer_evidence"]["discarded"] = not checkpoint["answer_evidence"]["discarded"]
    elif fault == "different_session":
        checkpoint["session_id"] = "unrelated-session"
        checkpoint["checkpoint_digest"] = _checkpoint_digest(checkpoint)
    elif fault == "wrong_step":
        checkpoint["plan"]["steps"][0]["id"] = "a-new-analysis"
        checkpoint["progress"]["unfinished_steps"][0]["id"] = "a-new-analysis"
        checkpoint["checkpoint_digest"] = _checkpoint_digest(checkpoint)
    else:
        disk = runner._sandbox.analysis_fingerprints.__self__
        path = SOURCE if fault == "source_changed" else generated[0]["path"]
        disk.records[path]["sha256"] = "c" * 64
    runner._sync_file_to_storage.reset_mock()
    with pytest.raises(ValueError):
        await collect(runner, resume)
    assert len(state["prompts"]) == 1 and requests == []
    runner._sync_file_to_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_upload_outage_retains_the_same_evidence_until_delivery_recovers():
    runner, flow, state, authority, resume, requests, generated, uploaded = await delivery_recovery_fixture()
    frozen = deepcopy(authority.checkpoint["answer_evidence"])
    state["current"] = [generated]
    runner._sync_file_to_storage.return_value = None
    _ = [event async for event in runner._run_flow(resume, trigger_event_seq=2)]
    assert flow.plan.steps[0].outcome.reason_code == "delivery_failed"
    assert flow.plan.steps[0].outcome.can_resume
    assert authority.checkpoint["answer_evidence"] == frozen
    assert authority.checkpoint["source_seq"] == 2
    assert len(requests) == 2  # Grounded answer + frozen review, never execution.
    resumed_again = resume.model_copy(deep=True)
    resumed_again.resume_from = authority.checkpoint["id"]
    resumed_again.client_message_id = "recovery-input-again"
    resumed_again._accepted_event_seq = 3
    authority.checkpoint["claimed_by"] = resumed_again.client_message_id
    authority.current_results = [True, True]
    state["current"] = [(generated[0], uploaded)]
    runner._sync_file_to_storage.return_value = uploaded
    await collect(runner, resumed_again)
    assert flow.plan.steps[0].success and len(state["prompts"]) == 1
    assert flow.plan.steps[0].result == FACT


@pytest.mark.asyncio
async def test_ordinary_new_input_never_restores_previous_unreviewed_computation():
    runner, flow, state, _, message, requests, generated, uploaded = await delivery_recovery_fixture()
    message.resume_from = None
    message._resume_checkpoint = None
    message.message = "Run a new analysis of these inputs"
    step = flow.plan.steps[0]

    async def new_execution(_message):
        yield StepEvent(status=StepStatus.STARTED, step=step)
        step.success = True
        step.attachments = [generated[0]["path"]]
        step.result = UNVERIFIED_DRAFT
        yield StepEvent(status=StepStatus.COMPLETED, step=step)
        yield DoneEvent()

    flow.run = new_execution
    await collect(runner, message)
    assert not step.success and step.outcome.status == "partial"
    assert step.outputs["answer_review"]["reason"] == "requirements_unverified"
    assert not any(item["kind"] == "tool_result" for item in requests[0]["sources"])


@pytest.mark.asyncio
async def test_oversized_frozen_observations_do_not_disable_safe_upload_recovery():
    evidence = AnswerEvidence()
    evidence.begin_step("plot")
    for index in range(12):
        evidence.observe(ToolEvent(tool_call_id=f"bounded-read-{index}", tool_name="file", function_name="file_read",
            function_args={}, status=ToolStatus.CALLED,
            function_result={"success": True, "data": {"content": "数据" * 2900}}))
    repository, _, _, token = await checkpoint_fixture(reason_code="delivery_failed", answer_evidence=evidence)
    assert token and repository.checkpoint["checkpoint_digest"] == _checkpoint_digest(repository.checkpoint)
    assert "answer_evidence" not in repository.checkpoint
    assert len(json.dumps(repository.checkpoint, default=str).encode()) <= 256 * 1024
