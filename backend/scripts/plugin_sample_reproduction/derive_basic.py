"""Reviewed format-only derivations; source files are data, never executed."""
import csv,io,json,shutil,struct,sys
from pathlib import Path
from xml.etree.ElementTree import Element,SubElement,tostring
import numpy as np
from netCDF4 import Dataset
BASE=Path('/samples')
REPO=Path('/repo')
def write(path,data):
    if path.exists():
        if path.read_bytes()!=data: raise ValueError('Refuse differing existing artifact')
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as out: out.write(data)

# A complete first MiniSEED2 record, not a fabricated or recomputed signal.
folder=BASE/'viz-usgs-anmo-seismic'
source=(folder/'IU.ANMO.10.BHZ.2018.001_first_minute.mseed').read_bytes()
assert source[:8]==b'000001M ' and source[48:52]==b'\x03\xe8\x008' and source[54]==9
write(folder/'IU.ANMO.10.BHZ.first-record.mseed',source[:512])

# MetPy's original pressure, temperature and dewpoint text tokens, with units in names.
folder=BASE/'viz-metpy-may4-sounding'
rows=[]
for line in (folder/'may4_sounding_original.txt').read_text().splitlines():
    fields=line.split()
    if len(fields)==11:
        try: [float(n) for n in fields]
        except ValueError: continue
        rows.append([fields[0],fields[2],fields[3]])
assert len(rows)>=3 and all(float(a[0])>float(b[0]) for a,b in zip(rows,rows[1:]))
buffer=io.StringIO(newline='');writer=csv.writer(buffer,lineterminator='\n')
writer.writerow(['pressure_hPa','temperature_degC','dewpoint_degC']);writer.writerows(rows)
write(folder/'may4_sounding.csv',buffer.getvalue().encode())

# Re-encode all twelve climatology planes as ENVI BSQ float32; do not convert units.
folder=BASE/'viz-noaa-climatology-envi'
source=REPO/'backend/app/resources/datasets/open-noaa-air-climatology/air.sig995.mon.ltm.1991-2020.nc'
write(folder/source.name,source.read_bytes())
with Dataset(source,'r') as ds:
    values=ds.variables['air'][:]
    assert values.shape==(12,73,144) and str(ds.variables['air'].units)=='degC' and not np.ma.getmaskarray(values).any()
    data=np.asarray(values,dtype='<f4')
write(folder/'monthly_air.img',data.tobytes(order='C'))
header='ENVI\nsamples = 144\nlines = 73\nbands = 12\nheader offset = 0\nfile type = ENVI Standard\ndata type = 4\ninterleave = bsq\nbyte order = 0\n'
write(folder/'monthly_air.hdr',header.encode('ascii'))
assert np.array_equal(np.frombuffer((folder/'monthly_air.img').read_bytes(),dtype='<f4').reshape(data.shape),data)

# Lossless coordinate selection from the official USGS GeoJSON (depth is NOT altitude).
folder=BASE/'viz-usgs-earthquakes-kml'
original=REPO/'.cache/visualization-datasets-20260918/viz-usgs-2024-m6-earthquakes/usgs-2024-m6-original.geojson'
write(folder/original.name,original.read_bytes())
features=json.loads(original.read_text())['features']
root=Element('kml',xmlns='http://www.opengis.net/kml/2.2');doc=SubElement(root,'Document')
for feature in features:
    assert feature['geometry']['type']=='Point'
    x,y,*depth=feature['geometry']['coordinates']
    assert -180<=x<=180 and -90<=y<=90
    mark=SubElement(doc,'Placemark');SubElement(mark,'name').text=feature['id']
    point=SubElement(mark,'Point');SubElement(point,'coordinates').text=f'{x},{y}'
write(folder/'usgs-2024-m6.kml',tostring(root,encoding='utf-8',xml_declaration=True))
print(json.dumps({'metpy_complete_rows':len(rows),'envi_shape':list(data.shape),'envi_values_equal':True,'kml_features':len(features),'seismic_bytes':512}))
