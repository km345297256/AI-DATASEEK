"""Only explicit core-shell authored writes redirect into tracked preparation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.tools import tool

from app.domain.services.analysis_program_dispatch import shell_source_write_redirect
from app.domain.services.tools.base import BaseToolkit, Tool
from app.domain.services.tools.shell import ShellToolkit


@pytest.fixture
def shell():
    return ShellToolkit(SimpleNamespace(exec_command=AsyncMock()))


def call(command, name="shell_run"):
    return {"id": "write", "name": name, "args": {
        "id": "process", "exec_dir": "/home/ubuntu/output", "command": command,
    }}


@pytest.mark.parametrize("command", [
    "cat > analysis.py <<'PY'\nprint(1)\nPY",
    'cat <<"PY" > analysis.py\nprint(1)\nPY',
    "cat <<-PY >> analysis.py\n\tprint(1)\n\tPY\n",
    "cat < original.py > analysis.py",
    "cat original.py > 'analysis (new).py'",
    "cat <<'DATA' > report.md\n# Actual report\nDATA",
    "tee script.R <<'R'\nprint(1)\nR",
    "printf '%s\\n' 'console.log(1)' > analysis.js",
    "printf '%s\\n' 'println(1)' 1>analysis.jl",
    "echo 'select 1;' > analysis.sql",
    "printf '%s' 'report text' >> report.md",
    "printf '%s' 'name,value' > table.csv",
    "printf '%s' '{}' > summary.json",
    "printf '%s' 'content' | tee --append report.md > /dev/null",
    "printf '%s' 'content' | cat | tee report.md",
    "cat input.md | tee -a -i -- report.md",
    "tee -- report.md < input.md",
    "printf '%s' '2'>report.md",
    "printf '%s' 'hello' &> report.md",
    "/usr/bin/printf '%s' 'print(1)' > analysis.py",
    "MODE=analysis command printf '%s' 'x' > report.txt",
    "env MODE=analysis printf '%s' 'x' > report.txt",
    "env -- printf '%s' 'x' > report.txt",
    "ls -l; printf '%s' 'x' > report.md",
    "printf '%s' 'x' > report.md && echo done",
    "sh -c 'printf x > report.md'",
    "bash -lc 'cat <<\"PY\" > analysis.py\nprint(1)\nPY'",
    "sh -c \"sh -c 'printf x > report.md'\"",
    "cat <<'TEXT'\ncat > not-a-command.py\nTEXT\nprintf actual > actual.md",
    "cat <<'ONE' <<'TWO' > report.md\nfirst body\nONE\nsecond body\nTWO",
    "cat <<'TEXT'\nprint(1)\nTEXT\nprintf '%s' '<svg/>' > figure.svg",
    "printf \\\n'%s' 'x' > report.md",
])
@pytest.mark.parametrize("name", ["shell_run", "shell_exec"])
def test_literal_source_and_report_writes_use_file_preparation(shell, command, name):
    reason = shell_source_write_redirect(shell.get_tool(name), call(command, name))
    assert reason and "NOT executed" in reason
    assert "file_write" in reason and "file_str_replace" in reason
    assert "non-Python files" in reason
    shell.sandbox.exec_command.assert_not_awaited()


@pytest.mark.parametrize("command", [
    "cat analysis.py",
    "cat < analysis.py",
    "head -20 report.md",
    "printf '%s' 'cat > script.py'",
    "printf '%s' '>' 'script.py'",
    "printf '%s' '>>' 'report.md'",
    "echo 'tee analysis.py'",
    "cat <<'TEXT'\ncat > analysis.py\nprintf x > report.md\nTEXT",
    'cat <<"TEXT"\ncat > analysis.py\nTEXT',
    "cat <<-TEXT\n\tprintf x > report.md\n\tTEXT",
    "cat <<'ONE' <<'TWO'\ncat > first.py\nONE\ntee second.R\nTWO",
    "python3 - <<'PY'\nprint('cat > analysis.py')\nPY",
    "python3 -c \"print('cat > analysis.py')\"",
    "python3 -c 'print(42)' > report.md",
    "python3 analysis.py > run.log",
    "ls -l > inventory.txt",
    "wc -l data.csv > rows.txt",
    "head -3 data.csv > sample.csv",
    "python3 -c 'print(42)' | tee report.md",
    "wc -l data.csv | tee rows.txt",
    "python3 -c 'print(42)' | cat > summary.json",
    "python3 -c 'print(42)' | tee summary.json | tee second.csv",
    "head -3 data.csv | cat | tee sample.csv",
    "awk '{print $1}' input.csv > selected.csv",
    "cat raw.bin > copy.bin",
    "printf '%s' 'diagnostic' 2>errors.txt",
    "printf '%s' 'x' > /dev/null",
    "tee /dev/null",
    "# printf x > report.md\nls -l",
    "echo data # tee report.md",
    "sh -c 'cat report.md'",
    "sh -c 'printf \"%s\" \"cat > analysis.py\"'",
    "sh -c \"$COMMAND\"",
    "printf x > $OUTPUT",
    "printf x > \"${OUTPUT}.py\"",
    "printf x > *.py",
    "$(printf cat) > report.md",
    "echo $(printf 'cat > analysis.py')",
    "printf x > `get_target`",
    "'FOO=x' printf x > report.md",
    "cat <<'MISSING' > report.md\nunterminated",
    "cat > 'unterminated.py",
    "tee --unknown-option report.md",
])
def test_data_diagnostics_read_only_commands_and_unknown_syntax_are_not_reinterpreted(shell, command):
    assert shell_source_write_redirect(shell.get_tool("shell_run"), call(command)) is None
    shell.sandbox.exec_command.assert_not_awaited()


class PluginShell(BaseToolkit):
    name: str = "plugin-shell"

    @tool
    async def shell_run(self, command: str) -> str:
        """Plugin-specific shell-like label, not a core execution identity."""
        return command


def test_same_named_plugin_and_unregistered_core_wrapper_are_not_intercepted(shell):
    value = call("printf x > analysis.py")
    plugin = PluginShell()
    assert shell_source_write_redirect(plugin.get_tool("shell_run"), value) is None
    unregistered = Tool(ShellToolkit.shell_run, toolkit=shell)
    assert shell_source_write_redirect(unregistered, value) is None
    assert shell_source_write_redirect(shell.get_tool("program_run"), value) is None
    assert shell_source_write_redirect(shell.get_tool("shell_run"), call("printf x > analysis.py", "plugin_write")) is None


@pytest.mark.parametrize("args", [None, [], {}, {"command": None}, {"command": 2}])
def test_invalid_arguments_do_not_create_a_dispatch_claim(shell, args):
    assert shell_source_write_redirect(shell.get_tool("shell_run"), {"name": "shell_run", "args": args}) is None


def test_nested_shell_depth_is_bounded_without_execution(shell):
    import shlex
    command = "printf x > report.md"
    for _ in range(7):
        command = "sh -c " + shlex.quote(command)
    assert shell_source_write_redirect(shell.get_tool("shell_run"), call(command)) is None
