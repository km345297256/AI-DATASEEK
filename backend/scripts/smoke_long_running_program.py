"""Run only synthetic programs in an explicitly disposable empty sandbox.

No model calls, user data mounts, session records or production database writes.
Use --seconds 125 to cross the former 120-second production tool deadline.
The caller creates and removes the isolated program-smoke container.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from app.domain.services.execution_evidence import ToolExecutionLedger, tool_execution_scope
from app.domain.services.program_execution import trusted_program_execution_feedback
from app.domain.services.tools.interceptors import ToolTimeoutInterceptor
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox


async def run(sandbox_url: str, sandbox_id: str, seconds: int) -> None:
    if not sandbox_id.startswith("ai-dataseek-program-smoke-") or not 10 <= seconds <= 180:
        raise ValueError("Use a disposable program-smoke sandbox and a 10–180 second synthetic wait")
    sandbox = DockerSandbox(ip="unused", container_name=sandbox_id)
    sandbox.base_url = sandbox_url.rstrip("/")
    toolkit = ShellToolkit(NodeBoundSandbox(sandbox, SimpleNamespace(node_id="synthetic-long-smoke")))
    toolkit.tool_execution_pipeline = ToolExecutionPipeline(interceptors=[
        ToolTimeoutInterceptor(default_timeout_seconds=120, maximum_timeout_seconds=120)])
    program = toolkit.get_tool("program_run")
    unique = uuid4().hex
    directory = f"/home/ubuntu/output/long-program-smoke-{unique}"
    source_path, chart_path = f"{directory}/analysis.py", f"{directory}/chart.png"
    source = (
        "import sys, time\n"
        "print('synthetic program started', flush=True)\n"
        "time.sleep(float(sys.argv[1]))\n"
        "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
        "plt.plot([1, 4, 2])\n"
        f"plt.savefig({chart_path!r})\nplt.close()\nprint('synthetic chart saved')\n"
    )
    counts = Counter()

    async def request_hook(request):
        counts[request.url.path] += 1

    sandbox.client.event_hooks["request"] = [request_hook]
    ledger = ToolExecutionLedger()
    sessions = [f"long-{unique}", f"cancel-{unique}"]
    try:
        written = await sandbox.file_write(source_path, source)
        assert written.success
        call = {"name": "program_run", "id": "long-analysis", "args": {
            "id": sessions[0], "exec_dir": directory, "script_path": source_path,
            "argv": [str(seconds)], "timeout_seconds": 1}}
        started = time.monotonic()
        with tool_execution_scope(ledger, call["id"]):
            result = (await program.ainvoke(call)).artifact
        elapsed = time.monotonic() - started
        assert result.success and result.data["returncode"] == 0
        assert elapsed >= seconds
        assert counts["/api/v1/shell/program"] == 1
        assert counts["/api/v1/shell/wait"] >= 2
        assert counts["/api/v1/shell/kill"] == 0
        assert ledger.summary()["tracked_operation_count"] == 1 and not ledger.summary()["pending_execution"]
        proof = trusted_program_execution_feedback(program, call, result, ledger)
        assert proof and proof["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
        checked = await sandbox.validate_artifacts([{"path": chart_path, "kind": "image"}])
        assert checked.success and checked.data["files"][0]["valid"] is True

        cancellation = ToolExecutionLedger()
        cancel_call = {"name": "program_run", "id": "cancel-analysis", "args": {
            **call["args"], "id": sessions[1], "argv": ["60"]}}
        with tool_execution_scope(cancellation, cancel_call["id"]):
            worker = asyncio.create_task(program.ainvoke(cancel_call))
            try:
                async with asyncio.timeout(10):
                    while not any(attempt.receipt is not None and attempt.receipt.state == "running"
                                  for attempt in cancellation._attempts.values()):
                        if worker.done():
                            raise AssertionError("Cancellation fixture ended before the cancel request")
                        await asyncio.sleep(0.05)
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Cancellation did not propagate")
                await cancellation.reconcile_pending(timeout_seconds=5, phase="final")
            finally:
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
        assert counts["/api/v1/shell/program"] == 2  # One launch per distinct synthetic operation.
        assert counts["/api/v1/shell/kill"] == 1
        assert cancellation.summary()["tracked_operation_count"] == 1
        assert not cancellation.summary()["pending_execution"]
        assert all(attempt.confirmed and attempt.receipt.state == "exited"
                   and attempt.receipt.returncode != 0 for attempt in cancellation._attempts.values())
        print(json.dumps({"passed": True, "synthetic_seconds": seconds, "elapsed_seconds": round(elapsed, 2),
            "crossed_old_120s_limit": seconds > 120, "launches_per_operation": 1,
            "png_content_validated": True, "cancellation_propagated": True,
            "cancellation_kill_count": 1, "cancellation_receipt_confirmed": True}), flush=True)
    finally:
        for session_id in sessions:
            await sandbox.release_shell(session_id)
        await sandbox.client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-url", required=True)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--seconds", type=int, default=10)
    options = parser.parse_args()
    asyncio.run(run(options.sandbox_url, options.sandbox_id, options.seconds))
