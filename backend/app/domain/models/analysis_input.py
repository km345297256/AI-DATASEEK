"""Server-owned, immutable input manifest shared by all analysis entry points.

These records are execution context, not dataset registrations or browser paths.
Only the admission service may choose upload membership and namespaces.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.models.file import FileInfo

UPLOAD_INPUT_ROOT = "/home/ubuntu/inputs"


class AnalysisInputFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str
    file_id: str | None = None
    logical_path: str
    runtime_path: str
    size: int = 0
    content_type: str | None = None


class AnalysisInputSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["dataset", "upload"]
    source_id: str
    name: str
    files: tuple[AnalysisInputFile, ...] = ()


class AnalysisInputContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    sources: tuple[AnalysisInputSource, ...] = ()

    @property
    def files(self) -> tuple[AnalysisInputFile, ...]:
        return tuple(item for source in self.sources for item in source.files)

    @property
    def source_paths(self) -> list[str]:
        return sorted({item.runtime_path for item in self.files if item.runtime_path})

    @property
    def upload_paths(self) -> list[str]:
        return sorted({item.runtime_path for source in self.sources if source.kind == "upload"
                       for item in source.files if item.runtime_path})

    @property
    def upload_file_ids(self) -> list[str]:
        return [item.file_id for source in self.sources if source.kind == "upload"
                for item in source.files if item.file_id]

    @property
    def manifest_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True,
                                        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def safe_upload_filename(value: str | None) -> str:
    name = PurePosixPath((value or "file").replace("\\", "/")).name
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)
    if name in {"", ".", ".."}:
        return "file"
    suffix = PurePosixPath(name).suffix[:32]
    return name if len(name) <= 220 else name[:220 - len(suffix)] + suffix


def upload_runtime_path(info: FileInfo) -> str:
    metadata = info.metadata or {}
    namespace = metadata.get("analysis_input_namespace", "")
    filename = metadata.get("analysis_input_filename", "")
    if (not info.file_id or not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{24}", namespace)
            or not isinstance(filename, str) or filename != safe_upload_filename(filename)):
        raise ValueError("Upload has no verified analysis input identity")
    return f"{UPLOAD_INPUT_ROOT}/{namespace}/{filename}"


def assign_upload_namespace(files: list[FileInfo]) -> list[FileInfo]:
    """Group sidecars submitted together; do not let equal names overwrite bytes."""
    namespace = hashlib.sha256(json.dumps(sorted(item.file_id for item in files if item.file_id)).encode()).hexdigest()[:24]
    names = [safe_upload_filename(item.filename) for item in files]
    results = []
    assigned_names: set[str] = set()
    for info, filename in zip(files, names):
        if names.count(filename) > 1:
            path = PurePosixPath(filename)
            suffix = hashlib.sha256(str(info.file_id).encode()).hexdigest()[:12]
            filename = f"{path.stem[:180]}-{suffix}{path.suffix}"
        collision = 0
        while filename in assigned_names or (filename in names and filename != safe_upload_filename(info.filename)):
            collision += 1
            path = PurePosixPath(safe_upload_filename(info.filename))
            suffix = hashlib.sha256(f"{info.file_id}:{collision}".encode()).hexdigest()[:16]
            filename = f"{path.stem[:180]}-{suffix}{path.suffix}"
        assigned_names.add(filename)
        metadata = dict(info.metadata or {})
        metadata.update(analysis_input_namespace=namespace, analysis_input_filename=filename)
        # Storage paths/URLs and user-provided input namespace metadata are not trusted.
        results.append(info.model_copy(deep=True, update={"metadata": metadata, "file_path": None}))
    return results


def build_analysis_inputs(datasets: list[MountedDataset], uploads: list[FileInfo]) -> AnalysisInputContext:
    sources = []
    for dataset in datasets:
        sources.append(AnalysisInputSource(kind="dataset", source_id=dataset.dataset_id, name=dataset.name,
            files=tuple(AnalysisInputFile(source_id=dataset.dataset_id, logical_path=item.path,
                runtime_path=str(PurePosixPath(dataset.sandbox_path) / item.path),
                size=item.size, content_type=item.content_type) for item in dataset.files)))
    groups: dict[str, list[AnalysisInputFile]] = {}
    for info in uploads:
        runtime_path = upload_runtime_path(info)
        namespace = (info.metadata or {})["analysis_input_namespace"]
        groups.setdefault(namespace, []).append(AnalysisInputFile(source_id=f"upload:{namespace}", file_id=info.file_id,
            logical_path=f"uploads/{namespace}/{PurePosixPath(runtime_path).name}", runtime_path=runtime_path,
            size=info.size or 0, content_type=info.content_type))
    for namespace, files in groups.items():
        sources.append(AnalysisInputSource(kind="upload", source_id=f"upload:{namespace}",
            name=PurePosixPath(files[0].runtime_path).name if len(files) == 1 else "上传资料", files=tuple(files)))
    return AnalysisInputContext(sources=tuple(sources))


def upload_catalog_views(uploads: list[FileInfo]) -> list[MountedDataset]:
    """Read-only catalog *views*, never persisted or passed to mount allocation."""
    context = build_analysis_inputs([], uploads)
    return [MountedDataset(dataset_id=source.source_id, data_center_id="session-uploads",
        data_center_name="用户上传资料", name=source.name,
        description="用户在本会话中明确提交的分析资料；文件内容尚需实际读取。",
        sandbox_path=UPLOAD_INPUT_ROOT, files=[DatasetFile(path=item.logical_path.removeprefix("uploads/"),
            size=item.size, content_type=item.content_type) for item in source.files],
        metadata={"source_kind": "upload", "inventory_complete": True}) for source in context.sources]


def analysis_catalog_sources(message) -> list[MountedDataset]:
    """Uniform inventory adapter without changing registered/mounted identities."""
    return list(message.datasets) + (upload_catalog_views(message.attachment_file_infos)
                                    if message.analysis_inputs else [])


def render_upload_context(context: AnalysisInputContext | None) -> str:
    if context is None or not context.upload_file_ids:
        return ""
    records = [{"source": source.name, "file_id": item.file_id, "logical_path": item.logical_path,
                "runtime_path": item.runtime_path, "size": item.size, "content_type": item.content_type}
               for source in context.sources if source.kind == "upload" for item in source.files]
    return ("\n<session_analysis_inputs>\nUser-submitted analysis inputs, not published datasets. "
            "The authoritative input scope is the current catalog. This bounded manifest view includes "
            "at most 64 files; list_dataset_files can query the complete selected inventory. Do not infer additional inputs "
            "from files left in the sandbox or previous generated outputs. Preserve source bytes; "
            "write all outputs under /home/ubuntu/output. Files with the same namespace are submitted "
            "together (including format sidecars). Use file_id or a qualified path when names are ambiguous.\n"
            + json.dumps({"file_count": len(records), "omitted_count": max(0, len(records) - 64),
                          "files": records[:64]}, ensure_ascii=False) + "\n</session_analysis_inputs>")
