"""Independent Ripple result provenance invariants; no scientific dependencies."""
import pytest

from app.services.ripple_window_payload import DTYPES, LIMITS, WARNING, expected_axes, validate_ripple_window_payload

def payload(kind="series", dtype="float32"):
    width=DTYPES[dtype][1]
    cube={"width":2,"height":2,"depth":2,"dtype":dtype,
          "byte_order":"dont-care" if width==1 else "big-endian",
          "data_offset":0,"signal_type":"unspecified","axes":[
              {"label":label,"unit":None,"origin":0,"scale":1,
               "calibration":"declared","origin_defaulted":True}
              for label in ("height","width","depth")]}
    selected={} if kind=="tree" else {"channel":0,"x":0,"y":0,"width":2,"height":1} if kind=="image" else {"x":0,"y":0,"channel_start":0,"channel_count":2}
    result={"contract_version":2,"type":"ripple-window","reader":"ripple-window","kind":kind,
            "media_type":"application/json","choices":{"cube":cube},"selected":selected,
            "warnings":[WARNING],"sampled":False,"metadata":{
                "format":"rpl","input_mode":"window","source_bytes":100+8*width,
                "header_bytes":100,"data_bytes":8*width,"read_bytes":100 if kind=="tree" else 100+2*width,
                "read_requests":1 if kind=="tree" else 2,"null_values":0,"limits":dict(LIMITS),
                "record_by":"vector","value_semantics":"raw-storage"}}
    if kind=="tree":
        result["tree"]=[{"path":"/cube","node_type":"array","shape":[2,2,2],"dtype":dtype}]
    else:
        result["array"]={"shape":[1,2] if kind=="image" else [2],
                         "dimensions":["height","width"] if kind=="image" else ["depth"],
                         "dtype":dtype,"values":[0,1]}
        result["axes"]=expected_axes(kind,selected,cube)
    return result

@pytest.mark.parametrize("kind",["tree","image","series"])
@pytest.mark.parametrize("dtype",list(DTYPES))
def test_valid_minimum_read_accounting_and_zero_default_origin(kind,dtype):
    value=payload(kind,dtype)
    assert validate_ripple_window_payload(value,kind=kind,options=value["selected"],
        source_bytes=value["metadata"]["source_bytes"],read_bytes=value["metadata"]["read_bytes"],
        read_requests=value["metadata"]["read_requests"]) is value

@pytest.mark.parametrize("kind",["image","series"])
@pytest.mark.parametrize("dtype",list(DTYPES))
@pytest.mark.parametrize("mutation",["one-read","header-only","one-byte-short"])
def test_worker_cannot_return_values_without_minimum_raw_reads(kind,dtype,mutation):
    value=payload(kind,dtype);m=value["metadata"]
    if mutation=="one-read":m["read_requests"]=1
    elif mutation=="header-only":m["read_bytes"]=m["header_bytes"]
    else:m["read_bytes"]-=1
    with pytest.raises(ValueError):
        validate_ripple_window_payload(value,read_bytes=m["read_bytes"],read_requests=m["read_requests"])

@pytest.mark.parametrize("kind",["tree","image","series"])
@pytest.mark.parametrize("axis",[0,1,2])
@pytest.mark.parametrize("origin",[-1,1])
def test_default_origin_cannot_claim_a_nonzero_coordinate(kind,axis,origin):
    value=payload(kind);value["choices"]["cube"]["axes"][axis]["origin"]=origin
    if kind!="tree":value["axes"]=expected_axes(kind,value["selected"],value["choices"]["cube"])
    with pytest.raises(ValueError):validate_ripple_window_payload(value)

@pytest.mark.parametrize("axis",[0,1,2])
def test_explicit_nonzero_origin_is_still_supported(axis):
    value=payload("tree")
    value["choices"]["cube"]["axes"][axis].update(origin=-25,origin_defaulted=False)
    assert validate_ripple_window_payload(value) is value

def test_ev_per_channel_zero_default_is_valid_nonzero_default_is_not():
    value=payload("tree")
    value["choices"]["cube"]["axes"][2].update(calibration="ev-per-chan",unit="eV",scale=5)
    assert validate_ripple_window_payload(value) is value
    value["choices"]["cube"]["axes"][2]["origin"]=100
    with pytest.raises(ValueError):validate_ripple_window_payload(value)
