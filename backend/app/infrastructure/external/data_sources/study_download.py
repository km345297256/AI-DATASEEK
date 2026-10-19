"""Fixed-origin GEO supplementary files and ENA mirrored SRA FASTQ downloads."""
import asyncio
import csv
import gzip
import hashlib
import json
import re
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from pydantic import Field
from app.domain.models.source_connector import SourceRecord
from app.domain.models.data_candidate import identity
from app.domain.external.data_source import SourceConnectorError

MAX_BYTES = 64 * 1024 * 1024
SAFE_NAME = r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}'


class StudyFilesInput(SourceRecord):
    accession: str = Field(pattern=r'^(?:GSE[0-9]+|[SED]R[XR][0-9]+)$')


class StudyAcquireInput(StudyFilesInput):
    file_ids: list[str] = Field(min_length=1, max_length=1, description='从 list_files 返回的文件 id 中选择一个；逐文件下载以显示独立进度和错误')


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href', '')
        if tag == 'a' and re.fullmatch(SAFE_NAME, href) and href != 'filelist.txt':
            self.names.append(href)


class StudyDownloadClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def get(self, url, *, limit=2 * 1024 * 1024, missing_ok=False):
        try:
            async with asyncio.timeout(45):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=25) as client:
                    async with client.stream('GET', url) as response:
                        if missing_ok and response.status_code == 404:
                            return b''
                        if response.status_code == 429:
                            raise SourceConnectorError('rate_limited')
                        if response.status_code != 200:
                            raise SourceConnectorError('download_timeout_or_unavailable')
                        if int(response.headers.get('content-length', 0)) > limit:
                            raise SourceConnectorError('download_too_large')
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(data) + len(chunk) > limit:
                                raise SourceConnectorError('download_too_large')
                            data.extend(chunk)
                        return bytes(data)
        except (httpx.HTTPError, TimeoutError):
            raise SourceConnectorError('download_timeout_or_unavailable') from None
        except (ValueError, UnicodeError):
            raise SourceConnectorError('invalid_response') from None

    async def size(self, url):
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=10) as client:
                response = await client.head(url)
                if response.status_code == 200:
                    value = response.headers.get('content-length')
                    return int(value) if value and value.isdigit() else None
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def record(connector, accession, url, size, kind, md5=None, run=None):
        filename = urlsplit(url).path.rsplit('/', 1)[-1]
        return dict(id=identity([connector, accession, url, size, md5]), connector_id=connector,
            accession=accession, filename=filename, url=url, size=size, kind=kind, md5=md5, run_accession=run,
            available=size is None or 0 < size <= MAX_BYTES,
            unavailable_reason='文件超过 64 MiB 单文件下载上限' if size and size > MAX_BYTES else '')

    async def list_files(self, connector, accession):
        if connector == 'ncbi-geo':
            if not re.fullmatch(r'GSE[0-9]+', accession):
                raise SourceConnectorError('invalid_study_accession')
            group = accession[:-3] + 'nnn' if len(accession) > 6 else 'GSEnnn'
            base = f'https://ftp.ncbi.nlm.nih.gov/geo/series/{group}/{accession}/'
            rows = []
            for folder in ('suppl', 'matrix'):
                body = await self.get(base + folder + '/', missing_ok=True)
                parser = Links()
                parser.feed(body.decode('utf-8', errors='replace'))
                names = list(dict.fromkeys(parser.names))[:20-len(rows)]
                # Bounded concurrency and source directory; never follow arbitrary listing links.
                for start in range(0, len(names), 5):
                    batch = names[start:start+5]
                    sizes = await asyncio.gather(*(self.size(base + folder + '/' + n) for n in batch))
                    for name, size in zip(batch, sizes):
                        kind = '表达矩阵（处理后）' if folder == 'matrix' else '原始数据包' if name.endswith('_RAW.tar') else '作者补充数据（类型以来源说明为准）'
                        rows.append(self.record(connector, accession, base + folder + '/' + name, size, kind))
            # The official RAW archive manifest identifies individual sample files.
            # Offer reachable sample copies as well, so users need not fetch a whole archive.
            manifest = await self.get(base + 'suppl/filelist.txt', missing_ok=True)
            in_raw = False
            for entry in csv.reader(manifest.decode('utf-8', errors='replace').splitlines(), delimiter='\t'):
                if len(rows) >= 20:
                    break
                if len(entry) < 4:
                    continue
                if entry[0] == 'Archive':
                    in_raw = entry[1].endswith('_RAW.tar')
                    continue
                match = re.fullmatch(r'(GSM[0-9]+)[._][A-Za-z0-9._-]+', entry[1])
                if not in_raw or entry[0] != 'File' or not match or not re.fullmatch(SAFE_NAME, entry[1]):
                    continue
                sample = match[1]
                sample_group = sample[:-3] + 'nnn' if len(sample) > 6 else 'GSMnnn'
                url = f'https://ftp.ncbi.nlm.nih.gov/geo/samples/{sample_group}/{sample}/suppl/{entry[1]}'
                size = await self.size(url)
                if size is not None:
                    rows.append(self.record(connector, accession, url, size, '原始样本文件（RAW 清单成员）'))
            return rows
        if connector != 'ncbi-sra' or not re.fullmatch(r'[SED]R[XR][0-9]+', accession):
            raise SourceConnectorError('invalid_study_accession')
        url = ('https://www.ebi.ac.uk/ena/portal/api/filereport?accession=' + accession
               + '&result=read_run&fields=run_accession,experiment_accession,fastq_ftp,fastq_bytes,fastq_md5&format=json')
        try:
            data = json.loads(await self.get(url))
            if not isinstance(data, list):
                raise ValueError()
            rows = []
            for entry in data:
                if accession not in (entry['experiment_accession'], entry['run_accession']):
                    raise ValueError()
                if not entry.get('fastq_ftp'):
                    continue
                paths, sizes, checksums = [entry[key].split(';') for key in ('fastq_ftp', 'fastq_bytes', 'fastq_md5')]
                if not len(paths) == len(sizes) == len(checksums):
                    raise ValueError()
                for path, size, checksum in zip(paths, sizes, checksums):
                    if not re.fullmatch(r'ftp\.sra\.ebi\.ac\.uk/vol1/fastq/[A-Za-z0-9/_-]+\.fastq\.gz', path):
                        raise ValueError()
                    if not re.fullmatch(r'[a-fA-F0-9]{32}', checksum) or not size.isdigit() or int(size) <= 0:
                        raise ValueError()
                    rows.append(self.record(connector, accession, 'https://' + path, int(size), '原始测序 FASTQ', checksum.lower(), entry['run_accession']))
            return rows[:20]
        except (ValueError, KeyError, TypeError):
            raise SourceConnectorError('invalid_response') from None

    async def ranged_download(self, url, size):
        """Bounded parallel ranges for slow international links; never return partial files."""
        chunk_size = 256 * 1024
        gate = asyncio.Semaphore(8)
        try:
            async with asyncio.timeout(45):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=25) as client:
                    async def part(start, end, first=False):
                        async with gate:
                            async with client.stream('GET', url, headers={'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}) as response:
                                full = first and response.status_code == 200
                                if not full and (response.status_code != 206 or response.headers.get('content-range') != f'bytes {start}-{end}/{size}'):
                                    raise SourceConnectorError('download_integrity_failed')
                                limit = size if full else end - start + 1
                                content = bytearray()
                                async for chunk in response.aiter_bytes(chunk_size=65536):
                                    if len(content) + len(chunk) > limit:
                                        raise SourceConnectorError('download_integrity_failed')
                                    content.extend(chunk)
                                if len(content) != limit:
                                    raise SourceConnectorError('download_integrity_failed')
                                return bytes(content), full
                    first, full = await part(0, min(chunk_size, size)-1, True)
                    if full:
                        return first
                    # TaskGroup cancels sibling requests if any range fails.
                    async with asyncio.TaskGroup() as group:
                        tasks = [group.create_task(part(start, min(start+chunk_size, size)-1))
                                 for start in range(chunk_size, size, chunk_size)]
                    return first + b''.join(task.result()[0] for task in tasks)
        except (httpx.HTTPError, TimeoutError):
            raise SourceConnectorError('download_timeout_or_unavailable') from None
        except ExceptionGroup:
            raise SourceConnectorError('download_integrity_failed') from None

    async def download(self, record):
        if not record['available']:
            raise SourceConnectorError('download_too_large')
        data = (await self.ranged_download(record['url'], record['size'])
                if record['size'] and 512 * 1024 <= record['size'] <= MAX_BYTES
                else await self.get(record['url'], limit=MAX_BYTES))
        if not data or (record['size'] is not None and len(data) != record['size']):
            raise SourceConnectorError('download_integrity_failed')
        if record['md5'] and hashlib.md5(data).hexdigest() != record['md5']:
            raise SourceConnectorError('download_integrity_failed')
        if record['filename'].endswith('.gz'):
            try:
                with gzip.GzipFile(fileobj=BytesIO(data)) as stream:
                    total = 0
                    while chunk := stream.read(65536):
                        total += len(chunk)
                        if total > 256 * 1024 * 1024:
                            raise SourceConnectorError('expanded_file_too_large')
            except (OSError, EOFError):
                raise SourceConnectorError('download_integrity_failed') from None
        elif data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise SourceConnectorError('download_integrity_failed')
        return data
