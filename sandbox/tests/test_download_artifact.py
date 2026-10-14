import io
import zipfile
from app.services.artifact_validation import validate_artifacts


def test_zip_data_delivery_requires_valid_bounded_archive(tmp_path):
    file = tmp_path / 'genome.zip'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('data/sequence.fna', '>sequence\nACGT\n')
    file.write_bytes(buffer.getvalue())
    result = validate_artifacts([{'path': str(file), 'kind': 'data'}], root=tmp_path)['files'][0]
    assert result['valid'] and result['metadata']['format'] == 'ZIP'
    assert result['sha256'] and result['size'] == len(buffer.getvalue())
    file.write_bytes(b'<html>download failed</html>')
    assert not validate_artifacts([{'path': str(file), 'kind': 'data'}], root=tmp_path)['files'][0]['valid']


def test_archive_traversal_is_not_a_downloadable_result(tmp_path):
    file = tmp_path / 'bad.zip'
    with zipfile.ZipFile(file, 'w') as z:
        z.writestr('../escape', 'bad')
    result = validate_artifacts([{'path': str(file), 'kind': 'data'}], root=tmp_path)['files'][0]
    assert not result['valid']


def test_zip_receipt_is_supported_through_actual_http_schema(client, tmp_path, monkeypatch):
    from conftest import BASE_URL
    from app.api.v1 import file as file_api
    path = tmp_path / 'data.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('data.fna', '>seq\nACGT\n')
    monkeypatch.setattr(file_api, 'validate_artifacts',
        lambda items, **kwargs: validate_artifacts(items, root=tmp_path, **kwargs))
    response = client.post(f'{BASE_URL}/api/v1/file/validate-artifacts',
                           json={'items': [{'path': str(path), 'kind': 'data'}]})
    assert response.status_code == 200
    assert response.json()['data']['files'][0]['valid'] is True
