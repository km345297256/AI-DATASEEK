from functools import lru_cache

from app.core.config import get_settings
from app.domain.external.file import FileStorage
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage


@lru_cache()
def get_file_storage() -> FileStorage:
    settings = get_settings()
    provider = (settings.file_storage_provider or "gridfs").lower()
    gridfs_storage = GridFSFileStorage(mongodb=get_mongodb())

    if provider == "gridfs":
        return gridfs_storage
    if provider == "minio":
        from app.infrastructure.external.file.miniofile import MinIOFileStorage

        return MinIOFileStorage()
    if provider == "hybrid":
        from app.infrastructure.external.file.hybridfile import HybridFileStorage
        from app.infrastructure.external.file.miniofile import MinIOFileStorage

        return HybridFileStorage(MinIOFileStorage(), gridfs_storage)
    raise ValueError(f"Unsupported FILE_STORAGE_PROVIDER: {settings.file_storage_provider}")
