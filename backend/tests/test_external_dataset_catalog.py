"""Portable checks for the reviewed host-data manifests (no host access)."""
from collections import Counter
import json
import hashlib
from pathlib import Path, PurePosixPath

from app.domain.models.dataset import CuratedDatasetSeed
from app.domain.services.domain_presets import get_domain_preset


ROOT = Path(__file__).resolve().parents[1] / 'app/resources/external-datasets'


def test_reviewed_catalog_has_thirty_two_distinct_verified_manifests():
    paths = sorted(ROOT.glob('*/*.json'))
    assert len(paths) == 32
    counts = Counter()
    ids = set()
    for path in paths:
        seed = CuratedDatasetSeed.model_validate_json(path.read_text())
        assert seed.dataset_id == path.stem and seed.dataset_id not in ids
        ids.add(seed.dataset_id)
        source = seed.metadata['source_catalog']
        assert source == path.parent.name
        counts[source] += 1
        assert get_domain_preset(seed.domain) is not None
        assert seed.metadata['curated'] is True
        assert seed.metadata['inventory_complete'] is True
        assert seed.metadata['recursive_file_count'] == len(seed.files)
        assert seed.metadata['total_size_bytes'] == sum(f.size for f in seed.files)
        assert seed.metadata['license'] and seed.metadata['sample_scope']
        if source == 'chemdc':
            # A normally downloaded, user-provided file bundle has no public
            # automatic-download descriptor. Preserve that distinction.
            assert 'source_descriptor_sha256' not in seed.metadata
            assert seed.metadata['acquisition_mode'] == 'user_provided_official_download'
            evidence = seed.metadata['source_evidence']
            digest = hashlib.sha256(json.dumps(
                evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest()
            assert digest == seed.metadata['source_evidence_sha256']
            assert evidence['dataset_id'] == seed.dataset_id
            assert evidence['external_id'] == seed.external_id
            assert evidence['files'] == seed.metadata['provenance']
        else:
            assert len(seed.metadata['source_descriptor_sha256']) == 64
        assert len({f.path for f in seed.files}) == len(seed.files)
        assert any(f.role == 'data' for f in seed.files)
        for file in seed.files:
            assert file.sha256 and file.size is not None
            relative = PurePosixPath(file.path)
            assert not relative.is_absolute() and '..' not in relative.parts
        public = json.dumps(seed.model_dump(), ensure_ascii=False)
        assert '/Users/' not in public and 'storage_directory' not in public
        assert 'source_path' not in public and 'Credential=' not in public
    assert counts == {'scidb': 10, 'tpdc': 10, 'ngdc': 10, 'chemdc': 2}


def test_chemdc_has_only_the_verified_official_pair_and_generated_source_note():
    paths = sorted((ROOT / 'chemdc').glob('*.json'))
    assert [path.stem for path in paths] == ['cn-chemdc-0002039', 'cn-chemdc-0006995']
    seed = CuratedDatasetSeed.model_validate_json((ROOT / 'chemdc/cn-chemdc-0002039.json').read_text())
    metadata = seed.metadata
    assert seed.external_id == metadata['doi'] == '10.57841/casdc.0002039'
    assert seed.domain == 'chemistry'
    assert seed.data_type == 'pdf,docx'
    assert metadata['authors'] == ['杨海艳', '高鹏']
    assert metadata['license'] == 'CC BY 4.0'
    assert metadata['license_url'] == 'https://creativecommons.org/licenses/by/4.0/'
    assert metadata['source_url'] == 'https://doi.org/10.57841/casdc.0002039'
    assert metadata['source_reported_file_count'] == 2
    assert metadata['source_reported_total_size_bytes'] == 2067725
    assert metadata['recursive_file_count'] == 3
    assert metadata['total_size_bytes'] == 2070363
    assert '不是可直接计算的CSV数值表' in seed.description

    originals = metadata['provenance']
    assert len(originals) == 2
    assert sum(file['size'] for file in originals) == 2067725
    assert {file['source_file_id'] for file in originals} == {
        '64eff7629b76f3623c3c0a5d', '64eff7629b76f3623c3c0a5e',
    }
    assert all(file['download_url'] is None for file in originals)
    inventory = {file.path: file for file in seed.files}
    assert set(inventory) == {file['path'] for file in originals} | {'SOURCE.md'}
    for file in originals:
        actual = inventory[file['path']]
        assert (actual.size, actual.sha256, actual.role) == (
            file['size'], file['sha256'], file['role'],
        )
    source_note = inventory['SOURCE.md']
    assert source_note.role == 'documentation'
    assert source_note.content_type == 'text/markdown'
    assert source_note.size == 2638
    assert source_note.sha256 == 'a75dbf0666ac26709cc6fd1a25c4457adf56004427dd39f80264119e763ccb89'

    readability = metadata['readability_validation']
    assert readability['file_count'] == 2
    assert readability['total_bytes'] == 2067725
    assert readability['all_files_readable'] is True
    assert readability['input_files_modified'] is False
    assert readability['downloaded_code_or_macros_executed'] is False
    assert {file['path'] for file in readability['files']} == {file['path'] for file in originals}
    for file in readability['files']:
        assert file['status'] == 'readable'
        assert (file['bytes'], file['sha256']) == (
            inventory[file['path']].size, inventory[file['path']].sha256,
        )
    pdf = next(file for file in readability['files'] if file['format'] == 'pdf')
    assert pdf['content_role'] == 'document'
    assert pdf['page_count'] == pdf['pages_with_text'] == 17
    assert pdf['raw_numeric_table_readability_verified'] is False
    assert pdf['ocr_performed'] is False
    docx = next(file for file in readability['files'] if file['format'] == 'docx')
    assert docx['zip_crc_verified'] is True
    assert docx['document_xml_verified'] is True
    assert docx['paragraph_count'] == 39 and docx['table_count'] == 0
    assert docx['embedded_macros_present'] is False


def test_chemdc_spintronics_bundle_preserves_all_seven_originals_and_read_only_evidence():
    seed = CuratedDatasetSeed.model_validate_json((ROOT / 'chemdc/cn-chemdc-0006995.json').read_text())
    metadata = seed.metadata
    assert seed.external_id == metadata['doi'] == '10.57841/casdc.0006995'
    assert seed.domain == 'chemistry'
    assert seed.data_type == 'xlsx,docx'
    assert metadata['authors'] == ['孟轲']
    assert metadata['license'] == 'CC BY 4.0'
    assert metadata['license_url'] == 'https://creativecommons.org/licenses/by/4.0/'
    assert metadata['source_url'] == 'https://doi.org/10.57841/casdc.0006995'
    assert metadata['source_reported_file_count'] == 7
    assert metadata['source_reported_total_size_bytes'] == 204197
    assert metadata['recursive_file_count'] == len(seed.files) == 8

    prefix = 'XDB0520302-006-室温有机自旋电子器件通过电光补偿策略实现宽范围磁电流调控和多功能性论文数据集'
    data_directory = prefix + '-数据集实体文件'
    docx_path = prefix + '-数据说明文件.docx'
    # Preserve the six official nested names, including the space before .xlsx;
    # browser download basenames must not flatten the published directory tree.
    workbooks = {
        'Fig1d.PC71BM材料UPS与LEIPES数据 .xlsx': (13779, '6960c69dd12c453f30fbde9c'),
        'Fig1e.P3HT材料UPS与LEIPES数据 .xlsx': (14205, '6960c69dd12c453f30fbde9d'),
        'Fig2.光照与黑暗条件I-V曲线 .xlsx': (15652, '6960c69dd12c453f30fbde9e'),
        'Fig2.光照与黑暗条件MC曲线 .xlsx': (32214, '6960c69dd12c453f30fbde9f'),
        'Fig3.光照与磁场条件电流测试数据 .xlsx': (81804, '6960c69dd12c453f30fbdea0'),
        'Fig4.器件磁响应测试数据 .xlsx': (25183, '6960c69dd12c453f30fbdea1'),
    }
    expected = {data_directory + '/' + name: values for name, values in workbooks.items()}
    expected[docx_path] = (21360, '6960c69dd12c453f30fbdea2')
    inventory = {file.path: file for file in seed.files}
    originals = metadata['provenance']
    assert len(originals) == 7
    assert {file['path'] for file in originals} == set(expected)
    assert set(inventory) == set(expected) | {'SOURCE.md'}
    assert sum(file['size'] for file in originals) == 204197
    assert Counter(PurePosixPath(file['path']).suffix for file in originals) == {'.xlsx': 6, '.docx': 1}
    for original in originals:
        file = inventory[original['path']]
        expected_size, source_id = expected[file.path]
        assert original['source_file_id'] == source_id
        assert original['size'] == file.size == expected_size
        assert original['sha256'] == file.sha256
        assert original['role'] == file.role == ('documentation' if file.path == docx_path else 'data')
        assert original['download_url'] is None
        assert 'source_path' not in original and 'storage_directory' not in original
        if file.path.endswith('.xlsx'):
            assert PurePosixPath(file.path).parent == PurePosixPath(data_directory)
            assert file.path.endswith(' .xlsx')
            assert file.content_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            assert PurePosixPath(file.path).parent == PurePosixPath('.')
            assert file.content_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    known_hashes = {
        'Fig1d.PC71BM材料UPS与LEIPES数据 .xlsx': '93f13275ef3e4106932d05234eb02f7eb757878560fa9e512043e659b4925755',
        'Fig1e.P3HT材料UPS与LEIPES数据 .xlsx': 'b27ed1f9a078f0dbb074ef1883c73b5e224dffecb0b20f31adb5a3479cbc3041',
        'Fig2.光照与黑暗条件I-V曲线 .xlsx': '5cb359525cb21fcbb157c51384cc66a73e011899decdc0183eb4f0707f77843e',
    }
    for name, digest in known_hashes.items():
        assert inventory[data_directory + '/' + name].sha256 == digest
    source_note = inventory['SOURCE.md']
    assert source_note.role == 'documentation' and source_note.content_type == 'text/markdown'
    assert source_note.size > 0 and len(source_note.sha256) == 64
    assert metadata['total_size_bytes'] == 204197 + source_note.size

    assert metadata['acquisition_mode'] == 'user_provided_official_download'
    assert metadata['source_evidence_kind'] == 'reviewed_official_metadata_and_user_provided_files'
    assert 'source_descriptor_sha256' not in metadata
    evidence = metadata['source_evidence']
    assert evidence['dataset_id'] == seed.dataset_id
    assert evidence['external_id'] == seed.external_id
    assert evidence['authors'] == metadata['authors']
    assert evidence['source_url'] == metadata['source_url']
    assert evidence['license'] == metadata['license']
    assert evidence['acquisition_mode'] == metadata['acquisition_mode']
    assert evidence['source_reported_file_count'] == 7
    assert evidence['source_reported_total_size_bytes'] == 204197
    assert evidence['files'] == originals
    assert hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode()).hexdigest() == metadata['source_evidence_sha256']

    readability = metadata['readability_validation']
    assert readability['file_count'] == len(readability['files']) == 7
    assert readability['total_bytes'] == 204197
    assert readability['all_files_readable'] is True
    assert readability['input_files_modified'] is False
    assert readability['downloaded_code_or_macros_executed'] is False
    assert {file['path'] for file in readability['files']} == set(expected)
    for item in readability['files']:
        file = inventory[item['path']]
        assert (item['bytes'], item['sha256']) == (file.size, file.sha256)
        assert item['status'] == 'readable'
        if item['format'] == 'xlsx':
            assert item['content_role'] == 'spreadsheet'
            assert item['parser'] == 'openpyxl (read_only, keep_links=False)'
            assert item['formulas_evaluated'] is False
            assert item['macros_executed'] is False
            assert item['external_links_followed'] is False
            assert item['sheet_count'] == len(item['sheets']) > 0
            assert sum(sheet['numeric_cells'] for sheet in item['sheets']) > 0
            assert all(sheet['formula_cells'] >= 0 for sheet in item['sheets'])
        else:
            assert item['format'] == 'docx' and item['path'] == docx_path
            assert item['content_role'] == 'document'
            assert item['parser'] == 'zipfile+defusedxml'
            assert item['zip_crc_verified'] is True
            assert item['document_xml_verified'] is True
            assert item['embedded_macros_present'] is False
            assert item['embedded_macros_executed'] is False
            assert item['paragraph_count'] > 0 and item['text_characters'] > 0


def test_ngdc_is_nonhuman_versioned_assemblies_not_unlicensed_placeholders():
    species = set()
    for path in (ROOT / 'ngdc').glob('*.json'):
        seed = CuratedDatasetSeed.model_validate_json(path.read_text())
        assert seed.external_id.startswith('GWH')
        assert seed.domain == 'sequence'
        assert 'academic' in seed.metadata['license']
        assert seed.metadata['organism'] != 'Homo sapiens'
        species.add(seed.metadata['organism'])
        assert any(f.path.endswith('.fasta') and f.size > 0 for f in seed.files)
        assert any(f.path.endswith('.fasta.gz') for f in seed.files)
    assert len(species) == 10


def test_public_download_descriptors_match_the_verified_manifest_identities():
    descriptors = ROOT.parent / 'external-descriptors'
    assert {path.stem for path in descriptors.glob('*.json')} == {'scidb','tpdc','ngdc'}
    for path in descriptors.glob('*.json'):
        source = json.loads(path.read_text())
        assert len(source['datasets']) == 10
        for item in source['datasets']:
            manifest = json.loads((ROOT / path.stem / (item['dataset_id']+'.json')).read_text())
            digest = hashlib.sha256(json.dumps(item,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            assert digest == manifest['metadata']['source_descriptor_sha256']
