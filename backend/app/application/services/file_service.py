from typing import Dict, Any, Optional, BinaryIO, Tuple, List
import hashlib
import logging
from datetime import datetime, UTC
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.application.services.token_service import TokenService
from app.infrastructure.models.documents import FileUploadSessionDocument
from app.application.services.file_preview import (
    CSV_PAGE_BYTES, TEXT_PAGE_BYTES, FilePreviewPage, PreviewVersionChanged,
    parse_preview_page, preview_version,
)

# Set up logger
logger = logging.getLogger(__name__)


def _opaque_reference(value: Any, namespace: str) -> str:
    digest = hashlib.sha256(
        str(value or "missing").encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{namespace}:sha256:{digest}"


def _is_private_spill(file_info: FileInfo | None) -> bool:
    metadata = file_info.metadata if file_info and isinstance(file_info.metadata, dict) else {}
    # The source marker alone is reserved. Requiring every companion field to
    # validate would turn malformed metadata into a fail-open public file.
    return metadata.get("source") == "tool_output_spill"


def _contains_reserved_spill_metadata(metadata: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get("source") == "tool_output_spill":
        return True
    return any(str(key).lower().startswith("spill_") for key in metadata)


def _close_download_stream(stream: Any) -> None:
    try:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    finally:
        release_conn = getattr(stream, "release_conn", None)
        if callable(release_conn):
            release_conn()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FileService:
    def __init__(self, file_storage: Optional[FileStorage] = None, token_service: Optional[TokenService] = None):
        self._file_storage = file_storage
        self._token_service = token_service

    async def preview_file(self, file_id: str, user_id: str, *, offset: int = 0, mode: str = "text", version: str | None = None, delimiter: str | None = None, header_pending: bool = False) -> FilePreviewPage:
        if mode not in {"text", "csv"} or delimiter not in {None, ",", "\t"} or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid preview request")
        info = await self.get_file_info(file_id, user_id)
        if info is None:
            raise FileNotFoundError("File not found")
        if info.size is None or offset > info.size:
            raise ValueError("Preview offset exceeds file size")
        expected = preview_version(info)
        if version is not None and version != expected:
            raise PreviewVersionChanged("File changed; restart preview from the first page")
        length = min(CSV_PAGE_BYTES if mode == "csv" else TEXT_PAGE_BYTES, info.size - offset)
        # Never fall back to download_file: some providers materialize the full
        # object before returning a stream. All supported stores have ranges.
        read_range = getattr(self._file_storage, "download_file_range", None)
        if not callable(read_range):
            raise NotImplementedError("Storage does not support bounded previews")
        data, ranged_info = await read_range(file_id, user_id, offset=offset, length=length)
        if _is_private_spill(ranged_info):
            raise FileNotFoundError("File not found")
        if preview_version(ranged_info) != expected:
            raise PreviewVersionChanged("File changed; restart preview from the first page")
        if len(data) != length:
            raise ValueError("Incomplete preview range")
        return parse_preview_page(data, ranged_info, offset=offset, mode=mode, delimiter=delimiter, header_pending=header_pending)

    async def upload_file(self, file_data: BinaryIO, filename: str, user_id: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> FileInfo:
        """Upload file"""
        logger.info(
            "Upload file request file=%s user=%s content_type=%s",
            _opaque_reference(filename, "file"),
            _opaque_reference(user_id, "user"),
            content_type,
        )
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        if _contains_reserved_spill_metadata(metadata):
            raise ValueError("Reserved file metadata is not accepted")
        
        try:
            result = await self._file_storage.upload_file(file_data, filename, user_id, content_type, metadata)
            logger.info(
                "File uploaded successfully file=%s user=%s",
                _opaque_reference(result.file_id, "file"),
                _opaque_reference(user_id, "user"),
            )
            return result
        except Exception as e:
            logger.error(
                "Failed to upload file user=%s error_type=%s",
                _opaque_reference(user_id, "user"),
                type(e).__name__,
            )
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
        if _contains_reserved_spill_metadata(metadata):
            raise ValueError("Reserved file metadata is not accepted")
        return await storage.init_large_upload(filename, user_id, size, content_type, metadata)

    async def _reject_reserved_large_upload_session(
        self,
        storage: Any,
        session: FileUploadSessionDocument,
    ) -> None:
        """Reject legacy multipart sessions that claimed the private spill namespace.

        Older sessions may predate the init-time guard.  Abort an unfinished
        multipart upload before rejecting it so callers cannot use that legacy
        record to materialize a browser-invisible object.  Abort remains best
        effort: its provider error must not turn the stable validation failure
        into an internal-detail response.
        """
        if not _contains_reserved_spill_metadata(getattr(session, "metadata", None)):
            return
        if getattr(session, "status", None) not in {"completed", "aborted"}:
            try:
                await storage.abort_large_upload(session)
            except Exception as error:
                logger.warning(
                    "Failed to abort reserved large upload upload=%s error_type=%s",
                    _opaque_reference(getattr(session, "upload_id", ""), "upload"),
                    type(error).__name__,
                )
        raise ValueError("Reserved file metadata is not accepted")

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
        await self._reject_reserved_large_upload_session(storage, session)
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
        await self._reject_reserved_large_upload_session(storage, session)
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
        file_ref = _opaque_reference(file_id, "file")
        user_ref = _opaque_reference(user_id, "user")
        logger.info("Download file request file=%s user=%s", file_ref, user_ref)
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            # Preflight metadata before obtaining a potentially huge stream.
            # Missing and unauthorized files share the same public response.
            file_info = await self._file_storage.get_file_info(file_id, user_id)
            if file_info is None or _is_private_spill(file_info):
                raise FileNotFoundError("File not found")
            result = await self._file_storage.download_file(file_id, user_id)
            if _is_private_spill(result[1]):
                _close_download_stream(result[0])
                raise FileNotFoundError("File not found")
            logger.info("File downloaded successfully file=%s user=%s", file_ref, user_ref)
            return result
        except Exception as e:
            logger.error(
                "Failed to download file file=%s user=%s error_type=%s",
                file_ref,
                user_ref,
                type(e).__name__,
            )
            raise

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        """Delete file"""
        file_ref = _opaque_reference(file_id, "file")
        user_ref = _opaque_reference(user_id, "user")
        logger.info("Delete file request file=%s user=%s", file_ref, user_ref)
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            file_info = await self._file_storage.get_file_info(file_id, user_id)
            if _is_private_spill(file_info):
                return False
            result = await self._file_storage.delete_file(file_id, user_id)
            if result:
                logger.info("File deleted successfully file=%s user=%s", file_ref, user_ref)
            else:
                logger.warning("File deletion failed or not found file=%s user=%s", file_ref, user_ref)
            return result
        except Exception as e:
            logger.error(
                "Failed to delete file file=%s user=%s error_type=%s",
                file_ref,
                user_ref,
                type(e).__name__,
            )
            raise

    async def get_file_info(self, file_id: str, user_id: Optional[str] = None) -> Optional[FileInfo]:
        """Get file information"""
        file_ref = _opaque_reference(file_id, "file")
        user_ref = _opaque_reference(user_id, "user")
        logger.info("Get file info request file=%s user=%s", file_ref, user_ref)
        if not self._file_storage:
            logger.error("File storage service not available")
            raise RuntimeError("File storage service not available")
        
        try:
            result = await self._file_storage.get_file_info(file_id, user_id)
            if _is_private_spill(result):
                return None
            if result:
                logger.info("File info retrieved successfully file=%s user=%s", file_ref, user_ref)
            else:
                logger.warning("File not found or access denied file=%s user=%s", file_ref, user_ref)
            return result
        except Exception as e:
            logger.error(
                "Failed to get file info file=%s user=%s error_type=%s",
                file_ref,
                user_ref,
                type(e).__name__,
            )
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
