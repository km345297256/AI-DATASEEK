"""Bounded official package download; never follows a caller-supplied URL."""
import asyncio
import hashlib
import io
import json
import stat
import zipfile
from pathlib import PurePosixPath

import httpx
from pydantic import TypeAdapter
from app.domain.models.source_connector import Accession
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.ncbi import BASE, _rate_limit

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024


def validate_package(data: bytes, accession: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 1000 or sum(x.file_size for x in entries) > MAX_EXPANDED_BYTES:
                raise ValueError()
            names = [x.filename for x in entries]
            if len(names) != len(set(names)):
                raise ValueError()
            for item in entries:
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                        or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError()
                # Reading each bounded entry verifies CRC and rejects truncated packages.
                with archive.open(item) as stream:
                    while stream.read(65536):
                        pass
            report = archive.read('ncbi_dataset/data/assembly_data_report.jsonl')
            rows = [json.loads(line) for line in report.splitlines() if line.strip()]
            if len(rows) != 1 or rows[0].get('accession') != accession:
                raise ValueError()
            fasta = [name for name in names if name.startswith(f'ncbi_dataset/data/{accession}/') and name.endswith('_genomic.fna')]
            if not fasta or any(not archive.read(name).startswith(b'>') for name in fasta):
                raise ValueError()
        return hashlib.sha256(data).hexdigest()
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, RuntimeError, UnicodeError):
        raise SourceConnectorError('invalid_download_package') from None


async def download_package(accession: str, *, transport=None):
    accession = TypeAdapter(Accession).validate_python(accession)
    url = f'{BASE}/genome/accession/{accession}/download'
    try:
        async with asyncio.timeout(50):
            await _rate_limit()
            async with httpx.AsyncClient(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
                async with client.stream('GET', url, params={'include_annotation_type': 'GENOME_FASTA'},
                                         headers={'Accept': 'application/zip'}) as response:
                    if response.status_code == 429:
                        raise SourceConnectorError('rate_limited')
                    if response.status_code in (400, 404):
                        raise SourceConnectorError('query_not_found_or_invalid')
                    if response.status_code != 200:
                        raise SourceConnectorError('upstream_unavailable')
                    if int(response.headers.get('content-length', '0')) > MAX_PACKAGE_BYTES:
                        raise SourceConnectorError('download_too_large')
                    data = bytearray()
                    async for block in response.aiter_bytes(65536):
                        if len(data) + len(block) > MAX_PACKAGE_BYTES:
                            raise SourceConnectorError('download_too_large')
                        data.extend(block)
            content = bytes(data)
            digest = await asyncio.to_thread(validate_package, content, accession)
            return content, digest, url
    except (httpx.HTTPError, TimeoutError):
        raise SourceConnectorError('download_timeout_or_unavailable') from None
    except ValueError:
        raise SourceConnectorError('invalid_download_package') from None
