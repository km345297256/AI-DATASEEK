"""Independent offline checks of bounded delivered-report content loading.

Only the storage boundary is mocked. No application tools, provider requests,
real object storage or source datasets are used.
"""
import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.file import FileInfo
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services import analysis_report_review as subject

USER = 'owner-fixture'
SESSION = 'session-fixture'


def manifest(body=b'# Complete report\nResult described.', *, identity='minio:object-fixture',
             path='/home/ubuntu/output/report.md', user=USER):
    return FileInfo(file_id=identity, file_path=path, filename=path.rsplit('/', 1)[-1],
        size=len(body), user_id=user, metadata={'source':'sandbox_artifact',
        'session_id':SESSION, 'artifact_size':len(body),
        'artifact_sha256':hashlib.sha256(body).hexdigest()})


class OwnerRangeStore:
    """Expose exactly the existing FileStorage owner/range API, never full read."""
    def __init__(self, files_and_bytes):
        self.objects={info.file_id:(info.model_copy(deep=True),body) for info,body in files_and_bytes}
        self.get_calls=[]
        self.range_calls=[]
        self.download_file=AsyncMock(side_effect=AssertionError('Full object fallback forbidden'))

    async def get_file_info(self, file_id, user_id=None):
        self.get_calls.append((file_id,user_id))
        assert user_id is not None, 'Caller must never bypass ownership with None'
        info,_body=self.objects[file_id]
        if info.user_id!=user_id:raise PermissionError('private owner and path details')
        return info.model_copy(deep=True)

    async def download_file_range(self, file_id, user_id, *, offset, length):
        self.range_calls.append((file_id,user_id,offset,length))
        info,body=self.objects[file_id]
        if info.user_id!=user_id:raise PermissionError('private owner and path details')
        return body[offset:offset+length],info.model_copy(deep=True)


async def load(info, storage, **kwargs):
    return (await subject.load_report_targets(files=[info], storage=storage,
        user_id=kwargs.get('user_id',USER),session_id=kwargs.get('session_id',SESSION),
        requirements=kwargs.get('requirements',())))[0]


@pytest.mark.asyncio
async def test_exact_owner_object_range_and_byte_version_are_bound():
    body='标题\r\nC/N = TOC/TN × 14/12；末尾合法 |r'.encode()
    info=manifest(body,user=None)
    stored=info.model_copy(update={'user_id':USER},deep=True)
    storage=OwnerRangeStore([(stored,body)])
    target=await load(info,storage)
    assert target.reason=='ready' and target.read_complete
    assert target.text.encode()==body and target.sha256==hashlib.sha256(body).hexdigest()
    assert storage.get_calls==[(info.file_id,USER)]
    assert storage.range_calls==[(info.file_id,USER,0,len(body))]
    storage.download_file.assert_not_awaited()
    assert 'object-fixture' not in json.dumps(target.payload())
    assert '/home/ubuntu' not in repr(target) and '标题' not in repr(target)
    with pytest.raises(FrozenInstanceError):target.text='mutated'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    lambda i:i.model_copy(update={'file_id':None}),
    lambda i:i.model_copy(update={'file_id':'dataset-preview:source'}),
    lambda i:i.model_copy(update={'user_id':'other-user'}),
    lambda i:i.model_copy(update={'file_path':'/home/ubuntu/source/report.md'}),
    lambda i:i.model_copy(update={'file_path':'/home/ubuntu/output/../report.md'}),
    lambda i:i.model_copy(update={'file_path':'/home/ubuntu/output/./report.md'}),
    lambda i:i.model_copy(update={'file_path':'/home/ubuntu/output/a\\report.md'}),
    lambda i:i.model_copy(update={'size':0}),
    lambda i:i.model_copy(update={'metadata':{**i.metadata,'source':'private_spill'}}),
    lambda i:i.model_copy(update={'metadata':{**i.metadata,'session_id':'other-session'}}),
    lambda i:i.model_copy(update={'metadata':{**i.metadata,'artifact_size':True}}),
    lambda i:i.model_copy(update={'metadata':{**i.metadata,'artifact_size':i.size+1}}),
    lambda i:i.model_copy(update={'metadata':{**i.metadata,'artifact_sha256':'not-a-digest'}}),
])
async def test_invalid_manifest_never_reaches_storage(mutation):
    info=mutation(manifest())
    storage=OwnerRangeStore([])
    target=await load(info,storage)
    assert target.reason=='manifest_invalid' and target.text is None and not target.read_complete
    assert not storage.get_calls and not storage.range_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['user_id','session_id'])
async def test_missing_caller_identity_never_uses_optional_access_bypass(key):
    storage=OwnerRangeStore([])
    target=await load(manifest(),storage,**{key:''})
    assert target.reason=='manifest_invalid' and not storage.get_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[
    ('file_id','minio:different-object-same-name'),('user_id','different-owner'),
    ('user_id',None),('size',999),('source','private_spill'),('session_id','different-session'),
    ('artifact_size',999),('artifact_sha256','f'*64),
])
@pytest.mark.parametrize('phase',['before','range'])
async def test_manifest_changes_before_or_during_read_are_not_full_coverage(field,value,phase):
    body=b'original body';info=manifest(body);storage=OwnerRangeStore([(info,body)])
    changed=info.model_copy(deep=True)
    if field in ('source','session_id','artifact_size','artifact_sha256'):changed.metadata[field]=value
    else:setattr(changed,field,value)
    if phase=='before':storage.get_file_info=AsyncMock(return_value=changed)
    else:storage.download_file_range=AsyncMock(return_value=(body,changed))
    target=await load(info,storage)
    assert target.reason=='version_unverified' and target.text is None and not target.read_complete
    if phase=='before':assert not storage.range_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('actual',[b'origina',b'original-extra',b'different',bytearray(b'original')])
async def test_short_overlong_changed_or_nonbytes_content_is_never_promoted(actual):
    info=manifest(b'original');storage=OwnerRangeStore([(info,b'original')])
    storage.download_file_range=AsyncMock(return_value=(actual,info.model_copy(deep=True)))
    target=await load(info,storage)
    assert target.reason=='version_unverified' and target.text is None and not target.read_complete


@pytest.mark.asyncio
async def test_object_metadata_and_bytes_cannot_follow_a_mutated_expected_manifest():
    old=b'old report';new=b'new report';info=manifest(old);storage=OwnerRangeStore([(info,old)])
    changed=manifest(new)
    async def get_info(*_args):
        # The caller's mutable FileInfo must not become a moving expected
        # target across awaits. Returned new metadata + old bytes is invalid.
        info.metadata['artifact_sha256']=changed.metadata['artifact_sha256']
        return changed.model_copy(deep=True)
    storage.get_file_info=get_info
    storage.download_file_range=AsyncMock(return_value=(old,changed.model_copy(deep=True)))
    target=await load(info,storage)
    assert target.reason=='version_unverified' and target.text is None


@pytest.mark.asyncio
async def test_all_candidates_are_frozen_before_first_read_not_each_iteration():
    body=b'original report';first=manifest(body);second=manifest(body,identity='minio:second')
    storage=OwnerRangeStore([(first,body),(second,body)])
    original_get=storage.get_file_info
    async def get_info(file_id,user_id):
        # A session update while the first object is awaited must not redirect
        # a later candidate to another object/version.
        if file_id==first.file_id:
            second.file_id='minio:replacement'
            second.metadata['artifact_sha256']='f'*64
        return await original_get(file_id,user_id)
    storage.get_file_info=get_info
    targets=await subject.load_report_targets(files=[first,second],storage=storage,user_id=USER,session_id=SESSION)
    assert [t.reason for t in targets]==['ready','ready']
    assert storage.get_calls==[(first.file_id,USER),('minio:second',USER)]
    assert targets[1].sha256==hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_per_file_limit_is_checked_before_io_and_exact_boundary_is_readable():
    body=b'a'*subject.MAX_REPORT_BYTES;at=manifest(body);over=manifest(body+b'b',identity='minio:over')
    storage=OwnerRangeStore([(at,body),(over,body+b'b')])
    targets=await subject.load_report_targets(files=[at,over],storage=storage,user_id=USER,session_id=SESSION)
    assert targets[0].reason=='ready' and targets[1].reason=='report_read_limit'
    assert len(storage.range_calls)==1 and storage.range_calls[0][3]==subject.MAX_REPORT_BYTES


@pytest.mark.asyncio
async def test_total_byte_budget_and_report_count_never_trigger_extra_reads(monkeypatch):
    monkeypatch.setattr(subject,'MAX_REPORT_BYTES',4)
    monkeypatch.setattr(subject,'MAX_REPORT_BATCH_BYTES',8)
    monkeypatch.setattr(subject,'MAX_REPORT_COUNT',3)
    body=b'abcd';infos=[manifest(body,identity=f'minio:{i}') for i in range(5)]
    storage=OwnerRangeStore([(i,body) for i in infos])
    targets=await subject.load_report_targets(files=infos,storage=storage,user_id=USER,session_id=SESSION)
    assert [t.reason for t in targets]==['ready','ready','report_read_limit','report_read_limit','report_read_limit']
    assert sum(c[3] for c in storage.range_calls)==8 and len(storage.range_calls)==2
    monkeypatch.setattr(subject,'MAX_REPORT_BATCH_BYTES',100)
    storage=OwnerRangeStore([(i,body) for i in infos])
    targets=await subject.load_report_targets(files=infos,storage=storage,user_id=USER,session_id=SESSION)
    assert [t.reason for t in targets]==['ready']*3+['report_read_limit']*2
    assert len(storage.range_calls)==3


@pytest.mark.asyncio
async def test_failed_reads_still_consume_reserved_byte_budget(monkeypatch):
    monkeypatch.setattr(subject,'MAX_REPORT_BYTES',4);monkeypatch.setattr(subject,'MAX_REPORT_BATCH_BYTES',4)
    body=b'abcd';a,b=manifest(body),manifest(body,identity='minio:next');storage=OwnerRangeStore([(a,body),(b,body)])
    storage.get_file_info=AsyncMock(side_effect=TimeoutError('private endpoint'))
    targets=await subject.load_report_targets(files=[a,b],storage=storage,user_id=USER,session_id=SESSION)
    assert [t.reason for t in targets]==['report_read_unavailable','report_read_limit']
    assert storage.get_file_info.await_count==1 and not storage.range_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix',['pdf','docx','html'])
async def test_unsupported_report_format_stays_explicitly_unverified_without_io(suffix):
    info=manifest(path=f'/home/ubuntu/output/report.{suffix}');storage=OwnerRangeStore([])
    target=await load(info,storage)
    assert target.reason=='unsupported_report_format' and target.text is None and not storage.get_calls


@pytest.mark.asyncio
async def test_machine_json_is_a_separate_semantic_target_without_report_obligation():
    body=b'{"metric": 1}'
    info=manifest(body,path='/home/ubuntu/output/metrics.json');storage=OwnerRangeStore([(info,body)])
    result=await subject.load_report_targets(files=[info],storage=storage,user_id=USER,session_id=SESSION)
    assert len(result)==1 and result[0].target_kind=='structured' and result[0].format=='json'
    assert result[0].text.encode()==body and result[0].reason=='ready'
    assert subject.artifact_kind(info.file_path)=='report'  # Existing delivery-kind convention is unchanged.


@pytest.mark.asyncio
@pytest.mark.parametrize('contract',[
    DeliverableRequirement(kind='report',formats=['json']),
    DeliverableRequirement(kind='report',output_paths=['/home/ubuntu/output/report.json']),
])
async def test_explicit_json_report_contract_has_complete_version_bound_content(contract):
    body=b'{"claim": "descriptive"}'
    info=manifest(body,path='/home/ubuntu/output/report.json')
    storage=OwnerRangeStore([(info,body)])
    target=await load(info,storage,requirements=[contract])
    assert target.reason=='ready' and target.text.encode()==body
    assert target.read_complete and storage.get_calls and storage.range_calls


@pytest.mark.asyncio
async def test_exact_markdown_table_contract_only_excludes_that_table():
    table=manifest(b'|A|B|\n|---|---|\n|1|2|',path='/home/ubuntu/output/table.md')
    body=b'Report interpretation.';report=manifest(body,identity='minio:report')
    storage=OwnerRangeStore([(report,body)])
    requirement=DeliverableRequirement(kind='table',output_paths=[table.file_path])
    targets=await subject.load_report_targets(files=[table,report],storage=storage,
        user_id=USER,session_id=SESSION,requirements=[requirement])
    assert len(targets)==1 and targets[0].reason=='ready' and targets[0].text==body.decode()
    assert storage.get_calls==[(report.file_id,USER)]


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix',['md','markdown'])
async def test_unambiguous_table_format_contract_does_not_add_a_report_obligation(suffix):
    info=manifest(b'|A|B|\n|---|---|\n|1|2|',path=f'/home/ubuntu/output/table.{suffix}')
    storage=OwnerRangeStore([])
    targets=await subject.load_report_targets(files=[info],storage=storage,user_id=USER,session_id=SESSION,
        requirements=[DeliverableRequirement(kind='table',formats=[suffix])])
    assert targets==() and not storage.get_calls and not storage.range_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('report_contract',[
    DeliverableRequirement(kind='report'),
    DeliverableRequirement(kind='report',formats=['md']),
    DeliverableRequirement(kind='report',output_paths=['/home/ubuntu/output/report.md']),
])
async def test_table_format_does_not_silently_exclude_an_independent_report(report_contract):
    body=b'Report interpretation.';info=manifest(body);storage=OwnerRangeStore([(info,body)])
    targets=await subject.load_report_targets(files=[info],storage=storage,user_id=USER,session_id=SESSION,
        requirements=[DeliverableRequirement(kind='table',formats=['md']),report_contract])
    assert len(targets)==1 and targets[0].text==body.decode() and targets[0].reason=='ready'


@pytest.mark.asyncio
async def test_conflicting_exact_table_and_report_path_is_not_excluded_from_review():
    body=b'Report interpretation.';info=manifest(body);storage=OwnerRangeStore([(info,body)])
    targets=await subject.load_report_targets(files=[info],storage=storage,user_id=USER,session_id=SESSION,
        requirements=[DeliverableRequirement(kind=kind,output_paths=[info.file_path]) for kind in ('table','report')])
    assert len(targets)==1 and targets[0].reason=='ready'


@pytest.mark.asyncio
async def test_separate_markdown_table_and_report_paths_select_only_the_report():
    table=manifest(b'|A|B|\n|---|---|\n|1|2|',path='/home/ubuntu/output/table.md')
    body=b'Report interpretation.';report=manifest(body,identity='minio:report')
    storage=OwnerRangeStore([(report,body)])
    targets=await subject.load_report_targets(files=[table,report],storage=storage,user_id=USER,session_id=SESSION,
        requirements=[DeliverableRequirement(kind='table',formats=['md']),
                      DeliverableRequirement(kind='report',output_paths=[report.file_path])])
    assert len(targets)==1 and targets[0].file_id==report.file_id and targets[0].reason=='ready'
    assert storage.get_calls==[(report.file_id,USER)]


@pytest.mark.asyncio
async def test_no_report_and_only_code_or_image_do_not_read_or_create_targets():
    storage=OwnerRangeStore([])
    files=[manifest(path='/home/ubuntu/output/code.py'),manifest(path='/home/ubuntu/output/plot.png')]
    assert await subject.load_report_targets(files=files,storage=storage,user_id=USER,session_id=SESSION)==()
    assert await subject.load_report_targets(files=[],storage=storage,user_id=USER,session_id=SESSION)==()
    assert not storage.get_calls and not storage.range_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('body,reason',[
    (b'\xff\xfe','invalid_report_encoding'),(b' \n\r\t','invalid_report_text'),
    (b'text\x00more','invalid_report_text'),
    (b'/Users/private-person/source.csv','protected_report_content'),
    (b'password=private-sentinel','protected_report_content'),
    (b'Authorization: Bearer private-sentinel','protected_report_content'),
    (b'https://user:private-sentinel@example.test/report','protected_report_content'),
])
async def test_invalid_encoding_or_protected_body_is_not_mutated_into_verified_text(body,reason,caplog):
    info=manifest(body);storage=OwnerRangeStore([(info,body)])
    target=await load(info,storage)
    assert target.reason==reason and target.text is None and target.read_complete
    assert target.sha256==hashlib.sha256(body).hexdigest()
    assert 'private-sentinel' not in str(target.payload()) and 'private-person' not in repr(target)
    assert 'private-sentinel' not in caplog.text


@pytest.mark.asyncio
async def test_storage_exceptions_have_fixed_private_free_diagnostics(caplog):
    storage=OwnerRangeStore([]);storage.get_file_info=AsyncMock(side_effect=RuntimeError('secret-user /Users/private-path?token=do-not-echo'))
    target=await load(manifest(),storage)
    assert target.reason=='report_read_unavailable' and target.text is None
    text=str(target.payload())+repr(target)+caplog.text
    assert 'secret-user' not in text and 'private-path' not in text and 'do-not-echo' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['get','range'])
async def test_cancellation_propagates_before_later_report_reads(phase):
    body=b'body';a,b=manifest(body),manifest(body,identity='minio:later');storage=OwnerRangeStore([(a,body),(b,body)])
    if phase=='get':storage.get_file_info=AsyncMock(side_effect=asyncio.CancelledError())
    else:storage.download_file_range=AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await subject.load_report_targets(files=[a,b],storage=storage,user_id=USER,session_id=SESSION)
    if phase=='get':assert storage.get_file_info.await_count==1
    else:assert storage.get_calls==[(a.file_id,USER)] and storage.download_file_range.await_count==1


@pytest.mark.asyncio
async def test_read_timeout_never_becomes_ready(monkeypatch):
    monkeypatch.setattr(subject,'REPORT_READ_TIMEOUT',0.01)
    storage=OwnerRangeStore([])
    async def slow(*_args):await asyncio.sleep(1)
    storage.get_file_info=slow
    target=await load(manifest(),storage)
    assert target.reason=='report_read_unavailable' and target.text is None


@pytest.mark.asyncio
async def test_batch_deadline_prevents_later_reads_after_first_report_timeout(monkeypatch):
    monkeypatch.setattr(subject,'REPORT_READ_TIMEOUT',0.01)
    a,b=manifest(),manifest(identity='minio:later');storage=OwnerRangeStore([])
    async def slow(*_args):
        await asyncio.sleep(1)
    storage.get_file_info=AsyncMock(side_effect=slow)
    targets=await subject.load_report_targets(files=[a,b],storage=storage,user_id=USER,session_id=SESSION)
    assert [t.reason for t in targets]==['report_read_unavailable']*2
    assert all(t.text is None and not t.read_complete for t in targets)
    storage.get_file_info.assert_awaited_once_with(a.file_id,USER)
    assert not storage.range_calls


@pytest.mark.asyncio
async def test_blocks_cover_multibyte_full_body_including_tail_without_byte_gaps():
    text='甲'*3000+'β'*3000+'尾部主张 |r';body=text.encode();info=manifest(body);storage=OwnerRangeStore([(info,body)])
    target=await load(info,storage);blocks=target.blocks()
    assert len(blocks)==3 and ''.join(b['text'] for b in blocks)==text
    assert blocks[0]['byte_start']==0 and blocks[-1]['byte_end']==len(body)
    assert all(a['byte_end']==b['byte_start'] for a,b in zip(blocks,blocks[1:]))
    assert blocks[-1]['text']=='尾部主张 |r'
    fitted=subject.fit_report_targets([target],text_budget=len(text))
    assert fitted[0].text==text
    short=subject.fit_report_targets([target],text_budget=len(text)-1)[0]
    assert short.text is None and short.read_complete and short.reason=='report_context_limit'
    assert short.size==len(body) and short.sha256==target.sha256


def test_context_budget_keeps_whole_targets_and_rechecks_byte_identity():
    def target(text,name):
        body=text.encode();return subject.ReportTarget(name,len(body),hashlib.sha256(body).hexdigest(),text,'ready',True)
    first,second=target('abc','report_1'),target('def','report_2')
    fit=subject.fit_report_targets([first,second],text_budget=5)
    assert fit[0].text=='abc' and fit[1].text is None and fit[1].reason=='report_context_limit'
    changed=subject.fit_report_targets([replace(first,text='abd')],text_budget=100)[0]
    assert changed.text is None and changed.reason=='version_unverified'
