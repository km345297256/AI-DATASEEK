from functools import lru_cache

from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.repositories.mongo_analysis_job_repository import MongoAnalysisJobRepository


@lru_cache()
def get_analysis_job_service() -> AnalysisJobService:
    return AnalysisJobService(MongoAnalysisJobRepository())
