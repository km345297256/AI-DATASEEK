from fastapi import APIRouter, Depends, Header, HTTPException
from app.application.services import visualization_jobs as service
from app.application.services.extended_visualization import ExtendedPreviewRequest
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError
from app.core.config import get_settings
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.infrastructure.external.file.spill_factory import get_spill_artifact_store
from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog
from app.interfaces.schemas.base import APIResponse

router = APIRouter()

async def safe(operation):
    try:
        return APIResponse.success(await operation)
    except VisualizationDisabledError:
        raise HTTPException(403, "该可视化插件已停用。") from None
    except (VisualizationNotFoundError, FileNotFoundError, PermissionError):
        raise HTTPException(404, "文件或任务不存在。") from None
    except PreviewVersionChanged:
        raise HTTPException(409, "文件或插件已更新，请重新运行。") from None
    except ScientificPreviewRejected as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        raise HTTPException(503, "质控任务暂不可用。") from None


@router.post("/{file_id}/visualization-jobs")
async def create_job(file_id: str, data: ExtendedPreviewRequest, user=Depends(get_current_user),
                     files=Depends(get_file_service), catalog=Depends(get_visualization_catalog),
                     jobs=Depends(get_analysis_job_service), artifacts=Depends(get_spill_artifact_store)):
    return await safe(service.start_job(files, catalog, get_settings().sandbox_image, jobs, artifacts, file_id, user.id, data))


@router.get("/{file_id}/visualization-jobs")
async def list_jobs(file_id: str, user=Depends(get_current_user), files=Depends(get_file_service),
                    catalog=Depends(get_visualization_catalog), jobs=Depends(get_analysis_job_service)):
    async def operation():
        await service.authorize(files, catalog, file_id, user.id)
        return [job.public_view() for job in await jobs.list_for_owner(user.id, service.scope_for_file(file_id), limit=20)]
    return await safe(operation())


@router.get("/{file_id}/visualization-jobs/{job_id}")
async def get_job(file_id: str, job_id: str, user=Depends(get_current_user), files=Depends(get_file_service),
                   catalog=Depends(get_visualization_catalog), jobs=Depends(get_analysis_job_service)):
    async def operation():
        return (await service.require_job(files, catalog, jobs, file_id, user.id, job_id)).public_view()
    return await safe(operation())


@router.post("/{file_id}/visualization-jobs/{job_id}/cancel")
async def cancel_job(file_id: str, job_id: str, user=Depends(get_current_user), jobs=Depends(get_analysis_job_service),
                      action: str | None = Header(default=None, alias="X-Analysis-Job-Action")):
    if action != "cancel":
        raise HTTPException(400, "必须明确取消任务。")
    async def operation():
        # Stopping owned work remains possible after its plugin/file is disabled/deleted.
        record = await jobs.get_for_owner(user.id, service.scope_for_file(file_id), job_id)
        if record is None or record.tool_name != "visualization:viz-fastqc":
            raise FileNotFoundError()
        cancelled = await jobs.request_cancel(user.id, service.scope_for_file(file_id), job_id)
        return cancelled.public_view()
    return await safe(operation())


@router.get("/{file_id}/visualization-jobs/{job_id}/result")
async def get_result(file_id: str, job_id: str, user=Depends(get_current_user), files=Depends(get_file_service),
                      catalog=Depends(get_visualization_catalog), jobs=Depends(get_analysis_job_service),
                      artifacts=Depends(get_spill_artifact_store)):
    return await safe(service.read_result(files, catalog, jobs, artifacts, file_id, user.id, job_id))
