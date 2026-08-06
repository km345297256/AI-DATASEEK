import shlex
from typing import Any, Optional
from app.domain.external.sandbox import Sandbox
from app.domain.services.tools.base import BaseToolkit
from langchain.tools import tool
from app.domain.models.tool_result import ToolResult

class ShellToolkit(BaseToolkit):
    """Shell tool class, providing Shell interaction related functions"""

    name: str = "shell"
    
    def __init__(self, sandbox: Sandbox):
        """Initialize Shell tool class
        
        Args:
            sandbox: Sandbox service
        """
        super().__init__()
        self.sandbox = sandbox
        
    @tool(parse_docstring=True)
    async def shell_exec(
        self,
        id: str,
        exec_dir: str,
        command: str
    ) -> ToolResult:
        """Start a command in a specified shell session. Use for interactive or long-running processes; prefer shell_run for bounded non-interactive commands.
        
        Args:
            id: Unique identifier of the target shell session
            exec_dir: Working directory for command execution (must use absolute path)
            command: Shell command to execute
        """
        return await self.sandbox.exec_command(id, exec_dir, command)

    @tool(parse_docstring=True)
    async def shell_run(
        self,
        id: str,
        exec_dir: str,
        command: str,
        timeout_seconds: int = 30,
    ) -> ToolResult:
        """Run a bounded non-interactive command and wait once for its result. Prefer this for inspection, extraction, scripts, and data processing that should finish promptly.

        Args:
            id: Unique identifier of the target shell session
            exec_dir: Working directory for command execution (must use absolute path)
            command: Shell command to execute
            timeout_seconds: Maximum bounded wait in seconds, clamped to 1-120
        """
        return await self._run_bounded_command(
            id=id,
            exec_dir=exec_dir,
            command=command,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def dataset_unpack(
        self,
        id: str,
        archive_path: str,
        output_dir: str,
        timeout_seconds: int = 120,
        source_root: Optional[str] = None,
    ) -> ToolResult:
        """Safely extract a ZIP, RAR, or 7z dataset, including nested archives, in one bounded call. Prefer this over manually chaining archive commands. It rejects traversal, links, encrypted members, and excessive expansion, and returns a final-file manifest.

        Args:
            id: Unique identifier of the target shell session
            archive_path: Absolute path to the source archive inside the sandbox
            output_dir: Absolute path to a new output directory under /home/ubuntu/output
            timeout_seconds: Maximum bounded wait in seconds, clamped to 1-120
            source_root: Optional trusted dataset root; the resolved archive must remain below it
        """
        source_root_option = (
            f"--source-root {shlex.quote(source_root)} " if source_root else ""
        )
        command = (
            f"ai-dataseek-unpack {shlex.quote(archive_path)} "
            f"--output {shlex.quote(output_dir)} "
            f"{source_root_option}"
            f"--timeout-seconds {max(1, min(timeout_seconds, 120))}"
        )
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=command,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def dataset_quicklook(
        self,
        id: str,
        input_path: str,
        output_dir: str,
        max_plots: int = 4,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create a bounded model-free profile, compact evidence, and 1-4 useful PNG charts for a CSV/TSV, Excel, GeoTIFF, directory, or ZIP/RAR/7z dataset in one call. Archives, including archives below a mounted directory and nested archives, are extracted safely. A successful result completes a broad request without another model turn only when the dataset contract allows terminal quicklook; specific multi-part questions must still be answered from the returned evidence. Use a new output directory under /home/ubuntu/output.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute dataset file, archive, or directory path inside the sandbox
            output_dir: Absolute new output directory under /home/ubuntu/output
            max_plots: Maximum number of PNG charts, clamped to 1-4
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        bounded_plots = max(1, min(max_plots, 4))
        bounded_timeout = max(5, min(timeout_seconds, 120))
        command = (
            f"ai-dataseek-quicklook {shlex.quote(input_path)} "
            f"--output {shlex.quote(output_dir)} "
            f"--max-plots {bounded_plots} "
            f"--timeout-seconds {bounded_timeout}"
        )
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=command,
            timeout_seconds=bounded_timeout,
        )

    async def _run_bounded_command(
        self,
        *,
        id: str,
        exec_dir: str,
        command: str,
        timeout_seconds: int,
    ) -> ToolResult:
        timeout_seconds = max(1, min(timeout_seconds, 120))
        exec_result = await self.sandbox.exec_command(id, exec_dir, command)
        exec_data = self._result_data(exec_result)
        if exec_data.get("status") != "running":
            return exec_result

        wait_result = await self.sandbox.wait_for_process(id, timeout_seconds)
        wait_data = self._result_data(wait_result)
        if wait_data.get("status") != "completed":
            return ToolResult(
                success=wait_result.success,
                message=f"Command is still running after {timeout_seconds} seconds",
                data={
                    "session_id": id,
                    "command": command,
                    "status": "running",
                    "returncode": None,
                },
            )

        view_result = await self.sandbox.view_shell(id)
        view_data = self._result_data(view_result)
        returncode = wait_data.get("returncode")
        succeeded = returncode == 0 and view_result.success
        return ToolResult(
            success=succeeded,
            message=(
                "Command completed successfully"
                if succeeded
                else f"Command failed with return code: {returncode}"
            ),
            data={
                "session_id": id,
                "command": command,
                "status": "completed",
                "returncode": returncode,
                "output": view_data.get("output", ""),
            },
        )

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return result.data if isinstance(result.data, dict) else {}
    
    @tool(parse_docstring=True)
    async def shell_view(self, id: str) -> ToolResult:
        """View the content of a specified shell session. Use for checking command execution results or monitoring output.
        
        Args:
            id: Unique identifier of the target shell session
        """
        return await self.sandbox.view_shell(id)
    
    @tool(parse_docstring=True)
    async def shell_wait(
        self,
        id: str,
        seconds: Optional[int] = None
    ) -> ToolResult:
        """Wait for the running process in a specified shell session to return. Use after running commands that require longer runtime.
        
        Args:
            id: Unique identifier of the target shell session
            seconds: Wait duration in seconds
        """
        return await self.sandbox.wait_for_process(id, seconds)
    
    @tool(parse_docstring=True)
    async def shell_write_to_process(
        self,
        id: str,
        input: str,
        press_enter: bool
    ) -> ToolResult:
        """Write input to a running process in a specified shell session. Use for responding to interactive command prompts.
        
        Args:
            id: Unique identifier of the target shell session
            input: Input content to write to the process
            press_enter: Whether to press Enter key after input
        """
        return await self.sandbox.write_to_process(id, input, press_enter)
    
    @tool(parse_docstring=True)
    async def shell_kill_process(self, id: str) -> ToolResult:
        """Terminate a running process in a specified shell session. Use for stopping long-running processes or handling frozen commands.
        
        Args:
            id: Unique identifier of the target shell session
        """
        return await self.sandbox.kill_process(id)
