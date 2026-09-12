"""Synthetic spatial files; official AnnData writer only in reference mode."""
from __future__ import annotations
import io
import os
import tempfile

def spatial_bytes(storage="dense",*,official=False,compression=None):
    import h5py
    import numpy as np
    import scipy.sparse as sp
    x=np.array([[0,2,0],[3,0,4],[0,5,6],[7,0,0]],dtype="<f4")
    coordinates=np.array([[1,10],[2,20],[3,30],[4,40]],dtype="<f8")
    matrix=x if storage=="dense" else sp.csr_matrix(x) if storage=="csr" else sp.csc_matrix(x)
    if official:
        import anndata as ad
        import pandas as pd
        obj=ad.AnnData(X=matrix,obs=pd.DataFrame({"patient":["PRIVATE-A"]*4},index=["病例-"+str(i) for i in range(4)]),
                       var=pd.DataFrame(index=["基因甲","ENSG0002","GENE-C"]),obsm={"spatial":coordinates})
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,"synthetic.h5ad");obj.write_h5ad(path,compression=compression)
            with open(path,"rb") as f:data=f.read()
            expected=ad.read_h5ad(path)
            assert np.array_equal(expected.obsm["spatial"],coordinates)
            assert np.array_equal(expected.X if storage=="dense" else expected.X.toarray(),x)
            return data
    b=io.BytesIO()
    with h5py.File(b,"w") as f:
        def encoded(obj,encoding,version):obj.attrs.update({"encoding-type":encoding,"encoding-version":version})
        encoded(f,"anndata","0.1.0")
        for name,count in (("obs",4),("var",3)):
            g=f.create_group(name);encoded(g,"dataframe","0.2.0");g.attrs["_index"]="_index"
            g.attrs["column-order"]=np.array([],dtype=h5py.string_dtype())
            d=g.create_dataset("_index",data=np.array(["identity-"+str(i) for i in range(count)],dtype=object),dtype=h5py.string_dtype())
            encoded(d,"string-array","0.2.0")
        obsm=f.create_group("obsm");encoded(obsm,"dict","0.1.0")
        d=obsm.create_dataset("spatial",data=coordinates,compression=compression);encoded(d,"array","0.2.0")
        if storage=="dense":
            d=f.create_dataset("X",data=matrix,compression=compression);encoded(d,"array","0.2.0")
        else:
            g=f.create_group("X");encoded(g,storage+"_matrix","0.1.0");g.attrs["shape"]=matrix.shape
            for key in ("data","indices","indptr"):g.create_dataset(key,data=getattr(matrix,key),compression=compression)
    return b.getvalue()

def preview(data,kind="tree",options=None,limits=None):
    from app.services.spatial_window_reader import spatial_window_preview
    calls=[]
    def read(offset,length):calls.append((offset,length));return data[offset:offset+length]
    value=spatial_window_preview(read,len(data),"h5ad",kind,options,limits)
    return value,calls

def mutate(data,fn):
    import h5py
    b=io.BytesIO(data)
    with h5py.File(b,"r+") as f:fn(f)
    return b.getvalue()
