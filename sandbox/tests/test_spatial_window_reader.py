import copy
import os
from pathlib import Path
import pytest
from spatial_window_fixtures import spatial_bytes,preview,mutate
from app.services.spatial_window_payload import validate_spatial_window_payload,validate_spatial_window_options
OPT={"feature":1,"observation_start":1,"observation_count":2,"decode":"raw"}

def require_reference():
    try:import anndata
    except ImportError:
        if os.environ.get("AI_DATASEEK_REQUIRE_SPATIAL_REFERENCE")=="1":pytest.fail("AnnData reference required")
        pytest.skip("optional AnnData writer; required in complete gate")

@pytest.mark.parametrize("storage",["dense","csr","csc"])
@pytest.mark.parametrize("compression",[None,"gzip"])
def test_official_anndata_writer_utf8_labels_and_raw_expected(storage,compression):
    require_reference()
    data=spatial_bytes(storage,official=True,compression=compression)
    tree,calls=preview(data)
    assert tree["metadata"]["numeric_values_read"]==0 and tree["metadata"]["matrix"]["storage"]==storage
    assert "PRIVATE" not in str(tree) and "病例" not in str(tree) and "基因" not in str(tree)
    result,calls=preview(data,"geometry",OPT)
    assert result["spatial"]=={"observations":[1,2],"x":[2,3],"y":[20,30],"values":[0,5]}
    assert result["metadata"]["read_bytes"]==sum(n for _,n in calls) and result["metadata"]["read_requests"]==len(calls)
    assert result["metadata"]["attribute_buffer_bytes"]<=4096

@pytest.mark.parametrize("storage",["dense","csr","csc"])
@pytest.mark.parametrize("feature",range(3))
@pytest.mark.parametrize("count",[1,2,4])
def test_all_layouts_raw_selection_and_implicit_zeros(storage,feature,count):
    data=spatial_bytes(storage)
    opt={"feature":feature,"observation_start":0,"observation_count":count,"decode":"raw"}
    result,_=preview(data,"geometry",opt)
    expected=[[0,2,0],[3,0,4],[0,5,6],[7,0,0]]
    assert result["spatial"]["values"]==[row[feature] for row in expected[:count]]
    assert result["sampled"]==(count<4)
    assert validate_spatial_window_payload(result,kind="geometry",options=opt,fmt="h5ad",source_bytes=len(data)) is result

@pytest.mark.parametrize("change",["soft","external","vds","unknown-filter","string-X","missing-spatial","wrong-encoding","non-scalar-encoding","wrong-coordinate-shape","bad-index-shape"])
def test_reject_unsafe_or_unsupported_metadata(change):
    import h5py
    import numpy as np
    def edit(f):
        if change=="soft":del f["X"];f["X"]=h5py.SoftLink("/obsm/spatial")
        elif change=="external":del f["X"];f["X"]=h5py.ExternalLink("/private/source.h5","/X")
        elif change=="vds":
            del f["X"];layout=h5py.VirtualLayout((4,3),dtype="f4");layout[:]=h5py.VirtualSource("/private/source.h5","/X",shape=(4,3));f.create_virtual_dataset("X",layout)
        elif change=="unknown-filter":
            del f["X"];f.create_dataset("X",shape=(4,3),dtype="f4",compression="lzf")
        elif change=="string-X":
            del f["X"];f.create_dataset("X",data=np.array(["x"]*12,dtype=object).reshape(4,3),dtype=h5py.string_dtype())
        elif change=="missing-spatial":del f["obsm/spatial"]
        elif change=="wrong-encoding":f.attrs["encoding-type"]="not-anndata"
        elif change=="non-scalar-encoding":f.attrs["encoding-type"]=["anndata"]
        elif change=="wrong-coordinate-shape":
            del f["obsm/spatial"];f["obsm/spatial"]=np.zeros((4,3))
        else:
            del f["obs/_index"];f["obs/_index"]=np.array(["x"],dtype=h5py.string_dtype())
    with pytest.raises(ValueError):preview(mutate(spatial_bytes(),edit))

@pytest.mark.parametrize("storage",["csr","csc"])
@pytest.mark.parametrize("change",["duplicates","unsorted","negative-index","bad-pointer","bad-endpoint"])
def test_sparse_invalid_inspected_segments_rejected_not_repaired(storage,change):
    def edit(f):
        group=f["X"]
        if change=="duplicates":group["indices"][1]=group["indices"][2]
        elif change=="unsorted":group["indices"][1:3]=group["indices"][1:3][::-1]
        elif change=="negative-index":group["indices"][1]=-1
        elif change=="bad-pointer":group["indptr"][1]=999
        else:group["indptr"][-1]-=1
    data=mutate(spatial_bytes(storage),edit)
    opt={"feature":0,"observation_start":0,"observation_count":4,"decode":"raw"}
    with pytest.raises(ValueError):preview(data,"geometry",opt)

def test_null_coordinates_and_raw_expression_are_not_scaled_or_masked():
    def edit(f):
        f["X"][1,1]=float("nan");f["X"][2,1]=-2;f["X"].attrs["scale_factor"]=100
        f["obsm/spatial"][2,0]=float("inf")
    result,_=preview(mutate(spatial_bytes(),edit),"geometry",OPT)
    assert result["spatial"]["values"]==[None,-2] and result["spatial"]["x"]==[2,None]
    assert result["metadata"]["null_coordinates"]==1 and result["metadata"]["null_expressions"]==1 and result["metadata"]["plottable_points"]==0

@pytest.mark.parametrize("options",[{},dict(OPT,feature=True),dict(OPT,observation_count=8193),dict(OPT,observation_start=-1),dict(OPT,decode="normalized"),dict(OPT,path="/other")])
def test_options_strict_before_any_source_read(options):
    from app.services.spatial_window_reader import spatial_window_preview
    calls=[]
    with pytest.raises(ValueError):spatial_window_preview(lambda o,n:calls.append((o,n)),1024,"h5ad","geometry",options)
    assert not calls

@pytest.mark.parametrize("field,value",[("source_bytes",1),("read_bytes",1),("read_requests",128),("format","h5")])
def test_host_accounting_bindings(field,value):
    result,_=preview(spatial_bytes(),"geometry",OPT)
    with pytest.raises(ValueError):validate_spatial_window_payload(result,**{("fmt" if field=="format" else field):value})

def test_request_selection_exact_binding():
    result,_=preview(spatial_bytes(),"geometry",OPT)
    for key,value in (("feature",0),("observation_start",0),("observation_count",1)):
        with pytest.raises(ValueError):validate_spatial_window_payload(result,options={**OPT,key:value})

def test_budget_and_callback_cancellation():
    data=spatial_bytes()
    with pytest.raises(ValueError):preview(data,limits={"max_read_bytes":1024,"max_total_bytes":1024,"max_reads":1})
    class Cancel(BaseException):pass
    from app.services.spatial_window_reader import spatial_window_preview
    def read(o,n):raise Cancel()
    with pytest.raises(Cancel):spatial_window_preview(read,len(data),"h5ad")

def test_backend_same_source():
    root=Path(__file__).resolve().parents[2]
    backend=root/"backend/app/application/services/spatial_window_visualization.py"
    if backend.exists():assert backend.read_bytes()==(root/"sandbox/app/services/spatial_window_payload.py").read_bytes()

@pytest.mark.parametrize("content",["x"*128,"anndata"+"x"*10000,b"\xff",""])
def test_encoding_attributes_never_truncate_into_an_accepted_value(content):
    data=mutate(spatial_bytes(),lambda f:f.attrs.__setitem__("encoding-type",content))
    with pytest.raises(ValueError):preview(data)

def test_tree_and_geometry_never_decode_identity_datasets(monkeypatch):
    import h5py
    original=h5py.Dataset.__getitem__;names=[]
    def guard(self,*args,**kwargs):
        names.append(self.name)
        assert not self.name.startswith(("/obs/","/var/","/uns/"))
        return original(self,*args,**kwargs)
    data=spatial_bytes()
    monkeypatch.setattr(h5py.Dataset,"__getitem__",guard)
    preview(data);assert names==[]
    preview(data,"geometry",OPT);assert set(names)=={"/X","/obsm/spatial"}

@pytest.mark.parametrize("length",[1,1000000])
def test_forged_compressed_chunk_is_checked_before_native_expression_read(monkeypatch,length):
    import h5py
    import zlib
    data=mutate(spatial_bytes(compression="gzip"),lambda f:f["X"].id.write_direct_chunk((0,0),zlib.compress(b"x"*length)))
    calls=[];original=h5py.Dataset.__getitem__
    def record(self,*args,**kwargs):
        calls.append(self.name)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(h5py.Dataset,"__getitem__",record)
    preview(data)
    with pytest.raises(ValueError):preview(data,"geometry",OPT)
    assert "/X" not in calls

def test_missing_sparse_values_remain_zero_but_overflowing_integers_are_rejected():
    import numpy as np
    def edit(f):
        del f["X"];d=f.create_dataset("X",data=np.full((4,3),2**53,dtype="<u8"))
        d.attrs.update({"encoding-type":"array","encoding-version":"0.2.0"})
    with pytest.raises(ValueError):preview(mutate(spatial_bytes(),edit),"geometry",OPT)

def test_real_gigabyte_file_uses_only_bounded_ranges(tmp_path):
    import h5py
    from app.services.spatial_window_reader import spatial_window_preview
    path=tmp_path/"large-spatial.h5ad";path.write_bytes(spatial_bytes())
    with h5py.File(path,"r+") as f:
        dcpl=h5py.h5p.create(h5py.h5p.DATASET_CREATE);dcpl.set_fill_time(h5py.h5d.FILL_TIME_NEVER)
        extra=h5py.Dataset(h5py.h5d.create(f.id,b"unrelated-padding",h5py.h5t.NATIVE_DOUBLE,
            h5py.h5s.create_simple((150000000,)),dcpl=dcpl))
        extra[0]=1
    size=path.stat().st_size;assert size>1024**3;calls=[]
    with path.open("rb") as stream:
        def read(offset,length):
            calls.append((offset,length));stream.seek(offset);return stream.read(length)
        tree=spatial_window_preview(read,size,"h5ad")
        assert tree["metadata"]["numeric_values_read"]==0;calls.clear()
        result=spatial_window_preview(read,size,"h5ad","geometry",OPT)
    assert result["spatial"]["values"]==[0,5]
    assert sum(n for _,n in calls)<131072 and len(calls)<10
    assert result["metadata"]["read_bytes"]==sum(n for _,n in calls)

def test_sparse_entry_scan_budget_rejects_before_indices_or_values(monkeypatch):
    import h5py
    import numpy as np
    def edit(f):
        # One CSR row with 65,537 distinct columns; valid shape, over preview scan budget.
        del f["X"];g=f.create_group("X")
        g.attrs.update({"encoding-type":"csr_matrix","encoding-version":"0.1.0","shape":[4,65537]})
        g.create_dataset("data",data=np.ones(65537,dtype="f4"));g.create_dataset("indices",data=np.arange(65537,dtype="i4"))
        g.create_dataset("indptr",data=np.array([0,65537,65537,65537,65537],dtype="i4"))
        del f["var/_index"]
        d=f["var"].create_dataset("_index",shape=(65537,),dtype=h5py.string_dtype())
        d.attrs.update({"encoding-type":"string-array","encoding-version":"0.2.0"})
    data=mutate(spatial_bytes(),edit);original=h5py.Dataset.__getitem__;names=[]
    def record(self,*args,**kwargs):names.append(self.name);return original(self,*args,**kwargs)
    monkeypatch.setattr(h5py.Dataset,"__getitem__",record)
    with pytest.raises(ValueError):preview(data,"geometry",{**OPT,"observation_start":0,"observation_count":1})
    assert "/X/data" not in names and "/X/indices" not in names

def test_total_chunk_budget_counts_coordinates_and_expression_together(monkeypatch):
    import h5py
    import numpy as np
    def edit(f):
        for name,shape in (("obsm/spatial",(4,2)),("X",(4,3))):
            del f[name];d=f.create_dataset(name,shape=shape,dtype="f8",chunks=(4,shape[1]),compression="gzip")
            d.attrs.update({"encoding-type":"array","encoding-version":"0.2.0"})
    data=mutate(spatial_bytes(),edit)
    import app.services.spatial_window_reader as module
    original=module._slice_plan
    def plan(dataset,options):
        index,axes,chunks,decoded=original(dataset,options)
        return index,axes,1,10*1024**2
    monkeypatch.setattr(module,"_slice_plan",plan)
    calls=[];getitem=h5py.Dataset.__getitem__
    def record(self,*a,**kw):calls.append(self.name);return getitem(self,*a,**kw)
    monkeypatch.setattr(h5py.Dataset,"__getitem__",record)
    with pytest.raises(ValueError):preview(data,"geometry",OPT)
    assert "/X" not in calls
