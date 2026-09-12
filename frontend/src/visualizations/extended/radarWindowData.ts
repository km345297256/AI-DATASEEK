const ERROR = '雷达响应、存储码或窗口选择无效，请重新打开预览。';
export const RADAR_UNITS: Record<string, string> = { DBZH: 'dBZ', DBZV: 'dBZ', TH: 'dBZ', TV: 'dBZ', ZDR: 'dB', RHOHV: '1', PHIDP: 'degrees', KDP: 'degrees/km', VRADH: 'm/s', WRADH: 'm/s' };
export const RADAR_LIMITS = { max_sweeps: 32, max_quantities: 16, max_fields: 128, max_rays: 128, max_gates: 128, max_values: 16384, max_attribute_bytes: 32768, max_chunk_bytes: 4194304, max_decoded_bytes: 16777216 };
export const RADAR_WARNING = '射线存储索引 × 斜距门中心，不是地理 PPI 地图；不推算方位或地面距离，不旋转 a1gate。nodata 和 undetect 不是测量零值。';
export type RadarKind = 'tree' | 'image';
export type RadarSelection = { sweep: number; quantity: string; ray_start: number; ray_count: number; gate_start: number; gate_count: number; decode: 'raw' };
export type RadarQuantity = { id: string; dtype: string; chunks: [number, number] | null; gain: number; offset: number; nodata: number; undetect: number; unit: string };
export type RadarSweep = { id: number; elevation: number; nrays: number; nbins: number; rstart_m: number; rscale_m: number; a1gate: number; quantities: RadarQuantity[] };
export type RadarData = { sweeps: RadarSweep[]; selected: RadarSelection | Record<string, never>; values: number[] | null; range: number[]; rays: number[]; nodata: number; undetect: number; sourceBytes: number; readBytes: number; reads: number; format: string; metadata: Record<string, unknown> };
function fail(): never { throw new Error(ERROR); }
function need(condition: unknown): asserts condition { if (!condition) fail(); }
function object(value: unknown): Record<string, any> { need(value && typeof value === 'object' && !Array.isArray(value)); return value as Record<string, any>; }
function keys(value: unknown, names: string): Record<string, any> { const result = object(value); need(Object.keys(result).sort().join('|') === names.split(' ').sort().join('|')); return result; }
function integer(value: unknown, low = 0, high = 2 ** 31 - 1): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= low && value <= high; }
function finite(value: unknown, bound = 1e12): value is number { return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= bound; }
function equal(a: unknown, b: unknown): boolean { return JSON.stringify(a) === JSON.stringify(b); }
const isDtype = (value: unknown): value is string => typeof value === 'string' && ['|u1', '<u2', '>u2'].includes(value);
const itemsize = (dtype: string) => dtype === '|u1' ? 1 : 2;

export function validateRadarOptions(kind: RadarKind, value: unknown, sweeps?: RadarSweep[]): RadarSelection | Record<string, never> {
  need(kind === 'tree' || kind === 'image');
  if (kind === 'tree') { keys(value, ''); return {}; }
  const s = keys(value, 'sweep quantity ray_start ray_count gate_start gate_count decode');
  need(integer(s.sweep, 1, 32) && typeof s.quantity === 'string' && Object.prototype.hasOwnProperty.call(RADAR_UNITS, s.quantity)
    && integer(s.ray_start, 0, 4095) && integer(s.gate_start, 0, 65535) && integer(s.ray_count, 1, 128) && integer(s.gate_count, 1, 128) && s.decode === 'raw');
  if (sweeps) { const sweep = sweeps.find(row => row.id === s.sweep); need(sweep && sweep.quantities.some(q => q.id === s.quantity) && s.ray_start + s.ray_count <= sweep.nrays && s.gate_start + s.gate_count <= sweep.nbins); }
  return { sweep: s.sweep, quantity: s.quantity, ray_start: s.ray_start, ray_count: s.ray_count, gate_start: s.gate_start, gate_count: s.gate_count, decode: 'raw' };
}

export function parseRadarWindow(kind: RadarKind, payload: unknown, metadata: unknown, expected?: unknown): RadarData {
  const p = keys(payload, 'media_type choices selected view_kind ' + (kind === 'tree' ? 'tree' : 'array radar'));
  need(p.view_kind === kind && p.media_type === 'application/json');
  const m = keys(metadata, 'format odim_version object input_mode source_bytes read_bytes read_requests attribute_bytes chunks_touched decoded_chunk_bytes numeric_bytes_read value_semantics range_semantics geometry limits');
  need(typeof m.format === 'string' && ['h5', 'hdf5'].includes(m.format) && m.odim_version === 'ODIM_H5/V2_4' && ['SCAN', 'PVOL'].includes(m.object) && m.input_mode === 'window'
    && integer(m.source_bytes, 256, 8 * 1024 ** 3) && integer(m.read_bytes, 1, 8 * 1024 ** 2) && integer(m.read_requests, 1, 128) && integer(m.attribute_bytes, 1, 32768)
    && integer(m.chunks_touched, 0, 128) && integer(m.decoded_chunk_bytes, 0, 16777216) && integer(m.numeric_bytes_read, 0, 32768)
    && m.value_semantics === 'raw unsigned storage codes; declared nodata/undetect retained; gain/offset not applied'
    && m.range_semantics === 'ODIM_H5/V2_4 rstart + (gate_index + 0.5) * rscale; slant range metres'
    && m.geometry === 'ray index versus slant range; not georeferenced');
  const limits = keys(m.limits, Object.keys(RADAR_LIMITS).join(' '));
  need(Object.entries(RADAR_LIMITS).every(([key, value]) => limits[key] === value));
  const choices = keys(p.choices, 'sweeps'); need(Array.isArray(choices.sweeps) && choices.sweeps.length >= 1 && choices.sweeps.length <= 32 && (m.object !== 'SCAN' || choices.sweeps.length === 1));
  let fields = 0;
  const sweeps: RadarSweep[] = choices.sweeps.map((entry: unknown, index: number) => {
    const s = keys(entry, 'id elevation nrays nbins rstart_m rscale_m a1gate quantities');
    need(integer(s.id, index + 1, index + 1) && finite(s.elevation, 90) && s.elevation >= -10 && integer(s.nrays, 1, 4096) && integer(s.nbins, 1, 65536) && integer(s.a1gate, 0, s.nrays - 1)
      && finite(s.rstart_m, 1e6) && s.rstart_m >= 0 && finite(s.rscale_m, 1e5) && s.rscale_m > 0 && s.rstart_m + s.nbins * s.rscale_m <= 1e7
      && Array.isArray(s.quantities) && s.quantities.length >= 1 && s.quantities.length <= 16);
    const ids = new Set<string>();
    const quantities = s.quantities.map((entry: unknown): RadarQuantity => {
      const q = keys(entry, 'id dtype chunks gain offset nodata undetect unit');
      need(typeof q.id === 'string' && Object.prototype.hasOwnProperty.call(RADAR_UNITS, q.id) && !ids.has(q.id) && q.unit === RADAR_UNITS[q.id] && isDtype(q.dtype)
        && finite(q.gain, 1e6) && q.gain !== 0 && finite(q.offset, 1e9));
      ids.add(q.id); fields++;
      const maximum = q.dtype === '|u1' ? 255 : 65535;
      need(integer(q.nodata, 0, maximum) && integer(q.undetect, 0, maximum) && q.nodata !== q.undetect && finite(q.offset + q.gain * maximum) && finite(q.offset));
      need(q.chunks === null || Array.isArray(q.chunks) && q.chunks.length === 2 && integer(q.chunks[0], 1, s.nrays) && integer(q.chunks[1], 1, s.nbins) && q.chunks[0] * q.chunks[1] * itemsize(q.dtype) <= 4194304);
      return { id: q.id, dtype: q.dtype, chunks: q.chunks, gain: q.gain, offset: q.offset, nodata: q.nodata, undetect: q.undetect, unit: q.unit };
    });
    return { id: s.id, elevation: s.elevation, nrays: s.nrays, nbins: s.nbins, rstart_m: s.rstart_m, rscale_m: s.rscale_m, a1gate: s.a1gate, quantities };
  });
  need(fields <= 128);
  const selected = validateRadarOptions(kind, p.selected, sweeps);
  if (expected !== undefined) need(equal(selected, validateRadarOptions(kind, expected, sweeps)));
  const result: RadarData = { sweeps, selected, values: null, range: [], rays: [], nodata: 0, undetect: 0, sourceBytes: m.source_bytes, readBytes: m.read_bytes, reads: m.read_requests, format: m.format, metadata: m };
  if (kind === 'tree') {
    need(equal(p.tree, sweeps.map(s => ({ path: `/sweeps/${s.id}`, node_type: 'array', shape: [s.nrays, s.nbins] }))) && m.chunks_touched === 0 && m.decoded_chunk_bytes === 0 && m.numeric_bytes_read === 0);
  } else {
    const s = selected as RadarSelection, sweep = sweeps[s.sweep - 1]!, quantity = sweep.quantities.find(q => q.id === s.quantity)!;
    const a = keys(p.array, 'shape dimensions dtype values'), r = keys(p.radar, 'range_m ray_indices nodata_count undetect_count');
    need(equal(a.shape, [s.ray_count, s.gate_count]) && equal(a.dimensions, ['ray', 'gate']) && a.dtype === quantity.dtype && Array.isArray(a.values) && a.values.length === s.ray_count * s.gate_count && a.values.every((v: unknown) => integer(v, 0, a.dtype === '|u1' ? 255 : 65535)));
    need(Array.isArray(r.range_m) && r.range_m.length === s.gate_count && r.range_m.every((v: unknown, i: number) => finite(v) && v === sweep.rstart_m + (s.gate_start + i + .5) * sweep.rscale_m)
      && equal(r.ray_indices, Array.from({ length: s.ray_count }, (_, i) => s.ray_start + i))
      && integer(r.nodata_count, 0, a.values.length) && r.nodata_count === a.values.filter((v: number) => v === quantity.nodata).length
      && integer(r.undetect_count, 0, a.values.length) && r.undetect_count === a.values.filter((v: number) => v === quantity.undetect).length && m.numeric_bytes_read === a.values.length * itemsize(a.dtype));
    const chunks = quantity.chunks;
    const touched = chunks ? [s.ray_start, s.gate_start].reduce((n, start, i) => n * (Math.floor((start + [s.ray_count, s.gate_count][i]! - 1) / chunks[i]!) - Math.floor(start / chunks[i]!) + 1), 1) : 0;
    need(m.chunks_touched === touched && m.decoded_chunk_bytes === (chunks ? touched * chunks[0] * chunks[1] * itemsize(a.dtype) : 0));
    result.values = a.values; result.range = r.range_m; result.rays = r.ray_indices; result.nodata = r.nodata_count; result.undetect = r.undetect_count;
  }
  return result;
}

/** Reserved-code classification ALWAYS precedes any optional linear conversion. */
export function radarDisplay(data: RadarData, calibrated: boolean) {
  need(typeof calibrated === 'boolean' && data.values !== null);
  const selected = data.selected as RadarSelection, sweep = data.sweeps[selected.sweep - 1]!, q = sweep.quantities.find(q => q.id === selected.quantity)!;
  const values = data.values.map(raw => raw === q.nodata || raw === q.undetect ? null : calibrated ? q.offset + q.gain * raw : raw);
  const flags = data.values.map(raw => raw === q.nodata ? 1 : raw === q.undetect ? 2 : null);
  const rows = <T>(list: T[]) => Array.from({ length: selected.ray_count }, (_, i) => list.slice(i * selected.gate_count, (i + 1) * selected.gate_count));
  return { z: rows(values), flags: rows(flags), raw: rows(data.values), x: data.range, y: data.rays,
    label: calibrated ? `${q.id} [${q.unit}] · 声明 gain/offset` : `${q.id} · 存储码（无物理单位）` };
}
