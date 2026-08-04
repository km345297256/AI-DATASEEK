from typing import Dict, Any, Optional, BinaryIO, Tuple, List
import logging
from datetime import datetime, UTC
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.application.services.token_service import TokenService
from app.infrastructure.models.documents import FileUploadSessionDocument

# Set up logger
logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FileService:
    def __init__(self, file_storage: Optional[FileStorage] = None, token_service: Optional[TokenService] = None):
        self._file_storage = file_storage
        self._token_service = token_service

    async def upload_file(self, file_data: BinaryIO, filename: str, user_id: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> FileInfo:
        """Upload file"""
        logger.info(f"Upload file request: filename={filename}, user_id={user_id}, content_type={content_type}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.upload_file(file_data, filename, user_id, content_type, metadata)
            logger.info(f"File uploaded successfully: file_id={result.file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to upload file for user {user_id}: {str(e)}")
            raise

    def _require_large_upload_storage(self):
        required = [
            "init_large_upload",
            "upload_large_upload_part",
            "complete_large_upload",
            "abort_large_upload",
        ]
        if not self._file_storage or not all(hasattr(self._file_storage, name) for name in required):
            raise RuntimeError("Large file upload requires MinIO or hybrid file storage")
        return self._file_storage

    async def init_large_upload(
        self,
        filename: str,
        size: int,
        user_id: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileUploadSessionDocument:
        storage = self._require_large_upload_storage()
        if not filename:
            raise ValueError("filename is required")
        if size <= 0:
            raise ValueError("size must be greater than 0")
        return await storage.init_large_upload(filename, user_id, size, content_type, metadata)

    async def get_large_upload(self, upload_id: str, user_id: str) -> FileUploadSessionDocument:
        session = await FileUploadSessionDocument.find_one(FileUploadSessionDocument.upload_id == upload_id)
        if not session or session.user_id != user_id:
            raise FileNotFoundError("Upload session not found")
        if _as_utc(session.expires_at) < datetime.now(UTC) and session.status not in {"completed", "aborted"}:
            session.status = "expired"
            session.updated_at = datetime.now(UTC)
            await session.save()
        return session

    async def upload_large_upload_part(self, upload_id: str, part_number: int, user_id: str, data: bytes) -> str:
        storage = self._require_large_upload_storage()
        session = await self.get_large_upload(upload_id, user_id)
        if session.status not in {"initiated", "uploading"}:
            raise ValueError(f"Upload session is {session.status}")
        if _as_utc(session.expires_at) < datetime.now(UTC):
            raise ValueError("Upload session expired")
        if not data:
            raise ValueError("part data is required")
        if len(data) > session.part_size:
            raise ValueError(f"part data exceeds configured part size {session.part_size}")
        if session.status == "initiated":
            session.status = "uploading"
            session.updated_at = datetime.now(UTC)
            await session.save()
        return await storage.upload_large_upload_part(session, part_number, data)

    async def complete_large_upload(self, upload_id: str, parts: List[Dict[str, Any]], user_id: str) -> FileInfo:
        storage = self._require_large_upload_storage()
        session = await self.get_large_upload(upload_id, user_id)
        if session.status == "completed":
            existing = await self.get_file_info(session.file_id, user_id)
            if existing:
                return existing
            raise FileNotFoundError("Completed file metadata not found")
        if session.status not in {"initiated", "uploading"}:
            raise ValueError(f"Upload session is {session.status}")
        if not parts:
            raise ValueError("parts are required")
        return await storage.complete_large_upload(session, parts)

    async def abort_large_upload(self, upload_id: str, user_id: str) -> None:
        storage = self._require_large_upload_storage()
        session = await self.get_large_upload(upload_id, user_id)
        if session.status in {"completed", "aborted"}:
            return
        await storage.abort_large_upload(session)
    
    async def download_file(self, file_id: str, user_id: Optional[str] = None) -> Tuple[BinaryIO, FileInfo]:
        """Download file"""
        logger.info(f"Download file request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.download_file(file_id, user_id)
            logger.info(f"File downloaded successfully: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to download file {file_id} for user {user_id}: {str(e)}")
            raise

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        """Delete file"""
        logger.info(f"Delete file request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.delete_file(file_id, user_id)
            if result:
                logger.info(f"File deleted successfully: file_id={file_id}, user_id={user_id}")
            else:
                logger.warning(f"File deletion failed or file not found: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to delete file {file_id} for user {user_id}: {str(e)}")
            raise

    async def get_file_info(self, file_id: str, user_id: Optional[str] = None) -> Optional[FileInfo]:
        """Get file information"""
        logger.info(f"Get file info request: file_id={file_id}, user_id={user_id}")
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.get_file_info(file_id, user_id)
            if result:
                logger.info(f"File info retrieved successfully: file_id={file_id}, user_id={user_id}")
            else:
                logger.warning(f"File not found or access denied: file_id={file_id}, user_id={user_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to get file info {file_id} for user {user_id}: {str(e)}")
            raise
    
    async def enrich_with_file_url(self, file_info: FileInfo) -> FileInfo:
        """Enrich file information with file URL"""
        logger.info(f"Enrich file info request: file_info={file_info}")
        
        try:
            signed_url = await self.create_signed_url(file_info.file_id, file_info.user_id)
            file_info.file_url = signed_url
            return file_info
        except Exception as e:
            logger.error(f"Failed to enrich file info {file_info.file_id} with file URL: {str(e)}")
            raise

    async def create_signed_url(self, file_id: str, user_id: Optional[str] = None, expire_minutes: int = 30) -> str:
        """Create signed URL for file download"""
        logger.info(f"Create signed URL request: file_id={file_id}, user_id={user_id}, expire_minutes={expire_minutes}")
        
        if not self._token_service:
            logger.error("Token service not available")
            raise RuntimeError("Token service not available")
        
        # Validate expiration time (max 15 minutes)
        if expire_minutes > 30:
            expire_minutes = 30
        
        # Check if file exists and user has access
        file_info = await self.get_file_info(file_id, user_id)
        if not file_info:
            logger.warning(f"File not found or access denied for signed URL: file_id={file_id}, user_id={user_id}")
            raise FileNotFoundError("File not found")
        
        # Create signed URL for file download
        base_url = f"/api/v1/files/{file_id}"
        signed_url = self._token_service.create_signed_url(
            base_url=base_url,
            expire_minutes=expire_minutes
        )
        
        logger.info(f"Created signed URL for file download for user {user_id}, file {file_id}")
        
        return signed_url
