"""Optional explicit pydicom 3.0.1 oracle on original synthetic Part10 bytes."""
import io
import json
from importlib.metadata import version
import numpy as np
import pydicom
from pydicom.pixels import apply_modality_lut, apply_windowing
from dicom_window_fixtures import fixture,preview

def main():
    assert version('pydicom')=='3.0.1'
    cases=0
    for explicit in (True,False):
        for bits,signed,stored in ((8,False,8),(8,True,7),(16,False,12),(16,True,12),(16,False,16),(16,True,16)):
            for mono in ('MONOCHROME1','MONOCHROME2'):
                values=[-2**(stored-1),-1,0,1,2**(stored-1)-1,2,3,4,5,6,7,8] if signed else None
                raw,_=fixture(explicit=explicit,bits=bits,stored=stored,signed=signed,mono=mono,pixel_values=values)
                ds=pydicom.dcmread(io.BytesIO(raw)); expected=ds.pixel_array
                frame=0 if signed else 1
                own,_=preview(raw,'image',{'frame':frame,'roi':[0,0,4,3],'confirm_deidentified':True})
                np.testing.assert_array_equal(own['array']['values'],(expected if signed else expected[frame]).ravel())
                cases+=1
    display=[]
    for mono in ('MONOCHROME1','MONOCHROME2'):
        raw,_=fixture(mono=mono);ds=pydicom.dcmread(io.BytesIO(raw));stored=ds.pixel_array[1,1:3,1:3]
        transformed=apply_modality_lut(stored,ds)
        # Official windowing output is on the rescaled theoretical dtype range.
        lo=0*float(ds.RescaleSlope)+float(ds.RescaleIntercept)
        hi=(2**ds.BitsStored-1)*float(ds.RescaleSlope)+float(ds.RescaleIntercept)
        for center,width in ((1350,1000),(1250,1),(1000,2048),(1220.5,2)):
            ds.WindowCenter=center;ds.WindowWidth=width
            scaled=(apply_windowing(transformed,ds)-lo)/(hi-lo)*255
            if mono=='MONOCHROME1':scaled=255-scaled
            rgba=np.empty((*stored.shape,4),dtype=np.uint8);rgba[:,:,:3]=np.floor(scaled+.5).astype(np.uint8)[:,:,None];rgba[:,:,3]=255
            display.append({'mono':mono,'center':center,'width':width,'rgba':rgba.ravel().tolist()})
    print(json.dumps({'native_cases':cases,'reader':'pydicom 3.0.1 real pixel_array / modality / windowing','display':display},allow_nan=False))

if __name__=='__main__':main()
