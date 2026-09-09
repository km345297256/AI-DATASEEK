from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.application.services.unified_visualization import VisualizationRequest, unified_visualization
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationPlugin, VisualizationPluginState


class _FileService:
    def __init__(self, file_info: FileInfo):
        self.file_info = file_info
        self.calls = []

    async def get_file_info(self, file_id, user_id):
        self.calls.append((file_id, user_id))
        return self.file_info if file_id == self.file_info.file_id and user_id == self.file_info.user_id else None

    async def download_file(self, *args):
        raise AssertionError("Structure preparation needs metadata only, never a download")


def _catalog():
    plugin = VisualizationPlugin.model_validate_json((Path(__file__).resolve().parents[2] / "plugin-host/visualizations/molecular.json").read_text())
    return SimpleNamespace(require_enabled=AsyncMock(return_value=plugin),
        list_for_user=AsyncMock(return_value=SimpleNamespace(revision="a" * 64,
            plugins=[VisualizationPluginState(**plugin.model_dump(), enabled=True)])))


async def _prepare(files, *, owner="user-1"):
    return await unified_visualization(files, _catalog(), None, "file-1", owner,
        VisualizationRequest(plugin_id="molecular", operation="prepare"))


def _file(filename: str, size: int = 1024) -> FileInfo:
    return FileInfo(
        file_id="file-1",
        filename=filename,
        file_path="/private/storage/tenant/structure.cif",
        content_type="chemical/x-cif",
        size=size,
        upload_date=datetime.now(UTC),
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_prepare_molecular_preview_authorizes_and_hides_storage_path():
    service = _FileService(_file("/private/storage/tenant/crystal.cif"))

    response = await _prepare(service)

    assert service.calls == [("file-1", "user-1")] * 2
    assert response.kind == "molecule" and response.contract_version == 2
    assert response.payload["source_name"] == "crystal.cif"
    assert response.payload["source_format"] == "cif"
    assert response.payload["supports_unit_cell"] is True
    assert "/private/" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_prepare_molecular_preview_supports_poscar_without_extension():
    service = _FileService(_file("POSCAR"))

    response = await _prepare(service)

    assert response.payload["source_format"] == "vasp"
    assert response.payload["periodic"] is True


@pytest.mark.asyncio
async def test_prepare_molecular_preview_rejects_unsupported_format_without_opening_stream():
    service = _FileService(_file("notes.txt"))

    with pytest.raises(ScientificPreviewRejected):
        await _prepare(service)


@pytest.mark.asyncio
async def test_prepare_molecular_preview_rejects_oversized_structure():
    service = _FileService(_file("large.sdf", size=51 * 1024 * 1024))

    with pytest.raises(ScientificPreviewRejected):
        await _prepare(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_prepare_molecular_preview_hides_foreign_and_private_files(private):
    service = _FileService(_file("structure.pdb"))
    if private: service.file_info.metadata = {"source": "tool_output_spill"}
    with pytest.raises(FileNotFoundError):
        await _prepare(service, owner="user-1" if private else "other")
