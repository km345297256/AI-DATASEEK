"""
Shell Service Implementation - Async Version
"""
import os
import uuid
import getpass
import socket
import logging
import asyncio
import codecs
import re
from typing import Dict, Any, Optional, List
from app.models.shell import (
    ShellExecResult, ShellViewResult, ShellWaitResult,
    ShellWriteResult, ShellKillResult, ShellTask, ConsoleRecord
)
from app.core.exceptions import AppException, ResourceNotFoundException, BadRequestException

# Set up logger
logger = logging.getLogger(__name__)

class ShellService:
    EXEC_COMPLETION_GRACE_SECONDS = 5
    OUTPUT_READER_DRAIN_GRACE_SECONDS = 1

    # Store active shell sessions
    active_shells: Dict[str, Dict[str, Any]] = {}
    
    # Store shell tasks
    shell_tasks: Dict[str, ShellTask] = {}

    def _remove_ansi_escape_codes(self, text: str) -> str:
        """Remove ANSI escape codes from text"""
        # Pattern to match ANSI escape sequences
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def _get_display_path(self, path: str) -> str:
        """Get the path for display, replacing user home directory with ~"""
        home_dir = os.path.expanduser("~")
        logger.debug(f"Home directory: {home_dir} , path: {path}")
        if path.startswith(home_dir):
            return path.replace(home_dir, "~", 1)
        return path

    def _format_ps1(self, exec_dir: str) -> str:
        """Format the command prompt"""
        username = getpass.getuser()
        hostname = socket.gethostname()
        display_dir = self._get_display_path(exec_dir)
        return f"{username}@{hostname}:{display_dir} $"

    async def _create_process(self, command: str, exec_dir: str) -> asyncio.subprocess.Process:
        """Create a new async subprocess"""
        logger.debug(f"Creating process for command: {command} in directory: {exec_dir}")
        return await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            cwd=exec_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Redirect stderr to stdout
            stdin=asyncio.subprocess.PIPE,
            limit=1024*1024  # Set buffer size to 1MB
        )

    def _append_process_output(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        console_record: Optional[ConsoleRecord],
        output: str,
    ) -> None:
        """Append output only to the process and record that produced it."""
        if not output:
            return

        shell = self.active_shells.get(session_id)
        if not shell:
            return

        if shell.get("process") is process:
            shell["output"] += output

        if console_record is not None and any(
            record is console_record for record in shell.get("console", [])
        ):
            console_record.output += output

    async def _start_output_reader(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        console_record: Optional[ConsoleRecord] = None,
    ):
        """Start a coroutine to continuously read process output and store it"""
        logger.debug(f"Starting output reader for session: {session_id}")
        shell = self.active_shells.get(session_id)
        if console_record is None and shell and shell.get("process") is process:
            console = shell.get("console") or []
            console_record = console[-1] if console else None

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while process.stdout:
                try:
                    buffer = await process.stdout.read(128)
                    if not buffer:
                        # Process output ended
                        break

                    output = decoder.decode(buffer, final=False)
                    self._append_process_output(
                        session_id,
                        process,
                        console_record,
                        output,
                    )
                except Exception as e:
                    logger.error(f"Error reading process output: {str(e)}", exc_info=True)
                    break
        finally:
            remaining_output = decoder.decode(b"", final=True)
            self._append_process_output(
                session_id,
                process,
                console_record,
                remaining_output,
            )

        logger.debug(f"Output reader for session {session_id} has finished")

    async def _wait_for_output_reader(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Give the matching reader time to drain output after process exit.

        A descendant can keep the inherited pipe open after the shell itself
        exits, so the wait is bounded and shielded rather than cancelling the
        reader and permanently discarding later output.
        """
        shell = self.active_shells.get(session_id)
        if not shell or shell.get("process") is not process:
            return

        reader_task = shell.get("reader_task")
        if not reader_task or reader_task is asyncio.current_task():
            return

        try:
            await asyncio.wait_for(
                asyncio.shield(reader_task),
                timeout=self.OUTPUT_READER_DRAIN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Output reader for session %s did not reach EOF within %ss",
                session_id,
                self.OUTPUT_READER_DRAIN_GRACE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Output reader for session %s failed while draining: %s",
                session_id,
                e,
                exc_info=True,
            )

    async def exec_command(self, session_id: str, exec_dir: Optional[str], command: str) -> ShellExecResult:
        """
        Asynchronously execute a command in the specified shell session
        """
        logger.info(f"Executing command in session {session_id}: {command}")
        if not exec_dir:
            exec_dir = os.path.expanduser("~")
        # Ensure directory exists
        if not os.path.exists(exec_dir):
            logger.error(f"Directory does not exist: {exec_dir}")
            raise BadRequestException(f"Directory does not exist: {exec_dir}")
        
        try:
            # Create PS1 format
            ps1 = self._format_ps1(exec_dir)
            
            # If it's a new session, create a new process
            if session_id not in self.active_shells:
                logger.debug(f"Creating new shell session: {session_id}")
                process = await self._create_process(command, exec_dir)
                console_record = ConsoleRecord(ps1=ps1, command=command, output="")
                shell = self.active_shells[session_id] = {
                    "process": process,
                    "exec_dir": exec_dir,
                    "output": "",
                    "console": [console_record],
                }
                # Start the output reader coroutine
                shell["reader_task"] = asyncio.create_task(
                    self._start_output_reader(session_id, process, console_record)
                )
            else:
                # Execute command in an existing session
                logger.debug(f"Using existing shell session: {session_id}")
                shell = self.active_shells[session_id]
                old_process = shell["process"]
                
                # If the old process is still running, terminate it first
                if old_process.returncode is None:
                    logger.debug(f"Terminating previous process in session: {session_id}")
                    try:
                        old_process.terminate()
                        await asyncio.wait_for(old_process.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        # If graceful termination fails, force kill
                        logger.warning(f"Forcefully killing process in session: {session_id}")
                        if old_process.returncode is None:
                            old_process.kill()
                            await old_process.wait()
                    except ProcessLookupError:
                        # The process exited between the returncode check and terminate.
                        pass

                await self._wait_for_output_reader(session_id, old_process)
                
                # Create a new process
                process = await self._create_process(command, exec_dir)
                
                # Update session information
                self.active_shells[session_id]["process"] = process
                self.active_shells[session_id]["exec_dir"] = exec_dir
                self.active_shells[session_id]["output"] = ""  # Clear previous output
                
                # Record command console record, but output is initially empty, will be updated later
                console_record = ConsoleRecord(ps1=ps1, command=command, output="")
                shell["console"].append(console_record)
                
                # Start the output reader coroutine
                shell["reader_task"] = asyncio.create_task(
                    self._start_output_reader(session_id, process, console_record)
                )
            
            # Try to wait for the process to complete (max 5 seconds)
            try:
                logger.debug(f"Waiting for process completion in session: {session_id}")
                wait_result = await self.wait_for_process(
                    session_id,
                    seconds=self.EXEC_COMPLETION_GRACE_SECONDS,
                )
                if wait_result.status == "completed":
                    # Process has completed, get the output
                    logger.debug(f"Process completed with code: {wait_result.returncode}")
                    view_result = await self.view_shell(session_id)
                    
                    return ShellExecResult(
                        session_id=session_id,
                        command=command,
                        status="completed",
                        returncode=wait_result.returncode,
                        output=view_result.output,
                    )
            except Exception as e:
                # Other exceptions, ignore and continue
                logger.warning(f"Exception while waiting for process: {str(e)}")
                pass
            
            # Get current console records
            console = self.get_console_records(session_id)
            
            return ShellExecResult(
                session_id=session_id,
                command=command,
                status="running",
            )
        except Exception as e:
            logger.error(f"Command execution failed: {str(e)}", exc_info=True)
            raise AppException(
                message=f"Command execution failed: {str(e)}",
                data={"session_id": session_id, "command": command}
            )

    async def view_shell(self, session_id: str, console: bool = False) -> ShellViewResult:
        """
        Asynchronously view the content of the specified shell session
        """
        logger.debug(f"Viewing shell content for session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        
        shell = self.active_shells[session_id]
        
        # Get raw output and filter ANSI escape codes
        raw_output = shell["output"]
        clean_output = self._remove_ansi_escape_codes(raw_output)
        
        # Get command console records with filtered output
        if console:
            console = self.get_console_records(session_id)
        else:
            console = None
        
        return ShellViewResult(
            output=clean_output,
            session_id=session_id,
            console=console
        )

    def get_console_records(self, session_id: str) -> List[ConsoleRecord]:
        """
        Get command console records for the specified session (this method doesn't need to be async)
        """
        logger.debug(f"Getting console records for session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        
        # Get raw console records and filter ANSI escape codes
        raw_console = self.active_shells[session_id]["console"]
        clean_console = []
        for record in raw_console:
            clean_record = ConsoleRecord(
                ps1=record.ps1,
                command=record.command,
                output=self._remove_ansi_escape_codes(record.output)
            )
            clean_console.append(clean_record)
        
        return clean_console

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ShellWaitResult:
        """
        Asynchronously wait for the process in the specified shell session to return
        """
        logger.debug(f"Waiting for process in session: {session_id}, timeout: {seconds}s")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            if seconds is None:
                seconds = 60

            # `asyncio.wait_for(coroutine, timeout=0)` cancels a newly-created
            # coroutine before it can observe an already-finished process. Check
            # returncode first so zero-second polling remains accurate.
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=seconds)

            await self._wait_for_output_reader(session_id, process)
            
            logger.info(f"Process completed with return code: {process.returncode}")
            return ShellWaitResult(
                status="completed",
                returncode=process.returncode
            )
        except asyncio.TimeoutError:
            if process.returncode is not None:
                await self._wait_for_output_reader(session_id, process)
                return ShellWaitResult(
                    status="completed",
                    returncode=process.returncode,
                )
            logger.info(
                "Process in session %s is still running after waiting %ss",
                session_id,
                seconds,
            )
            return ShellWaitResult(status="running", returncode=None)
        except Exception as e:
            logger.error(f"Failed to wait for process: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to wait for process: {str(e)}")

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool) -> ShellWriteResult:
        """
        Asynchronously write input to the process in the specified shell session
        """
        logger.debug(f"Writing to process in session: {session_id}, press_enter: {press_enter}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            # Check if the process is still running
            if process.returncode is not None:
                logger.error(f"Process has already terminated, cannot write input")
                raise BadRequestException("Process has ended, cannot write input")
            
            # Prepare input data
            if press_enter:
                input_data = f"{input_text}\n".encode()
            else:
                input_data = input_text.encode()
            
            # Add input to output and console records
            input_str = input_data.decode('utf-8')
            shell["output"] += input_str
            if shell["console"]:
                shell["console"][-1].output += input_str
            
            # Asynchronously write input
            process.stdin.write(input_data)
            await process.stdin.drain()
            
            logger.info(f"Successfully wrote input to process")
            
            return ShellWriteResult(
                status="success"
            )
        except Exception as e:
            logger.error(f"Failed to write input: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to write input: {str(e)}")

    async def kill_process(self, session_id: str) -> ShellKillResult:
        """
        Asynchronously terminate the process in the specified shell session
        """
        logger.info(f"Killing process in session: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Session ID not found: {session_id}")
            raise ResourceNotFoundException(f"Session ID does not exist: {session_id}")
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            # Check if the process is still running
            if process.returncode is None:
                # Try to terminate gracefully
                logger.debug(f"Attempting to terminate process gracefully")
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    # If graceful termination fails, force kill
                    logger.warning(f"Forcefully killing the process")
                    process.kill()
                    await process.wait()

                await self._wait_for_output_reader(session_id, process)
                
                logger.info(f"Process terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="terminated",
                    returncode=process.returncode
                )
            else:
                await self._wait_for_output_reader(session_id, process)
                logger.info(f"Process was already terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="already_terminated",
                    returncode=process.returncode
                )
        except Exception as e:
            logger.error(f"Failed to kill process: {str(e)}", exc_info=True)
            raise AppException(message=f"Failed to terminate process: {str(e)}")

    def create_session_id(self) -> str:
        """
        Create a new session ID (this method doesn't need to be async)
        """
        session_id = str(uuid.uuid4())
        logger.debug(f"Created new session ID: {session_id}")
        return session_id

shell_service = ShellService()
