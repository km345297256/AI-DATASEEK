"""Synthetic NetCDF4 writer fixtures; no external or user datasets."""
import io

def ugrid_bytes(*, start=0, transpose=False, packed=False, compression=False, large=False):
    import netCDF4
    import numpy as np
    handle = netCDF4.Dataset("synthetic", "w", memory=131072)
    handle.Conventions = "CF-1.8 UGRID-1.0"
    for name, length in (("node", 5), ("face", 2), ("corner", 4), ("time", 2), ("layer", 3)):
        handle.createDimension(name, length)
    topology = handle.createVariable("mesh", "i4")
    topology.cf_role = "mesh_topology"
    topology.topology_dimension = np.int32(2)
    topology.node_coordinates = "x y"
    topology.face_node_connectivity = "face_nodes"
    topology.face_dimension = "face"
    for name, values in (("x", [100., 101., 101., 100., 102.]), ("y", [30., 30., 31., 31., 30.])):
        variable = handle.createVariable(name, "f8", ("node",), zlib=compression)
        variable.standard_name = "longitude" if name == "x" else "latitude"
        variable.units = "degrees_east" if name == "x" else "degrees_north"
        variable[:] = values
    conn = handle.createVariable("face_nodes", "i4", ("corner", "face") if transpose else ("face", "corner"), fill_value=-999, zlib=compression)
    conn.cf_role = "face_node_connectivity"
    conn.start_index = np.int32(start)
    faces = np.array([[0, 1, 2, 3], [1, 4, 2, -999]])
    faces[faces >= 0] += start
    conn[:] = faces.T if transpose else faces
    node = handle.createVariable("temperature", "f4", ("time", "node", "layer"), fill_value=-999, zlib=compression)
    node.mesh = "mesh"
    node.location = "node"
    node.units = "K"
    data = np.arange(30, dtype=np.float32).reshape(2, 5, 3)
    data[1, 2, 2] = -999
    node[:] = data
    if packed:
        node.scale_factor = 0.1
        node.add_offset = 270.
    face = handle.createVariable("height", "f8", ("face", "time"), zlib=compression)
    face.mesh = "mesh"
    face.location = "face"
    face.units = "m"
    face[:] = [[1., 2.], [3., 4.]]
    if large:
        handle.createDimension("unused", 300000000)
        handle.createVariable("unread", "f8", ("unused",), chunksizes=(128,))
    return bytes(handle.close())

def preview(data, kind="tree", options=None, size=None):
    from app.services.ugrid_window_reader import ugrid_window_preview
    return ugrid_window_preview(lambda o, n: (data[o:o+n] + bytes(max(0, o+n-len(data))))[:n], len(data) if size is None else size, "nc", kind, options)

def browser_payloads():
    output = {}
    for name, packed in (("node", True), ("face", False)):
        data = ugrid_bytes(start=1, transpose=True, packed=packed)
        tree = preview(data)
        mesh = tree["choices"]["meshes"][0]
        field = next(f for f in mesh["fields"] if f["location"] == name)
        selection = {"mesh": mesh["id"], "field": field["id"], "indices": [1, 2] if name == "node" else [1]}
        output[name+"_tree"] = tree
        output[name+"_geometry"] = preview(data, "geometry", selection)
    return output

if __name__ == "__main__":
    import json
    print(json.dumps(browser_payloads(), ensure_ascii=False, allow_nan=False))
