/** Strict LAS-only geometry schema. Raw XYZ stays int32 until local rendering. */
export const POINTCLOUD_WARNING = '仅显示显式选择的连续点记录窗口，不代表全文件空间抽样；保留原始坐标、强度、分类及标志，不推断坐标系或单位。';
export const POINTCLOUD_SEMANTICS = 'XYZ = raw int32 * scale + offset; no CRS transformation';
const sourceLimit = 8 * 1024 ** 3, readLimit = 8 * 1024 ** 2, maxPoints = 16384;
const records: Record<number, number> = { 0: 20, 1: 28, 2: 26, 3: 34, 6: 30, 7: 36, 8: 38 };
const rgbFormats = [2, 3, 7, 8];
type Obj = Record<string, any>;
export interface PointSelection { point_offset: number; point_count: number }
export interface PointCloudData {
  kind: 'tree' | 'geometry'; metadata: Obj; selected: Record<string, never> | PointSelection;
  raw: number[]; intensity: number[]; classification: number[]; flags: number[]; rgb: number[] | null;
}
function require(ok: unknown): asserts ok { if (!ok) throw new Error('LAS 点窗口结构、数值或选择不符合安全规范。'); }
function keys(value: unknown, fields: string[]): value is Obj { return !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === fields.length && fields.every(k => Object.prototype.hasOwnProperty.call(value, k)); }
function int(value: unknown, lo: number, hi: number): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= lo && value <= hi; }
function finite(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e100; }
function triple(value: unknown, predicate = finite): value is number[] { return Array.isArray(value) && value.length === 3 && value.every(predicate); }
function bounds(value: unknown): value is number[][] { return Array.isArray(value) && value.length === 3 && value.every(v => Array.isArray(v) && v.length === 2 && v.every(finite) && v[0] <= v[1]); }
function same(a: unknown, b: unknown) { return JSON.stringify(a) === JSON.stringify(b); }
export function validatePointSelection(value: unknown, total?: number): PointSelection {
  require(keys(value, ['point_offset', 'point_count']) && int(value.point_offset, 0, Math.floor(sourceLimit / 20)) && int(value.point_count, 1, maxPoints));
  require(total === undefined || value.point_offset + value.point_count <= total);
  return { point_offset: value.point_offset, point_count: value.point_count };
}
export function pointCatalogIdentity(m: Obj) {
  const fields = Object.entries(m).filter(([k]) => !['read_bytes', 'read_requests', 'point_bytes', 'output_points', 'window_bounds'].includes(k));
  return JSON.stringify(Object.fromEntries(fields.sort(([a], [b]) => a.localeCompare(b))));
}
export function parsePointCloudWindow(kind: 'tree' | 'geometry', payload: unknown, meta: unknown, expected: unknown = {}): PointCloudData {
  require(keys(payload, ['view_kind', 'media_type', 'selected', ...(kind === 'tree' ? ['tree'] : ['array', 'point_attributes'])]));
  require(payload.view_kind === kind && payload.media_type === 'application/json');
  const selected = kind === 'tree' ? (require(keys(payload.selected, []) && keys(expected, [])), {}) : validatePointSelection(payload.selected);
  if (kind === 'geometry') require(same(selected, validatePointSelection(expected)));
  require(keys(meta, ['format', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'metadata_bytes', 'las_version', 'point_format', 'record_bytes', 'point_data_offset', 'total_points', 'extra_bytes_per_point', 'vlr_count', 'evlr_count', 'scales', 'offsets', 'declared_bounds', 'window_bounds', 'crs_declarations', 'units', 'coordinate_semantics', 'output_points', 'point_bytes']));
  const m = meta, pf = m.point_format;
  require(m.format === 'las' && m.input_mode === 'window' && ['1.2', '1.4'].includes(m.las_version) && int(pf, 0, 8) && Object.prototype.hasOwnProperty.call(records, pf) && (m.las_version === '1.4' || pf < 4));
  require(int(m.source_bytes, 227, sourceLimit) && int(m.read_bytes, 227, readLimit) && int(m.read_requests, 1, 128) && int(m.metadata_bytes, 227, 1048576));
  require(int(m.record_bytes, records[pf]!, 65535) && int(m.extra_bytes_per_point, 0, 65535) && m.extra_bytes_per_point === m.record_bytes - records[pf]!);
  require(int(m.point_data_offset, m.las_version === '1.2' ? 227 : 375, m.source_bytes) && int(m.total_points, 0, Math.floor(sourceLimit / 20)) && m.point_data_offset + m.record_bytes * m.total_points <= m.source_bytes);
  require(int(m.vlr_count, 0, 96) && int(m.evlr_count, 0, 96) && m.vlr_count + m.evlr_count <= 96 && (m.las_version === '1.4' || m.evlr_count === 0));
  require(m.metadata_bytes === (m.las_version === '1.2' ? 227 : 375) + m.vlr_count * 54 + m.evlr_count * 60);
  require(triple(m.scales, (v): v is number => finite(v) && v >= 1e-100 && v <= 1e90) && triple(m.offsets) && bounds(m.declared_bounds));
  require(m.units === 'unknown' && m.coordinate_semantics === POINTCLOUD_SEMANTICS && Array.isArray(m.crs_declarations) && same(m.crs_declarations, ['geotiff', 'wkt'].filter(v => m.crs_declarations.includes(v))));
  require(int(m.output_points, 0, maxPoints) && int(m.point_bytes, 0, readLimit) && m.read_bytes === m.metadata_bytes + m.point_bytes && m.read_bytes >= m.read_requests);
  const minimumReads = (m.las_version === '1.2' ? 1 : 2) + m.vlr_count + m.evlr_count;
  require(m.read_requests >= minimumReads + Math.ceil(m.point_bytes / 1048576));
  const out: PointCloudData = { kind, metadata: m, selected, raw: [], intensity: [], classification: [], flags: [], rgb: null };
  if (kind === 'tree') {
    require(same(payload.tree, [{ path: '/points', node_type: 'array', attributes: { label: 'LAS point records' } }]) && m.window_bounds === null && m.output_points === 0 && m.point_bytes === 0 && m.read_requests === minimumReads);
  } else {
    const selection = validatePointSelection(selected, m.total_points), n = selection.point_count;
    require(m.output_points === n && m.point_bytes === n * m.record_bytes);
    const a = payload.array, attrs = payload.point_attributes;
    require(keys(a, ['shape', 'dimensions', 'values']) && same(a.shape, [n, 3]) && same(a.dimensions, ['X_raw', 'Y_raw', 'Z_raw']) && Array.isArray(a.values) && a.values.length === 3 * n && a.values.every(v => int(v, -(2 ** 31), 2 ** 31 - 1)));
    const actualBounds = [0, 1, 2].map(d => { const values = a.values.filter((_: number, i: number) => i % 3 === d); return [Math.min(...values) * m.scales[d] + m.offsets[d], Math.max(...values) * m.scales[d] + m.offsets[d]]; });
    require(bounds(m.window_bounds) && same(m.window_bounds, actualBounds));
    require(keys(attrs, ['intensity', 'classification', 'classification_flags', 'rgb']));
    for (const [name, high] of [['intensity', 65535], ['classification', pf < 6 ? 31 : 255], ['classification_flags', pf < 6 ? 7 : 15]] as const) require(Array.isArray(attrs[name]) && attrs[name].length === n && attrs[name].every((v: unknown) => int(v, 0, high)));
    require(rgbFormats.includes(pf) ? Array.isArray(attrs.rgb) && attrs.rgb.length === 3 * n && attrs.rgb.every((v: unknown) => int(v, 0, 65535)) : attrs.rgb === null);
    Object.assign(out, { raw: a.values, intensity: attrs.intensity, classification: attrs.classification, flags: attrs.classification_flags, rgb: attrs.rgb });
  }
  require(new TextEncoder().encode(JSON.stringify({ payload, metadata: m })).length < 2 * 1024 ** 2 - 512);
  return out;
}
export function pointCoordinates(data: PointCloudData, index: number) {
  require(data.kind === 'geometry' && int(index, 0, data.raw.length / 3 - 1));
  const raw = data.raw.slice(index * 3, index * 3 + 3);
  return { raw, xyz: raw.map((v, d) => v * data.metadata.scales[d] + data.metadata.offsets[d]) };
}
export function localPointGeometry(data: PointCloudData) {
  require(data.kind === 'geometry' && data.raw.length > 0);
  const originRaw = data.raw.slice(0, 3), local = data.raw.map((v, i) => (v - originRaw[i % 3]!) * data.metadata.scales[i % 3]);
  // A single common display scale preserves aspect ratio and avoids Float32
  // overflow. Neither translation nor display scaling changes reported XYZ.
  const displayScale = Math.max(...local.map(Math.abs)) || Math.max(...data.metadata.scales);
  require(finite(displayScale) && displayScale > 0);
  return { positions: Float32Array.from(local, v => v / displayScale), originRaw, origin: pointCoordinates(data, 0).xyz, displayScale };
}
