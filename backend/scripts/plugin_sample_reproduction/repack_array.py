"""Lossless full-year scientific re-encoding, not downsampling or dummy padding."""
import hashlib,json
from pathlib import Path
import h5py,numpy as np
folder=Path('/samples/viz-noaa-pressure-air-2022-2025')
source=folder/'air.2025.nc';target=folder/'air.2025.contiguous.h5'
if target.exists():raise SystemExit('Refusing to overwrite existing derivative')
report={}
with h5py.File(source,'r') as s,h5py.File(target,'x',libver='earliest') as d:
 d.attrs['source_file']=np.bytes_('air.2025.nc')
 d.attrs['transformation']=np.bytes_('All numeric arrays copied as contiguous raw storage values; no CF decoding, no resampling.')
 for name in ['air','level','lat','lon','time']:
  original=s[name];copy=d.create_dataset(name,shape=original.shape,dtype=original.dtype,chunks=None)
  for attr in ('units','long_name','standard_name','calendar','scale_factor','add_offset','missing_value','_FillValue'):
   if attr in original.attrs:
    v=original.attrs[attr]
    if isinstance(v,str):v=np.bytes_(v)
    copy.attrs[attr]=v
  digest=hashlib.sha256()
  for start in range(0,original.shape[0],8):
   key=slice(start,min(start+8,original.shape[0]));data=np.asarray(original[key])
   copy[key]=data;digest.update(data.tobytes(order='C'))
  report[name]={'shape':list(original.shape),'dtype':str(original.dtype),'raw_array_sha256':digest.hexdigest()}
with h5py.File(target,'r') as d:
 for name,item in report.items():
  a=d[name];assert a.chunks is None
  digest=hashlib.sha256()
  for start in range(0,a.shape[0],8):digest.update(np.asarray(a[start:start+8]).tobytes(order='C'))
  assert digest.hexdigest()==item['raw_array_sha256']
print(json.dumps({'source':'air.2025.nc','derived':target.name,'size':target.stat().st_size,'all_raw_values_equal':True,'arrays':report}))
