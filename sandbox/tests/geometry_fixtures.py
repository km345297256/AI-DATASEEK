"""Synthetic, identity-free fixtures; no real research files."""
from xml.etree import ElementTree as ET

def gro_bytes(frames=2, atoms=4, precision=3, velocities=True):
    lines=[];width=precision+5
    for frame in range(frames):
        lines.extend([f'Synthetic trajectory t= {frame*0.25}',str(atoms)])
        for index in range(atoms):
            xyz=((index%3)*0.1+frame*0.01, (index%2)*0.2, index*0.05)
            line=f'{1:5d}{"SYN":<5}{("P"+str(index%1000)):>5}{index%100000:5d}'
            line+=''.join(f'{v:{width}.{precision}f}' for v in xyz)
            if velocities:line+=''.join(f'{v:{width}.{precision+1}f}' for v in (-0.001,0.002,0.0))
            lines.append(line)
        lines.append('1.0 2.0 3.0 0 0 0.2 0 0.3 0.4')
    return ('\n'.join(lines)+'\n').encode('ascii')

def mesh_bytes():
    return b'''<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian">
<UnstructuredGrid><Piece NumberOfPoints="5" NumberOfCells="2">
<Points><DataArray type="Float64" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0 0 0 1 1 1 1</DataArray></Points>
<Cells><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2 3 1 2 3 4</DataArray><DataArray type="Int32" Name="offsets" format="ascii">4 8</DataArray><DataArray type="UInt8" Name="types" format="ascii">10 10</DataArray></Cells>
<PointData><DataArray type="Float64" Name="velocity" NumberOfComponents="3" format="ascii">1 2 3 4 5 6 7 8 9 10 11 12 13 14 15</DataArray><DataArray type="Float32" Name="temperature" format="ascii">10 20 nan 40 50</DataArray></PointData>
<CellData><DataArray type="Float64" Name="pressure" format="ascii">-2 8</DataArray></CellData>
</Piece></UnstructuredGrid></VTKFile>'''

def mesh_variant(edit):
    root=ET.fromstring(mesh_bytes());edit(root);return ET.tostring(root)

def payloads():
    from app.services.gro_trajectory_reader import gro_trajectory_preview
    from app.services.simulation_mesh_reader import simulation_mesh_preview
    g=gro_bytes();m=mesh_bytes()
    return {'gro_tree':gro_trajectory_preview(g,'gro'),
            'gro_geometry':gro_trajectory_preview(g,'gro','geometry',{'frame':1}),
            'mesh_tree':simulation_mesh_preview(m,'vtu'),
            'mesh_geometry':simulation_mesh_preview(m,'vtu','geometry',{'field':'p-0','component':1}),
            'mesh_cell':simulation_mesh_preview(m,'vtu','geometry',{'field':'c-0','component':0}),
            'mesh_null':simulation_mesh_preview(m,'vtu','geometry',{'field':'p-1','component':0}),
            'mesh_only':simulation_mesh_preview(m,'vtu','geometry',{'field':None,'component':0})}

if __name__=='__main__':
    import json
    print(json.dumps(payloads(),ensure_ascii=False,allow_nan=False))
