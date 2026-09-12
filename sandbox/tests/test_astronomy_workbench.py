"""Actual FITS/TIFF producer-oracle tests; no external datasets or services."""
import copy
import gzip
import io
import json
import math
import struct
import numpy as np
import pytest
from astropy.io import fits
import tifffile
from PIL import Image
from app.services.astronomy_workbench_reader import astronomy_workbench_preview as preview
from app.services.astronomy_workbench_payload import ERROR, validate_options, validate_payload

from tests.astronomy_workbench_fixtures import fits_bytes, cube_bytes, selection, fixture_payloads

def test_main_cube_hdu_slice_wcs_pixel_roi_table_and_spectrum_parity():
    p=fixture_payloads();assert [d["kind"] for d in p["tree"]["choices"]["datasets"]]==["image","table","spectrum"]
    assert p["tree"]["choices"]["datasets"][0]["shape"]==[2,4,6]
    r=p["render"]["workbench"];assert (r["width"],r["height"])==(6,4)
    assert r["statistics"]["valid_count"]==23 and r["statistics"]["missing_count"]==1
    assert p["pixel"]["workbench"]["value"]==32.
    from astropy.wcs import WCS
    with fits.open(io.BytesIO(cube_bytes())) as hdus:
        assert p["pixel"]["workbench"]["world"]==pytest.approx(WCS(hdus[0].header).celestial.all_pix2world([[2,1]],0)[0])
    assert p["region"]["workbench"]["pixel_count"]==6
    assert p["region"]["workbench"]["statistics"]["sum"]==float(np.arange(48).reshape(2,4,6)[1,1:3,1:4].sum())
    assert p["table"]["workbench"]["rows"]==[[10.,"source-a"],[None,"source-b"]]
    assert p["series"]["workbench"]["values"]==[1.,2.,None,5.]

@pytest.mark.parametrize("stretch",["linear","log","sqrt","asinh"])
@pytest.mark.parametrize("colour",["gray","viridis","heat","cool"])
@pytest.mark.parametrize("invert",[False,True])
def test_main_all_display_controls_and_transparent_missing(stretch,colour,invert):
    result=preview(cube_bytes(),"fits","image",selection(stretch=stretch,colour_map=colour,invert=invert,interval="manual",low=24.,high=48.))
    import base64
    image=Image.open(io.BytesIO(base64.b64decode(result["workbench"]["image_base64"])))
    assert image.size==(6,4) and image.getpixel((0,0))[3]==0
    assert result["workbench"]["display_limits"]==[[24.,48.]]
    assert image.getpixel((3,3))[3]==255

def test_scaled_unsigned_fits_and_compressed_tile_hdu():
    values=np.array([[0,65535],[123,12]],dtype=np.uint16)
    data=fits_bytes(fits.PrimaryHDU(values))
    assert preview(data,"fits","image",selection("pixel",slices=[],x=1,y=0))["workbench"]["value"]==65535
    compressed=fits_bytes(fits.PrimaryHDU(),fits.CompImageHDU(values,compression_type="RICE_1"))
    assert preview(compressed,"fz")["choices"]["datasets"][1]["kind"]=="image"
    assert preview(compressed,"fz","image",selection("pixel",dataset=1,slices=[],x=1,y=0))["workbench"]["value"]==65535

def test_gzip_fits_allows_bounded_decompression():
    data=gzip.compress(cube_bytes()); result=preview(data,"fits.gz","image",selection("pixel"))
    assert result["workbench"]["value"]==32 and result["metadata"]["source_bytes"]==len(data)
    with pytest.raises(ValueError,match=ERROR):preview(gzip.compress(b"\x00"*(33554432+1)),"fits.gz")

@pytest.mark.parametrize("planar",["contig","separate"])
def test_tiff_rgb_band_page_exact_values(planar):
    image=np.arange(20,dtype=np.uint8).reshape(4,5);rgb=np.stack([image,image+10,image+20],axis=-1 if planar=="contig" else 0)
    output=io.BytesIO()
    with tifffile.TiffWriter(output) as writer:
        writer.write(image,photometric="minisblack");writer.write(rgb,photometric="rgb",planarconfig=planar)
    data=output.getvalue();catalog=preview(data,"tiff");assert len(catalog["choices"]["datasets"])==2
    assert catalog["choices"]["datasets"][1]["plane_shape"]==[4,5]
    assert preview(data,"tiff","image",selection("pixel",dataset=1,slices=[],band=2,x=3,y=2))["workbench"]["value"]==23
    result=preview(data,"tiff","image",selection("pixel",dataset=1,slices=[],band=0,x=3,y=2))
    assert result["workbench"]["value"]==[13,23,33]

def test_geotiff_crs_nodata_and_rgb_metadata():
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    values=np.stack([np.full((4,5),v,dtype=np.uint16) for v in [10,20,30]])
    values[:,0,0]=0
    with MemoryFile() as memory:
        with memory.open(driver="GTiff",width=5,height=4,count=3,dtype="uint16",crs="EPSG:4326",transform=from_origin(100,30,.1,.1),nodata=0) as target:target.write(values)
        data=memory.read()
    geo=preview(data,"tif")["choices"]["geospatial"]
    assert geo["bounds_wgs84"]==pytest.approx([100,29.6,100.5,30])
    result=preview(data,"tif","image",selection("pixel",slices=[],band=0,x=0,y=0))
    assert result["workbench"]["value"]==[None,None,None]

def test_detect_sources_uses_unmodified_values_and_no_missing_peak():
    data=np.random.default_rng(7).normal(0,.1,(64,64)); data[30:33,20:23]+=100;data[0,0]=np.inf
    result=preview(fits_bytes(fits.PrimaryHDU(data)),"fits","image",selection("sources",slices=[],threshold_sigma=5.))["workbench"]
    assert result["sources"] and any(abs(p["x"]-21)<=1 and abs(p["y"]-31)<=1 for p in result["sources"])
    for source in result["sources"]:assert source["peak"]==data[source["y"],source["x"]] and math.isfinite(source["snr"])

def test_spectrum_downsampling_is_declared_and_gaps_preserved():
    values=np.arange(5001,dtype=float);values[3]=np.nan
    result=preview(fits_bytes(fits.PrimaryHDU(values)),"fits","series",{"dataset":0})
    assert result["workbench"]["stride"]==3 and result["sampled"] is True
    assert result["workbench"]["values"][1] is None and result["workbench"]["indices"][1]==3

def test_paged_fixed_vector_table_and_large_integer_exact_text():
    table=fits.BinTableHDU.from_columns([fits.Column(name="VECTOR",format="3D",array=np.arange(180).reshape(60,3)),fits.Column(name="BIG",format="K",array=[2**60]*60)])
    data=fits_bytes(fits.PrimaryHDU(),table)
    result=preview(data,"fits","table",{"dataset":1,"row_offset":50,"column_offset":1})
    assert len(result["workbench"]["rows"])==10 and result["workbench"]["rows"][0]==[str(2**60)] and result["sampled"] is True
    result=preview(data,"fits","table",{"dataset":1,"row_offset":0,"column_offset":0})
    assert result["workbench"]["rows"][1][0]==[3.,4.,5.]

@pytest.mark.parametrize("changes",[{"dataset":True},{"slices":[-1]},{"band":17},{"action":"__proto__"},{"threshold_sigma":0},{"threshold_sigma":float("nan")},{"extra":"file:///etc/passwd"}])
def test_options_fail_before_parsers(changes):
    options=selection("sources");options.update(changes)
    with pytest.raises(ValueError,match=ERROR):preview(b"secret fake invalid source","fits","image",options)

@pytest.mark.parametrize("options",[selection("pixel",x=6),selection("region",bounds=[-2,0,3,2]),selection("region",bounds=[0,0,7,2]),selection("render",slices=[]),selection("render",slices=[2]),selection("render",band=0),selection("render",interval="manual",low=1,high=1)])
def test_image_selection_fails_closed(options):
    with pytest.raises(ValueError,match=ERROR):preview(cube_bytes(),"fits","image",options)

def test_declared_fits_bomb_rejected_before_hdu_open(monkeypatch):
    h=fits.Header({"SIMPLE":True,"BITPIX":-64,"NAXIS":2,"NAXIS1":1000000,"NAXIS2":1000000})
    data=h.tostring().encode();monkeypatch.setattr(fits,"open",lambda *a,**k:pytest.fail("unsafe parser allocation"))
    with pytest.raises(ValueError,match=ERROR):preview(data,"fits")

def test_plane_budget_before_data_access_and_variable_table_heap_rejected():
    data=fits_bytes(fits.PrimaryHDU(np.zeros((1536,1536),dtype=np.uint8)))
    assert preview(data,"fits")["choices"]["datasets"][0]["shape"]==[1536,1536]
    with pytest.raises(ValueError,match=ERROR):preview(data,"fits","image",selection(slices=[]))
    table=fits.BinTableHDU.from_columns([fits.Column(name="V",format="PD()",array=np.array([np.arange(3)],dtype=object))])
    with pytest.raises(ValueError,match=ERROR):preview(fits_bytes(fits.PrimaryHDU(),table),"fits","table",{"dataset":1,"row_offset":0,"column_offset":0})

def test_ascii_fits_table():
    table=fits.TableHDU.from_columns([fits.Column(name="VALUE",format="F10.3",array=[1.25,2.5]),fits.Column(name="LABEL",format="A8",array=["one","two"])])
    result=preview(fits_bytes(fits.PrimaryHDU(),table),"fits","table",{"dataset":1,"row_offset":0,"column_offset":0})
    assert result["workbench"]["rows"]==[[1.25,"one"],[2.5,"two"]]

def test_metadata_paths_redacted_and_no_private_diagnostics():
    data=fits_bytes(fits.PrimaryHDU(),fits.ImageHDU(np.ones((2,2)),name="/Users/secret/data.fits"))
    output=preview(data,"fits");assert output["choices"]["datasets"][1]["name"]=="[redacted]"
    assert "secret" not in json.dumps(output)
    with pytest.raises(ValueError) as caught:preview(b"https://secret.invalid/file.fits","fits")
    assert str(caught.value)==ERROR

@pytest.mark.parametrize("field",["selected","metadata","choices","workbench","root"])
def test_host_payload_binding_rejects_tampering(field):
    output=fixture_payloads()["pixel"]
    if field=="selected":output["selected"]["x"]=3
    elif field=="metadata":output["metadata"]["source_bytes"]+=1
    elif field=="choices":output["choices"]["datasets"][0]["plane_shape"]=[6,4]
    elif field=="workbench":output["workbench"]["x"]=3
    else:output["source_path"]="/tmp/private"
    with pytest.raises(ValueError,match=ERROR):validate_payload(output,kind="image",options=selection("pixel"),format="fits",source_bytes=len(cube_bytes()))

if __name__=="__main__":print(json.dumps(fixture_payloads(),ensure_ascii=False,allow_nan=False))
