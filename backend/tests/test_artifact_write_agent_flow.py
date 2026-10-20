"""Offline end-to-end write-phase tests through the registered core file tools.

Only the model and sandbox I/O are fake. Program receipt trust is explicitly
mocked where needed; those tests do not claim a real process was launched.
"""
import hashlib
import posixpath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage
from langchain.tools import tool
from pydantic import PrivateAttr

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.plan import ExecutionResult, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_recovery import AnalysisRecoveryContext, analysis_recovery_scope
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from test_tool_response_protocol_recovery import make_agent


REPORT = "/home/ubuntu/output/summary.md"
PROGRAM = "/home/ubuntu/output/analyze.py"
INPUT = "/home/ubuntu/datasets/synthetic/input.csv"


def call(name, identifier, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": identifier, "args": args}])


def write(identifier, body, path=REPORT, **kwargs):
    return call("file_write", identifier, file=path, content=body, **kwargs)


def final(result="已整理合成数据的说明。", attachments=None):
    return AIMessage(content=ExecutionResult(success=True, result=result,
        attachments=attachments or []).model_dump_json())


def memory_files(agent, initial=None):
    """Real FileToolkit dispatch, with a deterministic in-memory sandbox."""
    files = dict(initial or {})

    async def save(**args):
        path, body = posixpath.normpath(args["file"]), args["content"]
        files[path] = files.get(path, "") + body if args.get("append") else body
        return ToolResult(success=True, data={"file": path, "bytes_written": len(body.encode())})

    async def read(**args):
        path = posixpath.normpath(args["file"])
        if path not in files:
            return ToolResult(success=False, data={"error_code": "file_not_found"})
        body = files[path]
        if args.get("start_line") is not None or args.get("end_line") is not None:
            body = "\n".join(body.splitlines()[args.get("start_line"):args.get("end_line")])
        return ToolResult(success=True, data={"file": path, "content": body})

    async def replace(**args):
        path = posixpath.normpath(args["file"])
        if path not in files or args["old_str"] not in files[path]:
            return ToolResult(success=False, data={"error_code": "old_string_not_found"})
        files[path] = files[path].replace(args["old_str"], args["new_str"])
        return ToolResult(success=True, data={"file": path})

    sandbox = SimpleNamespace(id="synthetic-phase-sandbox", file_write=AsyncMock(side_effect=save),
        file_read=AsyncMock(side_effect=read), file_replace=AsyncMock(side_effect=replace))
    toolkit = FileToolkit(sandbox)
    agent.toolkits.append(toolkit)
    return sandbox, files, toolkit


def completed(events, identifier):
    return next(event for event in events if isinstance(event, ToolEvent)
        and event.status == ToolStatus.CALLED and event.tool_call_id == identifier)


def answer(events):
    return ExecutionResult.model_validate_json(next(event.message for event in reversed(events)
        if isinstance(event, MessageEvent)))


def receipt_runtime(agent, *, returncode):
    """Fake sandbox I/O while retaining genuine core runtime receipt checks."""
    from app.domain.services.execution_evidence import register_shell_attempt
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

    runtime = object.__new__(DockerSandbox)
    runtime._container_name = "synthetic-runtime"
    attempts = []

    async def launch(identifier, cwd, command):
        attempt = register_shell_attempt(runtime, identifier, cwd, command)
        assert attempt is not None
        attempts.append(attempt)
        assert attempt.observe({"version": 1, "operation_id": attempt.operation_id,
            "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
            "state": "exited", "returncode": returncode, "process_tree_quiescent": True})
        return ToolResult(success=returncode == 0,
            data={"status": "completed", "returncode": returncode, "output": "Synthetic runtime result"})

    runtime.exec_command = AsyncMock(side_effect=launch)
    agent.toolkits.append(ShellToolkit(runtime))
    return runtime, attempts


@pytest.mark.asyncio
async def test_saved_report_redraft_changes_to_tool_free_delivery_without_second_write(monkeypatch):
    agent, _, requests, bindings = make_agent(monkeypatch, [
        write("saved", "# 合成报告\n三条记录，无缺失。"),
        write("redraft", "# 新措辞\n共有三条完整记录。"),
        final(attachments=[REPORT]),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("解释合成数据")]
    assert sandbox.file_write.await_count == 1
    assert files[REPORT] == "# 合成报告\n三条记录，无缺失。"
    redraft = completed(events, "redraft")
    assert redraft.dispatch_status == "not_started"
    assert redraft.function_result["write_action"] == "finalize_saved"
    assert redraft.function_result["success"] is True
    assert bindings[-1]["tools"] == [] and len(requests) == 3
    assert answer(events).attachments == [REPORT]
    assert agent.last_execution_outcome["code"] == "completed"
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [AIMessage(content="not valid JSON"), RuntimeError("synthetic model unavailable")])
async def test_report_delivery_fallback_preserves_saved_body_not_rejected_redraft(monkeypatch, completion):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("saved", "# 合成报告\n观察值为 12。"),
        write("redraft", "This unexecuted proposal is not the saved report."), completion,
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("解释合成数据")]
    assert sandbox.file_write.await_count == 1 and bindings[-1]["tools"] == []
    assert answer(events).result == files[REPORT]
    assert answer(events).attachments == [REPORT]
    assert answer(events).success
    assert agent.last_execution_outcome["code"] == "completed"


@pytest.mark.asyncio
async def test_saved_report_does_not_complete_step_with_missing_chart(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("saved", "# Metadata summary"), write("redraft", "# Reworded summary"),
        final("图表仍需生成，不能用说明文件代替。", [REPORT]),
    ], agent_type=ExecutionAgent)
    agent._current_step = Step(deliverables=[DeliverableRequirement(kind="image", min_count=1)])
    sandbox, _, _ = memory_files(agent)
    events = [event async for event in agent.execute("生成图表并说明")]
    assert sandbox.file_write.await_count == 1
    assert all(binding["tools"] for binding in bindings)
    assert agent._saved_report_delivery_paths is None
    assert "图表仍需生成" in answer(events).result


@pytest.mark.asyncio
async def test_distinct_append_and_targeted_patch_stay_available(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("section-1", "# Heading\n"), write("section-2", "One\n", append=True),
        write("section-3", "Two\n", append=True),
        call("file_str_replace", "correct", file=REPORT, old_str="Two", new_str="Second"),
        final(attachments=[REPORT]),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("逐段构建合成报告")]
    assert sandbox.file_write.await_count == 3 and sandbox.file_replace.await_count == 1
    assert files[REPORT] == "# Heading\nOne\nSecond\n"
    assert all(binding["tools"] for binding in bindings)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_new_scoped_input_read_opens_a_report_revision(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("initial", "# Initial report"), call("file_read", "new-input", file=INPUT),
        write("informed", "# Updated from newly observed input"), final(attachments=[REPORT]),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent, {INPUT: "id,value\n1,12\n"})
    recovery = AnalysisRecoveryContext(review=AsyncMock())
    recovery.progress.read_scope_paths = frozenset({INPUT})
    with analysis_recovery_scope(recovery):
        events = [event async for event in agent.execute("读新输入并更新合成报告")]
    assert sandbox.file_write.await_count == 2 and sandbox.file_read.await_count == 1
    assert files[REPORT] == "# Updated from newly observed input"
    assert all(binding["tools"] for binding in bindings)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_reading_own_report_does_not_authorize_another_full_draft(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("initial", "# Saved report"), call("file_read", "own-report", file=REPORT),
        write("redundant", "# Redraft without new input"), final(attachments=[REPORT]),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("解释合成数据")]
    assert sandbox.file_write.await_count == 1 and sandbox.file_read.await_count == 1
    assert files[REPORT] == "# Saved report"
    assert bindings[-1]["tools"] == []
    assert completed(events, "redundant").function_result["write_action"] == "finalize_saved"


@pytest.mark.asyncio
async def test_pending_execution_cannot_skip_reconciliation_via_saved_report(monkeypatch):
    def pending(agent):
        agent._tool_execution_ledger.record_unknown("synthetic-unknown-operation")
        return write("redraft", "# Not executed")

    agent, _, _, _ = make_agent(monkeypatch, [
        write("initial", "# Saved report"), pending,
        final("先前操作尚未确认，保留已保存的说明。", [REPORT]),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("解释合成数据")]
    assert sandbox.file_write.await_count == 1 and files[REPORT] == "# Saved report"
    assert agent._tool_execution_ledger.summary()["pending_execution"]
    # An unresolvable unknown is rejected at batch admission, before even the
    # file-phase policy or the individual CALLING event may run.
    assert not any(isinstance(event, ToolEvent) and event.tool_call_id == "redraft" for event in events)
    assert any(item.type == "tool" and item.tool_call_id == "redraft"
        and "NOT executed" in item.content for item in agent.memory.messages)
    assert agent._saved_report_delivery_paths is None
    assert agent.last_execution_outcome["code"] != "completed"


class PluginFileToolkit(BaseToolkit):
    name: str = "synthetic_plugin"
    _writes: list[str] = PrivateAttr(default_factory=list)

    @tool
    async def file_write(self, file: str, content: str) -> ToolResult:
        """A namesake plugin; this is not the first-party file operation."""
        self._writes.append(content)
        return ToolResult(success=True, data={"file": file})


@pytest.mark.asyncio
async def test_plugin_namesake_is_not_governed_by_core_write_phase(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("plugin-1", "one"), write("plugin-2", "two"), final(),
    ], agent_type=ExecutionAgent)
    toolkit = PluginFileToolkit()
    agent.toolkits.append(toolkit)
    events = [event async for event in agent.execute("调用合成插件")]
    assert toolkit._writes == ["one", "two"]
    assert all(binding["tools"] for binding in bindings)
    assert agent._saved_report_delivery_paths is None
    assert not any(isinstance(event, ErrorEvent) for event in events)


class ProgramProbeToolkit(BaseToolkit):
    name: str = "synthetic_program"
    _runs: list[str] = PrivateAttr(default_factory=list)

    @tool
    async def program_run(self, script_path: str) -> ToolResult:
        """Fake only runtime I/O; receipt authentication is mocked explicitly."""
        self._runs.append(script_path)
        return ToolResult(success=True, data={"returncode": 0})


@pytest.mark.asyncio
async def test_full_script_redraft_moves_to_execution_and_receipt_allows_revision(monkeypatch):
    path = PROGRAM
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("initial", "initial source", path), write("blind", "blind new source", path),
        write("blind-second", "another blind source", path),
        call("program_run", "run", script_path=path),
        write("after-run", "next stage source", path),
        write("next-blind", "next blind rewrite", path), write("next-blind-second", "next blind again", path),
        call("program_run", "run-again", script_path=path),
        write("final-stage", "final stage source", path), final(),
    ])
    sandbox, files, _ = memory_files(agent)
    runner = ProgramProbeToolkit()
    agent.toolkits.append(runner)
    monkeypatch.setattr("app.domain.services.agents.base.resolved_program_path",
        lambda tool, invocation: path if tool.name == "program_run" else None)
    monkeypatch.setattr("app.domain.services.agents.base.trusted_program_prerequisites", AsyncMock(return_value=None))
    monkeypatch.setattr("app.domain.services.agents.base.program_preparation_failure", lambda *args: None)
    monkeypatch.setattr("app.domain.services.agents.base.trusted_program_execution_feedback",
        lambda _tool, invocation, *_rest: {"script_path": path, "operation_id": invocation["id"],
            "source_digest": hashlib.sha256(files[path].encode()).hexdigest(), "returncode": 0})
    events = [event async for event in agent.execute("构建并运行合成程序")]
    assert sandbox.file_write.await_count == 3
    assert runner._runs == [path, path] and files[path] == "final stage source"
    redraft = completed(events, "blind")
    assert redraft.dispatch_status == "not_started"
    assert redraft.function_result["write_action"] == "execute_saved"
    assert redraft.function_result["success"] is True
    assert "file_write" not in [tool.name for tool in bindings[2]["tools"]]
    assert "file_write" in [tool.name for tool in bindings[4]["tools"]]
    assert not agent._analysis_progress.should_stop
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".PY", ".R", ".js", ".sh", ".ipynb"])
async def test_other_script_languages_redirect_without_inventing_python_runner_support(monkeypatch, suffix):
    path = "/home/ubuntu/output/analyze" + suffix
    agent, _, _, _ = make_agent(monkeypatch, [
        write("initial", "saved source", path), write("blind", "blind new source", path),
        final("已保存源码，下一步需使用适配的运行环境。"),
    ])
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("准备合成程序")]
    assert sandbox.file_write.await_count == 1 and files[path] == "saved source"
    redraft = completed(events, "blind").function_result
    assert redraft["write_action"] == "execute_saved"
    assert "supported runtime" in redraft["message"]
    assert "program_run" not in redraft["message"]


@pytest.mark.asyncio
async def test_report_alias_and_newline_flags_preserve_exact_saved_body_in_fallback(monkeypatch):
    agent, _, _, _ = make_agent(monkeypatch, [
        write("saved", "Report", "/home/ubuntu/output/./summary.md",
            leading_newline=True, trailing_newline=True),
        write("again", "Unexecuted redraft"), AIMessage(content="bad wrapper"),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("准备合成报告")]
    assert sandbox.file_write.await_count == 1
    assert answer(events).result == files[REPORT] == "\nReport\n"
    assert answer(events).attachments == [REPORT]


@pytest.mark.asyncio
async def test_incrementally_built_report_finishes_with_latest_saved_contents(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("saved", "# Report\n"), write("append", "One\n", append=True),
        call("file_str_replace", "patch", file=REPORT, old_str="One", new_str="Two"),
        write("again", "Unexecuted redraft"), AIMessage(content="bad wrapper"),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("准备合成报告")]
    assert sandbox.file_write.await_count == 2 and sandbox.file_replace.await_count == 1
    assert answer(events).result == files[REPORT] == "# Report\nTwo\n"
    assert bindings[-1]["tools"] == []


class MutatingProbeToolkit(BaseToolkit):
    name: str = "synthetic_mutation"
    _mutate: object = PrivateAttr(default=None)
    _success: bool = PrivateAttr(default=True)

    @tool
    async def mutate_probe(self) -> ToolResult:
        """Fake a tool that has changed a saved file before returning."""
        self._mutate()
        return ToolResult(success=self._success,
            data={} if self._success else {"error_code": "synthetic_partial_failure"})


@pytest.mark.asyncio
async def test_unknown_mutating_tool_invalidates_cached_report_and_host_reads_actual_content(monkeypatch):
    agent, _, requests, bindings = make_agent(monkeypatch, [
        write("saved", "# Old cached report"), call("mutate_probe", "mutation"),
        write("redraft", "# Unexecuted redraft"), AIMessage(content="bad wrapper"),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    mutation = MutatingProbeToolkit()
    mutation._mutate = lambda: files.update({REPORT: "# Actual saved content after partial mutation"})
    agent.toolkits.append(mutation)
    events = [event async for event in agent.execute("准备合成报告")]
    assert sandbox.file_write.await_count == 1 and sandbox.file_read.await_count == 1
    assert sandbox.file_read.call_args.kwargs["file"] == REPORT
    assert answer(events).result == files[REPORT]
    assert "Old cached" not in answer(events).result
    assert len(requests) == 4 and bindings[-1]["tools"] == []
    reads = [event for event in events if isinstance(event, ToolEvent)
        and event.status == ToolStatus.CALLED and event.function_name == "file_read"]
    assert len(reads) == 1 and reads[0].tool_call_id.startswith("host-saved-output-")


@pytest.mark.asyncio
async def test_failed_mutation_clears_stale_cache_without_bypassing_unknown_execution(monkeypatch):
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("saved", "# Old cached report"), call("mutate_probe", "mutation"),
        AIMessage(content=ExecutionResult(success=False,
            result="操作状态尚未确认，不能使用之前的缓存说明代替当前文件。").model_dump_json()),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    mutation = MutatingProbeToolkit()
    mutation._success = False
    mutation._mutate = lambda: files.update({REPORT: "# Partly changed"})
    agent.toolkits.append(mutation)
    events = [event async for event in agent.execute("准备合成报告")]
    assert sandbox.file_write.await_count == 1 and sandbox.file_read.await_count == 0
    assert agent._saved_output_bodies == {} and agent._saved_report_delivery_paths is None
    assert agent._tool_execution_ledger.summary()["pending_execution"]
    assert bindings[-1]["tools"] == []
    assert "Old cached report" not in answer(events).result


@pytest.mark.asyncio
async def test_large_report_fallback_labels_excerpt_and_preserves_full_attachment(monkeypatch):
    body = "# Synthetic report\n" + "long observation\n" * 5000
    agent, _, _, bindings = make_agent(monkeypatch, [
        write("saved", body), write("again", "Unexecuted redraft"), AIMessage(content="bad wrapper"),
    ], agent_type=ExecutionAgent)
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("准备长报告")]
    result = answer(events)
    assert sandbox.file_write.await_count == 1 and files[REPORT] == body
    assert "节选" in result.result and "完整内容" in result.result
    assert len(result.result.encode()) < len(body.encode())
    assert result.attachments == [REPORT] and bindings[-1]["tools"] == []


@pytest.mark.asyncio
async def test_repeated_full_script_proposals_do_not_create_an_unbounded_control_loop(monkeypatch):
    agent, _, requests, bindings = make_agent(monkeypatch, [
        write("initial", "saved source", PROGRAM),
        *[write(f"proposal-{index}", f"blind redraft {index}", PROGRAM) for index in range(3)],
        final("已保存源码；未执行重复改写，尚需运行。"),
    ])
    sandbox, files, _ = memory_files(agent)
    events = [event async for event in agent.execute("准备合成程序")]
    assert sandbox.file_write.await_count == 1 and files[PROGRAM] == "saved source"
    assert len(requests) == 5 and bindings[-1]["tools"] == []
    assert all("file_write" not in [tool.name for tool in binding["tools"]] for binding in bindings[2:])
    assert agent.last_execution_outcome["code"] == "analysis_no_progress_loop"
    assert agent.last_execution_outcome["tool_batch_limit"] is None
    assert all(completed(events, f"proposal-{index}").function_result["success"] for index in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
async def test_distinct_real_non_python_terminal_receipts_reopen_revisions_for_same_command(monkeypatch, returncode):
    """Real receipt registration/identity/pipeline; no shell or model runs."""
    path = "/home/ubuntu/output/analyze.R"
    run = lambda index: call("shell_run", f"run-{index}", id=f"shell-{index}",
        exec_dir="/home/ubuntu/output", command="Rscript analyze.R")
    agent, _, requests, _ = make_agent(monkeypatch, [
        write("initial", "source one", path), run(1), write("after-one", "source two", path),
        run(2), write("after-two", "source three", path), final(),
    ])
    files_sandbox, files, _ = memory_files(agent)
    runtime, attempts = receipt_runtime(agent, returncode=returncode)
    events = [event async for event in agent.execute("按实际运行反馈修订合成程序")]
    assert runtime.exec_command.await_count == 2 and files_sandbox.file_write.await_count == 3
    assert files[path] == "source three" and len(requests) == 6
    assert len({attempt.operation_id for attempt in attempts}) == 2
    assert len({attempt.command_digest for attempt in attempts}) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_three_new_scoped_inputs_allow_three_informed_script_revisions(monkeypatch):
    sources = {f"/home/ubuntu/datasets/synthetic/part-{index}.csv": f"id,value\n{index},{index * 10}\n"
        for index in range(3)}
    responses = [write("initial", "initial source", PROGRAM)]
    for index, path in enumerate(sources):
        responses.extend([call("file_read", f"read-{index}", file=path),
            write(f"informed-{index}", f"informed source {index}", PROGRAM)])
    responses.append(final())
    agent, _, _, _ = make_agent(monkeypatch, responses)
    sandbox, files, _ = memory_files(agent, sources)
    recovery = AnalysisRecoveryContext(review=AsyncMock())
    recovery.progress.read_scope_paths = frozenset(sources)
    with analysis_recovery_scope(recovery):
        events = [event async for event in agent.execute("结合每个新输入继续完善合成分析")]
    assert sandbox.file_write.await_count == 4 and sandbox.file_read.await_count == 3
    assert files[PROGRAM] == "informed source 2"
    assert not recovery.progress.should_stop
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["plugin_namesake", "zero_replacements"])
async def test_no_actual_core_edit_cannot_unlock_failed_non_python_runtime(monkeypatch, mutation_kind):
    path = "/home/ubuntu/output/analyze.R"
    run = lambda index: call("shell_run", f"run-{index}", id=f"shell-{index}",
        exec_dir="/home/ubuntu/output", command="Rscript analyze.R")
    edit = (write("not-core", "claimed new source", path) if mutation_kind == "plugin_namesake" else
        call("file_str_replace", "not-changed", file=path, old_str="missing", new_str="replacement"))
    agent, _, _, _ = make_agent(monkeypatch, [run(1), edit, run(2), final("同一失败未得到实际源文件修正。")])
    if mutation_kind == "plugin_namesake":
        plugin = PluginFileToolkit()
        agent.toolkits.append(plugin)
    else:
        sandbox, files, _ = memory_files(agent, {path: "unchanged source"})
        sandbox.file_replace = AsyncMock(return_value=ToolResult(success=True,
            data={"file": path, "replaced_count": 0}))
    runtime, _ = receipt_runtime(agent, returncode=1)
    events = [event async for event in agent.execute("合成非Python失败恢复边界测试")]
    assert runtime.exec_command.await_count == 1
    if mutation_kind == "plugin_namesake":
        assert plugin._writes == ["claimed new source"]
    else:
        assert sandbox.file_replace.await_count == 1 and files[path] == "unchanged source"
    assert completed(events, "run-2").dispatch_status == "not_started"
    assert completed(events, "run-2").function_result["blocked_by_policy"] == "analysis_no_progress_loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["relative.py", "~/not-expanded.PY"])
async def test_unmanaged_path_does_not_disable_existing_legacy_draft_guard(monkeypatch, path):
    # Core file dispatch does not itself prove a target was accepted by the
    # phase policy. Never guess cwd/home just to label such a call managed.
    agent, _, _, _ = make_agent(monkeypatch, [
        *[write(f"draft-{index}", f"source {index}", path) for index in range(3)], final(),
    ])
    sandbox, _, _ = memory_files(agent)
    events = [event async for event in agent.execute("合成路径管理边界测试")]
    assert sandbox.file_write.await_count == 2
    result = completed(events, "draft-2").function_result
    assert result["blocked_by_policy"] == "analysis_no_progress_loop"
    assert "written twice" in result["message"]
