"""Owner/session-scoped file choices and persisted study download receipts."""
import hashlib
import time
from io import BytesIO
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_connector import ConnectorResource, ConnectorResult, ConnectorFile
from app.domain.models.data_candidate import identity
from app.infrastructure.external.data_sources.study_download import StudyDownloadClient


class StudyAcquisitionService:
    def __init__(self, repository, files, sessions, client=None):
        self.repository, self.files, self.sessions = repository, files, sessions
        self.client = client or StudyDownloadClient()

    async def list_files(self, owner, session, connector, accession):
        records = await self.client.list_files(connector, accession)
        resources = []
        for record in records:
            saved = await self.repository.put(owner, session, 'study_download_option', record['id'], record)
            resources.append(ConnectorResource(id=saved['id'], external_id=accession, title=saved['filename'],
                source_url=saved['url'], retrieved_at=int(time.time()), metadata={k:v for k,v in saved.items() if k not in ('url', 'id')}))
        return ConnectorResult(connector_id=connector, connector_version='1.0.0', operation='list_files', resources=resources)

    async def acquire(self, owner, session, connector, accession, file_ids):
        if await self.sessions.find_owned_by_id_and_user_id(session, owner) is None:
            raise SourceConnectorError('invalid_file_selection')
        receipts = []
        for key in file_ids:
            record = await self.repository.get(owner, session, 'study_download_option', key)
            if not record or record['connector_id'] != connector or record['accession'] != accession:
                raise SourceConnectorError('invalid_file_selection')
            previous = await self.repository.get(owner, session, 'acquired_package', key)
            if previous:
                info = await self.files.get_file_info(previous['file_id'], owner)
                if info and info.user_id == owner and info.size == previous['size'] and (info.metadata or {}).get('sha256') == previous['sha256']:
                    receipts.append(ConnectorFile.model_validate(previous))
                    await self.sessions.upsert_connector_file(session, owner, info)
                    continue
            data = await self.client.download(record)
            digest = hashlib.sha256(data).hexdigest()
            # Prefix prevents collisions between datasets with generic names such as matrix.mtx.gz.
            filename = accession + '_' + identity(record['url'])[:8] + '_' + record['filename']
            filename = filename if len(filename) <= 200 else accession + '_' + identity(record['url'])[:16] + '_' + record['filename'][-120:]
            info = await self.files.upload_file(BytesIO(data), filename, owner, 'application/gzip' if filename.endswith('.gz') else 'application/octet-stream',
                metadata={'source': 'data_connector', 'connector_id': connector, 'accession': accession,
                          'source_url': record['url'], 'sha256': digest, 'provider_md5': record['md5'], 'data_kind': record['kind']})
            await self.sessions.upsert_connector_file(session, owner, info)
            receipt = ConnectorFile(title=record['kind'], file_id=info.file_id, filename=info.filename,
                                    size=len(data), sha256=digest, source_url=record['url'])
            await self.repository.save_acquisition(owner, session, key, receipt.model_dump())
            receipts.append(receipt)
        return ConnectorResult(connector_id=connector, connector_version='1.0.0', operation='acquire',
                               kind='files', resources=[], files=receipts, files_downloaded=True)


def get_study_acquisition_service():
    from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
    from app.application.services.file_service import FileService
    from app.infrastructure.external.file.factory import get_file_storage
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    return StudyAcquisitionService(MongoDataCandidateRepository(), FileService(file_storage=get_file_storage()), MongoSessionRepository())
