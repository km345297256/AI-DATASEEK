from types import SimpleNamespace

import pytest
from langchain.messages import ToolMessage

from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_program_diagnostics import program_diagnostic_read_digest
from app.domain.services.tools.file import FileToolkit


PATH = "/home/ubuntu/output/program.py"


def fixture():
    toolkit = FileToolkit(SimpleNamespace())
    tool = toolkit.get_tool("file_read")
    call = {"name": "file_read", "id": "read", "args": {"file": PATH}}
    result = ToolMessage(tool_call_id="read", name="file_read", content="ignored model-visible text",
                         artifact=ToolResult(success=True, data={"file": PATH, "content": "missing_name()"}))
    return tool, call, result


def test_only_actual_core_read_contents_determine_diagnostic_identity():
    tool, call, result = fixture()
    first = program_diagnostic_read_digest(tool, call, result)
    assert first and len(first) == 64 and "missing_name" not in first
    call["id"] = "new-call"
    call["args"].update(start_line=10, end_line=20)
    result.tool_call_id = "new-call"
    result.content = "different explanatory text"
    assert program_diagnostic_read_digest(tool, call, result) == first
    result.artifact.data["content"] = "different_code()"
    assert program_diagnostic_read_digest(tool, call, result) != first


@pytest.mark.parametrize("fault", ["impostor", "unregistered", "failed", "error-status", "wrong-id", "wrong-path", "empty", "untrusted-content"])
def test_code_diagnostic_requires_original_successful_core_read(fault):
    tool, call, result = fixture()
    if fault == "impostor":
        tool = SimpleNamespace(name="file_read", toolkit=tool.toolkit, _tool=tool._tool)
    elif fault == "unregistered":
        tool.toolkit.tools.clear()
    elif fault == "failed":
        result.artifact.success = False
    elif fault == "error-status":
        result.status = "error"
    elif fault == "wrong-id":
        result.tool_call_id = "other"
    elif fault == "wrong-path":
        result.artifact.data["file"] = "/home/ubuntu/output/other.py"
    elif fault == "empty":
        result.artifact.data["content"] = ""
    else:
        result.artifact = None
        result.content = '{"file":"/home/ubuntu/output/program.py","content":"code"}'
    assert program_diagnostic_read_digest(tool, call, result) is None
