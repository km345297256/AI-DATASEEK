"""Root-group NetCDF4 UGRID-1.0 2D meshes through the authorized HDF5 VFD.

Only fixed-size metadata is inspected: never load DIMENSION_LIST/REFERENCE_LIST
heaps, links, VDS, plugins or a NetCDF source filename. No geographic transform.
"""
import hashlib
import math
import re

from .array_window_reader import RangeFile, _describe, _slice_plan, _verify_chunks
from .ugrid_window_payload import (FORMATS, LIMITS, COORDINATES, VALUES, WARNING,
    need, fail, label, number, dtype, validate_ugrid_window_options, validate_ugrid_window_payload)

def _id(name, prefix):
    return prefix + "-" + hashlib.sha256(name.encode()).hexdigest()[:32]

def _clean(value):
    return value if label(value) else "[redacted]"

def _name(value):
    need(type(value) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", value) is not None)
    return value

class _Attributes:
    def __init__(self):
        self.bytes = 0
        self.cache = {}

    def get(self, obj, name, *, numeric=False, vector=False):
        import h5py
        import numpy as np
        key = (h5py.h5o.get_info(obj.id).addr, name, numeric, vector)
        if key in self.cache:
            return self.cache[key]
        if name not in obj.attrs:
            return None
        aid = obj.attrs.get_id(name)
        shape, dt = aid.shape, aid.dtype
        need(shape in {(), (1,)} or vector and len(shape) == 1 and 1 <= shape[0] <= 8)
        need(dt.kind in ({"i", "u", "f"} if numeric else {"S"}))
        need(not dt.metadata or dt.kind == "S" and set(dt.metadata) <= {"h5py_encoding"})
        length = math.prod(shape or (1,))*dt.itemsize
        need(1 <= dt.itemsize <= (8 if numeric else 512) and length <= 512 and self.bytes + length <= 65536)
        self.bytes += length
        raw = obj.attrs[name]
        items = np.asarray(raw).reshape(-1).tolist()
        if not numeric:
            need(all(isinstance(v, bytes) for v in items))
            items = [v.decode("utf-8", errors="strict") for v in items]
            need(all("\0" not in v for v in items))
        else:
            need(all(type(v) in {int, float} and (number(v) or type(v) is float and math.isnan(v)) for v in items))
        value = items if vector else items[0]
        self.cache[key] = value
        return value

def _numeric(item):
    need(_describe(item)["selectable"] and dtype(item.dtype.str))
    return item

def _catalog(handle, attrs):
    import h5py
    conventions = (attrs.get(handle, "Conventions") or "").replace(",", " ").split()
    need("UGRID-1.0" in conventions and all(not token.startswith("UGRID-") or token == "UGRID-1.0" for token in conventions))
    count = handle.id.get_num_objs()
    need(1 <= count <= 128)
    objects, addresses = {}, set()
    for i in range(count):
        raw = handle.id.get_objname_by_idx(i)
        need(len(raw) <= 128)
        name = _name(raw.decode("utf-8", errors="strict"))
        need(handle.get(name, getlink=True, getclass=True) is h5py.HardLink)
        obj = handle[name]
        need(isinstance(obj, h5py.Dataset))
        address = h5py.h5o.get_info(obj.id).addr
        need(address not in addresses)
        addresses.add(address)
        objects[name] = obj
    dimensions = {}
    for name, obj in objects.items():
        dimid = attrs.get(obj, "_Netcdf4Dimid", numeric=True)
        if dimid is not None:
            need(type(dimid) is int and 0 <= dimid <= 2**31-1 and dimid not in dimensions and obj.ndim == 1 and attrs.get(obj, "CLASS") == "DIMENSION_SCALE")
            dimensions[dimid] = (name, obj.shape[0])

    def get(name):
        need(_name(name) in objects)
        return _numeric(objects[name])

    def dims(obj):
        indices = attrs.get(obj, "_Netcdf4Coordinates", numeric=True, vector=True)
        # A dimension coordinate itself need not carry _Netcdf4Coordinates.
        if indices is None and obj.ndim == 1:
            own = attrs.get(obj, "_Netcdf4Dimid", numeric=True)
            if own is not None:
                indices = [own]
        need(type(indices) is list and len(indices) == obj.ndim and all(type(i) is int and i in dimensions for i in indices) and len(set(indices)) == len(indices))
        need(all(dimensions[i][1] == size for i, size in zip(indices, obj.shape)))
        return indices

    choices, plans = [], {}
    total_fields = 0
    for mesh_name, topology in objects.items():
        if attrs.get(topology, "cf_role") != "mesh_topology":
            continue
        need(len(choices) < 8 and topology.shape == () and topology.dtype.kind in {"i", "u"})
        need(type(attrs.get(topology, "topology_dimension", numeric=True)) is int and attrs.get(topology, "topology_dimension", numeric=True) == 2)
        names = (attrs.get(topology, "node_coordinates") or "").split()
        need(len(names) == 2 and len(set(names)) == 2)
        axes = [get(n) for n in names]
        need(all(a.ndim == 1 for a in axes) and axes[0].shape == axes[1].shape and 3 <= axes[0].shape[0] <= 4096)
        node_dim = dims(axes[0])[0]
        need(dims(axes[1]) == [node_dim])
        for axis in axes:
            scale, offset = attrs.get(axis, "scale_factor", numeric=True), attrs.get(axis, "add_offset", numeric=True)
            need(scale is None or scale == 1)
            need(offset is None or offset == 0)
        connection = get(attrs.get(topology, "face_node_connectivity"))
        need(connection.ndim == 2 and connection.dtype.kind in {"i", "u"} and attrs.get(connection, "cf_role") == "face_node_connectivity")
        connection_dims = dims(connection)
        face_dimension = attrs.get(topology, "face_dimension")
        face_axis = 0
        if face_dimension is not None:
            matches = [i for i, d in enumerate(connection_dims) if dimensions[d][0] == face_dimension]
            need(len(matches) == 1)
            face_axis = matches[0]
        face_dim = connection_dims[face_axis]
        node_count, face_count, corners = axes[0].shape[0], connection.shape[face_axis], connection.shape[1-face_axis]
        need(face_dim != node_dim and 1 <= face_count <= 2048 and 3 <= corners <= 8)
        start = attrs.get(connection, "start_index", numeric=True)
        start = 0 if start is None else start
        need(type(start) is int and start in {0, 1})
        fill = attrs.get(connection, "_FillValue", numeric=True)
        need(fill is None or type(fill) is int and not start <= fill < node_count+start)
        need(not any(a in connection.attrs for a in ("scale_factor", "add_offset", "missing_value")))
        fields, field_plans = [], {}
        for name, field in objects.items():
            mesh_ref = attrs.get(field, "mesh")
            if mesh_ref != mesh_name:
                continue
            # Edges and location index sets need a separately reviewed model.
            location = attrs.get(field, "location")
            need(location in {"node", "face"} and "location_index_set" not in field.attrs and total_fields < 32)
            total_fields += 1
            _numeric(field)
            field_dims = dims(field)
            spatial_dim = node_dim if location == "node" else face_dim
            need(field_dims.count(spatial_dim) == 1)
            missing = [attrs.get(field, key, numeric=True) for key in ("_FillValue", "missing_value")]
            missing = ["NaN" if type(v) is float and math.isnan(v) else v for v in missing if v is not None]
            choice = {"id": _id(mesh_name + "/" + name, "f"), "label": _clean(name), "location": location,
                "dtype": field.dtype.str, "shape": list(field.shape), "spatial_axis": field_dims.index(spatial_dim),
                "dimensions": [{"label": _clean(dimensions[d][0]), "size": dimensions[d][1]} for d in field_dims],
                "unit": _clean(attrs.get(field, "units")) if "units" in field.attrs else None,
                "fill_values": missing, "scale_factor": attrs.get(field, "scale_factor", numeric=True), "add_offset": attrs.get(field, "add_offset", numeric=True)}
            need(all(choice[k] is None or number(choice[k]) for k in ("scale_factor", "add_offset")))
            fields.append(choice)
            field_plans[choice["id"]] = field
        mesh = {"id": _id(mesh_name, "u"), "label": _clean(mesh_name), "node_count": node_count, "face_count": face_count,
            "max_face_nodes": corners, "start_index": start, "fields": fields,
            "coordinates": [{"label": _clean(name), "unit": _clean(attrs.get(a, "units")) if "units" in a.attrs else None,
                "standard_name": _clean(attrs.get(a, "standard_name")) if "standard_name" in a.attrs else None, "dtype": a.dtype.str} for name, a in zip(names, axes)]}
        choices.append(mesh)
        plans[mesh["id"]] = (axes, connection, face_axis, fill, field_plans)
    need(choices)
    return choices, plans

def _all(dataset):
    return [{"start": 0, "stop": n, "step": 1} for n in dataset.shape]

def _values(dataset, selection, size):
    index, _, _, _ = _slice_plan(dataset, {"selection": selection})
    _verify_chunks(dataset, selection, size)
    values = dataset[index]
    need(values.size <= 16384)
    raw = values.reshape(-1).tolist()
    need(all(number(v) or type(v) is float and not math.isfinite(v) for v in raw))
    return values

def ugrid_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    options = validate_ugrid_window_options(kind, {} if options is None else options)
    need(type(fmt) is str and fmt in FORMATS)
    source = RangeFile(read_range, size, limits)
    try:
        import h5py
        while h5py.h5pl.size():
            h5py.h5pl.remove(0)
        need(source.read(8) == b"\x89HDF\r\n\x1a\n")
        source.seek(0)
        attrs = _Attributes()
        with h5py.File(source, "r", driver="fileobj", rdcc_nbytes=4*1024**2, rdcc_nslots=257, rdcc_w0=1) as handle:
            meshes, plans = _catalog(handle, attrs)
            output = {"contract_version": 2, "type": "ugrid-window", "reader": "ugrid-window", "kind": kind,
                "media_type": "application/json", "selected": options, "choices": {"meshes": meshes}, "warnings": [WARNING], "sampled": False}
            chunks = decoded = missing = 0
            bounds = None
            if kind == "tree":
                output["tree"] = [{"path": "/"+m["id"], "node_type": "mesh", "attributes": {"label": m["label"]}} for m in meshes]
            else:
                mesh = next((m for m in meshes if m["id"] == options["mesh"]), None)
                need(mesh is not None)
                axes, connection, face_axis, fill, fields = plans[mesh["id"]]
                choice = next((f for f in mesh["fields"] if f["id"] == options["field"]), None)
                need(options["field"] is None or choice is not None)
                reads = [(a, _all(a)) for a in axes] + [(connection, _all(connection))]
                if choice:
                    need(len(options["indices"]) == len(choice["shape"])-1)
                    iterator = iter(options["indices"])
                    selection = [{"start": 0, "stop": n, "step": 1} if i == choice["spatial_axis"] else next(iterator) for i, n in enumerate(choice["shape"])]
                    reads.append((fields[choice["id"]], selection))
                for dataset, selection in reads:
                    _, _, n, b = _slice_plan(dataset, {"selection": selection})
                    chunks += n
                    decoded += b
                need(chunks <= 128 and decoded <= 16*1024**2)
                coordinates = [_values(a, selection, size).tolist() for a, selection in reads[:2]]
                for i, axis in enumerate(axes):
                    fills = [attrs.get(axis, k, numeric=True) for k in ("_FillValue", "missing_value")]
                    need(all(number(v) and v not in fills for v in coordinates[i]))
                    standard = mesh["coordinates"][i]["standard_name"]
                    if standard == "longitude":
                        need(max(coordinates[i])-min(coordinates[i]) <= 180)
                    if standard == "latitude":
                        need(all(-90 <= v <= 90 for v in coordinates[i]))
                raw_faces = _values(connection, reads[2][1], size)
                if face_axis == 1:
                    raw_faces = raw_faces.T
                faces = []
                for row in raw_faces.tolist():
                    face, padded = [], False
                    for index in row:
                        if fill is not None and index == fill:
                            padded = True
                        else:
                            need(not padded)
                            face.append(index-mesh["start_index"])
                    need(3 <= len(face) <= 8 and len(set(face)) == len(face) and all(0 <= i < mesh["node_count"] for i in face))
                    faces.append(face)
                values = None
                if choice:
                    raw = _values(reads[3][0], reads[3][1], size).reshape(-1).tolist()
                    values = [v if number(v) and v not in choice["fill_values"] else None for v in raw]
                    missing = sum(v is None for v in values)
                output["ugrid"] = {"coordinates": [v for point in zip(*coordinates) for v in point], "faces": faces,
                    "values": values, "location": choice["location"] if choice else None}
                bounds = [[min(a), max(a)] for a in coordinates]
            output["metadata"] = {"format": fmt, "container": "HDF5", "conventions": "UGRID-1.0", "input_mode": "window",
                "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests,
                "attribute_bytes": attrs.bytes, "chunks_touched": chunks, "decoded_chunk_bytes": decoded,
                "topology_complete": kind == "geometry", "coordinate_semantics": COORDINATES, "value_semantics": VALUES,
                "bounds": bounds, "missing_values": missing, "limits": dict(LIMITS)}
        output["metadata"]["read_bytes"], output["metadata"]["read_requests"] = source.read_bytes, source.read_requests
        return validate_ugrid_window_payload(output, kind=kind, options=options, fmt=fmt, source_bytes=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        fail()
    finally:
        source.close()
