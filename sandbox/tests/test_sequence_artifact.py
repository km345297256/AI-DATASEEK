import pytest
from app.services.artifact_validation import validate_artifacts


@pytest.mark.parametrize('suffix,content,count,bases', [
    ('fastq', b'@seq\nACGTN\n+\n?????\n', 1, 5),
    ('fq', b'@seq\r\nAC\r\n+seq\r\n!!\r\n@two\r\nGT\r\n+\r\n~~', 2, 4),
    ('fna', b'>one\nACGT\nNN\n>two\nAC\n', 2, 8),
    ('faa', b'>protein\nMKWVTF*\n', 1, 7),
])
def test_sequence_files_are_verified_data(tmp_path, suffix, content, count, bases):
    path = tmp_path / ('result.' + suffix)
    path.write_bytes(content)
    receipt = validate_artifacts([{'path': str(path), 'kind': 'data'}], root=tmp_path)['files'][0]
    assert receipt['valid'] is True
    assert receipt['metadata']['record_count'] == count
    assert receipt['metadata']['base_count'] == bases
    assert receipt['metadata']['validation_scope'] == 'format_only'
    assert receipt['sha256'] and receipt['size'] == len(content)


@pytest.mark.parametrize('suffix,content', [
    ('fastq', b''), ('fastq', b'@s\nAC\n+\n!\n'),
    ('fastq', b'@s\nAC\n+\n'), ('fastq', b'@s\nAC\n+\n !\n'),
    ('fastq', b'@s\nAC\n+other\n!!\n'), ('fastq', b's\nAC\n+\n!!\n'),
    ('fastq', b'@s\nAC\n+\n!!\nextra'), ('fastq', b'@s\nA1\n+\n!!\n'),
    ('fasta', b'>s\n'), ('fna', b'ACGT\n'), ('fa', b'>s\n>empty\nAC\n'),
    ('fastq', b'<html>error</html>'), ('fasta', b'>s\n\xff\n'),
])
def test_invalid_sequence_content_is_not_delivered(tmp_path, suffix, content):
    path = tmp_path / ('bad.' + suffix)
    path.write_bytes(content)
    assert not validate_artifacts([{'path': str(path), 'kind': 'data'}], root=tmp_path)['files'][0]['valid']


@pytest.mark.parametrize('name,content', [('custom.xxcc1', b'\x00\xffCUSTOM'), ('no_extension', b''), ('report.pdf', b'opaque')])
def test_unknown_formats_get_integrity_receipt_not_content_certification(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    item = validate_artifacts([{'path': str(path), 'kind': 'data'}], root=tmp_path)['files'][0]
    assert item['valid']
    assert item['size'] == len(content)
    assert item['metadata'] == {'validation_scope': 'file_integrity', 'content_validation': 'unsupported'}


def test_unknown_format_does_not_bypass_missing_file_or_symlink_boundary(tmp_path):
    path = tmp_path / 'missing.xxcc1'
    assert not validate_artifacts([{'path': str(path), 'kind': 'data'}], root=tmp_path)['files'][0]['valid']
    real = tmp_path / 'source'
    real.write_bytes(b'data')
    path.symlink_to(real)
    assert not validate_artifacts([{'path': str(path), 'kind': 'data'}], root=tmp_path)['files'][0]['valid']
