"""Synthetic, non-user FITS/TIFF producers for unit and real-HTTP checks."""
import io
import numpy as np
from astropy.io import fits
from app.services.astronomy_workbench_reader import astronomy_workbench_preview as preview

def fits_bytes(*hdus):
    output=io.BytesIO();fits.HDUList(list(hdus)).writeto(output);return output.getvalue()

def cube_bytes():
    header=fits.Header({"CTYPE1":"RA---TAN","CTYPE2":"DEC--TAN","CRPIX1":3.,"CRPIX2":2.,"CRVAL1":120.,"CRVAL2":22.,"CDELT1":-.01,"CDELT2":.01})
    cube=np.arange(2*4*6,dtype=np.float32).reshape(2,4,6);cube[1,0,0]=np.nan
    columns=[fits.Column(name="RA",format="D",unit="deg",array=[10.,np.nan]),fits.Column(name="NAME",format="8A",array=["source-a","source-b"])]
    return fits_bytes(fits.PrimaryHDU(cube,header=header),fits.BinTableHDU.from_columns(columns,name="CATALOG"),fits.ImageHDU(np.array([1.,2.,np.nan,5.]),name="SPECTRUM"))

def tiff_bytes():
    import tifffile
    image=np.arange(20,dtype=np.uint8).reshape(4,5);output=io.BytesIO()
    with tifffile.TiffWriter(output) as writer:
        writer.write(image,photometric="minisblack")
        writer.write(np.stack([image,image+10,image+20],axis=-1),photometric="rgb")
    return output.getvalue()

def selection(action="render",**changes):
    result={"dataset":0,"slices":[1],"band":1,"action":action}
    if action=="render":result.update(stretch="asinh",interval="zscale",low=None,high=None,colour_map="heat",invert=False)
    elif action=="pixel":result.update(x=2,y=1)
    elif action=="region":result.update(bounds=[1,1,4,3])
    else:result.update(threshold_sigma=1.)
    result.update(changes);return result

def fixture_payloads():
    data=cube_bytes();out={"tree":preview(data,"fits")}
    for action in ("render","pixel","region","sources"):out[action]=preview(data,"fits","image",selection(action))
    out["table"]=preview(data,"fits","table",{"dataset":1,"row_offset":0,"column_offset":0})
    out["series"]=preview(data,"fits","series",{"dataset":2})
    return out
