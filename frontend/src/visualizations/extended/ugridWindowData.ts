/** Strict public boundary for the restricted UGRID-1.0 reader. */
export const UGRID_WARNING = '完整读取所选有界二维拓扑，仅读取显式索引的场切片；原生坐标平面不是地图，不投影、不跨日界线拼接、不插值或解码时间/垂向坐标。';
export interface UgridSelection { mesh: string; field: string | null; indices: number[] }
export interface UgridField { id: string; label: string; location: 'node' | 'face'; dtype: string; shape: number[]; spatial_axis: number; dimensions: { label: string; size: number }[]; unit: string | null; fill_values: (number | 'NaN')[]; scale_factor: number | null; add_offset: number | null }
export interface UgridMesh { id: string; label: string; node_count: number; face_count: number; max_face_nodes: number; start_index: number; coordinates: { label: string; unit: string | null; standard_name: string | null; dtype: string }[]; fields: UgridField[] }
export interface UgridGeometry { coordinates: number[]; faces: number[][]; values: (number | null)[] | null; location: 'node' | 'face' | null }
export interface UgridData { meshes: UgridMesh[]; selection: UgridSelection | Record<string, never>; geometry?: UgridGeometry; metadata: Record<string, any> }
function need(value: unknown): asserts value { if (!value) throw new Error('UGRID 响应不符合受限二维网格协议。'); }
function object(value: unknown, names: string): asserts value is Record<string, any> { need(value && typeof value === 'object' && !Array.isArray(value)); need(Object.keys(value).sort().join(' ') === names.split(' ').filter(Boolean).sort().join(' ')); }
function int(value: unknown, min = 0, max = 2147483647): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max; }
function num(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e100; }
function label(value: unknown): value is string { return typeof value === 'string' && value.length > 0 && value.length <= 128 && !/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|https?:\/\/|file:|[A-Za-z]:\\/i.test(value); }
function id(value: unknown, prefix: string): value is string { return typeof value === 'string' && new RegExp('^' + prefix + '-[a-f0-9]{32}$').test(value); }
function dtype(value: unknown): value is string { return typeof value === 'string' && /^(?:\|[iu]1|[<>][iu][248]|[<>]f[48])$/.test(value); }
function typed(value: unknown, code: string) { if (!num(value)) return false; const bits = Number(code[2]) * 8; if (code[1] === 'i') return Number.isSafeInteger(value) && value >= -(2 ** (bits - 1)) && value < 2 ** (bits - 1); if (code[1] === 'u') return Number.isSafeInteger(value) && value >= 0 && value < 2 ** bits; return code[2] !== '4' || Math.fround(value) === value; }
function faceGeometry(coordinates: number[], face: number[]) {
  const x0 = coordinates[face[0]! * 2]!, y0 = coordinates[face[0]! * 2 + 1]!;
  const points = face.map(i => [coordinates[2 * i]! - x0, coordinates[2 * i + 1]! - y0] as [number, number]);
  const cross = (a: number[], b: number[], c: number[]) => (b[0]! - a[0]!) * (c[1]! - a[1]!) - (b[1]! - a[1]!) * (c[0]! - a[0]!);
  const on = (a: number[], b: number[], c: number[]) => cross(a, b, c) === 0 && c[0]! >= Math.min(a[0]!, b[0]!) && c[0]! <= Math.max(a[0]!, b[0]!) && c[1]! >= Math.min(a[1]!, b[1]!) && c[1]! <= Math.max(a[1]!, b[1]!);
  need(new Set(points.map(p => p.join(','))).size === points.length);
  const area = points.reduce((v, p, i) => { const q = points[(i + 1) % points.length]!; return v + p[0] * q[1] - q[0] * p[1]; }, 0); need(Number.isFinite(area) && area !== 0);
  for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) { if (j === i + 1 || i === 0 && j === points.length - 1) continue;
    const a = points[i]!, b = points[(i + 1) % points.length]!, c = points[j]!, d = points[(j + 1) % points.length]!;
    need(!(cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0 || on(a, b, c) || on(a, b, d) || on(c, d, a) || on(c, d, b)));
  }
}
function optionalLabel(value: unknown) { return value === null || label(value); }
function same(a: unknown, b: unknown): boolean { if (a === b) return true; if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => same(v, b[i])); if (a && b && typeof a === 'object' && typeof b === 'object') return Object.keys(a).length === Object.keys(b).length && Object.entries(a).every(([k, v]) => Object.prototype.hasOwnProperty.call(b, k) && same(v, (b as Record<string, unknown>)[k])); return false; }
export function validateUgridSelection(selection: unknown): asserts selection is UgridSelection {
  object(selection, 'mesh field indices'); need(id(selection.mesh, 'u') && (selection.field === null || id(selection.field, 'f')));
  need(Array.isArray(selection.indices) && selection.indices.length <= 7 && selection.indices.every(v => int(v)) && (selection.field !== null || !selection.indices.length));
}
export function parseUgridWindow(kind: 'tree' | 'geometry', payload: unknown, metadata: unknown, expected?: UgridSelection): UgridData {
  object(payload, 'media_type choices selected view_kind ' + (kind === 'tree' ? 'tree' : 'ugrid'));
  need(payload.media_type === 'application/json' && payload.view_kind === kind);
  if (kind === 'tree') object(payload.selected, ''); else validateUgridSelection(payload.selected);
  if (expected) need(same(payload.selected, expected));
  object(payload.choices, 'meshes'); const meshes = payload.choices.meshes;
  need(Array.isArray(meshes) && meshes.length >= 1 && meshes.length <= 8);
  const ids = new Set<string>();
  for (const mesh of meshes) {
    object(mesh, 'id label node_count face_count max_face_nodes start_index coordinates fields');
    need(id(mesh.id, 'u') && !ids.has(mesh.id) && label(mesh.label)); ids.add(mesh.id);
    need(int(mesh.node_count, 3, 4096) && int(mesh.face_count, 1, 2048) && int(mesh.max_face_nodes, 3, 8) && int(mesh.start_index, 0, 1));
    need(Array.isArray(mesh.coordinates) && mesh.coordinates.length === 2);
    for (const axis of mesh.coordinates) { object(axis, 'label unit standard_name dtype'); need(label(axis.label) && optionalLabel(axis.unit) && optionalLabel(axis.standard_name) && dtype(axis.dtype)); }
    need(Array.isArray(mesh.fields) && mesh.fields.length <= 32); const fieldIds = new Set<string>();
    for (const field of mesh.fields) {
      object(field, 'id label location dtype shape spatial_axis dimensions unit fill_values scale_factor add_offset');
      need(id(field.id, 'f') && !fieldIds.has(field.id) && label(field.label)); fieldIds.add(field.id);
      need(['node', 'face'].includes(field.location) && dtype(field.dtype) && optionalLabel(field.unit));
      need(Array.isArray(field.shape) && field.shape.length >= 1 && field.shape.length <= 8 && field.shape.every(v => int(v, 1)) && field.shape.reduce((a: bigint, b: number) => a * BigInt(b), 1n) <= BigInt(Number.MAX_SAFE_INTEGER));
      need(int(field.spatial_axis, 0, field.shape.length - 1) && field.shape[field.spatial_axis] === mesh[field.location + '_count']);
      need(Array.isArray(field.dimensions) && field.dimensions.length === field.shape.length);
      field.dimensions.forEach((dim: unknown, i: number) => { object(dim, 'label size'); need(label(dim.label) && int(dim.size, 1) && dim.size === field.shape[i]); });
      need(new Set(field.dimensions.map((d: { label: string }) => d.label)).size === field.dimensions.length);
      need(Array.isArray(field.fill_values) && field.fill_values.length <= 2 && field.fill_values.every(v => v === 'NaN' || num(v)));
      need(field.fill_values.every(v => v === 'NaN' ? field.dtype.includes('f') : typed(v, field.dtype)));
      need((field.scale_factor === null || num(field.scale_factor)) && (field.add_offset === null || num(field.add_offset)));
    }
  }
  need(meshes.reduce((count: number, mesh: UgridMesh) => count + mesh.fields.length, 0) <= 32);
  object(metadata, 'format container conventions input_mode source_bytes read_bytes read_requests attribute_bytes chunks_touched decoded_chunk_bytes topology_complete coordinate_semantics value_semantics bounds missing_values limits');
  need(['nc', 'nc4', 'netcdf', 'h5', 'hdf5', 'hdf'].includes(metadata.format) && metadata.container === 'HDF5' && metadata.conventions === 'UGRID-1.0' && metadata.input_mode === 'window');
  need(int(metadata.source_bytes, 256, 8 * 1024 ** 3) && int(metadata.read_bytes, 1, 8 * 1024 ** 2) && int(metadata.read_requests, 1, 128) && metadata.read_bytes <= metadata.read_requests * 1024 ** 2);
  need(int(metadata.attribute_bytes, 1, 65536) && int(metadata.chunks_touched, 0, 128) && int(metadata.decoded_chunk_bytes, 0, 16 * 1024 ** 2));
  need(metadata.topology_complete === (kind === 'geometry') && metadata.coordinate_semantics === 'native coordinate plane; no projection or dateline wrapping' && metadata.value_semantics === 'raw storage values; declared fill and nonfinite masked; no CF scaling');
  object(metadata.limits, 'max_nodes max_faces max_face_nodes max_fields'); need(same(metadata.limits, { max_nodes: 4096, max_faces: 2048, max_face_nodes: 8, max_fields: 32 }));
  if (kind === 'tree') {
    need(same(payload.tree, meshes.map(m => ({ path: '/' + m.id, node_type: 'mesh', attributes: { label: m.label } }))));
    need(metadata.bounds === null && metadata.missing_values === 0 && metadata.chunks_touched === 0 && metadata.decoded_chunk_bytes === 0);
    return { meshes, selection: {}, metadata };
  }
  const selected = payload.selected as UgridSelection, mesh = (meshes as UgridMesh[]).find(m => m.id === selected.mesh); need(mesh);
  const field = mesh.fields.find(f => f.id === selected.field); need(selected.field === null || field);
  if (field) { const dimensions = field.shape.filter((_, i) => i !== field.spatial_axis); need(dimensions.length === selected.indices.length && dimensions.every((n, i) => selected.indices[i]! < n)); }
  const geometry = payload.ugrid; object(geometry, 'coordinates faces values location');
  need(Array.isArray(geometry.coordinates) && geometry.coordinates.length === 2 * mesh.node_count && geometry.coordinates.every(num));
  mesh.coordinates.forEach((axis, a) => { const values = (geometry.coordinates as number[]).filter((_, i) => i % 2 === a); need(values.every(v => typed(v, axis.dtype))); if (axis.standard_name === 'longitude') need(Math.max(...values) - Math.min(...values) <= 180); if (axis.standard_name === 'latitude') need(values.every(v => v >= -90 && v <= 90)); });
  need(Array.isArray(geometry.faces) && geometry.faces.length === mesh.face_count);
  for (const face of geometry.faces) { need(Array.isArray(face) && face.length >= 3 && face.length <= mesh.max_face_nodes && face.every(i => int(i, 0, mesh.node_count - 1)) && new Set(face).size === face.length); faceGeometry(geometry.coordinates, face); }
  need(geometry.location === (field?.location ?? null));
  if (field) { need(Array.isArray(geometry.values) && geometry.values.length === mesh[field.location === 'node' ? 'node_count' : 'face_count'] && geometry.values.every(v => v === null || num(v)));
    need(geometry.values.every(v => v === null || typed(v, field.dtype)));
    need(!geometry.values.some(v => v !== null && field.fill_values.includes(v)));
  } else need(geometry.values === null);
  const values = geometry.values as (number | null)[] | null;
  need(int(metadata.missing_values) && metadata.missing_values === (values?.filter(v => v === null).length ?? 0));
  const coordinates = geometry.coordinates as number[], bounds = [0, 1].map(a => { const points = coordinates.filter((_, i) => i % 2 === a); return [Math.min(...points), Math.max(...points)]; });
  need(same(metadata.bounds, bounds));
  return { meshes, selection: selected, metadata, geometry: geometry as unknown as UgridGeometry };
}

/** Fit raw coordinates after subtracting a local origin; no float32 or interpolation. */
export function ugridCanvasPoints(geometry: UgridGeometry, width: number, height: number): [number, number][] {
  const xs = geometry.coordinates.filter((_, i) => i % 2 === 0), ys = geometry.coordinates.filter((_, i) => i % 2 === 1);
  const x0 = Math.min(...xs), y0 = Math.min(...ys), dx = Math.max(...xs) - x0, dy = Math.max(...ys) - y0;
  const scale = Math.min(Math.max(1, width - 48) / (dx || 1), Math.max(1, height - 48) / (dy || 1));
  return xs.map((x, i) => [(width - dx * scale) / 2 + (x - x0) * scale, (height + dy * scale) / 2 - (ys[i]! - y0) * scale]);
}
