from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.domain.models.file import FileInfo


class FileViewRequest(BaseModel):
    """File view request schema"""
    file: str


class FileViewResponse(BaseModel):
    """File view response schema"""
    content: str
    file: str


class FileInfoResponse(BaseModel):
    """File info response schema"""
    file_id: str
    filename: str
    content_type: Optional[str]
    size: Optional[int] = None
    upload_date: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]]
    file_url: Optional[str]

    @staticmethod
    async def from_file_info(file_info: FileInfo) -> "FileInfoResponse":
        from app.interfaces.dependencies import get_file_service
        file_service = get_file_service()
        return FileInfoResponse(
            file_id=file_info.file_id,
            filename=file_info.filename,
            content_type=file_info.content_type,
            size=file_info.size,
            upload_date=file_info.upload_date,
            metadata=file_info.metadata,
            file_url=await file_service.create_signed_url(file_info.file_id)
        )


class LargeUploadInitRequest(BaseModel):
    filename: str
    size: int = Field(gt=0)
    content_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class LargeUploadInitResponse(BaseModel):
    upload_id: str
    file_id: str
    filename: str
    size: int
    part_size: int
    status: str
    expires_at: datetime


class LargeUploadPartUploadResponse(BaseModel):
    upload_id: str
    part_number: int
    etag: str
    size: int


class LargeUploadPart(BaseModel):
    part_number: int = Field(gt=0)
    etag: str
    size: Optional[int] = None


class LargeUploadCompleteRequest(BaseModel):
    parts: List[LargeUploadPart]


class LargeUploadStatusResponse(BaseModel):
    upload_id: str
    file_id: str
    filename: str
    size: int
    part_size: int
    status: str
    parts: List[Dict[str, Any]]
    error: Optional[str] = None
    expires_at: datetime
