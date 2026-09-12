/** Inert NXdata semantics; never execute metadata or dereference dataset paths. */
export const NEXUS_VALUE_SEMANTICS = 'raw signal and coordinates; no scaling, unit conversion or finite fill masking';
export const NEXUS_WARNING = '只显示 NXdata 声明的原始信号、坐标和标准差；不应用缩放、单位转换或有限填充值掩膜。未声明坐标的轴使用索引，非有限信号留空。';
export interface NexusSlice { start: number; stop: number; step: number }
export interface NexusSelection { nxdata: string; selection: NexusSlice[] }
export interface NexusAxis { label: string; unit: string | null; source: 'index' | 'dataset'; dtype: string | null; chunks: number[] | null }
export interface NexusSignal { id: string; label: string; signal: string; shape: number[]; dtype: string; chunks: number[] | null; unit: string | null; axes: NexusAxis[]; errors: { dtype: string; chunks: number[] | null } | null }
export interface NexusValuesAxis { dimension: number; indices: number[]; values: number[]; label: string; unit: string | null; source: 'index' | 'dataset' }
export interface NexusData {
  signals: NexusSignal[]; selected: NexusSelection | null; array: { shape: number[]; dimensions: string[]; values: (number | null)[] } | null;
  axes: NexusValuesAxis[]; errors: (number | null)[] | null; sourceBytes: number; readBytes: number; reads: number; skipped: number; truncated: boolean;
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const keys = (value: Record<string, unknown>, names: string) => { const list = names.split(' '); return Object.keys(value).length === list.length && list.every(k => Object.prototype.hasOwnProperty.call(value, k)); };
const int = (value: unknown, max = 2 ** 31 - 1, min = 0): value is number => Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max;
const label = (value: unknown): value is string => typeof value === 'string' && [...value].length <= 128 && !/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\/i.test(value);
const dtype = (value: unknown): value is string => typeof value === 'string' && /^(?:\|[iu]1|[<>][iu][248]|[<>]f[48])$/.test(value);
const id = (value: unknown): value is string => typeof value === 'string' && /^n-[0-9a-f]{32}$/.test(value);
const product = (values: number[]) => values.reduce((a, b) => a * b, 1);
const nullableLabel = (value: unknown): value is string | null => value === null || label(value);
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function fail(): never { throw new Error('NXdata 结构、坐标、选择或读取预算无效；可改用原始数组预览。'); }
function numeric(value: unknown, type: string, nullable = true): value is number | null {
  if (value === null) return nullable && type[1] === 'f';
  if (typeof value !== 'number' || !Number.isFinite(value)) return false;
  if (type[1] === 'f') return Math.abs(value) <= (type.endsWith('4') ? 3.4028234663852886e38 : Number.MAX_VALUE);
  const bits = Number(type.slice(-1)) * 8;
  return Number.isSafeInteger(value) && value >= (type[1] === 'u' ? 0 : -(2 ** (bits - 1))) && value <= (type[1] === 'u' ? 2 ** bits - 1 : 2 ** (bits - 1) - 1);
}
function chunks(value: unknown, rank: number, type: string): value is number[] | null {
  return value === null || Array.isArray(value) && value.length === rank && value.every(v => int(v, undefined, 1)) && product(value) * Number(type.slice(-1)) <= 4194304;
}
export function validateNexusSelection(kind: 'series' | 'image', input: unknown, signal?: NexusSignal): NexusSelection {
  if (!record(input) || !keys(input, 'nxdata selection') || !id(input.nxdata) || !Array.isArray(input.selection)
    || input.selection.length !== (kind === 'series' ? 1 : 2) || signal && (signal.id !== input.nxdata || signal.shape.length !== input.selection.length)) fail();
  let total = 1;
  const selection = input.selection.map((p, dimension) => {
    if (!record(p) || !keys(p, 'start stop step') || !int(p.start) || !int(p.stop) || !int(p.step, undefined, 1) || p.start >= p.stop
      || signal && p.stop > signal.shape[dimension]!) fail();
    total *= Math.ceil((p.stop - p.start) / p.step);
    return { start: p.start, stop: p.stop, step: p.step };
  });
  if (total > 16384) fail();
  if (signal && total * (signal.errors ? 2 : 1) + selection.reduce((n, p) => n + Math.ceil((p.stop - p.start) / p.step), 0) > 16384) fail();
  return { nxdata: input.nxdata, selection };
}
function cost(chunks: number[] | null, type: string, parts: NexusSlice[]): [number, number] {
  if (!chunks) return [0, 0];
  const touched = product(parts.map((p, axis) => new Set(Array.from({ length: Math.ceil((p.stop - p.start) / p.step) }, (_, i) => Math.floor((p.start + i * p.step) / chunks[axis]!))).size));
  return [touched, touched * product(chunks) * Number(type.slice(-1))];
}
export function parseNexusWindow(kind: 'tree' | 'series' | 'image', payload: Record<string, unknown>, meta: Record<string, unknown>, expected?: NexusSelection): NexusData {
  if (!record(payload) || !keys(payload, `media_type choices selected view_kind ${kind === 'tree' ? 'tree' : 'array axes errors'}`)
    || payload.media_type !== 'application/json' || payload.view_kind !== kind || !record(payload.choices) || !keys(payload.choices, 'signals')
    || !Array.isArray(payload.choices.signals) || payload.choices.signals.length > 32
    || !record(meta) || !keys(meta, 'format container standard input_mode value_semantics source_bytes read_bytes read_requests catalog_truncated skipped_nxdata attribute_bytes chunks_touched decoded_chunk_bytes nonfinite_values output_values')
    || typeof meta.format !== 'string' || !['nxs', 'nx', 'h5', 'hdf5', 'hdf'].includes(meta.format) || meta.container !== 'HDF5' || meta.standard !== 'NXdata'
    || meta.input_mode !== 'window' || meta.value_semantics !== NEXUS_VALUE_SEMANTICS || !int(meta.source_bytes, 8 * 1024 ** 3, 256)
    || !int(meta.read_bytes, 8 * 1024 ** 2, 1) || !int(meta.read_requests, 128, 1) || meta.read_bytes < meta.read_requests
    || typeof meta.catalog_truncated !== 'boolean' || !int(meta.skipped_nxdata, 128) || !int(meta.attribute_bytes, 65536)
    || !int(meta.chunks_touched, 128) || !int(meta.decoded_chunk_bytes, 16 * 1024 ** 2) || !int(meta.nonfinite_values, 16384) || !int(meta.output_values, 16384)) fail();
  const signals: NexusSignal[] = payload.choices.signals.map(v => {
    if (!record(v) || !keys(v, 'id label signal shape dtype chunks unit axes errors') || !id(v.id) || !label(v.label) || !label(v.signal)
      || !dtype(v.dtype) || !nullableLabel(v.unit) || !Array.isArray(v.shape) || !v.shape.length || v.shape.length > 2 || v.shape.some(n => !int(n, undefined, 1))
      || product(v.shape) > Number.MAX_SAFE_INTEGER || !chunks(v.chunks, v.shape.length, v.dtype) || !Array.isArray(v.axes) || v.axes.length !== v.shape.length) fail();
    const axes: NexusAxis[] = v.axes.map((a, dimension) => {
      if (!record(a) || !keys(a, 'label unit source dtype chunks') || !label(a.label) || !nullableLabel(a.unit) || !['dataset', 'index'].includes(a.source as string)) fail();
      if (a.source === 'index') {
        if (a.label !== `index_${dimension}` || a.unit !== null || a.dtype !== null || a.chunks !== null) fail();
      } else if (!dtype(a.dtype) || !chunks(a.chunks, 1, a.dtype)) fail();
      return { label: a.label, unit: a.unit, source: a.source as NexusAxis['source'], dtype: a.dtype as string | null, chunks: a.chunks as number[] | null };
    });
    if (v.errors !== null && (!record(v.errors) || !keys(v.errors, 'dtype chunks') || !dtype(v.errors.dtype) || !chunks(v.errors.chunks, v.shape.length, v.errors.dtype))) fail();
    return { id: v.id, label: v.label, signal: v.signal, shape: [...v.shape], dtype: v.dtype, chunks: v.chunks, unit: v.unit, axes, errors: v.errors as NexusSignal['errors'] };
  });
  if (new Set(signals.map(v => v.id)).size !== signals.length) fail();
  let selected: NexusSelection | null = null, array: NexusData['array'] = null, axes: NexusValuesAxis[] = [], errors: NexusData['errors'] = null;
  if (kind === 'tree') {
    const tree = signals.map(v => ({ path: '/' + v.id, node_type: 'array', attributes: { label: v.label } }));
    if (!record(payload.selected) || Object.keys(payload.selected).length || !same(tree, payload.tree) || meta.chunks_touched !== 0 || meta.decoded_chunk_bytes !== 0 || meta.nonfinite_values !== 0 || meta.output_values !== 0) fail();
  } else {
    selected = validateNexusSelection(kind, payload.selected);
    const signal = signals.find(v => v.id === selected!.nxdata);
    if (!signal) fail();
    selected = validateNexusSelection(kind, selected, signal);
    if (expected && !same(selected, validateNexusSelection(kind, expected, signal))) fail();
    const parts = selected.selection, indices = parts.map(p => Array.from({ length: Math.ceil((p.stop - p.start) / p.step) }, (_, i) => p.start + i * p.step));
    const shape = indices.map(v => v.length);
    if (!record(payload.array) || !keys(payload.array, 'shape dimensions values') || !same(payload.array.shape, shape) || !same(payload.array.dimensions, signal.axes.map(a => a.label))
      || !Array.isArray(payload.array.values) || payload.array.values.length !== product(shape) || payload.array.values.some(v => !numeric(v, signal.dtype))
      || !Array.isArray(payload.axes) || payload.axes.length !== shape.length) fail();
    const values = [...payload.array.values] as (number | null)[];
    array = { shape, dimensions: signal.axes.map(a => a.label), values };
    let [touched, decoded] = cost(signal.chunks, signal.dtype, parts);
    axes = payload.axes.map((a, dimension) => {
      const desc = signal.axes[dimension]!;
      if (!record(a) || !keys(a, 'dimension indices values label unit source') || a.dimension !== dimension || !same(a.indices, indices[dimension])
        || a.label !== desc.label || a.unit !== desc.unit || a.source !== desc.source || !Array.isArray(a.values) || a.values.length !== shape[dimension]) fail();
      if (desc.source === 'index') { if (!same(a.values, a.indices)) fail(); }
      else {
        if (a.values.some(v => !numeric(v, desc.dtype!, false))) fail();
        const coords = a.values as number[];
        if (coords.length > 1 && !coords.slice(1).every((v, i) => coords[i]! < v) && !coords.slice(1).every((v, i) => coords[i]! > v)) fail();
        const [n, bytes] = cost(desc.chunks, desc.dtype!, [parts[dimension]!]); touched += n; decoded += bytes;
      }
      values.push(...a.values);
      return { dimension, indices: indices[dimension]!, values: [...a.values] as number[], label: desc.label, unit: desc.unit, source: desc.source };
    });
    if (signal.errors === null) { if (payload.errors !== null) fail(); }
    else {
      if (!Array.isArray(payload.errors) || payload.errors.length !== product(shape) || payload.errors.some(v => !numeric(v, signal.errors!.dtype) || v !== null && v < 0)) fail();
      errors = [...payload.errors] as (number | null)[]; values.push(...errors);
      const [n, bytes] = cost(signal.errors.chunks, signal.errors.dtype, parts); touched += n; decoded += bytes;
    }
    if (meta.chunks_touched !== touched || meta.decoded_chunk_bytes !== decoded || meta.output_values !== values.length || meta.nonfinite_values !== values.filter(v => v === null).length) fail();
    // The accounting accumulator must never mutate the signal-only array.
    array.values = payload.array.values.slice() as (number | null)[];
  }
  return { signals, selected, array, axes, errors, sourceBytes: meta.source_bytes, readBytes: meta.read_bytes, reads: meta.read_requests, skipped: meta.skipped_nxdata, truncated: meta.catalog_truncated };
}
