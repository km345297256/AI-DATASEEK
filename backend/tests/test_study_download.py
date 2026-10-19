import gzip
import hashlib
import httpx
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.infrastructure.external.data_sources.study_download import StudyDownloadClient, MAX_BYTES
from app.application.services.study_acquisition_service import StudyAcquisitionService
from app.domain.external.data_source import SourceConnectorError


@pytest.mark.asyncio
async def test_geo_lists_only_real_children_and_marks_processed_and_oversized():
    urls=[]
    def respond(request):
        urls.append(str(request.url))
        if request.method=='HEAD': return httpx.Response(200,headers={'content-length':str(MAX_BYTES+1) if 'RAW' in str(request.url) else '12'})
        listing='<a href="GSE1000_RAW.tar">raw</a><a href="https://evil.org/file.gz">bad</a><a href="../escape">bad</a>' if '/suppl/' in str(request.url) else '<a href="GSE1000_series_matrix.txt.gz">matrix</a>'
        return httpx.Response(200,text=listing)
    client=StudyDownloadClient(httpx.MockTransport(respond))
    rows=await client.list_files('ncbi-geo','GSE1000')
    assert len(rows)==2 and not rows[0]['available']
    assert rows[1]['kind']=='表达矩阵（处理后）'
    assert all(url.startswith('https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1000/') for url in urls)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['url','checksum','association','size',None])
async def test_ena_validates_source_association_and_paired_file_metadata(bad):
    path='ftp.sra.ebi.ac.uk/vol1/fastq/SRR000/SRR000001/SRR000001_1.fastq.gz'
    row={'experiment_accession':'SRX1','run_accession':'SRR000001', 'fastq_ftp':path, 'fastq_bytes':'10','fastq_md5':'a'*32}
    if bad=='url':row['fastq_ftp']='localhost/private.fastq.gz'
    if bad=='checksum':row['fastq_md5']='bad'
    if bad=='association':row['experiment_accession']='SRX2'
    if bad=='size':row['fastq_bytes']='-1'
    client=StudyDownloadClient(httpx.MockTransport(lambda r:httpx.Response(200,json=[row])))
    if bad:
        with pytest.raises(SourceConnectorError): await client.list_files('ncbi-sra','SRX1')
    else:
        files=await client.list_files('ncbi-sra','SRX1')
        assert files[0]['md5']=='a'*32 and files[0]['kind']=='原始测序 FASTQ'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['md5','length','gzip','redirect','oversize',None])
async def test_download_complete_file_checks(bad):
    content=gzip.compress(b'@r\nAC\n+\nII\n')
    if bad=='gzip':content=b'not gzip'
    record={'available':True,'url':'https://ftp.sra.ebi.ac.uk/vol1/fastq/x.fastq.gz','filename':'x.fastq.gz',
            'size':len(content),'md5':hashlib.md5(content).hexdigest()}
    if bad=='md5':record['md5']='f'*32
    if bad=='length':record['size']+=1
    response=httpx.Response(200,content=content)
    if bad=='redirect':response=httpx.Response(302,headers={'location':'http://localhost/'})
    if bad=='oversize':response=httpx.Response(200,headers={'content-length':str(MAX_BYTES+1)})
    client=StudyDownloadClient(httpx.MockTransport(lambda r:response))
    if bad:
        with pytest.raises(SourceConnectorError):await client.download(record)
    else:assert await client.download(record)==content


@pytest.mark.asyncio
async def test_acquire_rejects_foreign_or_unlisted_file_before_network():
    repository=SimpleNamespace(get=AsyncMock(return_value=None))
    sessions=SimpleNamespace(find_owned_by_id_and_user_id=AsyncMock(return_value=object()))
    client=SimpleNamespace(download=AsyncMock())
    service=StudyAcquisitionService(repository,None,sessions,client)
    with pytest.raises(SourceConnectorError): await service.acquire('o','s','ncbi-sra','SRX1',['forged'])
    repository.get.assert_awaited_once_with('o','s','study_download_option','forged')
    client.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_geo_raw_manifest_offers_only_matching_sample_files():
    def respond(request):
        if request.method=='HEAD':return httpx.Response(200,headers={'content-length':'100'})
        if str(request.url).endswith('filelist.txt'):
            return httpx.Response(200,text='Archive\tGSE1000_RAW.tar\tdate\t1000\nFile\tGSM15785.cel.gz\tdate\t100\nFile\t../secret.gz\tdate\t100\n')
        return httpx.Response(200,text='')
    client=StudyDownloadClient(httpx.MockTransport(respond))
    rows=await client.list_files('ncbi-geo','GSE1000')
    assert len(rows)==1
    assert rows[0]['url']=='https://ftp.ncbi.nlm.nih.gov/geo/samples/GSM15nnn/GSM15785/suppl/GSM15785.cel.gz'
    assert rows[0]['kind']=='原始样本文件（RAW 清单成员）'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None,'range','truncated','redirect'])
async def test_parallel_ranges_are_complete_and_verified(bad):
    payload=b'01234567' * 100000
    def respond(request):
        start,end=map(int,request.headers['range'][6:].split('-'))
        if bad=='redirect':return httpx.Response(302,headers={'location':'http://localhost/'})
        data=payload[start:end+1]
        if bad=='truncated':data=data[:-1]
        return httpx.Response(206,content=data,headers={'content-range': 'bytes 0-1/2' if bad=='range' else f'bytes {start}-{end}/{len(payload)}'})
    client=StudyDownloadClient(httpx.MockTransport(respond))
    if bad:
        with pytest.raises(SourceConnectorError):await client.ranged_download('https://ftp.ncbi.nlm.nih.gov/test',len(payload))
    else:assert await client.ranged_download('https://ftp.ncbi.nlm.nih.gov/test',len(payload))==payload
