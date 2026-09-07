from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

import pytest

from app.domain.services.code_mode import (
    CodeModeInterpreter,
    CodeModeLimitExceeded,
    CodeModeLimits,
    CodeModeToolCallError,
    CodeModeToolSpec,
    CodeModeValidationError,
    eligible_code_mode_tool_specs,
)
from app.domain.services.tools.code_mode import (
    CODE_MODE_TOOL_NAME,
    CodeModeToolkit,
)


def _definition(
    name: str = "data_format_inspect",
    *,
    plugin: str = "data_foundation",
    scopes: list[str] | None = None,
    effects: list[str] | None = None,
    permissions: list[str] | None = None,
    credentials: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "contract_version": 2,
        "name": name,
        "plugin": plugin,
        "scopes": scopes or ["dataset_fast_path", "code_mode"],
        "execution": {
            "effects": effects or ["sandbox_read"],
            "permissions": permissions or [],
            "credentials": credentials or [],
        },
    }


def _spec(name: str = "data_format_inspect", **kwargs) -> CodeModeToolSpec:
    return CodeModeToolSpec.from_manifest(_definition(name, **kwargs))


@pytest.mark.asyncio
async def test_supported_program_chains_public_results_and_returns_only_last_value():
    calls: list[tuple[str, dict, str]] = []

    async def dispatch(tool_name: str, arguments: dict, *, call_id: str):
        calls.append((tool_name, arguments, call_id))
        if tool_name == "data_format_inspect":
            return {
                "success": True,
                "data": {"path": "/home/ubuntu/datasets/sample.csv", "rows": 3},
            }
        return {"success": True, "data": {"rows": 3, "columns": 2}}

    result = await CodeModeInterpreter().run(
        """
inspected = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/sample.csv"]})
profiled = await tools.call("table_profile", {"input_path": inspected["data"]["path"]})
profiled["data"]
""".strip(),
        dispatcher=dispatch,
        tool_specs=[_spec(), _spec("table_profile", plugin="tabular")],
    )

    assert result.output == {"rows": 3, "columns": 2}
    assert result.call_count == 2
    public = result.public_data()
    assert public["output"] == {"rows": 3, "columns": 2}
    assert [step["tool_name"] for step in public["steps"]] == [
        "data_format_inspect",
        "table_profile",
    ]
    assert all(step["status"] == "succeeded" for step in public["steps"])
    assert all(re.fullmatch(r"code-mode-[0-9a-f]{32}", step["call_id"])
               for step in public["steps"])
    assert calls[1][1] == {"input_path": "/home/ubuntu/datasets/sample.csv"}
    assert "input_paths" not in json.dumps(public)


@pytest.mark.asyncio
async def test_last_assignment_is_the_output_when_no_final_expression_is_present():
    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        assert call_id
        return {"success": True, "data": {"value": 7}}

    result = await CodeModeInterpreter().run(
        'value = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/a.nc"]})',
        dispatcher=dispatch,
        tool_specs=[_spec()],
    )

    assert result.output == {"success": True, "data": {"value": 7}}


@pytest.mark.asyncio
async def test_unsupported_later_statement_rejects_whole_program_before_first_call():
    calls = 0

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        nonlocal calls
        calls += 1
        return {"success": True}

    code = """
first = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/a.nc"]})
import os
""".strip()
    with pytest.raises(CodeModeValidationError):
        await CodeModeInterpreter().run(
            code,
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        'leak = __import__("os")',
        "leak = first.__class__",
        'leak = getattr(first, "data")',
        'leak = open("/etc/passwd")',
        'leak = await tools.call("data_format_inspect", {"value": tools.__class__})',
        "for item in first:\n    leak = item",
    ],
)
async def test_python_object_and_control_flow_injection_is_rejected_up_front(suffix):
    calls = 0

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        nonlocal calls
        calls += 1
        return {"success": True}

    code = (
        'first = await tools.call("data_format_inspect", '
        '{"input_paths": ["/home/ubuntu/datasets/a.nc"]})\n'
        + suffix
    )
    with pytest.raises(CodeModeValidationError):
        await CodeModeInterpreter().run(
            code,
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_path",
    [
        "/etc/passwd",
        "/Users/example/private.csv",
        "C:\\Users\\example\\private.csv",
        "file:///etc/passwd",
        "../../private.csv",
    ],
)
async def test_host_path_spellings_are_rejected_before_any_tool_runs(private_path):
    calls = 0

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        nonlocal calls
        calls += 1
        return {"success": True}

    code = (
        'first = await tools.call("data_format_inspect", '
        '{"input_paths": ["/home/ubuntu/datasets/a.nc"]})\n'
        f'second = await tools.call("data_format_inspect", {{"input_paths": [{private_path!r}]}})'
    )
    with pytest.raises(CodeModeValidationError, match="host_path"):
        await CodeModeInterpreter().run(
            code,
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert calls == 0


def test_manifest_admission_is_explicit_and_fail_closed():
    assert _spec().eligible is True
    assert _spec(scopes=["dataset_fast_path"]).eligible is False
    assert _spec(effects=["sandbox_read", "sandbox_write"]).eligible is False
    assert _spec(effects=["network"]).eligible is False
    assert _spec(permissions=["dataset:read"]).eligible is False
    assert _spec(
        effects=["sandbox_read", "credential_use"],
        credentials=[{"slot": "token", "provider": "example"}],
    ).eligible is False
    assert _spec("shell_run").eligible is False
    assert _spec(plugin="mcp").eligible is False


def test_only_reviewed_read_only_manifests_are_code_mode_eligible():
    repository_root = Path(__file__).resolve().parents[2]
    expected = {
        "data_format_inspect",
        "hierarchical_store_inspect",
        "cf_semantics_validate",
        "workbook_inspect",
        "table_profile",
    }
    definitions = []
    for relative in (
        "tools/data_foundation/manifest.json",
        "tools/tabular/manifest.json",
    ):
        manifest = json.loads((repository_root / relative).read_text(encoding="utf-8"))
        definitions.extend(
            {**item, "plugin": manifest["plugin"]}
            for item in manifest["tools"]
        )

    selected = eligible_code_mode_tool_specs(definitions)

    assert {spec.name for spec in selected} == expected
    assert all(spec.effects == frozenset({"sandbox_read"}) for spec in selected)
    assert all(not spec.permissions and spec.credential_count == 0 for spec in selected)


@pytest.mark.asyncio
async def test_call_count_limit_is_validated_before_dispatch():
    calls = 0

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        nonlocal calls
        calls += 1
        return {"success": True}

    code = """
first = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/a.nc"]})
second = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/b.nc"]})
""".strip()
    with pytest.raises(CodeModeLimitExceeded, match="calls"):
        await CodeModeInterpreter(CodeModeLimits(max_calls=1)).run(
            code,
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_utf8_code_bytes_ast_statements_and_depth_are_bounded():
    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        return {"success": True}

    with pytest.raises(CodeModeLimitExceeded, match="code_bytes"):
        await CodeModeInterpreter(CodeModeLimits(max_code_bytes=20)).run(
            '结果 = await tools.call("data_format_inspect", {"说明": "中文"})',
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )

    code = (
        'a = 1\n'
        'b = 2\n'
        'result = await tools.call("data_format_inspect", '
        '{"input_paths": ["/home/ubuntu/datasets/a.nc"]})'
    )
    with pytest.raises(CodeModeLimitExceeded, match="statements"):
        await CodeModeInterpreter(CodeModeLimits(max_statements=2)).run(
            code,
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )

    nested = "[" * 5 + "1" + "]" * 5
    with pytest.raises(CodeModeLimitExceeded, match="expression_depth"):
        await CodeModeInterpreter(CodeModeLimits(max_depth=2)).run(
            'result = await tools.call("data_format_inspect", '
            f'{{"value": {nested}}})',
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )

    with pytest.raises(CodeModeLimitExceeded, match="ast_nodes"):
        await CodeModeInterpreter(CodeModeLimits(max_ast_nodes=8)).run(
            'result = await tools.call("data_format_inspect", {})',
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )


@pytest.mark.asyncio
async def test_duration_and_result_size_are_bounded():
    async def slow_dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        await asyncio.sleep(10)
        return {"success": True}

    code = 'result = await tools.call("data_format_inspect", {})'
    with pytest.raises(CodeModeLimitExceeded, match="duration"):
        await CodeModeInterpreter(CodeModeLimits(timeout_seconds=0.01)).run(
            code,
            dispatcher=slow_dispatch,
            tool_specs=[_spec()],
        )

    async def huge_dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        return {"success": True, "data": "x" * 1000}

    with pytest.raises(CodeModeLimitExceeded, match="value_bytes"):
        await CodeModeInterpreter(CodeModeLimits(max_value_bytes=100)).run(
            code,
            dispatcher=huge_dispatch,
            tool_specs=[_spec()],
        )


@pytest.mark.asyncio
async def test_budget_cancellation_signal_is_propagated_unchanged():
    class BudgetStopped(asyncio.CancelledError):
        pass

    signal = BudgetStopped("stop dependent work")

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        raise signal

    with pytest.raises(BudgetStopped) as caught:
        await CodeModeInterpreter().run(
            'result = await tools.call("data_format_inspect", {})',
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert caught.value is signal


@pytest.mark.asyncio
async def test_private_dispatch_exception_and_arguments_are_not_returned():
    private = "credential=do-not-expose input=/Users/private/data.nc"

    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        raise RuntimeError(private)

    with pytest.raises(CodeModeToolCallError) as caught:
        await CodeModeInterpreter().run(
            'result = await tools.call("data_format_inspect", '
            '{"input_paths": ["/home/ubuntu/datasets/public.nc"]})',
            dispatcher=dispatch,
            tool_specs=[_spec()],
        )
    assert private not in str(caught.value)
    assert "input_paths" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_toolkit_is_disabled_by_default_and_updates_fast_path_visibility():
    async def dispatch(_tool_name: str, _arguments: dict, *, call_id: str):
        return {"success": True, "data": {"ok": True}}

    toolkit = CodeModeToolkit(dispatch, [_spec()])
    assert toolkit.get_tools() == []
    assert toolkit.dataset_fast_path_tool_names == set()

    toolkit.set_enabled(True)
    assert toolkit.dataset_fast_path_tool_names == {CODE_MODE_TOOL_NAME}
    wrapped = toolkit.get_tool(CODE_MODE_TOOL_NAME)
    message = await wrapped.ainvoke({
        "id": "outer-code-call",
        "name": CODE_MODE_TOOL_NAME,
        "args": {"code": 'result = await tools.call("data_format_inspect", {})'},
    })

    assert message.artifact.success is True
    assert message.artifact.data["call_count"] == 1
    assert message.artifact.data["output"] == {
        "success": True,
        "data": {"ok": True},
    }

    toolkit.set_enabled(False)
    assert toolkit.get_tools() == []
    assert toolkit.dataset_fast_path_tool_names == set()
