"""One-shot FITS/TIFF workbench. Only caller-owned bytes; no cache or URLs."""
from __future__ import annotations
import base64
import gzip
import io
import math
import re
import warnings
from contextlib import ExitStack

from app.services.astronomy_workbench_payload import ERROR, FORMATS, LIMITS, WARNING, need, label, validate_astronomy_workbench_options, validate_astronomy_workbench_payload

def _safe(value):
    text = str(value or "")[:128]
    return text if label(text) else "[redacted]"

def _num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and abs(value) <= 1e100 else None
    except (TypeError, ValueError, OverflowError): return None

def _fits_preflight(data):
    """Check declared allocation and block layout before Astropy accesses data."""
    from astropy.io import fits
    offset = count = decoded = 0
    while offset < len(data):
        need(count < 64); start = offset; ended = False
        while not ended:
            need(offset + 2880 <= len(data) and offset-start < 1048576)
            block = data[offset:offset+2880]
            for i in range(0,2880,80):
                if block[i:i+8] == b"END     ": ended = True; break
            offset += 2880
        header = fits.Header.fromstring(data[start:offset].decode("ascii"), sep="")
        need(header.get("SIMPLE") is True if count == 0 else header.get("XTENSION", "").strip() in {"IMAGE", "BINTABLE", "TABLE"})
        need(header.get("GROUPS", False) is False)
        rank = header.get("NAXIS"); need(type(rank) is int and 0 <= rank <= 8)
        shape = [header.get("NAXIS"+str(i+1)) for i in range(rank)]
        need(all(type(v) is int and 0 <= v <= 2**31-1 for v in shape))
        bitpix = header.get("BITPIX"); need(bitpix in {8,16,32,64,-32,-64})
        pcount,gcount = header.get("PCOUNT",0),header.get("GCOUNT",1)
        need(type(pcount) is int and 0 <= pcount <= LIMITS["source_bytes"] and type(gcount) is int and gcount == 1)
        nbytes = ((math.prod(shape) if shape else 0)+pcount)*abs(bitpix)//8
        need(nbytes <= LIMITS["source_bytes"])
        allocation = (math.prod(shape) if shape else 0)*max(8,abs(bitpix)//8)
        if header.get("ZIMAGE") is True:
            rank = header.get("ZNAXIS"); need(type(rank) is int and 1 <= rank <= 8)
            shape = [header.get("ZNAXIS"+str(i+1)) for i in range(rank)]
            need(all(type(v) is int and 1 <= v <= 2**31-1 for v in shape)); allocation = math.prod(shape)*8
        decoded += allocation; need(decoded <= LIMITS["decoded_bytes"])
        offset += math.ceil(nbytes/2880)*2880; need(offset <= len(data)); count += 1
    need(count > 0 and offset == len(data))

def _wcs(header):
    from astropy.wcs import WCS
    try:
        w = WCS(header, relax=False)
        coordinate = w.celestial if w.has_celestial else w
        summary = {"celestial":bool(w.has_celestial), "axis_types":[_safe(v) for v in (coordinate.world_axis_physical_types or [])], "units":[_safe(v) for v in (coordinate.world_axis_units or [])]}
        # A nonseparable celestial sub-WCS cannot be truthfully projected from
        # just x/y; do not guess a coupled spectral/time coordinate.
        return summary, w.celestial if w.has_celestial else None
    except Exception: return {"celestial":False,"axis_types":[],"units":[]}, None

def _world(wcs, x, y):
    if wcs is None: return None
    try:
        out = [_num(v) for v in wcs.all_pix2world([[x,y]],0)[0]]
        return out if len(out) == 2 and all(v is not None for v in out) else None
    except Exception: return None

def _description(index, name, kind, shape, dtype, channels=1, columns=None, rows=0, wcs=None, plane_shape=None):
    return {"index":index,"name":_safe(name),"kind":kind,"shape":list(shape),"plane_shape":list(plane_shape or (shape[-2:] if kind == "image" else [])),"dtype":_safe(dtype),"channels":channels,"columns":columns or [],"row_count":rows,"wcs":wcs or {"celestial":False,"axis_types":[],"units":[]}}

def _fits_catalog(hdus):
    catalog=[]; columns_count=0
    for i,hdu in enumerate(hdus):
        need(i < 64); header=hdu.header
        if not getattr(hdu,"is_image",False) and hasattr(hdu,"columns"):
            need(len(hdu.columns) <= 256); columns_count += len(hdu.columns); need(columns_count <= 256)
            columns=[{"index":j,"name":_safe(c.name),"format":_safe(c.format),"unit":_safe(c.unit)} for j,c in enumerate(hdu.columns)]
            catalog.append(_description(i,hdu.name,"table",[int(header.get("NAXIS2",0))],"table",columns=columns,rows=int(header.get("NAXIS2",0))))
        else:
            shape=list(hdu.shape or []); need(len(shape) <= 8)
            kind="image" if len(shape) >= 2 else "spectrum" if shape else "empty"
            summary,_ = _wcs(header) if kind == "image" else (None,None)
            catalog.append(_description(i,hdu.name,kind,shape,"BITPIX "+str(header.get("BITPIX",8)),wcs=summary))
    return catalog

def _tiff_catalog(tif):
    catalog=[]; total=0
    for i,page in enumerate(tif.pages):
        need(i < 64 and 2 <= len(page.shape) <= 3 and page.dtype.kind in "uif" and page.dtype.itemsize <= 8)
        shape=list(page.shape); total += math.prod(shape)*max(8,page.dtype.itemsize); need(total <= LIMITS["decoded_bytes"])
        channels = int(page.samplesperpixel or 1); need(1 <= channels <= 16 and len(shape) == (2 if channels == 1 else 3))
        need(page.photometric.name in {"MINISBLACK","MINISWHITE","RGB"})
        catalog.append(_description(i,"Page "+str(i+1),"image",shape,str(page.dtype),channels=channels,plane_shape=[int(page.imagelength),int(page.imagewidth)]))
    return catalog

def _geospatial(data):
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import transform_bounds
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO", PROJ_NETWORK="OFF"):
        with MemoryFile(data) as memory:
            with memory.open(driver="GTiff") as src:
                if src.crs is None: return None
                need(1 <= src.count <= 16)
                try: bounds=[_num(v) for v in transform_bounds(src.crs,"EPSG:4326",*src.bounds,densify_pts=21)]
                except Exception: bounds=None
                if bounds and any(v is None for v in bounds): bounds=None
                return {"crs":_safe(str(src.crs)),"bounds":[float(v) for v in src.bounds],"bounds_wgs84":bounds,"resolution":[float(v) for v in src.res],"bands":src.count,"nodata":_num(src.nodata)}

def _plane(handle, fmt, options, description):
    import numpy as np
    need(description["kind"] == "image")
    if fmt == "fits":
        shape = description["shape"]; need(options["band"] == 1 and len(options["slices"]) == len(shape)-2 and all(i < n for i,n in zip(options["slices"],shape[:-2])))
        need(math.prod(shape[-2:]) <= LIMITS["plane_values"])
        hdu=handle[options["dataset"]]
        array=hdu.section[tuple(options["slices"])+(slice(None),slice(None))]
        if array.dtype.kind in "iu": need(not np.any(array > 2**53-1) and not np.any(array < -(2**53-1)))
        return np.asarray(array,dtype=np.float64),_wcs(hdu.header)[1]
    need(not options["slices"])
    page=handle.pages[options["dataset"]]; band=options["band"]; channels=description["channels"]
    need(band <= channels and (band != 0 or channels >= 3))
    output_channels=3 if band == 0 else 1
    need(int(page.imagewidth)*int(page.imagelength)*output_channels <= LIMITS["plane_values"])
    data=page.asarray()
    if channels > 1:
        if int(page.planarconfig) == 2: data=np.moveaxis(data,0,-1)
        data=data[...,:3] if band == 0 else data[...,band-1]
    if data.dtype.kind in "iu": need(not np.any(data > 2**53-1) and not np.any(data < -(2**53-1)))
    data=np.asarray(data,dtype=np.float64)
    # GDAL_NODATA is a numeric TIFF declaration, never interpreted as a path.
    nodata=page.tags.get(42113)
    if nodata is not None:
        text=str(nodata.value).strip("\x00 "); need(len(text) <= 64)
        try: missing=float(text)
        except ValueError: missing=float("nan")
        if math.isfinite(missing): data[data == missing]=np.nan
    return data,None

def _stats(values):
    import numpy as np
    finite=values[np.isfinite(values)]; result={"valid_count":int(finite.size),"missing_count":int(values.size-finite.size)}
    for name,fn in (("minimum",np.min),("maximum",np.max),("mean",np.mean),("median",np.median),("std",np.std),("sum",np.sum)):
        result[name]=_num(fn(finite)) if finite.size else None
    return result

def _limits(values, options):
    import numpy as np
    finite=values[np.isfinite(values)]; need(finite.size > 0)
    if options["interval"] == "manual": return options["low"],options["high"]
    lo=hi=None
    if options["interval"] == "zscale":
        from astropy.visualization import ZScaleInterval
        lo,hi=ZScaleInterval().get_limits(finite)
    if lo is None or not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo: lo,hi=np.percentile(finite,[1,99])
    if hi <= lo: lo,hi=float(finite.min()),float(finite.max())
    if hi <= lo: hi=lo+max(1,abs(lo)*1e-12)
    return float(lo),float(hi)

def _colourize(values, name):
    import numpy as np
    if name == "gray": return np.stack([values]*3,axis=-1)
    if name == "heat": return np.stack([np.clip(values*3-i,0,1) for i in range(3)],axis=-1)
    if name == "cool": return np.stack([values,1-values,np.ones_like(values)],axis=-1)
    # Retain main's compact, explicitly approximate viridis mapping.
    return np.stack([np.clip(.28+.72*values-.45*values**2,0,1),np.clip(.05+1.25*values-.35*values**2,0,1),np.clip(.35+.9*(1-np.abs(values-.45)),0,1)],axis=-1)

def _render(plane,wcs,options):
    import numpy as np
    from PIL import Image
    parts=[plane] if plane.ndim == 2 else [plane[...,i] for i in range(3)]
    limits=[_limits(p,options) for p in parts]; channels=[]
    for p,(low,high) in zip(parts,limits):
        n=np.clip((p-low)/(high-low),0,1)
        if options["stretch"] == "log": n=np.log1p(1000*n)/math.log1p(1000)
        elif options["stretch"] == "sqrt": n=np.sqrt(n)
        elif options["stretch"] == "asinh": n=np.arcsinh(10*n)/np.arcsinh(10)
        if options["invert"]: n=1-n
        channels.append(np.nan_to_num(n,nan=0,posinf=0,neginf=0))
    rgb=_colourize(channels[0],options["colour_map"]) if plane.ndim == 2 else np.stack(channels,axis=-1)
    rgb=np.asarray(np.rint(rgb*255),dtype=np.uint8)
    # Missing samples transparent, not shown as real black/cool-map values.
    valid=np.isfinite(plane) if plane.ndim == 2 else np.all(np.isfinite(plane),axis=-1)
    rgba=np.dstack([rgb,valid.astype(np.uint8)*255]); image=Image.fromarray(rgba,"RGBA")
    image.thumbnail((1024,1024),Image.Resampling.LANCZOS)
    out=io.BytesIO(); image.save(out,format="PNG")
    finite=plane[np.isfinite(plane)]; sample=finite[::max(1,math.ceil(finite.size/500000))]
    lo,hi=_limits(plane,options); counts,edges=np.histogram(sample,bins=96,range=(lo,hi))
    height,width=plane.shape[:2]; corners=[_world(wcs,x,y) for x,y in ((0,0),(width-1,0),(width-1,height-1),(0,height-1))]
    return {"image_base64":base64.b64encode(out.getvalue()).decode("ascii"),"render_width":image.width,"render_height":image.height,"display_limits":[list(p) for p in limits],"statistics":_stats(plane),"histogram":{"counts":counts.tolist(),"edges":edges.tolist(),"sample_count":int(sample.size)},"wcs":{"corners":corners if all(p is not None for p in corners) else None}},image.width < width or image.height < height or sample.size < finite.size

def _cell(value):
    import numpy as np
    if isinstance(value,bytes): return _safe(value.decode("utf-8",errors="replace"))
    if isinstance(value,str): return _safe(value)
    if isinstance(value,(bool,np.bool_)): return bool(value)
    if isinstance(value,(int,np.integer)):
        return int(value) if abs(int(value)) <= 2**53-1 else str(int(value))
    if np.ndim(value):
        arr=np.asarray(value); need(arr.size <= 100 and arr.dtype.kind in "biufSU")
        return [_cell(x) for x in arr.ravel()]
    return _num(value)

def astronomy_workbench_preview(data,fmt,kind="tree",options=None):
    try:
        return _preview(data,fmt,kind,options)
    except Exception as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        raise ValueError(ERROR) from None

def _preview(data,fmt,kind,options):
    import numpy as np
    options=validate_astronomy_workbench_options(kind,options or {})
    need(fmt in FORMATS and isinstance(data,bytes) and 0 < len(data) <= LIMITS["source_bytes"])
    source_bytes=len(data); isfits=fmt not in {"tif","tiff"}
    if isfits and data[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream: data=stream.read(LIMITS["source_bytes"]+1)
        need(len(data) <= LIMITS["source_bytes"])
    with ExitStack() as stack:
        stack.enter_context(warnings.catch_warnings()); warnings.simplefilter("ignore")
        if isfits:
            from astropy.io import fits
            _fits_preflight(data); handle=stack.enter_context(fits.open(io.BytesIO(data),memmap=False,lazy_load_hdus=True,ignore_missing_simple=False)); datasets=_fits_catalog(handle); geo=None
        else:
            import tifffile
            need(data[:4] in {b"II*\x00",b"MM\x00*",b"II+\x00",b"MM\x00+"})
            handle=stack.enter_context(tifffile.TiffFile(io.BytesIO(data))); datasets=_tiff_catalog(handle); geo=_geospatial(data)
        need(datasets)
        result={"contract_version":2,"type":"astronomy-workbench","reader":"astronomy-workbench","kind":kind,"media_type":"application/json","choices":{"datasets":datasets,"geospatial":geo},"selected":options,"metadata":{"format":"fits" if isfits else "tiff","input_mode":"whole","source_bytes":source_bytes,"unpacked_bytes":len(data),"limits":dict(LIMITS),"value_semantics":"file-declared scaling; zero-based indices; upper-exclusive regions"},"warnings":[WARNING],"sampled":False,"workbench":{"action":"inspect"}}
        if kind == "tree": result["tree"]=[{"path":"/datasets/"+str(d["index"]),"node_type":"array","attributes":{"label":d["name"]}} for d in datasets]
        else:
            need(options["dataset"] < len(datasets)); desc=datasets[options["dataset"]]
            if kind == "table":
                need(isfits and desc["kind"] == "table"); ro,co=options["row_offset"],options["column_offset"]; need(ro <= desc["row_count"] and co <= len(desc["columns"]))
                cols=desc["columns"][co:co+32]; hdu=handle[options["dataset"]]
                # Reject variable-length/complex columns before dereferencing a
                # heap descriptor. Fixed vectors remain bounded to 100 values.
                for c in cols:
                    form=str(hdu.columns[c["index"]].format)
                    if isinstance(hdu,fits.TableHDU):
                        match=re.fullmatch(r"([AIFED])(\d+)(?:\.\d+)?",form); need(match is not None and int(match.group(2)) <= 128)
                    else:
                        match=re.fullmatch(r"(\d*)([LXBIJKAED])",form); need(match is not None and int(match.group(1) or 1) <= (128 if match.group(2) == "A" else 100))
                rows=[[_cell(row.field(c["index"])) for c in cols] for row in hdu.data[ro:ro+50]]
                result["workbench"]={"action":"table","columns":cols,"rows":rows,"row_offset":ro,"column_offset":co,"total_rows":desc["row_count"],"total_columns":len(desc["columns"])}; result["sampled"]=len(rows) < desc["row_count"] or len(cols) < len(desc["columns"])
            elif kind == "series":
                need(isfits and desc["kind"] == "spectrum"); n=desc["shape"][0]; stride=max(1,math.ceil(n/2000)); values=np.asarray(handle[options["dataset"]].data)[::stride]
                result["workbench"]={"action":"spectrum","indices":list(range(0,n,stride)),"values":[_num(x) for x in values],"stride":stride,"total_points":n}; result["sampled"]=stride > 1
            else:
                plane,wcs=_plane(handle,"fits" if isfits else "tiff",options,desc)
                need(plane.dtype.kind == "f" and plane.size <= LIMITS["plane_values"] and not np.any(np.isfinite(plane)&(np.abs(plane) > 1e90)))
                height,width=plane.shape[:2]; channels=1 if plane.ndim == 2 else 3
                w={"action":options["action"],"width":width,"height":height,"channels":channels}; result["workbench"]=w
                if options["action"] == "render":
                    rendering,result["sampled"]=_render(plane,wcs,options); w.update(rendering)
                elif options["action"] == "pixel":
                    x,y=options["x"],options["y"]; need(x < width and y < height)
                    w.update({"x":x,"y":y,"value":_num(plane[y,x]) if channels == 1 else [_num(x) for x in plane[y,x]],"world":_world(wcs,x,y)})
                elif options["action"] == "region":
                    x0,y0,x1,y1=options["bounds"]; need(x1 <= width and y1 <= height); region=plane[y0:y1,x0:x1]
                    w.update({"bounds":options["bounds"],"pixel_count":int(region.size),"statistics":_stats(region)})
                else:
                    from scipy.ndimage import gaussian_filter,maximum_filter
                    if channels == 3: plane=np.nanmean(plane,axis=-1)
                    finite=plane[np.isfinite(plane)]; need(finite.size > 0); background=float(np.median(finite)); noise=float(1.4826*np.median(np.abs(finite-background)))
                    if noise <= 0: noise=float(np.std(finite))
                    threshold=background+options["threshold_sigma"]*noise; sources=[]; truncated=False
                    if noise > 0:
                        smooth=gaussian_filter(np.nan_to_num(plane,nan=background,posinf=background,neginf=background),sigma=1)
                        mask=(smooth == maximum_filter(smooth,size=5)) & (smooth > threshold) & np.isfinite(plane)
                        ys,xs=np.where(mask); order=np.argsort(smooth[ys,xs],kind="stable")[::-1][:2000]; truncated=len(xs) > 2000
                        sources=[{"id":i+1,"x":int(xs[j]),"y":int(ys[j]),"peak":float(plane[ys[j],xs[j]]),"snr":float((plane[ys[j],xs[j]]-background)/noise),"world":_world(wcs,int(xs[j]),int(ys[j]))} for i,j in enumerate(order)]
                    w.update({"background":background,"noise":noise,"threshold":threshold,"sources":sources,"truncated":truncated}); result["sampled"]=truncated
        return validate_astronomy_workbench_payload(result,kind=kind,options=options,fmt=fmt,size=source_bytes)
