"""Additive read-only dataset reference routing; ordinary storage is unchanged."""
from app.application.services.dataset_file_preview import DatasetFilePreviewService, PREFIX


class DatasetPreviewFileStorage:
    def __init__(self, storage, previews=None):
        self.storage = storage
        self.previews = previews or DatasetFilePreviewService()

    def __getattr__(self, name):
        return getattr(self.storage, name)

    def _reader(self, file_id):
        return self.previews if file_id.startswith(PREFIX) else self.storage

    async def get_file_info(self, file_id, user_id=None):
        return await self._reader(file_id).get_file_info(file_id, user_id)

    async def download_file(self, file_id, user_id=None):
        return await self._reader(file_id).download_file(file_id, user_id)

    async def download_file_range(self, file_id, user_id, *, offset, length):
        return await self._reader(file_id).download_file_range(file_id, user_id, offset=offset, length=length)

    async def delete_file(self, file_id, user_id):
        if file_id.startswith(PREFIX):
            return False  # Never mutate a dataset source or its registration.
        return await self.storage.delete_file(file_id, user_id)

    async def create_presigned_url(self, file_id, *args, **kwargs):
        if file_id.startswith(PREFIX):
            raise NotImplementedError("Dataset previews use authenticated application URLs")
        return await self.storage.create_presigned_url(file_id, *args, **kwargs)
