from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from langchain.tools import tool

from app.domain.models.dataset import MountedDataset
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit


class DatasetCatalogToolkit(BaseToolkit):
    """Read-only registered-dataset operations available to the execution agent."""

    name: str = "dataset_catalog"

    def __init__(self) -> None:
        self._datasets: list[MountedDataset] = []
        super().__init__()

    def set_datasets(self, datasets: list[MountedDataset]) -> None:
        self._datasets = list(datasets or [])

    @staticmethod
    def _logical_path(value: str) -> str | None:
        normalized = (value or "").replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            return None
        return "/".join(part for part in path.parts if part not in {"", "."}) or None

    def _records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for dataset in self._datasets:
            for item in dataset.files:
                logical_path = self._logical_path(item.path)
                if not logical_path:
                    continue
                records.append({
                    "dataset": dataset.name,
                    "logical_path": logical_path,
                    "filename": PurePosixPath(logical_path).name,
                    "extension": PurePosixPath(logical_path).suffix.casefold(),
                    "size_bytes": max(0, int(item.size)),
                    "content_type": item.content_type or "",
                })
        return records

    @tool(parse_docstring=True)
    async def list_dataset_files(
        self,
        query: str = "",
        limit: int = 50,
    ) -> ToolResult:
        """List registered dataset files by literal filename or relative-path fragment. Use this when the user asks which files or file categories are present. It never reads file contents or exposes host paths.

        Args:
            query: Optional literal filename or relative-path fragment; do not pass a natural-language instruction.
            limit: Maximum records to return, from 1 to 200.
        """
        needle = (query or "").casefold().strip()
        records = [
            record for record in self._records()
            if not needle or needle in record["logical_path"].casefold()
        ]
        bounded_limit = max(1, min(int(limit), 200))
        return ToolResult(success=True, message="Registered dataset files listed", data={
            "match_count": len(records),
            "files": records[:bounded_limit],
            "omitted_count": max(0, len(records) - bounded_limit),
        })

    @tool(parse_docstring=True)
    async def resolve_dataset_file(self, reference: str) -> ToolResult:
        """Resolve an explicit filename or relative path to one registered dataset file. Use before reading or visualizing a named file. A non-unique result is deliberately returned as ambiguous; never replace it with a whole-dataset operation.

        Args:
            reference: Literal filename or relative-path reference from the user or conversation.
        """
        normalized = self._logical_path(reference)
        if not normalized:
            return ToolResult(success=False, message="A literal safe file reference is required", data={"status": "invalid"})
        records = self._records()
        needle = normalized.casefold()
        exact = [item for item in records if item["logical_path"].casefold() == needle]
        suffix = exact or [
            item for item in records
            if item["logical_path"].casefold().endswith(f"/{needle}")
            or item["filename"].casefold() == needle
        ]
        if len(suffix) == 1:
            return ToolResult(success=True, message="Registered file resolved", data={
                "status": "resolved", "file": suffix[0],
            })
        return ToolResult(success=False, message="Registered file reference is ambiguous or missing", data={
            "status": "ambiguous" if suffix else "missing",
            "candidates": suffix[:20],
            "omitted_count": max(0, len(suffix) - 20),
        })

    @tool(parse_docstring=True)
    async def inspect_dataset_catalog(self) -> ToolResult:
        """Return registered dataset names, descriptions, tags, file counts, and format groups. Use for catalog-only questions; do not infer file contents from this metadata.
        """
        summaries = []
        for dataset in self._datasets:
            records = [record for record in self._records() if record["dataset"] == dataset.name]
            formats: dict[str, int] = {}
            for record in records:
                extension = record["extension"] or "[no extension]"
                formats[extension] = formats.get(extension, 0) + 1
            summaries.append({
                "name": dataset.name,
                "description": dataset.description,
                "tags": dataset.tags,
                "file_count": len(records),
                "formats": formats,
                "inventory_complete": dataset.metadata.get("inventory_complete") is True,
            })
        return ToolResult(success=True, message="Registered dataset catalog inspected", data={"datasets": summaries})
