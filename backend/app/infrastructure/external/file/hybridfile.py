import logging
import asyncio
from typing import Any, BinaryIO, Dict, Optional, Tuple

from bson import ObjectId

from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage
from app.infrastructure.external.file.miniofile import MinIOFileStorage

logger = logging.getLogger(__name__)


def _is_legacy_gridfs_id(file_id: str) -> bool:
    try:
        ObjectId(file_id)
        return True
    except Exception:
        return False


class HybridFileStorage(FileStorage):
    """Writes new files to MinIO while keeping legacy GridFS reads working."""

    def __init__(self, minio_storage: MinIOFileStorage, gridfs_storage: GridFSFileStorage):
        self.minio_storage = minio_storage
        self.gridfs_storage = gridfs_storage

    def _select_storage(self, file_id: str) -> FileStorage:
        if file_id.startswith("minio:"):
            return self.minio_storage
        if file_id.startswith("gridfs:"):
            return self.gridfs_storage
        if _is_legacy_gridfs_id(file_id):
            return self.gridfs_storage
        return self.minio_storage

    def _normalize_file_id(self, file_id: str) -> str:
        return file_id.removeprefix("gridfs:")

    async def upload_file(
        self,
        file_data: BinaryIO,
        filename: str,
        user_id: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileInfo:
        return await self.minio_storage.upload_file(file_data, filename, user_id, content_type, metadata)

    async def init_large_upload(self, *args, **kwargs):
        return await self.minio_storage.init_large_upload(*args, **kwargs)

    async def upload_large_upload_part(self, *args, **kwargs):
        return await self.minio_storage.upload_large_upload_part(*args, **kwargs)

    async def complete_large_upload(self, *args, **kwargs):
        return await self.minio_storage.complete_large_upload(*args, **kwargs)

    async def abort_large_upload(self, *args, **kwargs):
        return await self.minio_storage.abort_large_upload(*args, **kwargs)

    async def download_file(self, file_id: str, user_id: Optional[str] = None) -> Tuple[BinaryIO, FileInfo]:
        storage = self._select_storage(file_id)
        return await storage.download_file(self._normalize_file_id(file_id), user_id)

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        storage = self._select_storage(file_id)
        return await storage.delete_file(self._normalize_file_id(file_id), user_id)

    async def get_file_info(self, file_id: str, user_id: Optional[str] = None) -> Optional[FileInfo]:
        storage = self._select_storage(file_id)
        return await storage.get_file_info(self._normalize_file_id(file_id), user_id)

    async def create_presigned_url(self, file_id: str, user_id: Optional[str] = None, expire_seconds: Optional[int] = None) -> str:
        if not file_id.startswith("minio:"):
            raise NotImplementedError("Storage-native presigned URLs are only available for MinIO files")
        return await self.minio_storage.create_presigned_url(file_id, user_id, expire_seconds)

    async def storage_usage(self) -> Dict[str, Any]:
        minio_usage, gridfs_usage = await asyncio.gather(
            self.minio_storage.storage_usage(),
            self.gridfs_storage.storage_usage(),
        )
        return {
            "provider": "hybrid",
            "mode": "hybrid",
            "object_count": int(minio_usage.get("object_count") or 0) + int(gridfs_usage.get("object_count") or 0),
            "total_bytes": int(minio_usage.get("total_bytes") or 0) + int(gridfs_usage.get("total_bytes") or 0),
            "backends": {
                "minio": minio_usage,
                "gridfs": gridfs_usage,
            },
        }

    async def health_check(self) -> Dict[str, Any]:
        minio_health, gridfs_health = await asyncio.gather(
            self.minio_storage.health_check(),
            self.gridfs_storage.health_check(),
        )
        return {
            "provider": "hybrid",
            "mode": "hybrid",
            "available": bool(minio_health.get("available")) and bool(gridfs_health.get("available")),
            "backends": {
                "minio": minio_health,
                "gridfs": gridfs_health,
            },
        }
