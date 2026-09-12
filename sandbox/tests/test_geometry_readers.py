import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from app.services.gro_trajectory_reader import gro_trajectory_preview
from app.services.gro_trajectory_payload import validate_gro_trajectory_payload
from app.services.simulation_mesh_reader import simulation_mesh_preview
from app.services.simulation_mesh_payload import validate_simulation_mesh_payload, CELL_SIZES
from geometry_fixtures import gro_bytes,mesh_bytes,mesh_variant,payloads

class GeometryReaders(unittest.TestCase):
    def test_gro_frames_precision_and_stored_units(self):
        for p in range(3,9):
            for velocities in (True,False):
                with self.subTest(precision=p,velocities=velocities):
                    data=gro_bytes(precision=p,velocities=velocities)
                    value=gro_trajectory_preview(data,'gro','geometry',{'frame':1})
                    self.assertEqual(value['choices']['frames'][1]['time_ps'],0.25)
                    self.assertEqual(value['trajectory']['positions'][0],[0.01,0,0])
                    self.assertEqual(value['trajectory']['velocities'],[[-0.001,0.002,0]]*4 if velocities else None)
                    self.assertEqual(value['choices']['frames'][0]['box'],[1,2,3,0,0,0.2,0,0.3,0.4])
                    self.assertEqual(gro_trajectory_preview(data,'gro')['kind'],'tree')
    def test_gro_rejects_wrong_identity_truncation_options_and_invalid_values(self):
        raw=gro_bytes();lines=raw.decode().splitlines()
        bad=[raw[:-5],raw+b'junk',raw.replace(b'0.010',b'  nan',1),raw.replace(b'1.0 2.0 3.0',b'1.0 -2.0 3.0'),raw.replace(b' t= 0.25',b' t= 0.25 t= 2'),raw.replace(b'Synthetic',b'\x00ynthetic',1),raw.replace(b'1.0 2.0 3.0 0 0',b'1.0 2.0 3.0 1 0')]
        changed=lines.copy();changed[9]=changed[9][:5]+'OTHER'+changed[9][10:];bad.append(('\n'.join(changed)+'\n').encode())
        for value in bad:
            with self.subTest(value=value[:15]):
                with self.assertRaises(ValueError):gro_trajectory_preview(value,'gro')
        for options in ({'frame':True},{'frame':-1},{'frame':64},{'frame':2},{'frame':0,'path':'private'},{},{'frame':0.0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):gro_trajectory_preview(raw,'gro','geometry',options)
        with self.assertRaises(ValueError):gro_trajectory_preview(raw,'gro','tree',{'frame':0})
    def test_gro_limits_and_no_identity_title_return(self):
        raw=gro_bytes(frames=1,atoms=8192)
        value=gro_trajectory_preview(raw,'gro','geometry',{'frame':0})
        self.assertEqual(len(value['trajectory']['positions']),8192)
        self.assertLess(len(json.dumps(value).encode()),2097152)
        for raw in (gro_bytes(frames=65),gro_bytes(frames=1,atoms=8193),gro_bytes(frames=17,atoms=8192)):
            with self.assertRaises(ValueError):gro_trajectory_preview(raw,'gro')
        value=gro_trajectory_preview(gro_bytes().replace(b'Synthetic trajectory',b'/Users/private/secret'),'gro')
        self.assertNotIn('private',json.dumps(value))
    def test_mesh_original_fields_association_components_and_null(self):
        p=payloads()
        self.assertEqual(p['mesh_geometry']['mesh']['field']['values'],[2,5,8,11,14])
        self.assertEqual(p['mesh_cell']['mesh']['field']['values'],[-2,8])
        self.assertEqual(p['mesh_cell']['mesh']['field']['association'],'cell')
        self.assertEqual(p['mesh_null']['mesh']['field']['values'],[10,20,None,40,50])
        self.assertIsNone(p['mesh_only']['mesh']['field'])
        self.assertEqual(p['mesh_tree']['choices']['fields'][0]['components'],3)
    def test_mesh_supported_linear_cell_orders(self):
        for kind,count in CELL_SIZES.items():
            def edit(root):
                p=root.find('./UnstructuredGrid/Piece');p.set('NumberOfPoints',str(count));p.set('NumberOfCells','1')
                p.find('./Points/DataArray').text=' '.join(str(v) for i in range(count) for v in (i%2,(i//2)%2,i//4))
                p.find('./Cells/DataArray[@Name="connectivity"]').text=' '.join(str(i) for i in range(count))
                p.find('./Cells/DataArray[@Name="offsets"]').text=str(count);p.find('./Cells/DataArray[@Name="types"]').text=str(kind)
                p.remove(p.find('PointData'));p.remove(p.find('CellData'))
            with self.subTest(kind=kind):
                v=simulation_mesh_preview(mesh_variant(edit),'vtu','geometry',{'field':None,'component':0})
                self.assertEqual(v['mesh']['cells'],[{'type':kind,'points':list(range(count))}])
    def test_mesh_rejects_xml_extensions_arrays_indices_and_solver_semantics(self):
        raw=mesh_bytes()
        variants=[raw.replace(b'format="ascii"',b'format="binary"',1),raw.replace(b'<VTKFile ',b'<VTKFile compressor="vtkZLibDataCompressor" ',1),raw.replace(b'<UnstructuredGrid>',b'<UnstructuredGrid><Piece Source="remote"/>'),raw.replace(b'0 1 2 3 1 2 3 4',b'0 0 2 3 1 2 3 4'),raw.replace(b'4 8',b'3 8'),raw.replace(b'10 10',b'42 42'),raw.replace(b'0 1 2 3 1 2 3 4',b'0 1 2 5 1 2 3 4'),raw.replace(b'0 0 0 1',b'nan 0 0 1',1),raw.replace(b'<Points>',b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secret">]><Points>'),raw.replace(b'Name="velocity"',b'Name="/Users/private"'),raw.replace(b'version="0.1"',b'version="2.2"'),raw.replace(b'type="Float64"',b'type="Imaginary"',1)]
        for i,bad in enumerate(variants):
            with self.subTest(variant=i):
                with self.assertRaises(ValueError):simulation_mesh_preview(bad,'vtu')
        for o in ({'field':'p-0','component':True},{'field':'p-0','component':3},{'field':'p-9','component':0},{'field':None,'component':1},{'field':'p-0','component':0,'deformation':2}):
            with self.subTest(options=o):
                with self.assertRaises(ValueError):simulation_mesh_preview(raw,'vtu','geometry',o)
    def test_mesh_xml_structure_limits_apply_while_constructing(self):
        from app.services.simulation_mesh_reader import _BoundedTree,_xml
        target=_BoundedTree();target.start('root',{})
        for i in range(255):target.start('a',{});target.end('a')
        with self.assertRaises(ValueError):target.start('one-too-many',{})
        for text in ('<a>'*8+'</a>'*8,'<a '+ ' '.join(f'k{i}="0"' for i in range(9))+'/>','<a x="'+'x'*129+'"/>','<root>'+'<a/>'*256+'</root>'):
            with self.subTest(text=text[:20]):
                with self.assertRaises(ValueError):_xml(text)
    def test_pure_contract_tamper_binding_and_backend_parity(self):
        for prefix,name,validator,options in [('gro','gro_trajectory',validate_gro_trajectory_payload,{'frame':1}),('mesh','simulation_mesh',validate_simulation_mesh_payload,{'field':'p-0','component':1})]:
            raw=payloads()[prefix+'_geometry']
            for key,value in [('sampled',True),('reader','vtk'),('warnings',['raw private']),('kind','series')]:
                item=copy.deepcopy(raw);item[key]=value
                with self.subTest(name=name,key=key):
                    with self.assertRaises(ValueError):validator(item)
            with self.assertRaises(ValueError):validator(raw,size=raw['metadata']['source_bytes']+1)
            with self.assertRaises(ValueError):validator(raw,kind='tree')
            with self.assertRaises(ValueError):validator(raw,options={})
            with self.assertRaises(ValueError):validator(raw,fmt='exe')
            self.assertIs(validator(raw,options=options),raw)
            backend=Path(__file__).resolve().parents[2]/'backend/app/application/services'/f'{name}_visualization.py'
            if backend.exists():self.assertEqual(backend.read_bytes(),(Path(__file__).resolve().parents[1]/'app/services'/f'{name}_payload.py').read_bytes())
    def test_extended_worker_dispatch(self):
        from app.services.extended_visualization_worker import preview_bytes
        for reader,fmt,data,options in [('gro-trajectory','gro',gro_bytes(),{'frame':1}),('simulation-mesh','vtu',mesh_bytes(),{'field':'c-0','component':0})]:
            with self.subTest(reader=reader):
                result=preview_bytes(data,reader,'geometry',options,format=fmt)
                self.assertEqual(result['reader'],reader);self.assertEqual(result['kind'],'geometry')

class GeometryReferences(unittest.TestCase):
    def _require(self,module):
        if importlib.util.find_spec(module) is None:
            if os.getenv('AI_DATASEEK_REQUIRE_GEOMETRY_REFERENCE')=='1':self.fail(f'Missing reference package {module}')
            self.skipTest(f'Optional test-only oracle {module}')
    def test_meshio_ascii_writer_and_independent_readback(self):
        self._require('meshio');import meshio;import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.vtu';path.write_bytes(mesh_bytes());original=meshio.read(path)
            meshio.write(path,original,binary=False);raw=path.read_bytes()
            actual=simulation_mesh_preview(raw,'vtu','geometry',{'field':'p-0','component':2})
            np.testing.assert_allclose(actual['mesh']['points'],original.points)
            np.testing.assert_allclose(actual['mesh']['field']['values'],original.point_data['velocity'][:,2])
            self.assertEqual(actual['mesh']['cells'][0]['points'],original.cells[0].data[0].tolist())
    def test_mdanalysis_gro_reader_units_and_writer(self):
        self._require('MDAnalysis');import MDAnalysis as mda;import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.gro';path.write_bytes(gro_bytes(frames=1));u=mda.Universe(str(path));actual=gro_trajectory_preview(path.read_bytes(),'gro','geometry',{'frame':0})
            np.testing.assert_allclose(np.array(actual['trajectory']['positions'])*10,u.atoms.positions,atol=1e-6)
            np.testing.assert_allclose(np.array(actual['trajectory']['velocities'])*10,u.atoms.velocities,atol=1e-7)
            np.testing.assert_allclose(np.array([[1,0,0],[0.2,2,0],[0.3,0.4,3]])*10,u.trajectory.ts.triclinic_dimensions,atol=1e-5)
            out=Path(directory)/'written.gro';u.atoms.write(str(out));written=gro_trajectory_preview(out.read_bytes(),'gro','geometry',{'frame':0})
            np.testing.assert_allclose(written['trajectory']['positions'],actual['trajectory']['positions'],atol=0.001)

if __name__=='__main__':unittest.main()
