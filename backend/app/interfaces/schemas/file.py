from datetime import datetime
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, field_validator

from app.domain.models.file import FileInfo


_PUBLIC_SANDBOX_ROOTS = (
    PurePosixPath("/home/ubuntu/output"),
    PurePosixPath("/home/ubuntu/upload"),
)
_PRIVATE_FILE_METADATA_KEYS = {
    "absolutepath",
    "filepath",
    "localpath",
    "sourcepath",
    "userid",
}
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`<>]+"
)
_UNC_ABSOLUTE_PATH = re.compile(r"(?<![\\])\\\\[^\s'\"`<>]+")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(^|[\s'\"`(<>=,:;\[])/(?!/)[^\s'\"`<>]+"
)


def _redact_absolute_paths(value: str) -> str:
    """Redact filesystem paths without damaging ordinary HTTP(S) URLs."""

    if value.lower().startswith("file://"):
        return "[redacted path]"
    if value.startswith("/") and not value.startswith("//"):
        return "[redacted path]"
    if PureWindowsPath(value).is_absolute() or value.startswith("\\\\"):
        return "[redacted path]"

    sanitized = _WINDOWS_ABSOLUTE_PATH.sub("[redacted path]", value)
    sanitized = _UNC_ABSOLUTE_PATH.sub("[redacted path]", sanitized)
    return _POSIX_ABSOLUTE_PATH.sub(
        lambda match: f"{match.group(1)}[redacted path]",
        sanitized,
    )


def public_file_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return file metadata that is safe to expose through HTTP or SSE.

    Storage and session models keep their absolute paths for sandbox hydration and
    artifact replacement.  Public payloads remove those internal keys and redact
    absolute paths that may also occur in nested, user-supplied metadata.
    """

    if metadata is None:
        return None

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            result: Dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized_key in _PRIVATE_FILE_METADATA_KEYS:
                    continue
                safe_key = _redact_absolute_paths(key)
                if safe_key != key:
                    continue
                result[key] = sanitize(item)
            return result
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item) for item in value]
        if isinstance(value, os.PathLike):
            rendered_path = os.fspath(value)
            if isinstance(rendered_path, bytes):
                try:
                    rendered_path = rendered_path.decode()
                except UnicodeDecodeError:
                    return "[redacted path]"
            return _redact_absolute_paths(rendered_path)
        if isinstance(value, str):
            return _redact_absolute_paths(value)
        return value

    return sanitize(metadata)


def _public_file_url(value: Optional[str]) -> Optional[str]:
    """Allow only browser URLs; reject filesystem-like legacy values."""

    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if any(ord(character) < 32 for character in candidate) or "\\" in candidate:
        return None

    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return candidate
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/"):
        return None
    decoded_path = unquote(parsed.path)
    if ".." in PurePosixPath(decoded_path).parts:
        return None
    return candidate


def public_filename(value: Optional[str]) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    filename = normalized.rsplit("/", 1)[-1]
    return filename if filename not in {"", ".", ".."} else "file"


def _public_path_reference(
    raw_path: object,
    *,
    fallback_name: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    normalized = raw_path.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if ".." in path.parts:
        return public_filename(fallback_name or normalized)
    if path.is_absolute():
        for root in _PUBLIC_SANDBOX_ROOTS:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if relative.parts:
                return relative.as_posix()
        return public_filename(fallback_name or normalized)
    if PureWindowsPath(raw_path).is_absolute():
        return public_filename(fallback_name or raw_path)
    return path.as_posix() if path.parts else None


def _public_relative_path(file_info: FileInfo) -> Optional[str]:
    raw_path = file_info.file_path or (file_info.metadata or {}).get("file_path")
    return _public_path_reference(raw_path, fallback_name=file_info.filename)


class FileViewRequest(BaseModel):
    """File view request schema"""
    file: str


class FileViewResponse(BaseModel):
    """File view response schema"""
    content: str
    file: str

    @field_validator("file")
    @classmethod
    def hide_internal_file_path(cls, value: str) -> str:
        return _public_path_reference(value, fallback_name=value) or "file"


class FileInfoResponse(BaseModel):
    """File info response schema"""
    file_id: str
    filename: str
    relative_path: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None
    upload_date: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    file_url: Optional[str] = None

    @classmethod
    def public_from_file_info(
        cls,
        file_info: FileInfo,
        *,
        file_url: Optional[str] = None,
    ) -> "FileInfoResponse":
        """Map the internal model to its path-safe public representation."""

        return cls(
            file_id=file_info.file_id or "",
            filename=public_filename(file_info.filename or file_info.file_path),
            relative_path=_public_relative_path(file_info),
            content_type=file_info.content_type,
            size=file_info.size,
            upload_date=file_info.upload_date,
            metadata=public_file_metadata(file_info.metadata),
            file_url=_public_file_url(
                file_url if file_url is not None else file_info.file_url
            ),
        )

    @classmethod
    async def from_file_info(cls, file_info: FileInfo) -> "FileInfoResponse":
        from app.interfaces.dependencies import get_file_service
        file_service = get_file_service()
        file_url = await file_service.create_signed_url(
            file_info.file_id,
            file_info.user_id,
        )
        return cls.public_from_file_info(file_info, file_url=file_url)


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
