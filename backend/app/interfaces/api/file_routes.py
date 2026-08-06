from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
import json
import logging

from app.application.services.file_service import FileService
from app.application.errors.exceptions import NotFoundError
from app.interfaces.dependencies import get_file_service, get_current_user, get_optional_current_user, verify_signature
from app.domain.models.user import User
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.file import (
    FileInfoResponse,
    LargeUploadCompleteRequest,
    LargeUploadInitRequest,
    LargeUploadInitResponse,
    LargeUploadPartUploadResponse,
    LargeUploadStatusResponse,
    public_filename,
)
from app.interfaces.schemas.resource import AccessTokenRequest, SignedUrlResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


def _close_file_stream(stream) -> None:
    try:
        if hasattr(stream, "close"):
            stream.close()
    finally:
        if hasattr(stream, "release_conn"):
            stream.release_conn()

@router.post("", response_model=APIResponse[FileInfoResponse])
async def upload_file(
    file: UploadFile = File(...),
    metadata: str | None = Form(default=None),
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user)
) -> APIResponse[FileInfoResponse]:
    """Upload file"""
    parsed_metadata = {}
    if metadata:
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                parsed_metadata = parsed
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid upload metadata JSON")
    # Upload file
    result = await file_service.upload_file(
        file_data=file.file,
        filename=file.filename,
        user_id=current_user.id,
        content_type=file.content_type,
        metadata=parsed_metadata,
    )
    
    return APIResponse.success(await FileInfoResponse.from_file_info(result))


def _large_upload_status_response(session) -> LargeUploadStatusResponse:
    return LargeUploadStatusResponse(
        upload_id=session.upload_id,
        file_id=session.file_id,
        filename=session.filename,
        size=session.size,
        part_size=session.part_size,
        status=session.status,
        parts=session.parts,
        error=session.error,
        expires_at=session.expires_at,
    )


@router.post("/large-uploads/init", response_model=APIResponse[LargeUploadInitResponse])
async def init_large_upload(
    request: LargeUploadInitRequest,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[LargeUploadInitResponse]:
    session = await file_service.init_large_upload(
        filename=request.filename,
        size=request.size,
        user_id=current_user.id,
        content_type=request.content_type,
        metadata=request.metadata,
    )
    return APIResponse.success(
        LargeUploadInitResponse(
            upload_id=session.upload_id,
            file_id=session.file_id,
            filename=session.filename,
            size=session.size,
            part_size=session.part_size,
            status=session.status,
            expires_at=session.expires_at,
        )
    )


@router.get("/large-uploads/{upload_id}", response_model=APIResponse[LargeUploadStatusResponse])
async def get_large_upload_status(
    upload_id: str,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[LargeUploadStatusResponse]:
    session = await file_service.get_large_upload(upload_id, current_user.id)
    return APIResponse.success(_large_upload_status_response(session))


@router.put("/large-uploads/{upload_id}/parts/{part_number}", response_model=APIResponse[LargeUploadPartUploadResponse])
async def upload_large_upload_part(
    upload_id: str,
    part_number: int,
    request: Request,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[LargeUploadPartUploadResponse]:
    data = await request.body()
    etag = await file_service.upload_large_upload_part(upload_id, part_number, current_user.id, data)
    return APIResponse.success(
        LargeUploadPartUploadResponse(
            upload_id=upload_id,
            part_number=part_number,
            etag=etag,
            size=len(data),
        )
    )


@router.post("/large-uploads/{upload_id}/complete", response_model=APIResponse[FileInfoResponse])
async def complete_large_upload(
    upload_id: str,
    request: LargeUploadCompleteRequest,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[FileInfoResponse]:
    file_info = await file_service.complete_large_upload(
        upload_id,
        [part.model_dump() for part in request.parts],
        current_user.id,
    )
    return APIResponse.success(await FileInfoResponse.from_file_info(file_info))


@router.post("/large-uploads/{upload_id}/abort", response_model=APIResponse[None])
async def abort_large_upload(
    upload_id: str,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[None]:
    await file_service.abort_large_upload(upload_id, current_user.id)
    return APIResponse.success()

@router.get("/{file_id}")
async def download_file_with_signature(
    file_id: str,
    file_service: FileService = Depends(get_file_service),
    signature: str = Depends(verify_signature),
):
    """Download file with optional access token"""
    
    # Download file (authentication is handled by middleware for non-token requests)
    try:
        file_data, file_info = await file_service.download_file(file_id)
    except FileNotFoundError:
        raise NotFoundError("File not found")
    except PermissionError:
        raise NotFoundError("File not found")  # Don't reveal if file exists but user has no access
    
    # Encode filename properly for Content-Disposition header
    # Use URL encoding for non-ASCII characters to ensure latin-1 compatibility
    import urllib.parse
    encoded_filename = urllib.parse.quote(public_filename(file_info.filename), safe='')
    
    headers = {
        'Content-Disposition': f'attachment; filename*=UTF-8\'\'{encoded_filename}'
    }
    
    return StreamingResponse(
        file_data,
        media_type=file_info.content_type or 'application/octet-stream',
        headers=headers,
        background=BackgroundTask(_close_file_stream, file_data),
    )

@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_optional_current_user)
):
    """Download file with optional access token"""
    
    # Download file (authentication is handled by middleware for non-token requests)
    try:
        file_data, file_info = await file_service.download_file(file_id, current_user.id if current_user else None)
    except FileNotFoundError:
        raise NotFoundError("File not found")
    except PermissionError:
        raise NotFoundError("File not found")  # Don't reveal if file exists but user has no access
    
    # Encode filename properly for Content-Disposition header
    # Use URL encoding for non-ASCII characters to ensure latin-1 compatibility
    import urllib.parse
    encoded_filename = urllib.parse.quote(public_filename(file_info.filename), safe='')
    
    headers = {
        'Content-Disposition': f'attachment; filename*=UTF-8\'\'{encoded_filename}'
    }
    
    return StreamingResponse(
        file_data,
        media_type=file_info.content_type or 'application/octet-stream',
        headers=headers,
        background=BackgroundTask(_close_file_stream, file_data),
    )

@router.delete("/{file_id}", response_model=APIResponse[None])
async def delete_file(
    file_id: str,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user)
) -> APIResponse[None]:
    """Delete file"""
    success = await file_service.delete_file(file_id, current_user.id)
    if not success:
        raise NotFoundError("File not found")
    return APIResponse.success()

@router.get("/{file_id}/info", response_model=APIResponse[FileInfoResponse])
async def get_file_info(
    file_id: str,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user)
) -> APIResponse[FileInfoResponse]:
    """Get file information"""
    file_info = await file_service.get_file_info(file_id, current_user.id)
    if not file_info:
        raise NotFoundError("File not found")
    
    return APIResponse.success(await FileInfoResponse.from_file_info(file_info))


@router.post("/{file_id}/signed-url", response_model=APIResponse[SignedUrlResponse])
async def create_file_signed_url(
    file_id: str,
    request_data: AccessTokenRequest,
    current_user: User = Depends(get_current_user),
    file_service: FileService = Depends(get_file_service)
) -> APIResponse[SignedUrlResponse]:
    """Generate signed URL for file download
    
    This endpoint creates a signed URL that allows temporary access to download
    a specific file without requiring authentication headers.
    """
    
    try:
        # Create signed URL using file service
        signed_url = await file_service.create_signed_url(
            file_id=file_id,
            user_id=current_user.id,
            expire_minutes=request_data.expire_minutes
        )
        
        return APIResponse.success(SignedUrlResponse(
            signed_url=signed_url,
            expires_in=request_data.expire_minutes * 60,
        ))
    except FileNotFoundError:
        raise NotFoundError("File not found")
