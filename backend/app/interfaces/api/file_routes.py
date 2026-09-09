from fastapi import APIRouter, Depends, UploadFile, File, Form, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask
import json
import asyncio
import logging
import io
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from pydantic import BaseModel

from app.application.services.file_service import FileService
from app.application.services.file_preview import FilePreviewPage, PreviewVersionChanged
from app.application.services.scientific_visualization import (
    ScientificVisualizationRequest, ScientificVisualizationResult, ScientificPreviewRejected,
    VisualizationWorkerError, scientific_visualization,
)
from app.application.services.extended_visualization import ExtendedPreviewRequest, extended_visualization
from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError
from app.domain.external.plugin_runtime import PluginRuntimeError
from app.core.config import get_settings
from typing import Literal
from app.application.errors.exceptions import NotFoundError
from app.interfaces.dependencies import get_file_service, get_current_user, get_optional_current_user, verify_signature
from app.interfaces.dependencies import get_visualization_catalog
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
from app.interfaces.api.visualization_job_routes import router as visualization_job_router
router.include_router(visualization_job_router)


class ShapefilePreviewRequest(BaseModel):
    file_id: str


class ShapefilePreviewLayer(BaseModel):
    name: str
    relative_path: str
    complete: bool
    components: list[FileInfoResponse]
    missing_components: list[str]


class ShapefilePreviewResponse(BaseModel):
    source_name: str
    layers: list[ShapefilePreviewLayer]


class MolecularPreviewRequest(BaseModel):
    file_id: str


class MolecularPreviewResponse(BaseModel):
    source_name: str
    source_format: str
    content_type: str | None = None
    size_bytes: int | None = None
    periodic: bool
    supports_unit_cell: bool


_SHAPEFILE_COMPONENTS = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_FILES = 10000
_MAX_MOLECULAR_PREVIEW_BYTES = 50 * 1024 * 1024
_MOLECULAR_FORMATS = {
    ".cif": "cif",
    ".pdb": "pdb",
    ".ent": "pdb",
    ".mol": "sdf",
    ".sdf": "sdf",
    ".xyz": "xyz",
    ".mol2": "mol2",
    ".vasp": "vasp",
}


def _safe_archive_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "\x00" in normalized:
        raise ValueError("Archive contains an unsafe member path")
    return path


def _extract_preview_archive(source: Path, destination: Path) -> None:
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > _MAX_ARCHIVE_FILES or sum(item.file_size for item in members) > _MAX_EXPANDED_BYTES:
                raise ValueError("Archive exceeds Shapefile preview limits")
            for item in members:
                relative = _safe_archive_member(item.filename)
                if item.file_size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("Archive member exceeds Shapefile preview limits")
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
        return
    listing = subprocess.run(["7z", "l", "-slt", str(source)], capture_output=True, text=True, timeout=60, check=True)
    names = []
    for line in listing.stdout.splitlines():
        if line.startswith("Path = "):
            candidate = line[7:].strip()
            if candidate and candidate != str(source): names.append(candidate)
    if len(names) > _MAX_ARCHIVE_FILES:
        raise ValueError("Archive contains too many files")
    for name in names: _safe_archive_member(name)
    subprocess.run(["7z", "x", "-y", f"-o{destination}", str(source)], capture_output=True, text=True, timeout=180, check=True)
    total = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    if total > _MAX_EXPANDED_BYTES:
        raise ValueError("Expanded archive exceeds Shapefile preview limits")


def _close_file_stream(stream) -> None:
    try:
        if hasattr(stream, "close"):
            stream.close()
    finally:
        if hasattr(stream, "release_conn"):
            stream.release_conn()


def _molecular_source_format(source_name: str) -> str | None:
    if Path(source_name).name.casefold() in {"poscar", "contcar"}:
        return "vasp"
    return _MOLECULAR_FORMATS.get(Path(source_name).suffix.casefold())

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


@router.post(
    "/shapefile-preview/prepare",
    response_model=APIResponse[ShapefilePreviewResponse],
)
async def prepare_shapefile_preview(
    request: ShapefilePreviewRequest,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[ShapefilePreviewResponse]:
    stream, file_info = await file_service.download_file(request.file_id, current_user.id)
    source_name = public_filename(file_info.filename)
    suffix = Path(source_name).suffix.lower()
    if suffix not in {".zip", ".rar"}:
        _close_file_stream(stream)
        raise ValueError("Shapefile preview preparation supports ZIP or RAR archives")
    with tempfile.TemporaryDirectory(prefix="dataseek-shapefile-preview-") as temporary:
        root = Path(temporary)
        archive_path = root / ("source" + suffix)
        with archive_path.open("wb") as target:
            shutil.copyfileobj(stream, target, length=1024 * 1024)
        _close_file_stream(stream)
        if archive_path.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds Shapefile preview limits")
        extracted = root / "extracted"
        extracted.mkdir()
        _extract_preview_archive(archive_path, extracted)

        groups: dict[str, list[Path]] = {}
        for path in extracted.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _SHAPEFILE_COMPONENTS:
                continue
            key = path.relative_to(extracted).with_suffix("").as_posix().casefold()
            groups.setdefault(key, []).append(path)
        layers: list[ShapefilePreviewLayer] = []
        for key, paths in sorted(groups.items()):
            extensions = {path.suffix.lower() for path in paths}
            if ".shp" not in extensions:
                continue
            required = {".shp", ".shx", ".dbf"}
            missing = sorted(required - extensions)
            component_responses = []
            for path in sorted(paths):
                relative = path.relative_to(extracted).as_posix()
                with path.open("rb") as component_stream:
                    uploaded = await file_service.upload_file(
                        component_stream,
                        path.name,
                        current_user.id,
                        "application/octet-stream",
                        {
                            "shapefile_preview": True,
                            "source_archive": source_name,
                            "logical_path": relative,
                            "layer_key": key,
                        },
                    )
                response = await FileInfoResponse.from_file_info(uploaded)
                response.relative_path = relative
                component_responses.append(response)
            layers.append(ShapefilePreviewLayer(
                name=PurePosixPath(key).name,
                relative_path=key,
                complete=not missing,
                components=component_responses,
                missing_components=missing,
            ))
    if not layers:
        raise ValueError("Archive contains no Shapefile layers")
    return APIResponse.success(ShapefilePreviewResponse(source_name=source_name, layers=layers))


@router.post(
    "/molecular-preview/prepare",
    response_model=APIResponse[MolecularPreviewResponse],
)
async def prepare_molecular_preview(
    request: MolecularPreviewRequest,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[MolecularPreviewResponse]:
    """Authorize a browser-side structure preview without exposing storage paths."""
    stream, file_info = await file_service.download_file(request.file_id, current_user.id)
    try:
        source_name = public_filename(file_info.filename)
        source_format = _molecular_source_format(source_name)
        if source_format is None:
            raise HTTPException(status_code=415, detail="Unsupported molecular structure format")
        if file_info.size is not None and file_info.size > _MAX_MOLECULAR_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="Molecular structure exceeds the 50 MB preview limit")
        periodic = source_format in {"cif", "vasp"}
        return APIResponse.success(MolecularPreviewResponse(
            source_name=source_name,
            source_format=source_format,
            content_type=file_info.content_type,
            size_bytes=file_info.size,
            periodic=periodic,
            supports_unit_cell=periodic,
        ))
    finally:
        _close_file_stream(stream)


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


@router.get("/{file_id}/preview", response_model=APIResponse[FilePreviewPage])
async def preview_file(
    file_id: str,
    offset: int = Query(default=0, ge=0),
    mode: Literal["text", "csv"] = "text",
    version: str | None = Query(default=None, max_length=64),
    delimiter: Literal[",", "\t"] | None = None,
    header_pending: bool = False,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
) -> APIResponse[FilePreviewPage]:
    """Read one bounded page using the same ownership checks as file info."""
    try:
        page = await file_service.preview_file(file_id, current_user.id, offset=offset, mode=mode, version=version, delimiter=delimiter, header_pending=header_pending)
    except (FileNotFoundError, PermissionError):
        raise NotFoundError("File not found")
    except PreviewVersionChanged:
        raise HTTPException(status_code=409, detail="File changed; reopen the preview")
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Bounded preview is unavailable for this storage; download the file instead")
    except ValueError as error:
        # Do not expose storage exception details, IDs, or host paths.
        logger.info("File preview rejected error_type=%s", type(error).__name__)
        raise HTTPException(status_code=422, detail="This page cannot be previewed. Use UTF-8 text/CSV with records under 128 KiB, or download the file.")
    return APIResponse.success(page)


@router.post("/{file_id}/visualization", response_model=APIResponse[ScientificVisualizationResult])
async def visualize_file(
    file_id: str,
    request_data: ScientificVisualizationRequest,
    request: Request,
    file_service: FileService = Depends(get_file_service),
    current_user: User = Depends(get_current_user),
    catalog=Depends(get_visualization_catalog),
) -> APIResponse[ScientificVisualizationResult]:
    """Data-only scientific preview. File and plugin grants are both required."""
    work = asyncio.create_task(scientific_visualization(
        file_service, catalog, get_settings().sandbox_image, file_id, current_user.id, request_data,
    ))
    try:
        # Starlette does not automatically cancel ordinary POST handlers on
        # browser abort. Explicitly fence this independent, non-Agent reader.
        while not work.done():
            await asyncio.wait({work}, timeout=0.25)
            if not work.done() and await request.is_disconnected():
                raise asyncio.CancelledError()
        return APIResponse.success(await work)
    except VisualizationDisabledError:
        raise HTTPException(status_code=403, detail="此可视化插件已停用，请先在插件页面启用。")
    except (VisualizationNotFoundError, FileNotFoundError, PermissionError):
        raise HTTPException(status_code=404, detail="文件或可视化插件不存在。")
    except PreviewVersionChanged:
        raise HTTPException(status_code=409, detail="文件或插件版本已变化，请重新打开预览。")
    except ScientificPreviewRejected as error:
        raise HTTPException(status_code=422, detail=str(error))
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="当前存储不支持有界预览。")
    except (PluginRuntimeError, VisualizationWorkerError):
        raise HTTPException(status_code=503, detail="Cordis 或隔离预览暂不可用，请确认服务及沙箱镜像已更新。")
    except Exception as error:
        logger.warning("Scientific preview unavailable error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="科学预览暂不可用，请稍后重试。")
    finally:
        if not work.done():
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                pass


async def _extended_preview_response(file_id, data, request, file_service, user, catalog, *, binary=False):
    work = asyncio.create_task(extended_visualization(
        file_service, catalog, get_settings().sandbox_image, file_id, user.id, data, binary=binary,
    ))
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=0.25)
            if not work.done() and await request.is_disconnected():
                raise asyncio.CancelledError()
        result = await work
        if binary:
            content, version = result
            return Response(content, media_type="application/octet-stream", headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "X-Preview-Version": version, "Content-Disposition": "attachment",
            })
        return APIResponse.success(result)
    except VisualizationDisabledError:
        raise HTTPException(403, "此可视化插件已停用。") from None
    except (VisualizationNotFoundError, FileNotFoundError, PermissionError):
        raise HTTPException(404, "文件或插件不存在。") from None
    except PreviewVersionChanged:
        raise HTTPException(409, "文件或插件已更新，请重新打开预览。") from None
    except ScientificPreviewRejected as error:
        raise HTTPException(422, str(error)) from None
    except NotImplementedError:
        raise HTTPException(501, "此存储不支持有界读取。") from None
    except Exception as error:
        logger.warning("Extended preview unavailable error_type=%s", type(error).__name__)
        raise HTTPException(503, "隔离读取器暂不可用，请检查沙箱镜像和插件服务。") from None
    finally:
        if not work.done():
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                pass


@router.post("/{file_id}/visualization-v2")
async def visualize_file_v2(file_id: str, data: ExtendedPreviewRequest, request: Request,
                            file_service: FileService = Depends(get_file_service),
                            user: User = Depends(get_current_user), catalog=Depends(get_visualization_catalog)):
    return await _extended_preview_response(file_id, data, request, file_service, user, catalog)


@router.post("/{file_id}/visualization-content")
async def visualization_content(file_id: str, data: ExtendedPreviewRequest, request: Request,
                                file_service: FileService = Depends(get_file_service),
                                user: User = Depends(get_current_user), catalog=Depends(get_visualization_catalog)):
    return await _extended_preview_response(file_id, data, request, file_service, user, catalog, binary=True)


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
