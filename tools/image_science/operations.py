from __future__ import annotations

import base64, hashlib, json, math, os, subprocess, tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageStat, ExifTags

IMAGE_EXTENSIONS={".jpg",".jpeg",".png",".tif",".tiff"}
SIGNATURE_FORMATS={".jpg":"JPEG",".jpeg":"JPEG",".png":"PNG",".tif":"TIFF",".tiff":"TIFF"}
ROOT=Path(os.environ.get("AI_DATASEEK_OUTPUT_ROOT","/home/ubuntu/output")).resolve()

def safe_output(raw: str) -> Path:
    p=Path(raw).resolve()
    try: p.relative_to(ROOT)
    except ValueError as e: raise ValueError(f"output path must be below {ROOT}") from e
    p.parent.mkdir(parents=True,exist_ok=True); return p

def files(paths: list[str], max_files: int=20000) -> list[Path]:
    out=[]
    for raw in paths:
        p=Path(raw)
        if p.is_dir(): out.extend(sorted(x for x in p.rglob("*") if x.suffix.lower() in IMAGE_EXTENSIONS))
        elif p.suffix.lower() in IMAGE_EXTENSIONS: out.append(p)
    return out[:max_files]

def frame_info(im: Image.Image) -> dict[str,Any]:
    """Describe the original first frame without converting scientific pixels."""
    return {"mode":im.mode,"n_frames":int(getattr(im,"n_frames",1)),"frame_index":0}

def preview_info(im: Image.Image, converted_mode: str="L") -> dict[str,Any]:
    info=frame_info(im)
    warnings=["Preview conversion is not original scientific intensity quantification."]
    if info["n_frames"]>1: warnings.append("Only the first frame is used; remaining TIFF/image frames are not analyzed.")
    if im.mode.startswith("I") or im.mode=="F": warnings.append("Higher-bit-depth or floating-point pixels are converted to an 8-bit preview and may be clipped; original pixel values are not preserved in this result.")
    return {"source_mode":info["mode"],"n_frames":info["n_frames"],"frame_index":0,"analysis_scope":f"first_frame_8bit_{converted_mode}_preview","quantitative":False,"warnings":warnings}

def exif(path: Path) -> dict[str,Any]:
    try:
        with Image.open(path) as im:
            raw=im.getexif(); data={}
            for k,v in raw.items():
                name=ExifTags.TAGS.get(k,str(k)); data[name]=str(v) if not isinstance(v,(int,float,str)) else v
            gps={}
            try:
                raw_gps=raw.get_ifd(ExifTags.IFD.GPSInfo)
                gps={ExifTags.GPSTAGS.get(k,str(k)):v for k,v in raw_gps.items()}
            except Exception: pass
            return {"capture_time":data.get("DateTimeOriginal") or data.get("DateTime"),"camera_make":data.get("Make"),"camera_model":data.get("Model"),"orientation":data.get("Orientation"),"gps":gps,"exif_fields":len(data)}
    except Exception: return {"capture_time":None,"camera_make":None,"camera_model":None,"orientation":None,"gps":{},"exif_fields":0}

def gps_coords(info: dict[str,Any]) -> tuple[float,float] | None:
    gps=info.get("gps") or {}
    def value(v):
        if isinstance(v,(tuple,list)) and len(v)==3: return float(v[0])+float(v[1])/60+float(v[2])/3600
        return float(v)
    try:
        lat=value(gps.get("GPSLatitude")); lon=value(gps.get("GPSLongitude"));
        if str(gps.get("GPSLatitudeRef","N")).upper()=="S": lat=-lat
        if str(gps.get("GPSLongitudeRef","E")).upper()=="W": lon=-lon
        return lon,lat
    except Exception: return None

def dump(payload: Any, output: str|None):
    if output: safe_output(output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"success":True,**payload}

def collection(a):
    ps=files([a["input_dir"]],a.get("max_files",10000)); rows=[]
    for p in ps:
        try:
            with Image.open(p) as im: row={"path":str(p),"name":p.name,"format":im.format,"width":im.width,"height":im.height,**frame_info(im),"analysis_scope":"first_frame_metadata","size_bytes":p.stat().st_size,"has_alpha":im.mode in ("RGBA","LA")}; row.update(exif(p)); rows.append(row)
        except Exception as e: rows.append({"path":str(p),"error":f"{type(e).__name__}: {e}"})
    return dump({"operation":"image_collection_inspect","file_count":len(rows),"files":rows,"gps_count":sum(bool(gps_coords(x)) for x in rows),"exif_count":sum(x.get("exif_fields",0)>0 for x in rows),"formats":sorted({x.get("format") for x in rows if x.get("format")})},a.get("output_path"))

def metadata(a):
    rows=[]
    for p in files(a["input_paths"]):
        try:
            with Image.open(p) as im: row={"path":str(p),"name":p.name,"format":im.format,"width":im.width,"height":im.height,**frame_info(im),"analysis_scope":"first_frame_metadata"}; row.update(exif(p)); row["gps_coordinates"]=gps_coords(row); rows.append(row)
        except Exception as e: rows.append({"path":str(p),"error":str(e)})
    return dump({"operation":"image_metadata_extract","records":rows},a.get("output_path"))

def integrity(a):
    rows=[]
    for p in files(a["input_paths"]):
        row={"path":str(p),"readable":False,"extension":p.suffix.lower()}
        try:
            with Image.open(p) as im:
                im.verify()
            with Image.open(p) as im:
                im.load()
                extrema=im.getextrema(); bands=extrema if isinstance(extrema[0],tuple) else [extrema]
                row.update(readable=True,format=im.format,width=im.width,height=im.height,**frame_info(im),blank=all(low==high for low,high in bands),blank_scope="original_first_frame_values",verification_scope="container_header_and_first_frame",signature_match=im.format==SIGNATURE_FORMATS.get(p.suffix.lower()))
                if row["n_frames"]>1: row["warnings"]=["Only the first frame is decoded; later frame integrity and blankness are not verified."]
        except Exception as e: row["error"]=f"{type(e).__name__}: {e}"
        rows.append(row)
    return dump({"operation":"image_integrity_check","records":rows,"invalid_count":sum(not x["readable"] for x in rows),"signature_mismatch_count":sum(x.get("signature_match") is False for x in rows)},a.get("output_path"))

def phash(im: Image.Image) -> int:
    small=im.convert("L").resize((16,16)); mean=sum(small.getdata())/256; return sum((1<<i) for i,v in enumerate(small.getdata()) if v>=mean)
def dup(a):
    rows=[]; exact={}; hashes=[]; warnings=set()
    for p in files(a["input_paths"]):
        digest=hashlib.sha256(p.read_bytes()).hexdigest()
        try:
            with Image.open(p) as im: warnings.update(preview_info(im)["warnings"]); h=phash(im)
        except Exception: continue
        rows.append({"path":str(p),"sha256":digest,"phash":h}); exact.setdefault(digest,[]).append(str(p)); hashes.append((str(p),h))
    similar=[]; distance=a.get("distance",5)
    for i,(p,h) in enumerate(hashes):
        for q,k in hashes[i+1:]:
            d=(h^k).bit_count()
            if d<=distance: similar.append({"left":p,"right":q,"distance":d})
    return dump({"operation":"image_duplicate_detect","exact_groups":[v for v in exact.values() if len(v)>1],"similar_pairs":similar,"image_count":len(rows),"analysis_scope":{"exact_duplicates":"whole_file_sha256","similar_pairs":"first_frame_8bit_grayscale_perceptual_hash"},"warnings":sorted(warnings)},a.get("output_path"))

def quality(a):
    rows=[]
    for p in files(a["input_paths"]):
        try:
            with Image.open(p) as im:
                gray=im.convert("L"); stat=ImageStat.Stat(gray); hist=gray.histogram(); total=sum(hist); dark=sum(hist[:8])/total; bright=sum(hist[248:])/total
                rows.append({"path":str(p),"width":im.width,"height":im.height,**preview_info(im),"mean":stat.mean[0],"stddev":stat.stddev[0],"dark_ratio":dark,"bright_ratio":bright,"near_blank":stat.stddev[0]<1.0,"sharpness_indicator":stat.stddev[0]})
        except Exception as e: rows.append({"path":str(p),"error":str(e)})
    return dump({"operation":"image_quality_assess","records":rows},a.get("output_path"))

def contact(a):
    outdir=safe_output(a["output_dir"]); outdir.mkdir(parents=True,exist_ok=True); paths=files(a["input_paths"],1000); cols=a.get("columns",4); thumb=a.get("thumb_size",240); per=a.get("per_page",40); artifacts=[]; warnings=set()
    for page in range(0,len(paths),per):
        group=paths[page:page+per]; cell_h=thumb+48; sheet=Image.new("RGB",(cols*thumb,math.ceil(len(group)/cols)*cell_h),(245,245,245)); draw=ImageDraw.Draw(sheet)
        for i,p in enumerate(group):
            try:
                with Image.open(p) as im: warnings.update(preview_info(im,"RGB")["warnings"]); im=im.convert("RGB"); im.thumbnail((thumb-8,thumb-8)); x=(i%cols)*thumb+(thumb-im.width)//2; y=(i//cols)*cell_h+4; sheet.paste(im,(x,y)); draw.text(((i%cols)*thumb+4,(i//cols)*cell_h+thumb),p.name[:32],fill=(0,0,0))
            except Exception: pass
        target=outdir/f"contact-sheet-{page//per+1}.png"; sheet.save(target); artifacts.append({"path":str(target),"size_bytes":target.stat().st_size})
    return {"success":True,"operation":"image_contact_sheet","image_count":len(paths),"artifacts":artifacts,"analysis_scope":"first_frame_8bit_RGB_thumbnail_preview","quantitative":False,"warnings":sorted(warnings)}

def ocr(a):
    rows=[]
    for p in files(a["input_paths"],100):
        try:
            with Image.open(p) as im: source_info=frame_info(im)
            with tempfile.TemporaryDirectory() as td:
                base=Path(td)/"ocr"; subprocess.run(["tesseract",str(p),str(base),"-l",a.get("languages","chi_sim+eng"),"tsv"],check=False,capture_output=True,timeout=90)
                tsv=base.with_suffix(".tsv"); words=[]
                if tsv.exists():
                    for line in tsv.read_text(errors="ignore").splitlines()[1:]:
                        cols=line.split("\t")
                        if len(cols)>=12 and cols[11].strip(): words.append({"text":cols[11],"page_number":int(cols[1]),"confidence":float(cols[10]),"box":[int(cols[6]),int(cols[7]),int(cols[8]),int(cols[9])]})
                rows.append({"path":str(p),**source_info,"analysis_scope":"ocr_engine_text_not_pixel_quantification","text":" ".join(w["text"] for w in words),"words":words})
        except Exception as e: rows.append({"path":str(p),"error":str(e)})
    return dump({"operation":"image_ocr_text","records":rows},a.get("output_path"))

def gps_map(a):
    features=[]
    for p in files(a["input_paths"],20000):
        info=exif(p); coord=gps_coords(info)
        if coord: features.append({"type":"Feature","geometry":{"type":"Point","coordinates":list(coord)},"properties":{"filename":p.name,"path":str(p),"capture_time":info.get("capture_time")}})
    geo={"type":"FeatureCollection","features":features}; safe_output(a["output_geojson"]).write_text(json.dumps(geo,ensure_ascii=False,indent=2),encoding="utf-8"); artifacts=[{"path":a["output_geojson"],"size_bytes":Path(a["output_geojson"]).stat().st_size}]
    if a.get("output_map"):
        import matplotlib.pyplot as plt
        xs=[f["geometry"]["coordinates"][0] for f in features]; ys=[f["geometry"]["coordinates"][1] for f in features]; plt.figure(figsize=(8,5)); plt.scatter(xs,ys); plt.xlabel("Longitude"); plt.ylabel("Latitude"); plt.title("Field photo locations"); plt.grid(True,alpha=.3); plt.savefig(safe_output(a["output_map"]),dpi=150,bbox_inches="tight"); plt.close(); artifacts.append({"path":a["output_map"],"size_bytes":Path(a["output_map"]).stat().st_size})
    return {"success":True,"operation":"image_gps_map","geotagged_count":len(features),"artifacts":artifacts}

def spatial_group(a):
    import geopandas as gpd
    from shapely.geometry import Point
    zones=gpd.read_file(a["zones_path"]); points=[]
    for p in files(a["input_paths"],20000):
        c=gps_coords(exif(p));
        if c: points.append({"path":str(p),"filename":p.name,"geometry":Point(c[0],c[1])})
    if not points: raise ValueError("no geotagged images found")
    gdf=gpd.GeoDataFrame(points,crs="EPSG:4326");
    if zones.crs and zones.crs.to_string()!="EPSG:4326": gdf=gdf.to_crs(zones.crs)
    joined=gpd.sjoin(gdf,zones[[a["zone_field"],"geometry"]],how="left",predicate="within"); records=[]
    for _,row in joined.iterrows(): records.append({"path":row["path"],"filename":row["filename"],"zone":None if row.get(a["zone_field"]) is None else str(row.get(a["zone_field"]))})
    return dump({"operation":"image_spatial_group","records":records,"unmatched_count":sum(x["zone"] is None for x in records)},a["output_path"])

def derivative(a):
    outdir=safe_output(a["output_dir"]); outdir.mkdir(parents=True,exist_ok=True); artifacts=[]
    for p in files(a["input_paths"],1000):
        with Image.open(p) as im:
            scope=preview_info(im,"RGB")
            im=im.convert("RGB"); im.thumbnail((a.get("max_dimension",2048),a.get("max_dimension",2048))); target=outdir/(p.stem+"."+a.get("format","jpeg")); savekw={"quality":a.get("quality",88)} if target.suffix==".jpeg" else {}
            im.save(target,**savekw); artifacts.append({"path":str(target),"size_bytes":target.stat().st_size,"source_name":p.name,**scope})
    return {"success":True,"operation":"image_safe_derivative","artifacts":artifacts}

FUNCTIONS={"image_collection_inspect":collection,"image_metadata_extract":metadata,"image_integrity_check":integrity,"image_duplicate_detect":dup,"image_quality_assess":quality,"image_contact_sheet":contact,"image_ocr_text":ocr,"image_gps_map":gps_map,"image_spatial_group":spatial_group,"image_safe_derivative":derivative}

def main():
    if len(os.sys.argv)!=3: raise SystemExit("usage: operations.py TOOL PAYLOAD")
    name=os.sys.argv[1]; args=json.loads(base64.urlsafe_b64decode(os.sys.argv[2]+"="*((4-len(os.sys.argv[2])%4)%4)))
    try: print(json.dumps(FUNCTIONS[name](args),ensure_ascii=False,allow_nan=False))
    except Exception as e: print(json.dumps({"success":False,"operation":name,"error":f"{type(e).__name__}: {e}"},ensure_ascii=False)); raise SystemExit(1)
if __name__=="__main__": main()
