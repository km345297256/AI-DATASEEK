import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np
from app.services import ugrid_window_reader as reader
from app.services.ugrid_window_payload import validate_ugrid_window_payload, validate_ugrid_window_options
from ugrid_window_fixtures import ugrid_bytes, preview, browser_payloads

def change(callback, data=None):
    stream = io.BytesIO(ugrid_bytes() if data is None else data)
    with h5py.File(stream, "r+") as handle:
        callback(handle)
    return stream.getvalue()

def attr(obj, key, value):
    if key in obj.attrs:
        del obj.attrs[key]
    obj.attrs[key] = np.bytes_(value) if type(value) is str else value

def selection(data, location=None):
    mesh = preview(data)["choices"]["meshes"][0]
    field = next((f for f in mesh["fields"] if f["location"] == location), None)
    return {"mesh": mesh["id"], "field": field["id"] if field else None, "indices": [1, 2] if location == "node" else [1] if location == "face" else []}

class UgridReader(unittest.TestCase):
    def test_real_netcdf4_node_face_and_all_nonspatial_dimensions(self):
        for start in (0, 1):
            for transpose in (False, True):
                for compressed in (False, True):
                    with self.subTest(start=start, transpose=transpose, compressed=compressed):
                        source = ugrid_bytes(start=start, transpose=transpose, compression=compressed, packed=True)
                        for location, expected in (("node", [17., 20., None, 26., 29.]), ("face", [2., 4.])):
                            value = preview(source, "geometry", selection(source, location))
                            self.assertEqual(value["ugrid"]["values"], expected)
                            self.assertEqual(value["ugrid"]["faces"], [[0, 1, 2, 3], [1, 4, 2]])
                            self.assertEqual(value["ugrid"]["coordinates"], [100., 30., 101., 30., 101., 31., 100., 31., 102., 30.])
                            self.assertEqual(value["ugrid"]["location"], location)
                            self.assertFalse(value["sampled"])
                        self.assertIsNone(preview(source, "geometry", selection(source))["ugrid"]["values"])

    def test_tree_only_describes_and_never_decodes_numeric_datasets(self):
        with patch.object(reader, "_values", side_effect=AssertionError("tree must not decode")):
            result = preview(ugrid_bytes(large=True))
        self.assertEqual(result["metadata"]["chunks_touched"], 0)
        self.assertIsNone(result["metadata"]["bounds"])

    def test_real_ranges_sparse_eight_gib_and_cancellation(self):
        raw = ugrid_bytes(large=True)
        requests = []
        def read(offset, length):
            requests.append((offset, length))
            self.assertLessEqual(length, 1048576)
            return raw[offset:offset+length].ljust(length, b"\0")
        result = reader.ugrid_window_preview(read, 8*1024**3, "nc", "geometry", selection(raw, "face"))
        self.assertLess(sum(n for _, n in requests), 1024**2)
        self.assertEqual(result["metadata"]["read_requests"], len(requests))
        self.assertEqual(result["metadata"]["read_bytes"], sum(n for _, n in requests))
        def cancel(offset, length):
            raise KeyboardInterrupt("cancelled")
        with self.assertRaises(KeyboardInterrupt):
            reader.ugrid_window_preview(cancel, len(raw), "nc")

    def test_invalid_options_source_format_and_range_budget(self):
        raw = ugrid_bytes()
        chosen = selection(raw, "node")
        for invalid in ({}, dict(chosen, indices=[]), dict(chosen, indices=[True, 2]), dict(chosen, indices=[2, 0]), dict(chosen, indices=[0, 3]), dict(chosen, field=None), dict(chosen, path="secret")):
            with self.subTest(options=invalid), self.assertRaises(ValueError):
                preview(raw, "geometry", invalid)
        for fmt in ("las", "nc3", "", [], None):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                reader.ugrid_window_preview(lambda o,n:raw[o:o+n],len(raw),fmt)
        for limits in ({"max_read_bytes":1,"max_total_bytes":1,"max_reads":1}, {"max_read_bytes":True,"max_total_bytes":8388608,"max_reads":128}):
            with self.assertRaises(ValueError):
                reader.ugrid_window_preview(lambda o,n:raw[o:o+n],len(raw),"nc",limits=limits)
        with self.assertRaises(ValueError):
            reader.ugrid_window_preview(lambda o,n:b"",len(raw),"nc")
        with self.assertRaises(ValueError):
            preview(b"CDF\1"+bytes(1024))

    def test_root_metadata_and_dimension_ambiguities_fail_closed(self):
        edits = {
            "no convention": lambda h: attr(h,"Conventions","CF-1.8"),
            "conflicting convention": lambda h: attr(h,"Conventions","UGRID-1.0 UGRID-2.0"),
            "wrong topology": lambda h: attr(h["mesh"],"topology_dimension",np.int32(3)),
            "float topology": lambda h: attr(h["mesh"],"topology_dimension",2.),
            "three coordinates": lambda h: attr(h["mesh"],"node_coordinates","x y height"),
            "alias coordinates": lambda h: attr(h["mesh"],"node_coordinates","x x"),
            "wrong conn role": lambda h: attr(h["face_nodes"],"cf_role","edge_node_connectivity"),
            "bad face dimension": lambda h: attr(h["mesh"],"face_dimension","unknown"),
            "bad dimension identity": lambda h: attr(h["temperature"],"_Netcdf4Coordinates",np.array([3,1,4],dtype="i4")),
            "missing dimids": lambda h: h["temperature"].attrs.__delitem__("_Netcdf4Coordinates"),
            "edge field": lambda h: attr(h["temperature"],"location","edge"),
            "index set": lambda h: attr(h["temperature"],"location_index_set","subset"),
            "packed x": lambda h: attr(h["x"],"scale_factor",.1),
            "offset y": lambda h: attr(h["y"],"add_offset",1.),
            "start2": lambda h: attr(h["face_nodes"],"start_index",np.int32(2)),
            "inrangefill": lambda h: attr(h["face_nodes"],"_FillValue",np.int32(1)),
            "softlink": lambda h: h.__setitem__("evil",h5py.SoftLink("/x")),
            "external": lambda h: h.__setitem__("evil",h5py.ExternalLink("/private/secret","x")),
            "hardalias": lambda h: h.__setitem__("alias",h["x"]),
            "vlen attr": lambda h: h["mesh"].attrs.__setitem__("node_coordinates",["x","y"]),
            "group": lambda h: h.create_group("child"),
        }
        for name, edit in edits.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                preview(change(edit))

    def test_bad_coordinates_connectivity_and_polygons_rejected(self):
        edits = {
            "nan coordinate": lambda h:h["x"].__setitem__(0,np.nan),
            "infinite coordinate": lambda h:h["x"].__setitem__(0,np.inf),
            "missing coordinate": lambda h:(h["x"].__setitem__(0,-999),attr(h["x"],"missing_value",-999.)),
            "dateline": lambda h:h["x"].__setitem__(slice(None),[-179,179,179,-179,180]),
            "bad latitude": lambda h:h["y"].__setitem__(0,91),
            "out of range": lambda h:h["face_nodes"].__setitem__((0,0),5),
            "duplicate": lambda h:h["face_nodes"].__setitem__((0,1),0),
            "nontrailing fill": lambda h:h["face_nodes"].__setitem__((0,1),-999),
            "selfintersect": lambda h:h["face_nodes"].__setitem__(0,[0,2,1,3]),
            "collapsed": lambda h:h["y"].__setitem__(slice(None),30),
        }
        raw = ugrid_bytes()
        chosen = selection(raw)
        for name, edit in edits.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                preview(change(edit,raw),"geometry",chosen)

    def test_bounds_and_unknown_schema_binding(self):
        result = browser_payloads()["node_geometry"]
        for kwargs in ({"kind":"tree"},{"fmt":"h5"},{"source_bytes":1},{"read_bytes":1},{"read_requests":100},{"options":dict(result["selected"],indices=[0,0])}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):validate_ugrid_window_payload(result,**kwargs)
        validate_ugrid_window_payload(result,kind="geometry",options=result["selected"],source_bytes=result["metadata"]["source_bytes"])
        self.assertNotIn("/private",json.dumps(result))

    def test_global_fields_across_multiple_meshes_are_bounded(self):
        def add(handle):
            handle.copy("mesh", "mesh2")
            for i in range(31):
                name = "extra" + str(i)
                handle.copy("height", name)
                attr(handle[name], "mesh", "mesh2" if i % 2 else "mesh")
        with self.assertRaises(ValueError):
            preview(change(add))

    def test_standard_float32_fill_nonfinite_and_identity_coordinates(self):
        def add(handle):
            attr(handle["temperature"], "_FillValue", np.float32(9.969209968386869e36))
            handle["temperature"][1, 2, 2] = np.float32(9.969209968386869e36)
            handle["temperature"][1, 3, 2] = np.nan
            attr(handle["x"], "scale_factor", 1.)
            attr(handle["y"], "add_offset", 0.)
        raw = change(add)
        result = preview(raw, "geometry", selection(raw, "node"))
        self.assertEqual(result["ugrid"]["values"], [17.,20.,None,None,29.])

    def test_unsafe_chunks_external_storage_vds_and_dtype_never_decode(self):
        def replace(handle, variant):
            values = handle["temperature"][:]
            del handle["temperature"]
            if variant == "chunk":
                field = handle.create_dataset("temperature",shape=(2,5,300000),dtype="f8",chunks=(1,5,300000))
            elif variant == "external":
                field = handle.create_dataset("temperature",shape=(2,5,3),dtype="f4",external=[("/private/never-read",0,120)])
            elif variant == "vds":
                layout = h5py.VirtualLayout(shape=(2,5,3),dtype="f4")
                layout[:] = h5py.VirtualSource("/private/never-read","data",shape=(2,5,3))
                field = handle.create_virtual_dataset("temperature",layout)
            else:
                field = handle.create_dataset("temperature",data=values.astype("c8"))
            attr(field,"mesh","mesh"); attr(field,"location","node")
            attr(field,"_Netcdf4Coordinates",np.array([3,0,4],dtype="i4"))
        for variant in ("chunk","external","vds","complex"):
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                preview(change(lambda h:replace(h,variant)))

    def test_full_bounded_topology_at_declared_limits_without_sampling(self):
        import netCDF4
        h = netCDF4.Dataset("synthetic-large", "w", memory=262144)
        h.Conventions = "UGRID-1.0"
        for name, n in (("node",4096),("face",2048),("corner",3),("time",2)):
            h.createDimension(name,n)
        mesh=h.createVariable("mesh","i4");mesh.cf_role="mesh_topology";mesh.topology_dimension=np.int32(2)
        mesh.node_coordinates="x y";mesh.face_node_connectivity="conn"
        x=h.createVariable("x","f8",("node",));x[:]=np.arange(4096)%64
        y=h.createVariable("y","f8",("node",));y[:]=np.arange(4096)//64
        conn=h.createVariable("conn","i4",("face","corner"));conn.cf_role="face_node_connectivity"
        starts=(np.arange(2048)//63)*64+np.arange(2048)%63
        conn[:]=np.stack([starts,starts+1,starts+64],axis=1)
        values=h.createVariable("value","f4",("time","node"),zlib=True,chunksizes=(1,128))
        values.mesh="mesh";values.location="node";values[:]=np.arange(8192).reshape(2,4096)
        raw=bytes(h.close());tree=preview(raw);m=tree["choices"]["meshes"][0]
        result=preview(raw,"geometry",{"mesh":m["id"],"field":m["fields"][0]["id"],"indices":[1]})
        self.assertEqual(len(result["ugrid"]["coordinates"]),8192)
        self.assertEqual(len(result["ugrid"]["faces"]),2048)
        self.assertEqual(result["ugrid"]["values"],list(range(4096,8192)))
        self.assertFalse(result["sampled"])
        self.assertLess(len(json.dumps(result).encode()),2097152)

    def test_concave_faces_preserved_without_triangulation_or_winding_repair(self):
        from app.services.ugrid_window_payload import face_geometry
        coordinates=[0,0,2,0,1,1,2,2,0,2]
        face_geometry(coordinates,[0,1,2,3,4])
        face_geometry(coordinates,[4,3,2,1,0])

class UgridReference(unittest.TestCase):
    def test_official_xugrid_topology_and_writer_roundtrip(self):
        try:
            import xugrid as xu
        except ImportError:
            if os.getenv("AI_DATASEEK_REQUIRE_UGRID_REFERENCE") == "1":
                self.fail("Xugrid independent oracle is required")
            self.skipTest("optional independent Xugrid reference")
        import xarray as xr
        with tempfile.TemporaryDirectory(prefix="ugrid-oracle-") as directory:
            path = Path(directory)/"synthetic.nc"
            path.write_bytes(ugrid_bytes(start=1,transpose=True))
            dataset = xr.open_dataset(path,engine="netcdf4",decode_cf=False,mask_and_scale=False)
            try:
                grid = xu.Ugrid2d.from_dataset(dataset,topology="mesh")
                expected = preview(path.read_bytes(),"geometry",selection(path.read_bytes(),"face"))
                np.testing.assert_equal(grid.node_coordinates,np.array(expected["ugrid"]["coordinates"]).reshape(-1,2))
                faces = [[int(i) for i in row if i != grid.fill_value] for row in grid.face_node_connectivity]
                self.assertEqual(faces,expected["ugrid"]["faces"])
                written = Path(directory)/"xugrid.nc"
                grid.to_dataset().to_netcdf(written,engine="netcdf4")
            finally:
                dataset.close()
            raw = written.read_bytes()
            actual = preview(raw,"geometry",selection(raw))
            np.testing.assert_equal(np.array(actual["ugrid"]["coordinates"]).reshape(-1,2),grid.node_coordinates)
            self.assertEqual(actual["ugrid"]["faces"],faces)
