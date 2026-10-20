"""Source recognition supplies a target, never evidence of runtime success."""
import pytest

from app.domain.services.analysis_program_dispatch import shell_runtime_source


def call(command, **kwargs):
    return {"name": "shell_run", "args": {
        "command": command, "exec_dir": "/home/ubuntu/output", **kwargs,
    }}


@pytest.mark.parametrize("command,target", [
    ("Rscript analysis.R", "analysis.R"),
    ("/usr/bin/Rscript analysis.R --input input.csv", "analysis.R"),
    ("node analysis.js", "analysis.js"),
    ("/usr/bin/node ./src/../analysis.MJS --arg 'literal && data'", "analysis.MJS"),
    ("nodejs analysis.cjs", "analysis.cjs"),
    ("bash ./analysis.sh", "analysis.sh"),
    ("sh analysis.SH", "analysis.SH"),
    ("zsh analysis.zsh", "analysis.zsh"),
    ("julia analysis.jl", "analysis.jl"),
    ("ruby analysis.rb", "analysis.rb"),
    ("perl analysis.pl", "analysis.pl"),
    ("node 'analysis (copy).js' 'a > b'", "analysis (copy).js"),
    ("node /home/ubuntu/scripts/analysis.js", "/home/ubuntu/scripts/analysis.js"),
    ("Rscript analysis.R\n", "analysis.R"),
])
def test_static_supported_runtime_source_target_is_identified(command, target):
    expected = target if target.startswith("/") else "/home/ubuntu/output/" + target
    assert shell_runtime_source(call(command)) == expected


@pytest.mark.parametrize("command", [
    "python3 analysis.py",  # Python has its own typed direct-runner receipt.
    "node -e 'console.log(1)'",
    "node --check analysis.js",
    "Rscript --help",
    "bash -c 'node analysis.js'",
    "env MODE=analysis node analysis.js",
    "timeout 10 node analysis.js",
    "node analysis.js | head -1",
    "node analysis.js | cat",
    "node analysis.js > output.json",
    "node analysis.js 2>&1",
    "node analysis.js;",
    "node analysis.js && echo done",
    "node analysis.js || true",
    "node analysis.js\nnode second.js",
    "node $SCRIPT",
    "node ${DIR}/analysis.js",
    "node analysis.js $INPUT",
    "node analysis.js *.csv",
    "node analysis{1,2}.js",
    "node $(whichscript)",
    "node analysis.js `get_input`",
    "node - <<'JS'\nconsole.log(1)\nJS",
    "node analysis.R",
    "Rscript analysis.js",
    "cat analysis.R",
    "./analysis.sh",
    "node 'unterminated.js",
    "node analysis.js\0",
])
def test_unknown_compound_or_inline_commands_have_no_claimed_source(command):
    assert shell_runtime_source(call(command)) is None


@pytest.mark.parametrize("exec_dir", [None, 2, "", "relative", "/tmp\0invalid"])
def test_working_directory_must_be_a_literal_absolute_path(exec_dir):
    assert shell_runtime_source(call("node analysis.js", exec_dir=exec_dir)) is None


@pytest.mark.parametrize("value", [
    {}, {"name": "shell_run", "args": None}, {"name": "shell_run", "args": []},
    {"name": "program_run", "args": {"command": "node analysis.js", "exec_dir": "/tmp"}},
    {"name": "plugin_shell", "args": {"command": "node analysis.js", "exec_dir": "/tmp"}},
])
def test_invalid_call_shapes_cannot_be_reinterpreted(value):
    assert shell_runtime_source(value) is None
