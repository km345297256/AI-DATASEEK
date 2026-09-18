import asyncio
from collections import OrderedDict
import hashlib
import json
import os
import uuid

import pytest

from app.core.exceptions import AppException, BadRequestException
from app.services.program import (
    MAX_PROGRAM_OUTPUT_CHARS, PROGRAM_OUTPUT_OMISSION, append_program_output,
    prepare_program, program_command,
)
from app.services.shell import ShellService
from conftest import BASE_URL


@pytest.fixture
def service():
    value = ShellService()
    value.active_shells = {}
    value._pending_execs = {}
    value._pre_cancelled_execs = OrderedDict()
    value._execution_operations = {}
    return value


async def run(service, tmp_path, source, args=None):
    path = tmp_path / "analysis script.py"
    path.write_text(source)
    result = await service.exec_program("test", str(tmp_path), str(path), args or [],
                                        operation_id=uuid.uuid4().hex)
    assert result.status == "completed"
    await service.release_shell("test")
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["| tail -80 && true", "; echo success", "$(touch injected)", "two words", "'\"\\"])
async def test_literal_arguments_cannot_mask_failure_or_execute_shell(service, tmp_path, argument):
    source = "import sys\nprint(repr(sys.argv[1:]))\nraise ValueError('bad input')\n"
    result = await run(service, tmp_path, source, [argument])
    assert result.returncode == 1
    assert argument in result.output or repr(argument) in result.output
    assert result.program_execution["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
    assert result.program_execution["diagnostic"]["exception_type"] == "ValueError"
    assert result.program_execution["failure_fingerprint"]
    assert not (tmp_path / "injected").exists()
    assert result.execution_receipt.returncode == 1


@pytest.mark.asyncio
async def test_printed_traceback_is_not_a_failed_process(service, tmp_path):
    result = await run(service, tmp_path, "print('Traceback (most recent call last): ValueError: example')\n")
    assert result.returncode == 0
    assert result.program_execution["failure_fingerprint"] is None
    assert result.program_execution["diagnostic"] is None


@pytest.mark.asyncio
async def test_snapshot_is_exact_even_when_source_changes_during_launch(service, tmp_path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    path = tmp_path / "analysis script.py"
    async def mutate_then_spawn(*args, **kwargs):
        path.write_text("raise RuntimeError('new version must not run')")
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mutate_then_spawn)
    source = "import __main__\nx = 42\nassert __main__.x == 42\nprint('original')\n"
    result = await run(service, tmp_path, source)
    assert result.returncode == 0
    assert result.output == "original\n"
    assert result.program_execution["source_digest"] == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.asyncio
async def test_output_truncation_does_not_terminate_or_hide_exit_status(service, tmp_path):
    result = await run(service, tmp_path, "print('records=1652, missing=0')\nprint('x' * 200000)\nraise KeyError('missing column')\n")
    assert result.returncode == 1
    assert len(result.output) <= MAX_PROGRAM_OUTPUT_CHARS
    assert result.program_execution["output_truncated"] is True
    assert result.program_execution["diagnostic"]["exception_type"] == "KeyError"
    assert result.output.startswith("records=1652, missing=0\n")
    assert result.output.rstrip().endswith("KeyError: 'missing column'")
    assert result.output.count(PROGRAM_OUTPUT_OMISSION) == 1


def test_bounded_output_is_chunk_independent_and_short_output_is_exact():
    short = "统计：数量 = 42\n末尾正常🧬\n"
    output, truncated = "", False
    for character in short:
        output, truncated = append_program_output(output, character, truncated)
    assert (output, truncated) == (short, False)
    long = short + "αβ汉字🧬" * 10000 + "\n最后错误：字段缺失\n"
    expected, _ = append_program_output("", long, False)
    for chunk_size in (1, 127, 20000, 50000):
        output, truncated = "", False
        for offset in range(0, len(long), chunk_size):
            output, truncated = append_program_output(output, long[offset:offset + chunk_size], truncated)
        assert output == expected
        assert truncated is True
        assert len(output) == MAX_PROGRAM_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_program_output_decoder_preserves_unicode_across_byte_chunks(service, tmp_path):
    result = await run(service, tmp_path,
                       "import os\ndata=('统计：记录=42\\n' + 'αβ汉字🧬'*10000 + '\\n结束🧬\\n').encode('utf-8')\n"
                       "for start in range(0, len(data), 127):\n    os.write(1,data[start:start+127])\n")
    assert result.returncode == 0
    assert result.output.startswith("统计：记录=42\n")
    assert result.output.endswith("\n结束🧬\n")
    assert "\ufffd" not in result.output
    assert result.program_execution["output_truncated"] is True


@pytest.mark.asyncio
async def test_same_error_fingerprint_survives_line_movement(service, tmp_path):
    first = await run(service, tmp_path, "raise ValueError('invalid row')\n")
    second = await run(service, tmp_path, "# revised\n\nraise ValueError('invalid row')\n")
    assert first.program_execution["source_digest"] != second.program_execution["source_digest"]
    assert first.program_execution["failure_fingerprint"] == second.program_execution["failure_fingerprint"]
    assert first.program_execution["diagnostic"]["line"] != second.program_execution["diagnostic"]["line"]


@pytest.mark.asyncio
async def test_distinct_missing_columns_have_distinct_error_identity(service, tmp_path):
    first = await run(service, tmp_path, "raise KeyError('field_a')\n")
    second = await run(service, tmp_path, "raise KeyError('field_b')\n")
    assert first.program_execution["failure_fingerprint"] != second.program_execution["failure_fingerprint"]


@pytest.mark.asyncio
async def test_program_wait_and_cancel_use_original_operation(service, tmp_path):
    service.EXEC_COMPLETION_GRACE_SECONDS = 0
    path = tmp_path / "wait.py"
    path.write_text("import time\ntime.sleep(60)\n")
    operation_id = uuid.uuid4().hex
    result = await service.exec_program("wait", str(tmp_path), str(path), [], operation_id=operation_id)
    assert result.status == "running"
    assert (await service.wait_for_process("wait", 0, operation_id=operation_id)).status == "running"
    await service.kill_process("wait", operation_id=operation_id)
    receipt = await service.operation_status("wait", operation_id)
    assert receipt.state == "exited"
    assert receipt.returncode != 0
    await service.release_shell("wait", operation_id=operation_id)


@pytest.mark.asyncio
async def test_program_imports_sibling_and_preserves_filename(service, tmp_path):
    (tmp_path / "sibling.py").write_text("VALUE=42\n")
    result = await run(service, tmp_path,
                       "import sibling,sys\nassert sibling.VALUE == 42\nassert __file__ == sys.argv[0]\n")
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_dataclass_pickling_has_normal_main_module_semantics(service, tmp_path):
    result = await run(service, tmp_path,
                       "from __future__ import annotations\nfrom dataclasses import dataclass\nimport pickle\n"
                       "@dataclass\nclass Record:\n    value: int = 42\n"
                       "assert pickle.loads(pickle.dumps(Record())).value == 42\n")
    assert result.returncode == 0


def test_command_identity_is_json_not_shell():
    value = program_command("/home/ubuntu/output/a b.py", ["$(false)"])
    assert json.loads(value) == {"kind": "python_program", "script_path": "/home/ubuntu/output/a b.py", "args": ["$(false)"]}


def test_named_pipe_script_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "not-a-script.py"
    os.mkfifo(path)
    with pytest.raises(BadRequestException, match="regular file"):
        prepare_program(str(path), [])


@pytest.mark.parametrize("path,args", [("relative.py", []), ("/tmp/a.sh", []), ("/tmp/a.py", ["nul\x00byte"])])
def test_program_paths_and_arguments_are_validated(path, args):
    with pytest.raises(BadRequestException):
        prepare_program(path, args)


@pytest.mark.asyncio
async def test_unreadable_source_is_proven_not_started(service, tmp_path):
    operation_id = uuid.uuid4().hex
    with pytest.raises(AppException):
        await service.exec_program("missing", str(tmp_path), str(tmp_path / "missing.py"), [],
                                   operation_id=operation_id)
    receipt = await service.operation_status("missing", operation_id)
    assert receipt.state == "not_started"
    assert receipt.process_tree_quiescent is True


@pytest.mark.parametrize("exit_code", [0, 1])
def test_program_api_projects_actual_exit_and_original_receipt(client, tmp_path, exit_code):
    path = tmp_path / "api analysis.py"
    source = f"import sys\nprint(repr(sys.argv[1:]))\nsys.exit({exit_code})\n"
    path.write_text(source)
    identity = {"id": uuid.uuid4().hex, "operation_id": uuid.uuid4().hex}
    try:
        response = client.post(f"{BASE_URL}/api/v1/shell/program", json={
            **identity, "exec_dir": str(tmp_path), "script_path": str(path),
            "args": ["| tail -80 && true"],
        }, timeout=15)
        assert response.status_code == 200
        body = response.json()
        if body["data"]["status"] == "running":
            response = client.post(f"{BASE_URL}/api/v1/shell/wait", json={
                **identity, "seconds": 5,
            }, timeout=15)
            assert response.status_code == 200
            body = response.json()
        assert body["data"]["status"] == "completed"
        assert body["success"] is (exit_code == 0)
        assert body["data"]["returncode"] == exit_code
        assert "| tail -80 && true" in body["data"]["output"]
        assert body["data"]["program_execution"]["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
        receipt = body["data"]["execution_receipt"]
        assert receipt["operation_id"] == identity["operation_id"]
        assert receipt["state"] == "exited"
        assert receipt["returncode"] == exit_code
    finally:
        client.post(f"{BASE_URL}/api/v1/shell/release", json=identity, timeout=15)
