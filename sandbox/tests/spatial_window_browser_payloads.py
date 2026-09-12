"""Browser payloads produced by the actual spatial reader from AnnData 0.11.4."""
import json
from spatial_window_fixtures import spatial_bytes,preview
def generate():
    import anndata
    out={"provenance":"AnnData "+anndata.__version__+" official write_h5ad/read_h5ad; synthetic only","cases":{}}
    for storage in ("dense","csr","csc"):
        data=spatial_bytes(storage,official=True,compression="gzip")
        out["cases"][storage]={"tree":preview(data)[0],
            "first":preview(data,"geometry",{"feature":0,"observation_start":0,"observation_count":4,"decode":"raw"})[0],
            "window":preview(data,"geometry",{"feature":1,"observation_start":1,"observation_count":2,"decode":"raw"})[0]}
    return out
if __name__=="__main__":print(json.dumps(generate(),ensure_ascii=False,allow_nan=False))
