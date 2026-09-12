"""Batch-one readers use existing owner, version, budget and lifecycle gates."""
import copy
import struct

import pytest

from test_unified_visualization import environment, invoke
from app.application.services import extended_visualization as extended
from app.application.services import unified_visualization as unified
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationDisabledError


def content(env, name, data):
    storage, _ = env
    storage.content['file'] = data
    storage.infos['file'].filename = name
    storage.infos['file'].size = len(data)


def result(reader):
    common = dict(contract_version=2, type=reader, reader=reader, media_type='application/json',
                  metadata={}, warnings=[], sampled=False)
    if reader == 'structure':
        return dict(common, kind='tree', tree=[dict(path='/0', node_type='number',
            attributes=dict(label='root', value='9007199254740993', children_count=0, numeric_representation='source lexeme'))])
    return dict(common, kind='table', table=dict(columns=['成员', '类型', '声明大小（字节）', '压缩大小（字节）', '嵌套压缩包'],
        rows=[['values.csv', 'file', 100, 20, False]], row_offset=0, column_offset=0, total_rows=1, total_columns=5))


@pytest.mark.asyncio
@pytest.mark.parametrize('reader,filename,plugin', [('structure', 'values.json', 'viz-structured-tree'), ('archive', 'values.zip', 'viz-archive-directory')])
async def test_new_readers_share_authorized_worker_and_public_result(environment, monkeypatch, reader, filename, plugin):
    content(environment, filename, b'bounded fixture')
    calls = []
    def worker(image, data, **kwargs):
        calls.append((image, data, kwargs))
        return {'ok': True, 'data': result(reader)}
    async def read(*args, **kwargs):
        return await extended.extended_visualization(*args, **kwargs, worker=worker)
    monkeypatch.setattr(unified, 'extended_visualization', read)
    output = await invoke(environment, plugin, 'preview')
    assert output.kind == ('tree' if reader == 'structure' else 'table')
    assert output.contract_version == 2 and output.plugin_id == plugin
    assert calls[0][0] == 'sandbox:test' and calls[0][2]['reader'] == reader
    assert calls[0][2]['format'] == filename.rsplit('.', 1)[1]
    assert environment[0].reads == [('file', 0, len(b'bounded fixture'))]
    if reader == 'structure':
        assert output.payload['tree'][0]['attributes']['value'] == '9007199254740993'


@pytest.mark.asyncio
@pytest.mark.parametrize('plugin,name,operation', [
    ('viz-structured-tree', 'values.json', 'preview'), ('viz-archive-directory', 'values.zip', 'preview'),
    ('viz-video-player', 'sample.mp4', 'bytes'), ('viz-audio-waveform', 'sample.wav', 'bytes'),
])
async def test_new_plugins_cannot_bypass_owner_enable_or_version(environment, plugin, name, operation):
    content(environment, name, b'test')
    with pytest.raises(FileNotFoundError): await invoke(environment, plugin, operation, user='foreign')
    with pytest.raises(PreviewVersionChanged): await invoke(environment, plugin, operation, version='f' * 64)
    await environment[1].set_state('owner', plugin, False)
    with pytest.raises(VisualizationDisabledError): await invoke(environment, plugin, operation)
    assert environment[0].reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('plugin,name,options', [
    ('viz-structured-tree', 'a.json', {'path': '/etc/passwd'}),
    ('viz-structured-tree', 'a.yaml', {'url': 'https://example.invalid'}),
    ('viz-archive-directory', 'a.zip', {'extract': True}),
    ('viz-archive-directory', 'a.zip', {'row_offset': True}),
    ('viz-archive-directory', 'a.zip', {'row_offset': 4096}),
])
async def test_new_readers_reject_undeclared_operations_before_io(environment, plugin, name, options):
    content(environment, name, b'data')
    with pytest.raises(unified.ScientificPreviewRejected): await invoke(environment, plugin, 'preview', options=options)
    assert environment[0].reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('plugin,name,limit', [('viz-structured-tree', 'a.json', 4), ('viz-video-player', 'a.mp4', 64), ('viz-audio-waveform', 'a.wav', 16)])
async def test_reader_hard_limits_remain_even_if_manifest_budget_is_raised(environment, plugin, name, limit):
    storage, catalog = environment
    content(environment, name, b'')
    storage.infos['file'].size = limit * 1024 * 1024 + 1
    snapshot = await catalog.runtime.visualization_snapshot()
    catalog.runtime.visualization_snapshot.return_value = snapshot.model_copy(update={'plugins': [
        p.model_copy(update={'limits': p.limits.model_copy(update={'max_input_bytes': 512 * 1024 * 1024})}) if p.id == plugin else p for p in snapshot.plugins]})
    operation = 'preview' if plugin == 'viz-structured-tree' else 'bytes'
    with pytest.raises(unified.ScientificPreviewRejected): await invoke(environment, plugin, operation)
    assert storage.reads == []


@pytest.mark.asyncio
async def test_content_prepare_is_typed_bounded_and_has_no_worker(environment):
    # One empty classic TIFF IFD, no pixel decoding or analysis task.
    content(environment, 'sample.tif', b'II' + struct.pack('<HIHI', 42, 8, 0, 0))
    output = await invoke(environment, 'tiff', 'prepare')
    assert output.kind == 'resources' and output.metadata['purpose'] == 'content-profile'
    profile = output.payload['profile']
    assert profile['container'] == 'tiff' and profile['profile_version'] == 1
    assert set(profile) == {'profile_version', 'container', 'dialect', 'traits', 'evidence', 'bytes_read', 'truncated'}
    assert len(environment[0].reads) == 1 and profile['bytes_read'] <= 65536


@pytest.mark.asyncio
async def test_prepare_cannot_accept_a_path_or_silently_change_other_plugins(environment):
    content(environment, 'a.tif', b'tiff')
    with pytest.raises(unified.ScientificPreviewRejected): await invoke(environment, 'tiff', 'prepare', options={'path': '/etc/passwd'})
    content(environment, 'a.json', b'{}')
    with pytest.raises(unified.ScientificPreviewRejected): await invoke(environment, 'viz-structured-tree', 'prepare')
    assert environment[0].reads == []


@pytest.mark.parametrize('reader', ['structure', 'archive'])
def test_payload_rejects_media_substitution_and_preserves_approved_shape(reader):
    value = result(reader)
    assert extended.validate_payload(value, reader, value['kind'], 1024 * 1024) == value
    other = copy.deepcopy(value); other['array'] = {'shape': [1], 'values': [1]}
    with pytest.raises(ValueError): extended.validate_payload(other, reader, value['kind'], 1024 * 1024)


@pytest.mark.parametrize('patch', [{'path': '/etc/passwd'}, {'node_type': 'script'}, {'attributes': {'label': 'x', 'value': 9007199254740993, 'children_count': 0}}, {'attributes': {'label': 'x', 'children_count': True}}])
def test_structure_payload_rejects_path_exec_and_precision_loss(patch):
    value = result('structure'); value['tree'][0].update(patch)
    with pytest.raises(ValueError): extended.validate_payload(value, 'structure', 'tree', 1024 * 1024)


@pytest.mark.parametrize('patch', [{'row_offset': True}, {'total_rows': 5000}, {'column_offset': 1}, {'rows': [['x', 'file', 1, 0, 'false']]}, {'rows': [['x', 'symlink', 1, 0, False]]}])
def test_archive_payload_rejects_malformed_directory(patch):
    value = result('archive'); value['table'].update(patch)
    with pytest.raises(ValueError): extended.validate_payload(value, 'archive', 'table', 1024 * 1024)
