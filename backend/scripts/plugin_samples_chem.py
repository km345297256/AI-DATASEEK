"""Reproduce chemistry/physics visualization fixtures and reviewed conversions.

No network, database, imported source scripts, analysis solver or live data access.
Use a workspace staging directory. Existing files must match; never overwrite.
Generated NMR/ROOT/EDF values are explicitly synthetic, not scientific observations.
"""
from __future__ import annotations

import argparse
from collections import Counter
import io
from pathlib import Path
import struct
import uuid


def output(root: Path, dataset: str, name: str, data: bytes) -> None:
    directory = root / dataset
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Existing output differs: {dataset}/{name}")
        return
    with path.open("xb") as stream:
        stream.write(data)


def mesh(root: Path) -> None:
    """Preserve Gmsh 4.1 nodes/cells; derive exterior tetrahedron faces for OBJ."""
    dataset = "viz-plugin-mesh-scikit-fem"
    text = (root / dataset / "cube_oriented_sub.msh").read_text()
    assert "$MeshFormat\n4.1 0 8\n" in text
    tokens = iter(text.split("$Nodes\n", 1)[1].split("$EndNodes", 1)[0].split())
    blocks, count, low, high = [int(next(tokens)) for _ in range(4)]
    nodes = {}
    for _ in range(blocks):
        dim, entity, parametric, length = [int(next(tokens)) for _ in range(4)]
        assert parametric == 0
        ids = [int(next(tokens)) for _ in range(length)]
        for ident in ids:
            nodes[ident] = [next(tokens) for _ in range(3)]
    assert len(nodes) == count and next(tokens, None) is None
    ids = sorted(nodes); index = {ident: i for i, ident in enumerate(ids)}
    tokens = iter(text.split("$Elements\n", 1)[1].split("$EndElements", 1)[0].split())
    blocks, count, low, high = [int(next(tokens)) for _ in range(4)]
    cells, entities, kinds = [], [], []
    mapping = {1: (2, 3), 2: (3, 5), 4: (4, 10), 15: (1, 1)}
    for _ in range(blocks):
        dim, entity, kind, length = [int(next(tokens)) for _ in range(4)]
        size, vtk_type = mapping[kind]
        for _ in range(length):
            ident = int(next(tokens))
            cells.append([index[int(next(tokens))] for _ in range(size)])
            entities.append(entity); kinds.append(vtk_type)
    assert len(cells) == count and next(tokens, None) is None
    assert len(ids) <= 4096 and len(cells) <= 2048
    points = "\n".join(" ".join(nodes[ident]) for ident in ids)
    offsets=[]; total=0
    for cell in cells:
        total += len(cell); offsets.append(total)
    xml = ('<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian">'
        f'<UnstructuredGrid><Piece NumberOfPoints="{len(ids)}" NumberOfCells="{len(cells)}">'
        '<Points><DataArray type="Float64" NumberOfComponents="3" format="ascii">'+points+'</DataArray></Points>'
        '<Cells><DataArray type="Int32" Name="connectivity" format="ascii">'+" ".join(str(v) for c in cells for v in c)+'</DataArray>'
        '<DataArray type="Int32" Name="offsets" format="ascii">'+" ".join(map(str,offsets))+'</DataArray>'
        '<DataArray type="UInt8" Name="types" format="ascii">'+" ".join(map(str,kinds))+'</DataArray></Cells>'
        '<CellData><DataArray type="Int32" Name="GmshEntity" format="ascii">'+" ".join(map(str,entities))+'</DataArray></CellData>'
        '</Piece></UnstructuredGrid></VTKFile>\n')
    output(root,dataset,"mesh.vtu",xml.encode())
    faces = []
    for cell,kind in zip(cells,kinds):
        if kind == 10:
            a,b,c,d = cell
            faces.extend([(a,c,b),(a,b,d),(b,c,d),(c,a,d)])
    counts = Counter(tuple(sorted(f)) for f in faces)
    surface = [f for f in faces if counts[tuple(sorted(f))] == 1]
    obj = '# Derived exterior of official scikit-fem numerical mesh; not measured geometry.\n'
    obj += "\n".join("v "+" ".join(nodes[i]) for i in ids)+"\n"
    obj += "\n".join("f "+" ".join(str(v+1) for v in f) for f in surface)+"\n"
    output(root,dataset,"surface.obj",obj.encode())


def normalize_official(root: Path) -> None:
    """No source values invented; retain originals in the staging directory."""
    import numpy as np
    import h5py
    dataset='viz-plugin-mca-sigima'
    source=(root/dataset/'spectrum.mca').read_bytes()
    assert source.count(b'\xb0') == 1 and b'Board Temp: 34\xb0C' in source
    # Footer text only; the complete <<DATA>> through <<END>> byte segment is identical.
    normalized=source.replace(b'Board Temp: 34\xb0C',b'Board Temp: 34 degC')
    output(root,dataset,'spectrum_ascii.mca',normalized)
    dataset='viz-plugin-ripple-rosettasciio'
    source=(root/dataset/'test_ripple_sdim-1_ndim-2_float32.raw').read_bytes()
    assert np.array_equal(np.frombuffer(source,dtype='<f4'),np.arange(24,dtype=np.float32))
    header=(root/dataset/'test_ripple_sdim-1_ndim-2_float32.rpl').read_bytes()
    assert header.count(b'byte-order\tdont-care') == 1
    output(root,dataset,'ripple_little_endian.rpl',header.replace(b'byte-order\tdont-care',b'byte-order\tlittle-endian'))
    output(root,dataset,'ripple_little_endian.raw',source)
    dataset='viz-plugin-nexus-rosettasciio'
    with h5py.File(root/dataset/'nexus_dls_example.nxs','r') as original:
        group=original['entry1/testdata/nexustest']
        data=group['data'][...,0]; x=group['x'][...]; y=group['y'][...]
    buffer=io.BytesIO()
    with h5py.File(buffer,'w') as f:
        entry=f.create_group('entry',track_order=False)
        entry.attrs['NX_class']=np.bytes_('NXentry')
        group=entry.create_group('data',track_order=False)
        group.attrs['NX_class']=np.bytes_('NXdata')
        group.attrs['signal']=np.bytes_('data')
        group.attrs['axes']=np.array([b'y',b'x'],dtype='S1')
        group.create_dataset('data',data=data,track_times=False)
        group.create_dataset('x',data=x,track_times=False)
        group.create_dataset('y',data=y,track_times=False)
    # HDF5 groups may have creation timestamps; compare payload semantically on rerun.
    path=root/dataset/'nexus_fixed.nxs'
    if path.exists():
        with h5py.File(path,'r') as saved:
            assert np.array_equal(saved['entry/data/data'][...],data)
            assert np.array_equal(saved['entry/data/x'][...],x)
            assert np.array_equal(saved['entry/data/y'][...],y)
    else:
        output(root,dataset,'nexus_fixed.nxs',buffer.getvalue())


def synthetic(root: Path) -> None:
    """Fixtures: exact integer patterns, not measurements or physical simulations."""
    import numpy as np
    import uproot
    text='\n'.join(['##TITLE=Synthetic 1H parser fixture - NOT an experimental spectrum',
        '##JCAMP-DX=5.01','##DATA TYPE=NMR SPECTRUM','##XUNITS=PPM',
        '##YUNITS=ARBITRARY UNITS','##.OBSERVE NUCLEUS=1H','##.OBSERVE FREQUENCY=400',
        '##NPOINTS=1024','##XFACTOR=1','##YFACTOR=1','##XYPOINTS=(XY..XY)'] +
        [f'{10-i/100:.2f}, {max(0,120-abs(i-230)*8)+max(0,80-abs(i-700)*4)}' for i in range(1024)] + ['##END=',''])
    output(root,'viz-plugin-nmr-project','synthetic-1h.jdx',text.encode())
    width,height,frames=64,48,3
    binary=bytearray()
    for frame in range(frames):
        fields={'HeaderID':'EH:000001:000000:000000','Image':frame,'ByteOrder':'LowByteFirst',
            'DataType':'UnsignedShort','Dim_1':width,'Dim_2':height,'Size':width*height*2,
            'EDF_HeaderSize':512,'Comment':'SYNTHETIC calibration grid NOT an experiment'}
        header=('{\n'+''.join(f'{k} = {v} ;\n' for k,v in fields.items())).encode()
        binary.extend(header+b' '*(510-len(header))+b'}\n')
        values=[frame*1000+y*width+x for y in range(height) for x in range(width)]
        binary.extend(struct.pack('<'+'H'*len(values),*values))
    output(root,'viz-plugin-instrument-project','calibration-grid.edf',bytes(binary))
    dataset='viz-plugin-root-project'; path=root/dataset/'synthetic-histogram.root'
    if not path.exists():
        directory=root/dataset; directory.mkdir(exist_ok=True)
        # Fixed UUIDs; ROOT date fields still encode creation time (documented).
        with uproot.recreate(path,uuid_function=lambda:uuid.UUID(int=0)) as f:
            f['synthetic_counts']=(np.array([2.,5.,3.,8.]),np.array([0.,1.,3.,6.,10.]))
    with uproot.open(path) as f:
        values,edges=f['synthetic_counts'].to_numpy()
        assert values.tolist()==[2.,5.,3.,8.] and edges.tolist()==[0.,1.,3.,6.,10.]


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staging-root',type=Path,required=True)
    args=parser.parse_args(); root=args.staging_root
    if not root.is_absolute() or root.resolve()!=root or not root.is_dir():
        raise ValueError('Use an existing absolute non-symlink workspace staging directory')
    mesh(root); normalize_official(root); synthetic(root)
    print('Chemistry/physics fixture generation and conversions verified.')


if __name__ == '__main__':
    main()
