import copy
import importlib.util
from pathlib import Path
import numpy as np
import pytest
from app.services.ripple_window_reader import ripple_window_preview,parse_ripple_header
from app.services.ripple_window_payload import validate_ripple_window_payload,validate_ripple_window_options,validate_ripple_resources
from ripple_window_fixtures import fixture,preview,browser_payloads

@pytest.mark.parametrize("dtype",["i1","u1","<i2",">i2","<u2",">u2","<i4",">i4","<u4",">u4","<f4",">f4","<f8",">f8"])
def test_original_vector_order_endian_and_axes(dtype):
    h,d,v=fixture(dtype)
    tree,calls=preview(h,d);assert calls==[(0,len(h))]
    image,calls=preview(h,d,"image",{"channel":3,"x":1,"y":1,"width":3,"height":2})
    assert image["array"]["values"]==v[1:3,1:4,3].ravel().tolist()
    assert image["axes"]==[{"label":"height","unit":"nm","values":[17,14]},{"label":"width","unit":"nm","values":[12,14,16]}]
    spectrum,calls=preview(h,d,"series",{"x":2,"y":1,"channel_start":1,"channel_count":4})
    assert spectrum["array"]["values"]==v[1,2,1:5].tolist()
    assert spectrum["axes"][0]["values"]==[105,110,115,120]
    assert len(calls)==2 and calls[1][1]==4*v.dtype.itemsize

@pytest.mark.parametrize("dtype",["uint8","uint16","int32","float32","float64"])
def test_official_rosettasciio_writer_and_reader_oracle(tmp_path,dtype):
    # Required test dependency, not production; no conditional skip.
    from rsciio.ripple import file_writer,file_reader
    # Native '=' multi-byte arrays make this writer emit ambiguous dont-care.
    # Use explicitly declared big-endian storage; do not infer the writer host.
    data=np.arange(3*4*8,dtype=dtype).astype(np.dtype(dtype).newbyteorder(">" )).reshape(3,4,8)
    axes=[{"size":3,"index_in_array":0,"name":"height","scale":-3,"offset":20,"units":"nm","navigate":True},
          {"size":4,"index_in_array":1,"name":"width","scale":2,"offset":10,"units":"nm","navigate":True},
          {"size":8,"index_in_array":2,"name":"depth","scale":5,"offset":100,"units":"eV","navigate":False}]
    path=tmp_path/"reference.rpl"
    file_writer(str(path),{"data":data,"axes":axes,"metadata":{"General":{"title":"synthetic"},"Signal":{"signal_type":"EDS_TEM"},"Acquisition_instrument":{"TEM":{"Detector":{"EDS":{}},"Stage":{}}}}})
    official=file_reader(str(path))[0]
    assert b"byte-order\tbig-endian" in path.read_bytes() or data.dtype.itemsize==1
    actual,_=preview(path.read_bytes(),path.with_suffix(".raw").read_bytes(),"series",{"x":2,"y":1,"channel_start":1,"channel_count":4})
    assert actual["array"]["values"]==official["data"][1,2,1:5].tolist()
    axis=official["axes"][2]
    assert actual["axes"][0]["values"]==[axis["offset"]+i*axis["scale"] for i in range(1,5)]

@pytest.mark.parametrize("before,after",[(b"record-by\tvector",b"record-by\timage"),(b"depth\t8",b"depth\t1"),(b"offset\t8",b"offset\t7"),
    (b"data-length\t4",b"data-length\t8"),(b"data-type\tfloat",b"data-type\tcomplex"),(b"byte-order\tlittle-endian",b"byte-order\tdont-care"),
    (b"depth-scale\t5",b"depth-scale\t0"),(b"depth-scale\t5",b"depth-scale\t1e-999"),(b"signal\tEDS_TEM",b"signal\tunknown"),
    (b"depth-units\teV",b"depth-units\t/Users/private"),(b"width\t4",b"width\t4\nwidth\t4")])
def test_bad_declarations_rejected_before_raw(before,after):
    h,d,_=fixture();calls=[]
    def read(o,n): calls.append((o,n));return (h.replace(before,after)+d)[o:o+n]
    bad=h.replace(before,after)
    with pytest.raises(ValueError): ripple_window_preview(read,len(bad)+len(d),[{"key":"header","offset":0,"size":len(bad)},{"key":"data","offset":len(bad),"size":len(d)}])
    assert calls==[(0,len(bad))]

def test_extra_metadata_is_not_a_resource_or_identity():
    h,d,_=fixture()
    out,_=preview(h+b"title\t/private/original.raw\nfoo\thttps://example.invalid\n",d)
    assert "private" not in str(out) and "example" not in str(out)
    with pytest.raises(ValueError): preview(h+b"raw-file\t/private/target.raw\n",d)

def test_no_units_or_scale_means_indices_ev_only_marks_default_origin():
    h,d,_=fixture(calibrated=False)
    result,_=preview(h,d,"series",{"x":0,"y":0,"channel_start":0,"channel_count":2})
    assert result["axes"][0]=={"label":"depth","unit":None,"values":[0,1]}
    result,_=preview(h+b"ev-per-chan\t5\n",d,"series",{"x":0,"y":0,"channel_start":0,"channel_count":2})
    assert result["axes"][0]=={"label":"depth","unit":"eV","values":[0,5]}
    assert result["choices"]["cube"]["axes"][2]["origin_defaulted"] is True
    with pytest.raises(ValueError): preview(h+b"depth-units\teV\n",d)
    with pytest.raises(ValueError): preview(h+b"depth-scale\t6\nev-per-chan\t5\n",d)

def test_nonfinite_nulls_not_invented_counts_or_repaired():
    h,d,_=fixture(); values=np.frombuffer(d[8:],dtype="<f4").copy();values[:3]=[np.nan,np.inf,-4]
    result,_=preview(h,d[:8]+values.tobytes(),"series",{"x":0,"y":0,"channel_start":0,"channel_count":3})
    assert result["array"]["values"]==[None,None,-4] and result["metadata"]["null_values"]==2

def test_large_logical_source_only_touches_header_and_explicit_spectrum():
    h,d,_=fixture(calibrated=False)
    h=h.replace(b"width\t4",b"width\t10000").replace(b"height\t3",b"height\t1000").replace(b"depth\t8",b"depth\t100")
    size=8+10000*1000*100*4;calls=[]
    def read(o,n):
        calls.append((o,n))
        if o==0:return h
        assert o==len(h)+8+((900*10000+9999)*100+5)*4 and n==16
        return np.array([5,6,7,8],dtype="<f4").tobytes()
    out=ripple_window_preview(read,len(h)+size,[{"key":"header","offset":0,"size":len(h)},{"key":"data","offset":len(h),"size":size}],"series",{"x":9999,"y":900,"channel_start":5,"channel_count":4})
    assert out["array"]["values"]==[5,6,7,8] and out["metadata"]["read_bytes"]==len(h)+16 and len(calls)==2

@pytest.mark.parametrize("kind,options",[("tree",{"x":0}),("series",{"x":True,"y":0,"channel_start":0,"channel_count":1}),
    ("image",{"x":0,"y":0,"channel":0,"width":129,"height":1}),("series",{"x":0,"y":0,"channel_start":0,"channel_count":16385})])
def test_bad_options(kind,options):
    with pytest.raises(ValueError): validate_ripple_window_options(kind,options)

@pytest.mark.parametrize("mutate",[lambda v:v["metadata"].__setitem__("read_bytes",False),lambda v:v["array"]["values"].__setitem__(0,True),
    lambda v:v["array"]["shape"].__setitem__(0,True),lambda v:v["axes"][0]["values"].__setitem__(0,0),lambda v:v["choices"]["cube"]["axes"][0].__setitem__("unit","/private/x"),
    lambda v:v["selected"].__setitem__("channel",0),lambda v:v.__setitem__("unknown","x")])
def test_private_payload_strict_and_request_bound(mutate):
    value=browser_payloads()["image"]; mutate(value)
    with pytest.raises(ValueError): validate_ripple_window_payload(value,kind="image",options={"channel":3,"x":1,"y":1,"width":3,"height":2})

def test_short_read_cancellation_and_resource_crossing_are_not_bypassed():
    h,d,_=fixture(); resources=[{"key":"header","offset":0,"size":len(h)},{"key":"data","offset":len(h),"size":len(d)}]
    with pytest.raises(ValueError): ripple_window_preview(lambda o,n:b"",len(h)+len(d),resources)
    class Cancelled(BaseException): pass
    def cancelled(o,n): raise Cancelled()
    with pytest.raises(Cancelled): ripple_window_preview(cancelled,len(h)+len(d),resources)
    resources[1]["offset"]-=1
    with pytest.raises(ValueError): validate_ripple_resources(resources,len(h)+len(d))

def test_whole_request_budget_preflight_before_any_raw_fetch():
    h,d,_=fixture()
    with pytest.raises(ValueError): preview(h,d,"series",{"x":0,"y":0,"channel_start":0,"channel_count":4},{"max_read_bytes":1048576,"max_total_bytes":len(h)+15,"max_reads":256})

def test_backend_validator_is_same_pure_contract():
    root=Path(__file__).resolve().parents[2]
    host=root/"backend/app/application/services/ripple_window_visualization.py"
    if not host.exists(): pytest.skip("separate sandbox build context")
    assert host.read_bytes()==(root/"sandbox/app/services/ripple_window_payload.py").read_bytes()
