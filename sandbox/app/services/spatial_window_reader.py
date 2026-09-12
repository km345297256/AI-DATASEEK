"""AnnData spatial ordinal windows using the existing approved HDF5 range VFD.

Only allowlisted scalar encoding attributes use a fixed-memory H5A conversion.
This bounds Python output, NOT native HDF5 heap/RSS; range and worker process
limits still apply. No label datasets, obs columns, images, uns or layers read.
"""
from __future__ import annotations
import math
import re
from .array_window_reader import RangeFile, _describe, _slice_plan, _verify_chunks
from .spatial_window_payload import (AXES,LABELS,LIMITS,READ_STRATEGIES,SCOPE,SPARSE_SEMANTICS,VALUE_SEMANTICS,WARNINGS,
    dtype,fail,integer,require,validate_spatial_window_options,validate_spatial_window_payload)

class Metadata:
    def __init__(self):self.bytes=0
    def text(self,obj,name):
        import h5py
        import numpy as np
        require(name in {"encoding-type","encoding-version","_index"} and name in obj.attrs)
        aid=obj.attrs.get_id(name);info=h5py.check_string_dtype(aid.dtype)
        require(aid.shape==() and info is not None and (info.length is None or 0<info.length<=127))
        require(self.bytes+128<=LIMITS["max_attribute_buffer_bytes"]);self.bytes+=128
        # Never let h5py create an object array / arbitrary-length Python string.
        out=np.zeros((),dtype="S128");mem=h5py.h5t.C_S1.copy()
        mem.set_size(128);mem.set_cset(h5py.h5t.CSET_UTF8);mem.set_strpad(h5py.h5t.STR_NULLPAD)
        aid.read(out,mtype=mem)
        data=out.tobytes();require(data[-1:]==b"\0")
        raw=data.rstrip(b"\0");require(raw and b"\0" not in raw)
        value=raw.decode("utf-8",errors="strict")
        require(len(value)<=127 and not re.search(r"[/\\<>\x00-\x1f\x7f]",value) and value not in {".",".."})
        return value
    def encoding(self,obj,expected,version):
        require(self.text(obj,"encoding-type")==expected and self.text(obj,"encoding-version")==version)
    def shape(self,obj):
        import numpy as np
        require("shape" in obj.attrs)
        aid=obj.attrs.get_id("shape")
        require(aid.shape==(2,) and aid.dtype.kind in {"i","u"} and aid.dtype.itemsize in {4,8} and not aid.dtype.metadata)
        amount=2*aid.dtype.itemsize;require(self.bytes+amount<=4096);self.bytes+=amount
        data=obj.attrs["shape"]
        require(isinstance(data,np.ndarray) and data.shape==(2,))
        value=[int(v) for v in data];require(all(integer(v,1) for v in value));return value

def child(group,name,cls):
    import h5py
    require(name in group and group.get(name,getlink=True,getclass=True) is h5py.HardLink)
    value=group[name];require(isinstance(value,cls));return value

def numeric(group,name,rank):
    import h5py
    value=child(group,name,h5py.Dataset)
    # Empty sparse data/indices are allowed, but _describe rejects zero shape.
    if value.shape==(0,) and rank==1:
        props=value.id.get_create_plist()
        require(dtype(value.dtype.str) and not value.dtype.metadata and props.get_layout()!=h5py.h5d.VIRTUAL
                and not props.get_external_count() and props.get_nfilters()==0)
    else:require(_describe(value)["selectable"])
    require(value.ndim==rank and dtype(value.dtype.str));return value

def index_metadata(group,name,size,meta):
    import h5py
    meta.encoding(group,"dataframe","0.2.0")
    key=meta.text(group,"_index")
    value=child(group,key,h5py.Dataset);props=value.id.get_create_plist()
    require(value.shape==(size,) and h5py.check_string_dtype(value.dtype) is not None
            and props.get_layout()!=h5py.h5d.VIRTUAL and not props.get_external_count() and props.get_nfilters()<=3
            and [props.get_filter(i)[0] for i in range(props.get_nfilters())] in [[],[1],[2],[3],[2,1],[2,3],[1,3],[2,1,3]])
    # Only index type/shape is inspected; never read labels or other dataframe fields.
    meta.encoding(value,"string-array","0.2.0")

class NumericBudget:
    def __init__(self,size):self.size=size;self.chunks=self.decoded=self.values=self.bytes=0
    def read(self,dataset,selection):
        import numpy as np
        index,_,chunks,decoded=_slice_plan(dataset,{"selection":selection})
        count=math.prod(len(range(v["start"],v["stop"],v["step"])) if isinstance(v,dict) else 1 for v in selection)
        require(0<count<=65536 and self.chunks+chunks<=128 and self.decoded+decoded<=LIMITS["max_decoded_chunk_bytes"]
                and self.bytes+count*dataset.dtype.itemsize<=LIMITS["max_numeric_bytes"] and self.values+count<=200000)
        self.chunks+=chunks;self.decoded+=decoded;self.values+=count;self.bytes+=count*dataset.dtype.itemsize
        _verify_chunks(dataset,selection,self.size)
        value=dataset[index];require(value.size==count)
        if value.dtype.kind in {"i","u"}:require(not np.any(value>2**53-1) and not np.any(value<-(2**53-1)))
        return value.reshape(-1)

def span(start,stop):return {"start":start,"stop":stop,"step":1}
def _directory(handle,meta):
    import h5py
    meta.encoding(handle,"anndata","0.1.0")
    obsm=child(handle,"obsm",h5py.Group);meta.encoding(obsm,"dict","0.1.0")
    coordinates=numeric(obsm,"spatial",2);meta.encoding(coordinates,"array","0.2.0")
    require(coordinates.shape[1]==2)
    obs=coordinates.shape[0]
    require("X" in handle and handle.get("X",getlink=True,getclass=True) is h5py.HardLink)
    value=handle["X"]
    if isinstance(value,h5py.Dataset):
        expression=numeric(handle,"X",2);meta.encoding(expression,"array","0.2.0")
        shape=list(expression.shape);storage="dense";datasets={"expression":expression}
        matrix={"storage":storage,"shape":shape,"dtype":expression.dtype.str,"nnz":None,"index_dtype":None,"pointer_dtype":None}
    else:
        require(isinstance(value,h5py.Group))
        encoding=meta.text(value,"encoding-type");require(encoding in {"csr_matrix","csc_matrix"})
        require(meta.text(value,"encoding-version")=="0.1.0")
        shape=meta.shape(value);storage=encoding[:3]
        expression=numeric(value,"data",1);indices=numeric(value,"indices",1);pointers=numeric(value,"indptr",1)
        nnz=expression.shape[0]
        require(indices.shape==(nnz,) and indices.dtype.kind in {"i","u"} and pointers.dtype.kind in {"i","u"}
                and pointers.shape==((shape[0] if storage=="csr" else shape[1])+1,))
        require(len({h5py.h5o.get_info(v.id).addr for v in (expression,indices,pointers)})==3)
        datasets={"expression":expression,"indices":indices,"pointers":pointers}
        matrix={"storage":storage,"shape":shape,"dtype":expression.dtype.str,"nnz":nnz,"index_dtype":indices.dtype.str,"pointer_dtype":pointers.dtype.str}
    require(shape[0]==obs and all(integer(n,1) for n in shape))
    index_metadata(child(handle,"obs",h5py.Group),"obs",obs,meta)
    index_metadata(child(handle,"var",h5py.Group),"var",shape[1],meta)
    return coordinates,datasets,matrix

def _values(value):
    result=[]
    for item in value.tolist():
        result.append(item if math.isfinite(item) else None)
    return result

def _geometry(coordinates,datasets,matrix,options,budget):
    import numpy as np
    start,count,feature=options["observation_start"],options["observation_count"],options["feature"]
    require(start+count<=matrix["shape"][0] and feature<matrix["shape"][1])
    xy=budget.read(coordinates,[span(start,start+count),span(0,2)]).reshape(count,2)
    storage=matrix["storage"];entries=0
    if storage=="dense":out=budget.read(datasets["expression"],[span(start,start+count),feature])
    else:
        pointers=datasets["pointers"];nnz=matrix["nnz"]
        first=budget.read(pointers,[0])[0];last=budget.read(pointers,[pointers.shape[0]-1])[0]
        require(first==0 and last==nnz)
        major_start,major_count=(start,count) if storage=="csr" else (feature,1)
        pointer=budget.read(pointers,[span(major_start,major_start+major_count+1)])
        require(np.all(pointer>=0) and np.all(pointer<=nnz) and np.all(pointer[1:]>=pointer[:-1]))
        lo,hi=int(pointer[0]),int(pointer[-1]);entries=hi-lo
        require(entries<=LIMITS["max_sparse_entries"])
        # Sparse missing entries have defined zeros; duplicates are never silently summed.
        out=np.zeros(count,dtype=datasets["expression"].dtype)
        if entries:
            indices=budget.read(datasets["indices"],[span(lo,hi)])
            require(np.all(indices>=0) and np.all(indices<matrix["shape"][1 if storage=="csr" else 0]))
            values=budget.read(datasets["expression"],[span(lo,hi)])
            for row in range(major_count):
                a,b=int(pointer[row])-lo,int(pointer[row+1])-lo
                ix=indices[a:b];require(np.all(ix[1:]>ix[:-1]))
                if storage=="csr":
                    matches=np.flatnonzero(ix==feature)
                    if matches.size:out[row]=values[a+int(matches[0])]
                else:
                    matches=(ix>=start)&(ix<start+count)
                    out[(ix[matches]-start).astype(np.intp)]=values[a:b][matches]
    return {"observations":list(range(start,start+count)),"x":_values(xy[:,0]),"y":_values(xy[:,1]),"values":_values(out)},entries

def spatial_window_preview(read_range,size,fmt,kind="tree",options=None,limits=None):
    options=validate_spatial_window_options(kind,{} if options is None else options);require(fmt=="h5ad")
    source=None
    try:
        source=RangeFile(read_range,size,limits)
        import h5py
        while h5py.h5pl.size():h5py.h5pl.remove(0)
        # H5AD is an HDF5 file; signature detection also handles standard userblocks.
        found=False
        for offset in [0]+[2**i for i in range(9,21)]:
            if offset+8>size:break
            source.seek(offset)
            if source.read(8)==b"\x89HDF\r\n\x1a\n":found=True;break
        require(found);source.seek(0)
        meta=Metadata();budget=NumericBudget(size)
        with h5py.File(source,"r",driver="fileobj",rdcc_nbytes=4194304,rdcc_nslots=257,rdcc_w0=1) as handle:
            coordinates,datasets,matrix=_directory(handle,meta)
            obs,features=matrix["shape"]
            result={"contract_version":2,"type":"spatial-window","reader":"spatial-window","kind":kind,"media_type":"application/json",
                    "choices":{"observation_count":obs,"feature_count":features,"feature_labels":"zero-based ordinal"},
                    "selected":options,"warnings":list(WARNINGS),"sampled":False}
            scanned=null_coords=null_expr=plottable=0
            if kind=="tree":
                result["tree"]=[{"path":"/spatial","node_type":"array","shape":[obs,2]},{"path":"/X","node_type":"array","shape":[obs,features]}]
            else:
                s,scanned=_geometry(coordinates,datasets,matrix,options,budget);result["spatial"]=s
                null_coords=sum(v is None for k in ("x","y") for v in s[k]);null_expr=sum(v is None for v in s["values"])
                plottable=sum(all(s[k][i] is not None for k in ("x","y","values")) for i in range(len(s["x"])))
                result["sampled"]=options["observation_count"]<obs
            result["metadata"]={"format":"h5ad","h5ad_encoding":"0.1.0","input_mode":"window","source_bytes":size,"read_bytes":source.read_bytes,"read_requests":source.read_requests,
                "matrix":matrix,"coordinates":{"path":"/obsm/spatial","shape":[obs,2],"dtype":coordinates.dtype.str,"unit":None,"axis_order":AXES},
                "labels":LABELS,"value_semantics":VALUE_SEMANTICS,"sparse_semantics":SPARSE_SEMANTICS,"scope":SCOPE,"read_strategy":READ_STRATEGIES[matrix["storage"]],
                "attribute_buffer_bytes":meta.bytes,"chunks_touched":budget.chunks,"decoded_chunk_bytes":budget.decoded,
                "numeric_values_read":budget.values,"numeric_bytes_read":budget.bytes,"sparse_entries_scanned":scanned,
                "null_coordinates":null_coords,"null_expressions":null_expr,"plottable_points":plottable,"limits":dict(LIMITS)}
        result["metadata"].update(read_bytes=source.read_bytes,read_requests=source.read_requests)
        return validate_spatial_window_payload(result,kind=kind,options=options,fmt=fmt,source_bytes=size,read_bytes=source.read_bytes,read_requests=source.read_requests)
    except Exception:raise ValueError("该 AnnData 空间结构、数值窗口或读取预算不在当前安全支持范围。") from None
    finally:
        if source is not None:source.close()
