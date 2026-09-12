"""Host validation remains pure: no scientific parser or private paths."""
import copy
import json
from pathlib import Path
import pytest
from app.application.services.astronomy_workbench_visualization import ERROR, validate_options, validate_payload

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=json.loads((ROOT/'frontend/tests/browser/main-astronomy-data.json').read_text())

@pytest.mark.parametrize('key',list(FIXTURES))
def test_actual_isolated_reader_payload(key):
    v=copy.deepcopy(FIXTURES[key]);assert validate_payload(v,kind=v['kind'],options=v['selected'],format='fits',source_bytes=v['metadata']['source_bytes'])==v

@pytest.mark.parametrize('key',list(FIXTURES))
@pytest.mark.parametrize('attack',['extra','size','format','selected','limit','source-label'])
def test_private_reader_output_rejected(key,attack):
    v=copy.deepcopy(FIXTURES[key]);expected=copy.deepcopy(v['selected']);size=v['metadata']['source_bytes'];fmt='fits'
    if attack=='extra':v['workbench']['private_path']='/Users/private/data'
    elif attack=='size':size+=1
    elif attack=='format':fmt='tif'
    elif attack=='selected':v['selected']['extra']='untrusted'
    elif attack=='limit':v['metadata']['limits']['source_bytes']=True
    else:v['choices']['datasets'][0]['name']='file:///tmp/source'
    with pytest.raises(ValueError,match=ERROR):validate_payload(v,kind=v['kind'],options=expected,format=fmt,source_bytes=size)

def test_host_and_sandbox_contract_are_identical():
    assert (ROOT/'sandbox/app/services/astronomy_workbench_payload.py').read_bytes()==(ROOT/'backend/app/application/services/astronomy_workbench_visualization.py').read_bytes()
