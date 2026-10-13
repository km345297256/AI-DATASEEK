import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.data_candidate_service import DataCandidateService, MAX_FILE_BYTES
from app.domain.models.data_candidate import CandidateInput, VersionInput
from app.domain.models.file import FileInfo
from app.interfaces.api.data_candidate_routes import owned_session


class Repository:
    def __init__(self):
        self.rows = {}

    async def put(self, owner, session, kind, key, payload):
        return self.rows.setdefault((owner, session, kind, key), payload)

    async def get(self, owner, session, kind, key):
        return self.rows.get((owner, session, kind, key))

    async def page(self, owner, session, kind, cursor=None):
        return [value for (o, s, k, key), value in sorted(self.rows.items())
                if (o, s, k) == (owner, session, kind) and (cursor is None or key > cursor)][:21]


def request(**kwargs):
    return CandidateInput(title='Research data', source_url='https://example.org/data?token=secret#part',
                          external_id=kwargs.pop('external_id', 'DS-1'), **kwargs)


def storage(data=b'scientific data', owner='owner'):
    info = FileInfo(file_id='file1', user_id=owner, size=len(data), file_path='/private/not-public', metadata={'secret': 'never'})
    async def read(file_id, user_id, *, offset, length):
        assert user_id == 'owner'
        return data[offset:offset+length], info
    return SimpleNamespace(get_file_info=AsyncMock(return_value=info), download_file_range=AsyncMock(side_effect=read))


@pytest.mark.parametrize('url', ['http://example.org', 'https://localhost/data', 'https://127.0.0.1',
    'https://user:pass@example.org', 'https://10.1.1.1', 'file:///private/data', 'https://example.org:8080',
    'https://host.local', 'https://example.org/\nsecret', 'https://example.org\\@evil.test', 'https://[::1]/'])
def test_reject_private_or_credential_references(url):
    with pytest.raises(ValidationError):
        CandidateInput(title='data', source_url=url, external_id='one')


def test_input_cannot_claim_verification_or_digest():
    with pytest.raises(ValidationError):
        CandidateInput(**request().model_dump(), provenance='official')
    with pytest.raises(ValidationError):
        VersionInput(file_ids=['file1'], sha256='a' * 64)
    for ids in [[], ['file1', 'file1'], ['/private/file'], ['x'] * 17]:
        with pytest.raises(ValidationError):
            VersionInput(file_ids=ids)


@pytest.mark.asyncio
async def test_candidate_identity_is_idempotent_and_partitioned():
    service = DataCandidateService(Repository(), storage())
    first = await service.create('owner', 's', request())
    repeat = await service.create('owner', 's', request())
    assert first == repeat and first.source_url == 'https://example.org/data'
    changed = await service.create('owner', 's', request().model_copy(update={'title': 'New observation'}))
    assert first.asset_id == changed.asset_id and first.candidate_id != changed.candidate_id
    assert (await service.list_candidates('other', 's')).candidates == []
    assert (await service.list_candidates('owner', 'other')).candidates == []
    with pytest.raises(NotFoundError):
        await service.register('other', 's', first.candidate_id, VersionInput(file_ids=['file1']))
    other = await service.create('owner', 's', request(external_id='DS-2'))
    assert other.asset_id != first.asset_id


@pytest.mark.asyncio
async def test_server_hashes_bytes_atomic_idempotent_and_no_private_fields():
    files = storage()
    service = DataCandidateService(Repository(), files)
    candidate = await service.create('owner', 's', request())
    version = await service.register('owner', 's', candidate.candidate_id, VersionInput(file_ids=['file1']))
    repeated = await service.register('owner', 's', candidate.candidate_id, VersionInput(file_ids=['file1']))
    assert version == repeated
    assert version.files[0].sha256 == hashlib.sha256(b'scientific data').hexdigest()
    assert version.acquisition.source_verified is False and version.retention == 'reference_only'
    assert not any(term in version.model_dump_json() for term in ['private', 'secret', 'file_path'])
    files.get_file_info.assert_awaited_with('file1', 'owner')
    assert (await service.list_versions('owner', 's', candidate.candidate_id)).versions == [version]
    service.files = storage(b'changed')
    updated = await service.register('owner', 's', candidate.candidate_id, VersionInput(file_ids=['file1']))
    assert updated.version_id != version.version_id
    assert len((await service.list_versions('owner', 's', candidate.candidate_id)).versions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['foreign', 'oversize', 'truncated', 'missing'])
async def test_failed_registration_never_creates_version(failure):
    files = storage(owner='other' if failure == 'foreign' else 'owner')
    if failure == 'oversize':
        files.get_file_info.return_value.size = MAX_FILE_BYTES + 1
    if failure == 'truncated':
        files.download_file_range = AsyncMock(return_value=(b'', files.get_file_info.return_value))
    if failure == 'missing':
        files.download_file_range = AsyncMock(side_effect=FileNotFoundError('/private/secret'))
    service = DataCandidateService(Repository(), files)
    candidate = await service.create('owner', 's', request())
    with pytest.raises((BadRequestError, NotFoundError)) as caught:
        await service.register('owner', 's', candidate.candidate_id, VersionInput(file_ids=['file1']))
    assert 'private' not in str(caught.value)
    assert (await service.list_versions('owner', 's', candidate.candidate_id)).versions == []
    if failure in ('foreign', 'oversize'):
        files.download_file_range.assert_not_awaited()


@pytest.mark.asyncio
async def test_pagination_does_not_lose_candidates():
    service = DataCandidateService(Repository(), storage())
    for i in range(23):
        await service.create('owner', 's', request(external_id=f'DS-{i}'))
    first = await service.list_candidates('owner', 's')
    second = await service.list_candidates('owner', 's', first.next_cursor)
    assert len(first.candidates) == 20 and len(second.candidates) == 3
    assert len({c.candidate_id for c in first.candidates + second.candidates}) == 23
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_every_route_requires_owned_session():
    from app.interfaces.api.data_candidate_routes import router
    assert len(router.routes) == 7
    for route in router.routes:
        assert any(dependency.call is owned_session for dependency in route.dependant.dependencies)
    agent = SimpleNamespace(get_session=AsyncMock(return_value=None))
    with pytest.raises(NotFoundError):
        await owned_session('s', SimpleNamespace(id='owner'), agent)
    agent.get_session.assert_awaited_once_with('s', 'owner')


@pytest.mark.asyncio
async def test_empty_and_multichunk_files_are_hashed_with_owned_bounded_reads():
    from app.application.services.data_candidate_service import CHUNK_BYTES
    for data in (b'', b'x' * (CHUNK_BYTES + 3)):
        files = storage(data)
        service = DataCandidateService(Repository(), files)
        candidate = await service.create('owner', 's', request())
        version = await service.register('owner', 's', candidate.candidate_id, VersionInput(file_ids=['file1']))
        assert version.files[0].sha256 == hashlib.sha256(data).hexdigest()
        assert all(call.kwargs['length'] <= CHUNK_BYTES for call in files.download_file_range.await_args_list)
        assert files.download_file_range.await_count == (2 if data else 1)


def test_existing_storage_identifiers_are_supported_without_accepting_paths():
    assert VersionInput(file_ids=['minio:abcd-1234', 'gridfs:abc123']).file_ids == ['gridfs:abc123', 'minio:abcd-1234']
    with pytest.raises(ValidationError):
        VersionInput(file_ids=['minio:/private/data'])
