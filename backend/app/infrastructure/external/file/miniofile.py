import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, UTC, timedelta
from typing import Any, BinaryIO, Dict, Optional, Tuple

from app.core.config import get_settings
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.infrastructure.models.documents import FileUploadSessionDocument, StoredFileDocument

logger = logging.getLogger(__name__)

_MINIO_METADATA_MAX_BYTES = 1800
_MINIO_METADATA_VALUE_MAX_CHARS = 512


def _safe_filename(filename: str) -> str:
    name = filename.strip().replace("\\", "/").split("/")[-1] or "file"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:180] or "file"


def _stream_length(stream: BinaryIO) -> int:
    try:
        current = stream.tell()
        stream.seek(0, 2)
        length = stream.tell() - current
        stream.seek(current)
        return max(0, length)
    except Exception:
        return -1


def _minio_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    total_bytes = 0
    for key, value in metadata.items():
        if value is None:
            continue
        clean_key = re.sub(r"[^A-Za-z0-9-]+", "-", str(key)).strip("-").lower()
        if not clean_key:
            continue
        raw_value = str(value)
        ascii_value = raw_value
        try:
            ascii_value.encode("ascii")
        except UnicodeEncodeError:
            ascii_value = json.dumps(raw_value, ensure_ascii=True)
        ascii_value = ascii_value[:_MINIO_METADATA_VALUE_MAX_CHARS]
        header_bytes = len(clean_key.encode("ascii")) + len(ascii_value.encode("ascii"))
        if total_bytes + header_bytes > _MINIO_METADATA_MAX_BYTES:
            logger.warning("Dropping MinIO object metadata key %s because metadata headers are too large", clean_key)
            continue
        result[clean_key] = ascii_value
        total_bytes += header_bytes
    return result


class MinIOFileStorage(FileStorage):
    def __init__(self) -> None:
        settings = get_settings()
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("minio dependency is not installed; run uv sync or rebuild the backend image") from exc

        self.settings = settings
        self.bucket_name = settings.minio_bucket
        self._client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region=settings.minio_region,
        )
        self._bucket_ready = False

    async def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return

        def ensure() -> None:
            if not self._client.bucket_exists(self.bucket_name):
                self._client.make_bucket(self.bucket_name)

        await asyncio.to_thread(ensure)
        self._bucket_ready = True

    def _object_key(self, *, user_id: str, file_id: str, filename: str, metadata: Dict[str, Any]) -> str:
        now = datetime.now(UTC)
        session_id = str(metadata.get("session_id") or metadata.get("sessionId") or "none")
        return (
            f"{self.settings.minio_object_prefix.strip('/')}/"
            f"users/{user_id}/sessions/{_safe_filename(session_id)}/"
            f"{now:%Y/%m/%d}/{file_id}/{_safe_filename(filename)}"
        )

    def _file_info_from_doc(self, doc: StoredFileDocument) -> FileInfo:
        return FileInfo(
            file_id=doc.file_id,
            filename=doc.filename,
            content_type=doc.content_type,
            size=doc.size,
            upload_date=doc.upload_date,
            metadata={**(doc.metadata or {}), "provider": doc.provider},
            user_id=doc.user_id,
        )

    async def upload_file(
        self,
        file_data: BinaryIO,
        filename: str,
        user_id: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileInfo:
        await self._ensure_bucket()
        now = datetime.now(UTC)
        file_uuid = uuid.uuid4().hex
        file_id = f"minio:{file_uuid}"
        merged_metadata: Dict[str, Any] = {
            "filename": filename,
            "user_id": user_id,
            "uploadDate": now.isoformat(),
            **(metadata or {}),
        }
        if content_type:
            merged_metadata["contentType"] = content_type
        object_key = self._object_key(user_id=user_id, file_id=file_uuid, filename=filename, metadata=merged_metadata)
        length = _stream_length(file_data)
        put_kwargs = {
            "bucket_name": self.bucket_name,
            "object_name": object_key,
            "data": file_data,
            "length": length,
            "content_type": content_type or "application/octet-stream",
            "metadata": _minio_metadata(merged_metadata),
        }
        if length < 0:
            put_kwargs["part_size"] = 10 * 1024 * 1024

        try:
            result = await asyncio.to_thread(self._client.put_object, **put_kwargs)
            stat = await asyncio.to_thread(self._client.stat_object, self.bucket_name, object_key)
            size = int(getattr(stat, "size", 0) or 0)
            doc = StoredFileDocument(
                file_id=file_id,
                provider="minio",
                bucket=self.bucket_name,
                object_key=object_key,
                filename=filename,
                content_type=content_type,
                size=size,
                user_id=user_id,
                metadata=merged_metadata,
                upload_date=now,
                created_at=now,
                updated_at=now,
            )
            await doc.insert()
            logger.info("File uploaded to MinIO: %s -> %s/%s etag=%s", file_id, self.bucket_name, object_key, result.etag)
            return self._file_info_from_doc(doc)
        except Exception:
            try:
                await asyncio.to_thread(self._client.remove_object, self.bucket_name, object_key)
            except Exception:
                pass
            raise

    async def init_large_upload(
        self,
        filename: str,
        user_id: str,
        size: int,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileUploadSessionDocument:
        await self._ensure_bucket()
        now = datetime.now(UTC)
        file_uuid = uuid.uuid4().hex
        file_id = f"minio:{file_uuid}"
        merged_metadata: Dict[str, Any] = {
            "filename": filename,
            "user_id": user_id,
            "uploadDate": now.isoformat(),
            "uploadMode": "multipart",
            **(metadata or {}),
        }
        if content_type:
            merged_metadata["contentType"] = content_type
        object_key = self._object_key(user_id=user_id, file_id=file_uuid, filename=filename, metadata=merged_metadata)
        headers = {
            "Content-Type": content_type or "application/octet-stream",
            **{f"x-amz-meta-{key}": value for key, value in _minio_metadata(merged_metadata).items()},
        }
        minio_upload_id = await asyncio.to_thread(
            self._client._create_multipart_upload,
            self.bucket_name,
            object_key,
            headers,
        )
        session = FileUploadSessionDocument(
            file_id=file_id,
            bucket=self.bucket_name,
            object_key=object_key,
            minio_upload_id=minio_upload_id,
            filename=filename,
            content_type=content_type,
            size=size,
            user_id=user_id,
            metadata=merged_metadata,
            part_size=max(5 * 1024 * 1024, int(self.settings.large_upload_part_size)),
            status="initiated",
            expires_at=now + timedelta(hours=self.settings.large_upload_session_expire_hours),
            created_at=now,
            updated_at=now,
        )
        await session.insert()
        return session

    async def upload_large_upload_part(
        self,
        session: FileUploadSessionDocument,
        part_number: int,
        data: bytes,
    ) -> str:
        if part_number < 1:
            raise ValueError("part_number must be greater than 0")
        return await asyncio.to_thread(
            self._client._upload_part,
            session.bucket,
            session.object_key,
            data,
            None,
            session.minio_upload_id,
            part_number,
        )

    async def complete_large_upload(
        self,
        session: FileUploadSessionDocument,
        parts: list[Dict[str, Any]],
    ) -> FileInfo:
        from minio.datatypes import Part

        now = datetime.now(UTC)
        normalized_parts = [
            {
                "part_number": int(part["part_number"]),
                "etag": str(part["etag"]).strip('"'),
                "size": part.get("size"),
            }
            for part in sorted(parts, key=lambda item: int(item["part_number"]))
        ]
        minio_parts = [
            Part(part_number=part["part_number"], etag=part["etag"], size=part.get("size"))
            for part in normalized_parts
        ]
        try:
            await asyncio.to_thread(
                self._client._complete_multipart_upload,
                session.bucket,
                session.object_key,
                session.minio_upload_id,
                minio_parts,
            )
            stat = await asyncio.to_thread(self._client.stat_object, session.bucket, session.object_key)
            size = int(getattr(stat, "size", 0) or session.size or 0)
            doc = StoredFileDocument(
                file_id=session.file_id,
                provider="minio",
                bucket=session.bucket,
                object_key=session.object_key,
                filename=session.filename,
                content_type=session.content_type,
                size=size,
                user_id=session.user_id,
                metadata=session.metadata,
                upload_date=now,
                created_at=now,
                updated_at=now,
            )
            await doc.insert()
            session.status = "completed"
            session.parts = normalized_parts
            session.updated_at = now
            await session.save()
            return self._file_info_from_doc(doc)
        except Exception as exc:
            session.status = "failed"
            session.error = str(exc)
            session.updated_at = now
            await session.save()
            raise

    async def abort_large_upload(self, session: FileUploadSessionDocument) -> None:
        try:
            await asyncio.to_thread(
                self._client._abort_multipart_upload,
                session.bucket,
                session.object_key,
                session.minio_upload_id,
            )
        finally:
            session.status = "aborted"
            session.updated_at = datetime.now(UTC)
            await session.save()

    async def download_file(self, file_id: str, user_id: Optional[str] = None) -> Tuple[BinaryIO, FileInfo]:
        doc = await StoredFileDocument.find_one(StoredFileDocument.file_id == file_id)
        if not doc or doc.provider != "minio" or not doc.object_key:
            raise FileNotFoundError(f"File not found with ID: {file_id}")
        if user_id is not None and doc.user_id != user_id:
            raise PermissionError(f"Access denied: file {file_id} does not belong to user {user_id}")
        response = await asyncio.to_thread(self._client.get_object, doc.bucket or self.bucket_name, doc.object_key)
        return response, self._file_info_from_doc(doc)

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        doc = await StoredFileDocument.find_one(StoredFileDocument.file_id == file_id)
        if not doc or doc.provider != "minio" or not doc.object_key:
            return False
        if doc.user_id != user_id:
            logger.warning("Delete access denied: file %s does not belong to user %s", file_id, user_id)
            return False
        await asyncio.to_thread(self._client.remove_object, doc.bucket or self.bucket_name, doc.object_key)
        await doc.delete()
        return True

    async def get_file_info(self, file_id: str, user_id: Optional[str] = None) -> Optional[FileInfo]:
        doc = await StoredFileDocument.find_one(StoredFileDocument.file_id == file_id)
        if not doc or doc.provider != "minio":
            return None
        if user_id is not None and doc.user_id != user_id:
            return None
        return self._file_info_from_doc(doc)

    async def create_presigned_url(self, file_id: str, user_id: Optional[str] = None, expire_seconds: Optional[int] = None) -> str:
        doc = await StoredFileDocument.find_one(StoredFileDocument.file_id == file_id)
        if not doc or doc.provider != "minio" or not doc.object_key:
            raise FileNotFoundError(f"File not found with ID: {file_id}")
        if user_id is not None and doc.user_id != user_id:
            raise PermissionError(f"Access denied: file {file_id} does not belong to user {user_id}")
        seconds = min(expire_seconds or self.settings.minio_presigned_expire_seconds, self.settings.minio_presigned_expire_seconds)
        return await asyncio.to_thread(
            self._client.presigned_get_object,
            doc.bucket or self.bucket_name,
            doc.object_key,
            expires=timedelta(seconds=seconds),
        )

    async def storage_usage(self) -> Dict[str, Any]:
        collection = StoredFileDocument.get_pymongo_collection()
        rows = await collection.aggregate([
            {"$match": {"provider": "minio"}},
            {"$group": {"_id": None, "total_bytes": {"$sum": "$size"}, "object_count": {"$sum": 1}}},
        ]).to_list(length=1)
        row = rows[0] if rows else {}
        return {
            "provider": "minio",
            "bucket": self.bucket_name,
            "object_count": int(row.get("object_count") or 0),
            "total_bytes": int(row.get("total_bytes") or 0),
        }

    async def health_check(self) -> Dict[str, Any]:
        try:
            await self._ensure_bucket()
            exists = await asyncio.to_thread(self._client.bucket_exists, self.bucket_name)
            return {"provider": "minio", "available": bool(exists), "bucket": self.bucket_name}
        except Exception as exc:
            return {"provider": "minio", "available": False, "bucket": self.bucket_name, "error": str(exc)}
