"""Authoritative connector observations, distinct from manually linked file versions."""
from app.application.errors.exceptions import BadRequestError
from app.application.services.data_candidate_service import DataCandidateService
from app.domain.models.data_candidate import DataCandidate, DiscoveryPage, identity
from app.domain.models.source_connector import AcquisitionPlan, NcbiObservation, NcbiSearch


class NcbiDiscoveryService:
    def __init__(self, repository, connector, *, files=None, sessions=None, downloader=None):
        self.repository, self.connector = repository, connector
        self.files, self.sessions, self.downloader = files, sessions, downloader
        self.candidates = DataCandidateService(repository, None)

    async def _record(self, owner, session, observation: NcbiObservation):
        assembly = observation.assembly
        asset_id = identity(['ncbi-datasets-v2', owner, assembly.accession.split('.')[0]])
        evidence = observation.model_dump(exclude={'retrieved_at'})
        candidate_id = identity([asset_id, evidence])
        candidate = DataCandidate(candidate_id=candidate_id, asset_id=asset_id,
            title=f'{assembly.organism_name} · {assembly.accession}'[:200],
            source_url=f'https://www.ncbi.nlm.nih.gov/datasets/genome/{assembly.accession}/',
            external_id=assembly.accession, created_at=observation.retrieved_at,
            provenance='ncbi_datasets', discovery=observation)
        return DataCandidate.model_validate(await self.repository.put(owner, session, 'candidate', candidate_id, candidate.model_dump()))

    async def search(self, owner: str, session: str, request: NcbiSearch) -> DiscoveryPage:
        page = await self.connector.search(request)
        candidates = [await self._record(owner, session, observation) for observation in page.observations]
        return DiscoveryPage(candidates=candidates, total_count=page.total_count, next_page_token=page.next_page_token)

    async def describe(self, owner: str, session: str, accession: str) -> DataCandidate:
        observation = await self.connector.describe(accession)
        return await self._record(owner, session, observation)

    async def acquire(self, owner: str, session: str, accession: str):
        from io import BytesIO
        from app.infrastructure.external.data_sources.ncbi_download import download_package
        from app.infrastructure.external.file.factory import get_file_storage
        from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
        from app.application.services.file_service import FileService
        from app.domain.models.data_connector import ConnectorFile
        sessions = self.sessions or MongoSessionRepository()
        if await sessions.find_by_id_and_user_id(session, owner) is None:
            raise BadRequestError('任务不存在或无权访问')
        files = self.files or FileService(file_storage=get_file_storage())
        # Reuse only a successfully persisted file owned by this user/session.
        previous = await self.repository.get(owner, session, 'acquired_package', accession)
        if previous:
            info = await files.get_file_info(previous['file_id'], owner)
            if info and info.user_id == owner and info.size == previous['size'] and (info.metadata or {}).get('sha256') == previous['sha256']:
                await sessions.upsert_connector_file(session, owner, info)
                return ConnectorFile.model_validate(previous)
        candidate = await self.describe(owner, session, accession)
        content, digest, source_url = await (self.downloader or download_package)(accession)
        info = await files.upload_file(BytesIO(content), f'{accession}_genomic.zip', owner, 'application/zip',
            metadata={'source': 'data_connector', 'connector_id': 'ncbi-datasets-v2',
                      'accession': accession, 'candidate_id': candidate.candidate_id,
                      'source_url': source_url, 'sha256': digest})
        await sessions.upsert_connector_file(session, owner, info)
        receipt = ConnectorFile(title=candidate.title, file_id=info.file_id, filename=info.filename, size=info.size,
                                sha256=digest, source_url=source_url)
        await self.repository.save_acquisition(owner, session, accession, receipt.model_dump())
        return receipt

    async def plan_acquisition(self, owner: str, session: str, candidate_id: str) -> AcquisitionPlan:
        candidate = await self.candidates.candidate(owner, session, candidate_id)
        if candidate.discovery is None or candidate.provenance != 'ncbi_datasets':
            raise BadRequestError('请先通过 NCBI 连接器取得官方元数据')
        assembly = candidate.discovery.assembly
        return AcquisitionPlan(candidate_id=candidate.candidate_id, accession=assembly.accession,
            metadata_sha256=candidate.discovery.report_sha256, source_url=candidate.source_url,
            reported_sequence_bases=assembly.total_sequence_length)


def get_ncbi_discovery_service():
    from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
    from app.infrastructure.external.data_sources.ncbi import NcbiDatasetsConnector
    return NcbiDiscoveryService(MongoDataCandidateRepository(), NcbiDatasetsConnector())
