export const ENVI_WARNING = '仅显示原始存储值；不应用增益、反射率缩放、坏波段修复或地图投影。缺失值不插值。';
const ERROR = 'ENVI 响应或窗口选择无效，请重新打开预览。';
export type EnviKind = 'tree' | 'image' | 'series';
export type EnviOptions = Record<string, number>;
export interface EnviCube { samples: number; lines: number; bands: number; data_type: number; byte_order: number; interleave: string; header_offset: number; wavelengths: number[] | null; wavelength_unit: string | null; ignore_value: number | null }
export interface EnviData { cube: EnviCube; sourceBytes: number; headerBytes: number; readBytes: number; reads: number; nulls: number; selected: EnviOptions; array: { shape: number[]; dimensions: string[]; dtype: string; values: (number | null)[] } | null; axes: { label: string; unit: string | null; values: number[] }[] }
const types: Record<number, [string, number, number | null, number | null]> = { 1: ['uint8', 1, 0, 255], 2: ['int16', 2, -32768, 32767], 3: ['int32', 4, -2147483648, 2147483647], 4: ['float32', 4, null, null], 5: ['float64', 8, null, null], 12: ['uint16', 2, 0, 65535], 13: ['uint32', 4, 0, 4294967295] };
function assert(ok: unknown): asserts ok { if (!ok) throw new Error(ERROR); }
const object = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v);
function keys(v: unknown, names: string[]): asserts v is Record<string, any> { assert(object(v) && Object.keys(v).sort().join('|') === [...names].sort().join('|')); }
const int = (v: unknown, lo: number, hi: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= lo && v <= hi;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 1e300;
const same = (a: unknown, b: unknown): boolean => Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((v, i) => same(v, b[i])) : object(a) && object(b) ? Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k])) : a === b;
export function validateEnviOptions(kind: EnviKind, value: unknown, cube?: EnviCube): EnviOptions {
  const names = kind === 'tree' ? [] : kind === 'image' ? ['band', 'x', 'y', 'width', 'height'] : ['x', 'y', 'band_start', 'band_count'];
  assert(['tree', 'image', 'series'].includes(kind)); keys(value, names);
  if (kind !== 'tree') {
    assert(int(value.x, 0, 2**31-1) && int(value.y, 0, 2**31-1));
    if (cube) assert(value.x < cube.samples && value.y < cube.lines);
    if (kind === 'image') { assert(int(value.band, 0, 2047) && int(value.width, 1, 128) && int(value.height, 1, 128)); if (cube) assert(value.band < cube.bands && value.x+value.width <= cube.samples && value.y+value.height <= cube.lines); }
    else { assert(int(value.band_start, 0, 2047) && int(value.band_count, 1, 128)); if (cube) assert(value.band_start+value.band_count <= cube.bands); }
  }
  return Object.fromEntries(names.map(k => [k, value[k]]));
}
export function parseEnviWindow(kind: EnviKind, payload: unknown, metadata: unknown, expected?: EnviOptions): EnviData {
  keys(payload, ['view_kind', 'media_type', 'choices', 'selected', ...(kind === 'tree' ? ['tree'] : ['array', 'axes'])]);
  assert(payload.view_kind === kind && payload.media_type === 'application/json');
  keys(metadata, ['format', 'input_mode', 'source_bytes', 'header_bytes', 'data_bytes', 'read_bytes', 'read_requests', 'null_values', 'limits', 'no_calibration', 'no_georeferencing']);
  const m = metadata;
  assert(m.format === 'hdr' && m.input_mode === 'window' && int(m.source_bytes, 2, 8*1024**3) && int(m.header_bytes, 1, 65536) && int(m.data_bytes, 1, 8*1024**3) && m.source_bytes === m.header_bytes+m.data_bytes
    && int(m.read_bytes, m.header_bytes, 8*1024**2) && int(m.read_requests, 1, 256) && int(m.null_values, 0, 16384) && m.no_calibration === true && m.no_georeferencing === true
    && same(m.limits, { max_values: 16384, max_bands: 2048, max_spectrum_bands: 128 }));
  keys(payload.choices, ['cube']); const c = payload.choices.cube;
  keys(c, ['samples', 'lines', 'bands', 'data_type', 'byte_order', 'interleave', 'header_offset', 'wavelengths', 'wavelength_unit', 'ignore_value']);
  assert(int(c.samples, 1, 2**31-1) && int(c.lines, 1, 2**31-1) && int(c.bands, 1, 2048) && int(c.data_type, 1, 13) && types[c.data_type]
    && int(c.byte_order, 0, 1) && ['bsq', 'bil', 'bip'].includes(c.interleave) && int(c.header_offset, 0, 1024**2));
  const type = types[c.data_type]!;
  assert(c.header_offset+c.samples*c.lines*c.bands*type[1] === m.data_bytes);
  assert(c.wavelengths === null || (Array.isArray(c.wavelengths) && c.wavelengths.length === c.bands && c.wavelengths.every((v: unknown) => finite(v) && v > 0 && v <= 1e12)));
  assert(c.wavelength_unit === null || (typeof c.wavelength_unit === 'string' && /^[A-Za-z0-9 µμ^(). -]{1,64}$/.test(c.wavelength_unit) && c.wavelengths !== null));
  assert(c.ignore_value === null || finite(c.ignore_value));
  if (c.ignore_value !== null) assert(type[2] !== null ? int(c.ignore_value, type[2], type[3]!) : c.data_type !== 4 || Math.fround(c.ignore_value) === c.ignore_value);
  const cube = c as unknown as EnviCube, selected = validateEnviOptions(kind, payload.selected, cube);
  if (expected) assert(same(selected, validateEnviOptions(kind, expected, cube)));
  let array: EnviData['array'] = null, axes: EnviData['axes'] = [];
  if (kind === 'tree') assert(same(payload.tree, [{ path: '/cube', node_type: 'array', shape: [c.bands, c.lines, c.samples], dtype: type[0] }]) && m.null_values === 0 && m.read_requests === 1 && m.read_bytes === m.header_bytes);
  else {
    const shape = kind === 'image' ? [selected.height!, selected.width!] : [selected.band_count!], dimensions = kind === 'image' ? ['row', 'column'] : ['band'];
    keys(payload.array, ['shape', 'dimensions', 'dtype', 'values']); const a = payload.array;
    assert(same(a.shape, shape) && same(a.dimensions, dimensions) && a.dtype === type[0] && Array.isArray(a.values) && a.values.length === shape.reduce((a, b) => a*b, 1) && a.values.length <= 16384);
    assert(a.values.every((v: unknown) => v === null || (finite(v) && (type[2] === null || int(v, type[2], type[3]!)))) && a.values.filter((v: unknown) => v === null).length === m.null_values);
    const range = (start: number, count: number) => Array.from({ length: count }, (_, i) => start+i);
    const expectedAxes = kind === 'image' ? [{ label: '行索引', unit: null, values: range(selected.y!, selected.height!) }, { label: '列索引', unit: null, values: range(selected.x!, selected.width!) }] : [{ label: c.wavelengths ? '光谱坐标' : '波段索引', unit: c.wavelengths ? c.wavelength_unit : null, values: range(selected.band_start!, selected.band_count!).map(i => c.wavelengths ? c.wavelengths[i] : i) }];
    assert(same(payload.axes, expectedAxes)); array = a as EnviData['array']; axes = expectedAxes;
  }
  return { cube, sourceBytes: m.source_bytes, headerBytes: m.header_bytes, readBytes: m.read_bytes, reads: m.read_requests, nulls: m.null_values, selected, array, axes };
}
