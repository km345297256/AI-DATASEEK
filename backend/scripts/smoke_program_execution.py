"""Opt-in real tool-pipeline smoke against a disposable, empty sandbox.

Creates only uniquely named synthetic programs and PNGs in that sandbox. Never
use a user-task sandbox: the caller must create and later remove a disposable
container. No model calls, session records or production database writes.

The optional --base-agent-recovery phase uses scripted model responses but the
real Agent loop, tool pipeline, HTTP adapters and private execution receipts.
"""
import argparse
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from langchain.messages import AIMessage

from app.domain.models.event import ErrorEvent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.execution_evidence import ToolExecutionLedger, tool_execution_scope
from app.domain.services.program_execution import trusted_program_execution_feedback
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox


async def verify_base_agent_recovery(
    sandbox: DockerSandbox,
    wrapper: NodeBoundSandbox,
    unique: str,
    sessions: list[str],
) -> dict:
    """Exercise a failed launch, real receipt reconciliation, and exact retry.

    Only model responses are substituted. In particular, do not replace
    invoke_tool, the progress guard, prerequisite reads, or ledger reconciliation:
    the regression is in their real integration, not individual tool execution.
    """
    directory = f"/home/ubuntu/output/program-smoke-recovery-{unique}"
    script_path, chart_path = f"{directory}/analysis.py", f"{directory}/chart.png"
    preflight = await wrapper.program_preflight(directory, script_path)
    assert preflight.success and preflight.data["ready"] is False
    assert preflight.data["cwd"]["state"] == "missing"
    assert preflight.data["source"]["state"] == "missing"
    argv = ["--plot", "| tail -80 && true"]
    source = (
        "import sys\n"
        f"assert sys.argv[1:] == {argv!r}\n"
        "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
        "plt.plot([1, 4, 2])\n"
        f"plt.savefig({chart_path!r})\nplt.close()\nprint('recovery chart saved')\n"
    )
    session_id = f"smoke-agent-recovery-{unique}"
    sessions.append(session_id)
    arguments = {
        "id": session_id, "exec_dir": directory, "script_path": script_path,
        "argv": argv, "timeout_seconds": 30,
    }
    first_call = {"name": "program_run", "id": f"missing-{unique}", "args": dict(arguments)}
    write_call = {"name": "file_write", "id": f"prepare-{unique}", "args": {
        "file": script_path, "content": source,
    }}
    retry_call = {"name": "program_run", "id": f"retry-{unique}", "args": dict(arguments)}
    assert first_call["args"] == retry_call["args"]

    # Bypass construction of the live model and persistent repository only.
    # BaseAgent's normal live ToolRegistry, execute/invoke methods, argument
    # validation, failure handling and progress state all remain unchanged.
    agent = object.__new__(BaseAgent)
    agent.name = "synthetic-program-recovery-smoke"
    agent.max_retries = 0
    agent.toolkits = [ShellToolkit(wrapper), FileToolkit(wrapper)]
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[first_call]))
    agent.ask_with_messages = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[write_call]),
        AIMessage(content="", tool_calls=[retry_call]),
        AIMessage(content="Synthetic analysis program finished."),
    ])

    events = []
    reconciled_before_write = False
    async for event in agent.execute("Create the synthetic plot in the disposable smoke sandbox."):
        events.append(event)
        if (isinstance(event, ToolEvent) and event.tool_call_id == write_call["id"]
                and event.status == ToolStatus.CALLING):
            prior = [attempt for attempt in agent._tool_execution_ledger._attempts.values()
                     if attempt.tool_call_id == first_call["id"]]
            assert len(prior) == 1, "Missing launch-bound first operation"
            assert prior[0].confirmed and prior[0].receipt.state == "not_started"
            assert prior[0].reconciliation_queries >= 1, "Real operation-status reconciliation was not exercised"
            assert prior[0].program_execution is None, "The absent script must never have run"
            reconciled_before_write = True

    assert not any(isinstance(event, ErrorEvent) for event in events), "Agent emitted an execution error"
    assert reconciled_before_write, "Agent never recovered the initial launch before file preparation"
    terminals = {event.tool_call_id: event for event in events
                 if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED}
    assert set(terminals) == {first_call["id"], write_call["id"], retry_call["id"]}
    for call, expected in ((first_call, False), (write_call, True), (retry_call, True)):
        result = terminals[call["id"]].function_result
        success = result.success if isinstance(result, ToolResult) else result.get("success")
        assert success is expected, f"Unexpected Agent tool result for {call['name']} ({call['id']})"
    assert first_call["args"] == retry_call["args"], "Recovery must not depend on changed launch arguments"
    ledger = agent._tool_execution_ledger
    feedback = trusted_program_execution_feedback(
        agent.get_tool("program_run"), retry_call, terminals[retry_call["id"]].function_result, ledger,
    )
    assert feedback is not None and feedback["returncode"] == 0
    assert feedback["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
    assert ledger.summary()["tracked_operation_count"] == 2
    assert not ledger.summary()["pending_execution"]
    assert agent.last_execution_outcome["code"] == "completed"
    assert agent.ask_with_messages.await_count == 3
    checked = await sandbox.validate_artifacts([{"path": chart_path, "kind": "image"}])
    assert checked.success and checked.data["files"][0]["valid"] is True
    record = checked.data["files"][0]
    assert record["size"] > 0 and len(record["sha256"]) == 64
    return {
        "passed": True, "agent_tool_calls": 3, "confirmed_operations": 2,
        "initial_receipt": "not_started", "reconciled_before_write": True,
        "same_arguments_recovery": True, "pending_execution": False,
        "png_content_validated": True,
    }


async def run(sandbox_url: str, sandbox_id: str, *, base_agent_recovery: bool = False) -> None:
    if not sandbox_id.startswith("ai-dataseek-program-smoke-"):
        raise ValueError("Use a separately created disposable program-smoke sandbox")
    sandbox = DockerSandbox(ip="unused", container_name=sandbox_id)
    sandbox.base_url = sandbox_url.rstrip("/")
    wrapper = NodeBoundSandbox(sandbox, SimpleNamespace(node_id="synthetic-smoke"))
    toolkit = ShellToolkit(wrapper)
    program = toolkit.get_tool("program_run")
    properties = program.args_schema.model_json_schema()["properties"]
    assert "argv" in properties and "v__args" not in properties and "args" not in properties
    unique = uuid4().hex
    directory = f"/home/ubuntu/output/program-smoke-{unique}"
    script_path, chart_path = f"{directory}/analysis.py", f"{directory}/chart.png"
    source = (
        "import sys, json\n"
        "values = [1, 4, 2]\n"
        "assert len(values) == 3 and all(isinstance(x, int) for x in values)\n"
        "if '--fail' in sys.argv:\n"
        "    assert '| tail -80 && true' in sys.argv\n"
        "    raise ValueError('synthetic failure')\n"
        "if '--validate-only' in sys.argv:\n"
        "    print(json.dumps({'rows': 3, 'validation': 'passed'}))\n"
        "    sys.exit(0)\n"
        "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
        "plt.plot(values)\n"
        f"plt.savefig({chart_path!r})\nplt.close()\nprint('chart saved')\n"
    )
    ledger = ToolExecutionLedger()
    sessions = []
    try:
        written = await sandbox.file_write(script_path, source)
        assert written.success, "Synthetic fixture could not be created"
        for phase, argv, expected_code in (
            ("validate", ["--validate-only"], 0),
            ("plot", [], 0),
            ("failure", ["--fail", "| tail -80 && true"], 1),
        ):
            session_id = f"smoke-{phase}-{unique}"
            sessions.append(session_id)
            call = {"name": "program_run", "id": f"call-{phase}", "args": {
                "id": session_id, "exec_dir": directory, "script_path": script_path,
                "argv": argv, "timeout_seconds": 30,
            }}
            with tool_execution_scope(ledger, call["id"]):
                message = await program.ainvoke(call)
            result = message.artifact
            assert result.success is (expected_code == 0), (phase, result.success)
            assert result.data["returncode"] == expected_code
            trusted = trusted_program_execution_feedback(program, call, result, ledger)
            assert trusted is not None, f"Missing launch-bound feedback: {phase}"
            assert trusted["returncode"] == expected_code
            assert trusted["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
            assert not ledger.summary()["pending_execution"]
        checked = await sandbox.validate_artifacts([{"path": chart_path, "kind": "image"}])
        assert checked.success and checked.data["files"][0]["valid"] is True
        record = checked.data["files"][0]
        assert record["size"] > 0 and len(record["sha256"]) == 64
        report = {"passed": True, "pipeline_invocations": 3,
                  "confirmed_operations": ledger.summary()["tracked_operation_count"],
                  "pending_execution": False, "png_content_validated": True}
        if base_agent_recovery:
            report["base_agent_recovery"] = await verify_base_agent_recovery(
                sandbox, wrapper, unique, sessions,
            )
        print(json.dumps(report))
    finally:
        for session_id in sessions:
            await sandbox.release_shell(session_id)
        await sandbox.client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-url", required=True)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--base-agent-recovery", action="store_true",
                        help="Also verify absent prerequisites recover through the real BaseAgent loop")
    options = parser.parse_args()
    asyncio.run(run(options.sandbox_url, options.sandbox_id,
                    base_agent_recovery=options.base_agent_recovery))
