"""TIFF support keeps source precision explicit and never rewrites originals."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import numpy as np
from PIL import Image
import pytest


@pytest.fixture
def operations():
    candidates = [
        Path(__file__).resolve().parents[2] / 'tools/image_science/operations.py',
        Path('/tools/image_science/operations.py'),
        Path('/opt/ai-dataseek/tools/image_science/operations.py'),
    ]
    source = next(path for path in candidates if path.is_file())
    spec = importlib.util.spec_from_file_location('image_tiff_operations', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def high_depth_stack(tmp_path):
    path = tmp_path / 'science.TIFF'
    first = Image.fromarray(np.array([[1000, 2000], [3000, 65000]], dtype=np.uint16))
    second = Image.fromarray(np.array([[0, 0], [0, 0]], dtype=np.uint16))
    first.save(path, save_all=True, append_images=[second])
    return path


def test_collection_and_metadata_keep_original_tiff_mode_and_frames(operations, high_depth_stack):
    collection = operations.collection({'input_dir': str(high_depth_stack.parent)})
    assert collection['file_count'] == 1
    assert collection['formats'] == ['TIFF']
    item = collection['files'][0]
    assert item['mode'].startswith('I;16')
    assert item['n_frames'] == 2
    assert item['width'] == item['height'] == 2
    assert item['analysis_scope'] == 'first_frame_metadata'
    metadata = operations.metadata({'input_paths': [str(high_depth_stack)]})['records'][0]
    assert metadata['mode'] == item['mode']
    assert metadata['n_frames'] == 2


def test_integrity_checks_original_values_without_8bit_clipping(operations, high_depth_stack):
    result = operations.integrity({'input_paths': [str(high_depth_stack)]})
    record = result['records'][0]
    assert result['invalid_count'] == 0
    assert record['signature_match'] is True
    assert record['blank'] is False  # An 8-bit conversion would clip all values to 255.
    assert record['blank_scope'] == 'original_first_frame_values'
    assert record['n_frames'] == 2
    assert any('later frame' in value for value in record['warnings'])


@pytest.mark.parametrize(('actual_format', 'extension'), [('PNG', '.jpg'), ('JPEG', '.png'), ('TIFF', '.png')])
def test_integrity_matches_signature_against_actual_extension(operations, tmp_path, actual_format, extension):
    path = tmp_path / ('renamed' + extension)
    Image.new('RGB', (3, 3), (1, 2, 3)).save(path, format=actual_format)
    result = operations.integrity({'input_paths': [str(path)]})
    assert result['records'][0]['readable'] is True
    assert result['records'][0]['signature_match'] is False
    assert result['signature_mismatch_count'] == 1


def test_quality_discloses_high_depth_first_frame_preview(operations, high_depth_stack):
    before = hashlib.sha256(high_depth_stack.read_bytes()).hexdigest()
    record = operations.quality({'input_paths': [str(high_depth_stack)]})['records'][0]
    assert record['quantitative'] is False
    assert record['source_mode'].startswith('I;16')
    assert record['analysis_scope'] == 'first_frame_8bit_L_preview'
    assert record['n_frames'] == 2
    assert any('clipped' in value for value in record['warnings'])
    assert any('first frame' in value for value in record['warnings'])
    assert hashlib.sha256(high_depth_stack.read_bytes()).hexdigest() == before


def test_duplicate_detection_separates_exact_bytes_from_preview_similarity(operations, high_depth_stack):
    result = operations.dup({'input_paths': [str(high_depth_stack)]})
    assert result['image_count'] == 1
    assert result['analysis_scope']['exact_duplicates'] == 'whole_file_sha256'
    assert result['analysis_scope']['similar_pairs'] == 'first_frame_8bit_grayscale_perceptual_hash'
    assert any('clipped' in value for value in result['warnings'])


@pytest.mark.parametrize('operation', ['contact', 'derivative'])
def test_visual_outputs_disclose_conversion_and_preserve_source(operations, high_depth_stack, tmp_path, monkeypatch, operation):
    monkeypatch.setattr(operations, 'ROOT', tmp_path.resolve())
    before = high_depth_stack.read_bytes()
    result = getattr(operations, operation)({'input_paths': [str(high_depth_stack)], 'output_dir': str(tmp_path / operation)})
    assert result['success'] is True
    assert result['artifacts']
    scope = result if operation == 'contact' else result['artifacts'][0]
    assert scope['quantitative'] is False
    assert 'first_frame_8bit_RGB' in scope['analysis_scope']
    assert any('clipped' in value for value in scope['warnings'])
    assert any('first frame' in value for value in scope['warnings'])
    assert high_depth_stack.read_bytes() == before


def test_ocr_retains_page_numbers_for_multiframe_tiff(operations, high_depth_stack, monkeypatch):
    def fake_tesseract(command, **kwargs):
        Path(command[2] + '.tsv').write_text(
            'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
            '5\t2\t1\t1\t1\t1\t0\t0\t2\t2\t95\tPageTwo\n'
        )
    monkeypatch.setattr(operations.subprocess, 'run', fake_tesseract)
    record = operations.ocr({'input_paths': [str(high_depth_stack)]})['records'][0]
    assert record['n_frames'] == 2
    assert record['words'][0]['page_number'] == 2
    assert record['analysis_scope'] == 'ocr_engine_text_not_pixel_quantification'


def test_existing_png_and_corrupt_tiff_remain_supported(operations, tmp_path):
    png = tmp_path / 'ordinary.png'
    Image.new('L', (4, 5), 17).save(png)
    broken = tmp_path / 'corrupt.tif'
    broken.write_bytes(b'not a TIFF')
    result = operations.integrity({'input_paths': [str(png), str(broken)]})
    assert result['invalid_count'] == 1
    assert result['records'][0]['signature_match'] is True
    assert result['records'][0]['n_frames'] == 1
    assert result['records'][1]['readable'] is False
