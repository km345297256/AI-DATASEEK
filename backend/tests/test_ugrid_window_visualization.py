"""Actual NetCDF4 fixture contract, host-only strict validation and bindings."""
import copy
import json
from pathlib import Path
import unittest
from app.application.services.ugrid_window_visualization import validate_ugrid_window_payload, validate_ugrid_window_options

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT/"frontend/tests/browser/ugrid-window-data.json").read_text())

def clone():
    return copy.deepcopy(FIXTURE["node_geometry"])

MUTATIONS = {
    "unknown_key":lambda r:r.update(url="https://example.org"),
    "kind_array":lambda r:r.update(kind="array"),
    "kind_list":lambda r:r.update(kind=[]),
    "bool_version":lambda r:r.update(contract_version=True),
    "foreign_reader":lambda r:r.update(reader="array-window"),
    "media_html":lambda r:r.update(media_type="text/html"),
    "sampled":lambda r:r.update(sampled=True),
    "warning":lambda r:r.update(warnings=[]),
    "selected_unknown":lambda r:r["selected"].update(path="secret"),
    "bool_index":lambda r:r["selected"].update(indices=[True,2]),
    "out_of_range_index":lambda r:r["selected"].update(indices=[2,2]),
    "missing_index":lambda r:r["selected"].update(indices=[1]),
    "field_null_index":lambda r:r["selected"].update(field=None),
    "bad_mesh_id":lambda r:r["selected"].update(mesh="u-"+"0"*32),
    "metadata_key":lambda r:r["metadata"].update(path="/private/secret"),
    "source_bound":lambda r:r["metadata"].update(source_bytes=8*1024**3+1),
    "read_budget":lambda r:r["metadata"].update(read_bytes=8388609),
    "read_count":lambda r:r["metadata"].update(read_requests=129),
    "attribute_budget":lambda r:r["metadata"].update(attribute_bytes=65537),
    "decoded_budget":lambda r:r["metadata"].update(decoded_chunk_bytes=16*1024**2+1),
    "chunk_count":lambda r:r["metadata"].update(chunks_touched=129),
    "topology_partial":lambda r:r["metadata"].update(topology_complete=False),
    "conventions":lambda r:r["metadata"].update(conventions="CF-1.8"),
    "limits_bool":lambda r:r["metadata"]["limits"].update(max_faces=True),
    "limits_unknown":lambda r:r["metadata"]["limits"].update(max_bytes=100),
    "missing_count":lambda r:r["metadata"].update(missing_values=0),
    "bounds":lambda r:r["metadata"].update(bounds=[[0,102],[30,31]]),
    "bool_coordinate":lambda r:r["ugrid"]["coordinates"].__setitem__(0,True),
    "null_coordinate":lambda r:r["ugrid"]["coordinates"].__setitem__(0,None),
    "nan_coordinate":lambda r:r["ugrid"]["coordinates"].__setitem__(0,float("nan")),
    "coordinates_length":lambda r:r["ugrid"]["coordinates"].pop(),
    "negative_node":lambda r:r["ugrid"]["faces"][0].__setitem__(0,-1),
    "huge_node":lambda r:r["ugrid"]["faces"][0].__setitem__(0,4096),
    "duplicate_node":lambda r:r["ugrid"]["faces"][0].__setitem__(0,1),
    "self_intersection":lambda r:r["ugrid"]["faces"].__setitem__(0,[0,2,1,3]),
    "association":lambda r:r["ugrid"].update(location="face"),
    "value_count":lambda r:r["ugrid"]["values"].pop(),
    "bool_value":lambda r:r["ugrid"]["values"].__setitem__(0,True),
    "infinite_value":lambda r:r["ugrid"]["values"].__setitem__(0,float("inf")),
    "unrounded_float32":lambda r:r["ugrid"]["values"].__setitem__(0,.1),
    "unmasked_fill":lambda r:r["ugrid"]["values"].__setitem__(0,-999),
    "coordinate_unit_leak":lambda r:r["choices"]["meshes"][0]["coordinates"][0].update(unit="/Users/private"),
    "field_kind":lambda r:r["choices"]["meshes"][0]["fields"][1].update(dtype="object"),
    "field_location":lambda r:r["choices"]["meshes"][0]["fields"][1].update(location=[]),
    "field_dimension":lambda r:r["choices"]["meshes"][0]["fields"][1]["dimensions"][0].update(size=3),
    "field_spatial_axis":lambda r:r["choices"]["meshes"][0]["fields"][1].update(spatial_axis=0),
    "field_narrow_dtype":lambda r:r["choices"]["meshes"][0]["fields"][1].update(dtype="|u1"),
    "unsafe_fill":lambda r:r["choices"]["meshes"][0]["fields"][1].update(fill_values=[1e80]),
}

class UgridPayload(unittest.TestCase):
    def test_actual_private_payloads(self):
        for value in FIXTURE.values():
            self.assertIs(validate_ugrid_window_payload(value),value)
    def test_copies_are_identical(self):
        self.assertEqual((ROOT/"sandbox/app/services/ugrid_window_payload.py").read_bytes(),(ROOT/"backend/app/application/services/ugrid_window_visualization.py").read_bytes())
    def test_request_bindings(self):
        value=clone()
        for kwargs in ({"kind":"tree"},{"fmt":"h5"},{"source_bytes":1},{"read_bytes":1},{"read_requests":128},{"options":dict(value["selected"],indices=[0,0])}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):validate_ugrid_window_payload(value,**kwargs)
        validate_ugrid_window_payload(value,kind="geometry",fmt="nc",options=value["selected"],**{k:value["metadata"][k] for k in ("source_bytes","read_bytes","read_requests")})
    def test_tree_requires_no_selection_and_no_geometry(self):
        with self.assertRaises(ValueError):validate_ugrid_window_options("tree",{"mesh":"u-"+"0"*32})
        value=copy.deepcopy(FIXTURE["node_tree"]);value["ugrid"]={}
        with self.assertRaises(ValueError):validate_ugrid_window_payload(value)
    def test_global_fields_limit(self):
        value=clone();one=copy.deepcopy(value["choices"]["meshes"][0]);two=copy.deepcopy(one);two["id"]="u-"+"0"*32
        one["fields"]=[dict(one["fields"][0],id="f-"+f"{i:032x}") for i in range(17)]
        two["fields"]=[dict(two["fields"][0],id="f-"+f"{i:032x}") for i in range(17)]
        value["choices"]["meshes"]=[one,two]
        with self.assertRaises(ValueError):validate_ugrid_window_payload(value)

def generated(mutate):
    def check(self):
        value=clone();mutate(value)
        with self.assertRaises(ValueError):validate_ugrid_window_payload(value)
    return check

for name, mutate in MUTATIONS.items():
    setattr(UgridPayload,"test_reject_"+name,generated(mutate))
