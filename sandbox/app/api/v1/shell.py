from fastapi import APIRouter
from app.schemas.shell import (
    ShellExecRequest, ShellViewRequest, ShellWaitRequest,
    ShellWriteToProcessRequest, ShellKillProcessRequest,
    ShellOperationStatusRequest,
)
from app.schemas.response import Response
from app.services.shell import shell_service
from app.core.exceptions import BadRequestException

router = APIRouter()

@router.post("/exec", response_model=Response)
async def exec_command(request: ShellExecRequest):
    """
    Execute command in the specified shell session
    """
    # If no session ID is provided, automatically create one
    if not request.id or request.id == "":
        request.id = shell_service.create_session_id()
        
    private = ({"credentials": {slot: value.get_secret_value() for slot, value in request.credentials.items()}}
               if request.credentials else {})
    if request.operation_id is not None:
        private["operation_id"] = request.operation_id
    result = await shell_service.exec_command(
        session_id=request.id,
        exec_dir=request.exec_dir,
        command=request.command,
        **private,
    )

    succeeded = result.status != "completed" or result.returncode == 0
    if result.status == "running":
        message = "Command started and is still running"
    elif succeeded:
        message = "Command completed successfully"
    else:
        message = f"Command failed with return code: {result.returncode}"

    # Construct response
    return Response(
        success=succeeded,
        message=message,
        data=result.model_dump()
    )


@router.post("/operation-status", response_model=Response)
async def operation_status(request: ShellOperationStatusRequest):
    """Observe a launch-bound operation without executing or cancelling it."""
    receipt = await shell_service.operation_status(request.id, request.operation_id)
    return Response(
        success=receipt is not None,
        message="Execution state retrieved" if receipt is not None else "Execution state unavailable",
        data=receipt.model_dump() if receipt is not None else None,
    )

@router.post("/view", response_model=Response)
async def view_shell(request: ShellViewRequest):
    """
    View output of the specified shell session
    """
    if not request.id or request.id == "":
        raise BadRequestException("Session ID not provided")
        
    tracked = {"operation_id": request.operation_id} if request.operation_id is not None else {}
    result = await shell_service.view_shell(session_id=request.id, console=request.console, **tracked)
    
    # Construct response
    return Response(
        success=True,
        message="Session content retrieved successfully",
        data=result.model_dump()
    )

@router.post("/wait", response_model=Response)
async def wait_for_process(request: ShellWaitRequest):
    """
    Wait for the process in the specified shell session to return
    """
    tracked = {"operation_id": request.operation_id} if request.operation_id is not None else {}
    result = await shell_service.wait_for_process(
        session_id=request.id,
        seconds=request.seconds,
        **tracked,
    )
    
    succeeded = result.status != "completed" or result.returncode == 0
    if result.status == "running":
        message = "Process is still running"
    elif succeeded:
        message = "Process completed successfully"
    else:
        message = f"Process failed with return code: {result.returncode}"

    return Response(
        success=succeeded,
        message=message,
        data=result.model_dump()
    )

@router.post("/write", response_model=Response)
async def write_to_process(request: ShellWriteToProcessRequest):
    """
    Write input to the process in the specified shell session
    """
    if not request.id or request.id == "":
        raise BadRequestException("Session ID not provided")
        
    result = await shell_service.write_to_process(
        session_id=request.id,
        input_text=request.input,
        press_enter=request.press_enter
    )
    
    # Construct response
    return Response(
        success=True,
        message="Input written",
        data=result.model_dump()
    )

@router.post("/kill", response_model=Response)
async def kill_process(request: ShellKillProcessRequest):
    """
    Terminate the process in the specified shell session
    """
    tracked = {"operation_id": request.operation_id} if request.operation_id is not None else {}
    result = await shell_service.kill_process(session_id=request.id, **tracked)
    
    # Construct response
    message = "Process terminated" if result.status == "terminated" else "Process ended"
    return Response(
        success=True,
        message=message,
        data=result.model_dump()
    )


@router.post("/release", response_model=Response)
async def release_shell(request: ShellKillProcessRequest):
    """Terminate if necessary and forget one internal shell session."""
    tracked = {"operation_id": request.operation_id} if request.operation_id is not None else {}
    result = await shell_service.release_shell(session_id=request.id, **tracked)
    return Response(
        success=True,
        message="Shell session released",
        data=result.model_dump(),
    )
