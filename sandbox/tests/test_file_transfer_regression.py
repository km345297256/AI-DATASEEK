"""Real multipart/FileResponse loopback HTTP checks using disposable tmp files."""

import hashlib
import json
from pathlib import Path
import time
from urllib.parse import unquote

import pytest

from conftest import BASE_URL


@pytest.mark.file_api
@pytest.mark.parametrize('label,filename,content', [
    ('empty', 'empty.bin', b''),
    ('binary', 'binary.bin', bytes(range(256)) * 4),
    ('one_mib', 'one-mib.bin', bytes(range(256)) * 4096),
    ('unicode_filename', '科学数据-测试.bin', '数据集探查\n'.encode('utf-8') + b'\x00\xff'),
], ids=['empty', 'binary', 'one_mib', 'unicode_filename'])
def test_http_upload_download_is_byte_exact(client, tmp_path, label, filename, content):
    destination = tmp_path / 'uploads' / filename
    assert not destination.exists()
    started = time.perf_counter()
    upload = client.post(
        f'{BASE_URL}/api/v1/file/upload',
        files={'file': (filename, content, 'application/octet-stream')},
        data={'path': str(destination)}, timeout=15,
    )
    upload_ms = (time.perf_counter() - started) * 1000
    assert upload.status_code == 200
    result = upload.json()
    assert result['success'] is True
    assert result['data']['success'] is True
    assert result['data']['file_size'] == len(content)
    assert Path(result['data']['file_path']) == destination
    assert destination.read_bytes() == content

    started = time.perf_counter()
    with client.get(f'{BASE_URL}/api/v1/file/download', params={'path': str(destination)},
                    stream=True, timeout=15) as download:
        assert download.status_code == 200
        received = b''.join(download.iter_content(chunk_size=8192))
        assert download.headers['content-type'] == 'application/octet-stream'
        assert int(download.headers['content-length']) == len(content)
        disposition = unquote(download.headers.get('content-disposition', ''))
        assert 'attachment' in disposition and filename in disposition
    download_ms = (time.perf_counter() - started) * 1000
    assert received == content
    digest = hashlib.sha256(content).hexdigest()
    assert hashlib.sha256(received).hexdigest() == digest
    print('TRANSFER_RESULT ' + json.dumps({'case': label, 'status': 'PASS', 'bytes': len(content),
        'sha256': digest, 'upload_ms': round(upload_ms, 3), 'download_ms': round(download_ms, 3)}, ensure_ascii=False))


@pytest.mark.file_api
def test_http_missing_download_returns_404_without_creating_file(client, tmp_path):
    destination = tmp_path / 'does-not-exist.bin'
    response = client.get(f'{BASE_URL}/api/v1/file/download', params={'path': str(destination)}, timeout=15)
    assert response.status_code == 404
    assert response.json()['success'] is False
    assert not destination.exists()
    print('TRANSFER_RESULT ' + json.dumps({'case': 'missing_download', 'status': 'PASS', 'http_status': 404}))


@pytest.mark.file_api
def test_http_missing_upload_part_returns_422_without_creating_file(client, tmp_path):
    destination = tmp_path / 'should-not-be-written.bin'
    response = client.post(f'{BASE_URL}/api/v1/file/upload', data={'path': str(destination)}, timeout=15)
    assert response.status_code == 422
    assert response.json()['success'] is False
    assert not destination.exists()
    print('TRANSFER_RESULT ' + json.dumps({'case': 'missing_upload', 'status': 'PASS', 'http_status': 422}))
