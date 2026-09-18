"""Reviewed bounded derivatives. Never execute downloaded source code."""
import hashlib, io, json, sys
from pathlib import Path
import numpy as np
from netCDF4 import Dataset
import eccodes
BASE=Path('/samples')
def write(path,data):
    if path.exists():
        if path.read_bytes()!=data: raise ValueError('Refusing to replace different data')
    else:
        with path.open('xb') as f: f.write(data)

# One standard WMO temperature parameter: only remove the unused local-table declaration.
p=BASE/'viz-noaa-gfs-grib/gfs-20260916-00-2mt-original.grib2'
with p.open('rb') as f: h=eccodes.codes_grib_new_from_file(f)
keys=['discipline','parameterCategory','parameterNumber','typeOfFirstFixedSurface','scaledValueOfFirstFixedSurface']
print('GRIB standard parameter metadata',[(k,eccodes.codes_get(h,k)) for k in keys])
assert [eccodes.codes_get_long(h,k) for k in keys]==[0,0,0,103,2]
assert eccodes.codes_get(h,'localTablesVersion')==1
before={k:eccodes.codes_get_array(h,k).copy() for k in ['values','latitudes','longitudes']}
eccodes.codes_set(h,'localTablesVersion',0)
data=eccodes.codes_get_message(h);eccodes.codes_release(h)
write(p.with_name('gfs-20260916-00-2mt-wmo.grib2'),data)
h=eccodes.codes_new_from_message(data)
assert eccodes.codes_get(h,'localTablesVersion')==0
assert all(np.array_equal(v,eccodes.codes_get_array(h,k)) for k,v in before.items())
eccodes.codes_release(h)
changes=[i for i,(a,b) in enumerate(zip(p.read_bytes(),data)) if a!=b]
assert len(data)==p.stat().st_size and changes==[26]

# First 512 source triangles and all their source vertices: no interpolation or CRS guessing.
folder=BASE/'viz-xugrid-netherlands'
target=folder/'elevation_nl_512faces.nc'
with Dataset(folder/'elevation_nl_original.nc') as s:
    faces=np.asarray(s.variables['mesh2d_face_nodes'][:512],dtype=np.int32)
    assert faces.shape==(512,3) and faces.min()>=0
    ids=np.unique(faces)
    remapped=np.searchsorted(ids,faces).astype('i4')
    x=np.asarray(s.variables['mesh2d_node_x'][:])[ids]
    y=np.asarray(s.variables['mesh2d_node_y'][:])[ids]
    values=np.asarray(s.variables['elevation'][:512])
    assert str(s.variables['elevation'].unit)=='m NAP'
if not target.exists():
    with Dataset(target,'w',format='NETCDF4') as d:
        d.Conventions='CF-1.8 UGRID-1.0'
        d.createDimension('node',len(ids));d.createDimension('face',512);d.createDimension('corner',3)
        mesh=d.createVariable('mesh','i4');mesh.cf_role='mesh_topology';mesh.topology_dimension=2
        mesh.node_coordinates='node_x node_y';mesh.face_node_connectivity='face_nodes';mesh.face_dimension='face'
        vx=d.createVariable('node_x','f8',('node',));vy=d.createVariable('node_y','f8',('node',));vx[:]=x;vy[:]=y
        vf=d.createVariable('face_nodes','i4',('face','corner'),fill_value=-1);vf.cf_role='face_node_connectivity';vf.start_index=0;vf[:]=remapped
        ve=d.createVariable('elevation','f4',('face',),fill_value=np.nan);ve.mesh='mesh';ve.location='face';ve.units='m NAP';ve[:]=values
with Dataset(target) as d:
    assert np.array_equal(d.variables['node_x'][:],x) and np.array_equal(d.variables['node_y'][:],y)
    assert np.array_equal(ids[d.variables['face_nodes'][:]],faces)
    assert np.array_equal(d.variables['elevation'][:],values,equal_nan=True)
index={'source_file':'elevation_nl_original.nc','source_face_indices':list(range(512)),'source_node_indices':ids.tolist(),'method':'First 512 faces; stable source vertex selection, zero-based compact remapping; no interpolation or CRS assignment.'}
write(folder/'subset-index.json',(json.dumps(index,ensure_ascii=False,indent=2)+'\n').encode())
print(json.dumps({'grib_changed_byte_offsets':changes,'grib_values_and_coordinates_equal':True,'ugrid_faces':512,'ugrid_nodes':len(ids),'ugrid_values_and_geometry_equal':True}))
