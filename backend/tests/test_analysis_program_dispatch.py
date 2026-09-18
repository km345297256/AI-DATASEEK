import pytest

from app.domain.services.analysis_program_dispatch import shell_saved_program, saved_program_redirect


def command(value, *, name="shell_run"):
    return {"name": name, "args": {"command": value, "exec_dir": "/home/ubuntu/output"}}


@pytest.mark.parametrize("value", [
    "python3 analysis.py",
    "python3 analysis.py 2>&1 | tail -80 && ls -l",
    "python3 -u -- analysis.py --input data.csv",
    "/usr/bin/python3.12 -W ignore -X dev analysis.py",
    "./analysis.py",
    "env MODE=diagnostic python3 analysis.py",
    "MPLBACKEND=Agg MODE=diagnostic python3 analysis.py 2>&1 | tail -80",
    "timeout 30s python3 analysis.py",
    "bash -c 'python3 analysis.py 2>&1 | tail -80'",
    "sh -lc 'python3 analysis.py'",
    "ls -la; python3 analysis.py",
    "ls -la\npython3 analysis.py",
    "if true; then python3 analysis.py; fi",
    "exec python3 analysis.py",
])
def test_recognizable_saved_programs_use_the_direct_runner(value):
    assert shell_saved_program(command(value)) == "/home/ubuntu/output/analysis.py"
    assert "program_run" in saved_program_redirect(command(value, name="shell_exec"))


@pytest.mark.parametrize("value", [
    "ls -l analysis.py",
    "cat analysis.py",
    "echo 'python3 analysis.py'",
    "printf '%s' 'python3 analysis.py | tail -80'",
    "python3 -c 'print(\"analysis.py\")'",
    "python3 -m json.tool evidence.json",
    "ai-dataseek-quicklook /data/input.csv --output /home/ubuntu/output/quicklook",
    "python3 - <<'PY'\nprint('analysis.py')\nPY",
    "cat <<'TEXT'\npython3 analysis.py\nTEXT",
    "python3 '$SCRIPT_PATH'",
    "echo arbitrary && cat program.py",
    "'unterminated",
])
def test_inspection_inline_diagnostics_data_strings_and_unknown_syntax_are_preserved(value):
    assert shell_saved_program(command(value)) is None
    assert saved_program_redirect(command(value)) is None


def test_arguments_with_spaces_are_kept_as_one_saved_path():
    assert shell_saved_program(command("python3 'analysis (1).py' 'argument with spaces'")) == (
        "/home/ubuntu/output/analysis (1).py"
    )


def test_file_writes_and_plugins_cannot_be_reinterpreted_as_shell_commands():
    assert shell_saved_program(command("python3 analysis.py", name="file_write")) is None
    assert shell_saved_program(command("python3 analysis.py", name="custom_plugin")) is None
