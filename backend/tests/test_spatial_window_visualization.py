"""Strict spatial public-boundary tests with actual AnnData-derived private output."""
import copy
import json
from pathlib import Path
import pytest
from app.application.services.spatial_window_visualization import validate_spatial_window_payload,validate_spatial_window_options
FIXTURES=json.loads(Path(__file__).with_name("spatial_window_contract_fixtures.json").read_text())
@pytest.mark.parametrize("storage",["dense","csr","csc"])
@pytest.mark.parametrize("kind",["tree","geometry"])
def test_actual_anndata_payload_and_host_accounting(storage,kind):
    v=copy.deepcopy(FIXTURES[storage][kind]);m=v["metadata"]
    assert validate_spatial_window_payload(v,kind=kind,options=v["selected"],fmt="h5ad",
        source_bytes=m["source_bytes"],read_bytes=m["read_bytes"],read_requests=m["read_requests"]) is v
MUTATIONS=[
("root-extra",lambda v:v.update(url="file:///private")),
("wrong-kind",lambda v:v.update(kind="series")),
("version-bool",lambda v:v.update(contract_version=True)),
("sampled",lambda v:v.update(sampled=False)),
("format",lambda v:v["metadata"].update(format="h5")),
("scope",lambda v:v["metadata"].update(scope="whole file verified")),
("value-semantics",lambda v:v["metadata"].update(value_semantics="normalized")),
("sparse-semantics",lambda v:v["metadata"].update(sparse_semantics="duplicate values summed")),
("labels",lambda v:v["metadata"].update(labels="patient names")),
("strategy",lambda v:v["metadata"].update(read_strategy="whole-file download")),
("attr-budget",lambda v:v["metadata"].update(attribute_buffer_bytes=4097)),
("read-budget",lambda v:v["metadata"].update(read_bytes=8388609)),
("request-budget",lambda v:v["metadata"].update(read_requests=129)),
("decoded-budget",lambda v:v["metadata"].update(decoded_chunk_bytes=16777217)),
("numeric-budget",lambda v:v["metadata"].update(numeric_bytes_read=4194305)),
("numeric-count",lambda v:v["metadata"].update(numeric_values_read=0)),
("sparse-count",lambda v:v["metadata"].update(sparse_entries_scanned=65537)),
("source-bool",lambda v:v["metadata"].update(source_bytes=True)),
("coordinate-unit",lambda v:v["metadata"]["coordinates"].update(unit="micrometers")),
("coordinate-order",lambda v:v["metadata"]["coordinates"].update(axis_order="y reversed")),
("coordinate-path",lambda v:v["metadata"]["coordinates"].update(path="/uns/private")),
("coordinate-shape",lambda v:v["metadata"]["coordinates"].update(shape=[4,3])),
("null-count",lambda v:v["metadata"].update(null_coordinates=1)),
("plottable-count",lambda v:v["metadata"].update(plottable_points=0)),
("feature-count",lambda v:v["choices"].update(feature_count=4)),
("labels-extra",lambda v:v["choices"].update(names=["patient"])),
("spatial-extra",lambda v:v["spatial"].update(patient=["A","B"])),
("point-count",lambda v:v["spatial"]["x"].pop()),
("observation-order",lambda v:v["spatial"].update(observations=[2,1])),
("value-bool",lambda v:v["spatial"]["values"].__setitem__(0,True)),
("coord-nan",lambda v:v["spatial"]["x"].__setitem__(0,float("nan"))),
("value-infinite",lambda v:v["spatial"]["values"].__setitem__(0,float("inf"))),
("over-float32",lambda v:v["spatial"]["values"].__setitem__(0,1e100)),
("selected-extra",lambda v:v["selected"].update(path="/var")),
("selected-bool",lambda v:v["selected"].update(feature=True)),
("selected-count",lambda v:v["selected"].update(observation_count=8193)),
]
@pytest.mark.parametrize("name,mutate",MUTATIONS,ids=[m[0] for m in MUTATIONS])
@pytest.mark.parametrize("storage",["dense","csr","csc"])
def test_mutated_result_rejected(storage,name,mutate):
    v=copy.deepcopy(FIXTURES[storage]["geometry"]);mutate(v)
    with pytest.raises(ValueError):validate_spatial_window_payload(v)
@pytest.mark.parametrize("storage",["dense","csr","csc"])
@pytest.mark.parametrize("field,value",[("kind","tree"),("fmt","nc"),("source_bytes",999),("read_bytes",1),("read_requests",128)])
def test_exact_host_bindings(storage,field,value):
    with pytest.raises(ValueError):validate_spatial_window_payload(FIXTURES[storage]["geometry"],**{field:value})
@pytest.mark.parametrize("storage",["dense","csr","csc"])
@pytest.mark.parametrize("field,value",[("feature",0),("observation_start",0),("observation_count",1)])
def test_exact_selection_binding(storage,field,value):
    v=FIXTURES[storage]["geometry"]
    with pytest.raises(ValueError):validate_spatial_window_payload(v,options={**v["selected"],field:value})
@pytest.mark.parametrize("options",[None,[],{"x":0}])
def test_tree_options_are_empty_dict(options):
    with pytest.raises(ValueError):validate_spatial_window_options("tree",options)
@pytest.mark.parametrize("storage",["dense","csr","csc"])
def test_tree_must_not_claim_numeric_reads(storage):
    for field in ("numeric_values_read","numeric_bytes_read","sparse_entries_scanned","decoded_chunk_bytes","chunks_touched"):
        v=copy.deepcopy(FIXTURES[storage]["tree"]);v["metadata"][field]=1
        with pytest.raises(ValueError):validate_spatial_window_payload(v)
