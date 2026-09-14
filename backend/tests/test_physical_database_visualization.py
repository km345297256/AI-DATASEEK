import copy
import json
from pathlib import Path
import pytest
from app.application.services.physical_database_visualization import validate_physical_payload
from app.application.services.extended_visualization import validate_payload, validate_requested_selection
from app.application.services.unified_visualization import normalize_result

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=json.loads((ROOT/"frontend/tests/browser/physical-database-data.json").read_text())


@pytest.mark.parametrize("reader",FIXTURES)
def test_exact_physical_payload_binding_and_normalization(reader):
    p=copy.deepcopy(FIXTURES[reader])
    assert validate_payload(p,reader,"tree",2097152)==p
    validate_requested_selection(p,reader,"tree",{},p["metadata"]["format"],p["metadata"]["source_bytes"])
    r=normalize_result({**p,"plugin_id":"viz-"+reader,"version":"a"*64,"revision":"b"*64})
    assert r.kind=="table" and r.payload["view_kind"]=="tree"
    for kwargs in ({"reader":"sql-dump"},{"kind":"table"},{"size":1},{"fmt":"bak"},{"options":{"restore":True}},{"limit":1}):
        with pytest.raises(ValueError): validate_physical_payload(p,**kwargs)


def test_physical_validator_copy_is_exact():
    assert (ROOT/"backend/app/application/services/physical_database_visualization.py").read_bytes()==(ROOT/"sandbox/app/services/physical_database_payload.py").read_bytes()
