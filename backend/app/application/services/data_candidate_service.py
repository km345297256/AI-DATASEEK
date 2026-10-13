"""Register user-provided provenance and bounded server-computed content receipts."""
import hashlib
import time
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.models.data_candidate import (
    AcquisitionReceipt, CandidateInput, CandidatePage, DataCandidate, DataVersion,
    VersionFile, VersionInput, VersionPage, identity,
)

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class DataCandidateService:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    async def create(self, owner: str, session: str, request: CandidateInput) -> DataCandidate:
        asset_id = identity(["user_reference", owner, request.source_url, request.external_id])
        candidate_id = identity([asset_id, request.model_dump()])
        candidate = DataCandidate(**request.model_dump(), candidate_id=candidate_id,
                                  asset_id=asset_id, created_at=int(time.time()))
        return DataCandidate.model_validate(await self.repository.put(owner, session, "candidate", candidate_id, candidate.model_dump()))

    async def list_candidates(self, owner, session, cursor=None):
        rows = await self.repository.page(owner, session, "candidate", cursor)
        return CandidatePage(candidates=rows[:20], next_cursor=rows[19]["candidate_id"] if len(rows) > 20 else None)

    async def candidate(self, owner, session, candidate_id):
        row = await self.repository.get(owner, session, "candidate", candidate_id)
        if row is None:
            raise NotFoundError("数据线索不存在")
        return DataCandidate.model_validate(row)

    async def list_versions(self, owner, session, candidate_id, cursor=None):
        await self.candidate(owner, session, candidate_id)
        rows = await self.repository.page(owner, session, "version:" + candidate_id, cursor)
        return VersionPage(versions=rows[:20], next_cursor=rows[19]["version_id"] if len(rows) > 20 else None)

    async def register(self, owner: str, session: str, candidate_id: str, request: VersionInput) -> DataVersion:
        candidate = await self.candidate(owner, session, candidate_id)
        manifest = []
        try:
            infos = [await self.files.get_file_info(file_id, owner) for file_id in request.file_ids]
            if any(info is None or info.user_id != owner for info in infos):
                raise NotFoundError("文件不存在或不可访问")
            if any(info.size is None or not 0 <= info.size <= MAX_FILE_BYTES for info in infos):
                raise BadRequestError("登记的每个文件不得超过 64 MiB")
            if sum(info.size for info in infos) > MAX_TOTAL_BYTES:
                raise BadRequestError("一次登记的文件总量不得超过 128 MiB")
            for file_id, info in zip(request.file_ids, infos):
                digest = hashlib.sha256()
                # Zero-byte objects still undergo an owned read. No raw storage path is exposed.
                for offset in range(0, max(1, info.size), CHUNK_BYTES):
                    length = min(CHUNK_BYTES, info.size - offset)
                    block, current = await self.files.download_file_range(file_id, owner, offset=offset, length=length)
                    if current.user_id != owner or current.size != info.size or len(block) != length:
                        raise BadRequestError("文件在登记期间发生变化或读取不完整，请重试")
                    digest.update(block)
                manifest.append(VersionFile(file_id=file_id, size=info.size, sha256=digest.hexdigest()))
        except (PermissionError, FileNotFoundError, ValueError):
            raise NotFoundError("文件不存在或不可访问") from None
        version_id = identity([candidate_id, [item.model_dump() for item in manifest]])
        version = DataVersion(version_id=version_id, candidate_id=candidate_id, asset_id=candidate.asset_id,
                              files=manifest, acquisition=AcquisitionReceipt(recorded_at=int(time.time())))
        return DataVersion.model_validate(await self.repository.put(owner, session, "version:" + candidate_id,
                                                                    version_id, version.model_dump()))
