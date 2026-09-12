/** Inert range-array schema and exact request binding, independent of renderer. */
export const ARRAY_VALUE_SEMANTICS = 'raw storage values; no CF scale/add_offset or finite fill masking';
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, fields: string[]) => Object.keys(v).length === fields.length && fields.every(k => Object.prototype.hasOwnProperty.call(v, k));
const int = (v: unknown, max = 2 ** 31 - 1, min = 0): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const label = (v: unknown): v is string => typeof v === 'string' && [...v].length <= 128 && !/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\/i.test(v);
const id = (v: unknown): v is string => typeof v === 'string' && /^v-[0-9a-f]{32}$/.test(v);
const product = (values: number[]) => values.reduce((a, b) => a * b, 1);
const reasons = new Set(['', 'link', 'alias', 'depth', 'type', 'shape', 'storage', 'filter', 'chunk']);
function fail(): never { throw new Error('数组窗口的结构、选择或读取预算无效。'); }
export interface Slice { start: number; stop: number; step: number }
export interface ArraySelection { variable: string; selection: (number | Slice)[]; decode: 'raw' }
export interface ArrayVariable { id: string; label: string; shape: number[]; dtype: string; chunks: number[] | null; selectable: boolean; reason: string }
export interface ArrayTreeNode { path: string; node_type: 'array' | 'object'; attributes: { label: string; depth: number; reason: string } }
export interface ArrayWindowData {
  variables: ArrayVariable[]; tree: ArrayTreeNode[]; selected: ArraySelection | null;
  array: { shape: number[]; dimensions: string[]; values: (number | null)[] } | null;
  axes: { dimension: number; indices: number[] }[]; sourceBytes: number; readBytes: number; reads: number;
  attributes: Record<string, string | number>; truncated: boolean;
}
export function arraySelectionsEqual(left: ArraySelection, right: ArraySelection): boolean {
  const parts = (value: ArraySelection) => [value.variable, value.decode, value.selection.map(v => typeof v === 'number' ? v : [v.start, v.stop, v.step])];
  return JSON.stringify(parts(left)) === JSON.stringify(parts(right));
}
export function validateArraySelection(kind: 'series' | 'image', input: unknown, variable?: ArrayVariable): ArraySelection {
  if (!record(input) || !keys(input, ['variable', 'selection', 'decode']) || !id(input.variable) || input.decode !== 'raw'
    || !Array.isArray(input.selection) || !input.selection.length || input.selection.length > 8
    || variable && (variable.id !== input.variable || !variable.selectable || variable.shape.length !== input.selection.length)) fail();
  let slices = 0, elements = 1;
  for (const [axis, value] of input.selection.entries()) {
    if (int(value)) { if (variable && value >= variable.shape[axis]!) fail(); continue; }
    if (!record(value) || !keys(value, ['start', 'stop', 'step']) || !int(value.start) || !int(value.stop) || !int(value.step)
      || value.stop <= value.start || value.step < 1 || variable && value.stop > variable.shape[axis]!) fail();
    slices++; elements *= Math.ceil((value.stop - value.start) / value.step);
  }
  if (slices !== (kind === 'series' ? 1 : 2) || elements > 16384) fail();
  return JSON.parse(JSON.stringify(input)) as ArraySelection;
}
export function parseArrayWindow(kind: 'tree' | 'series' | 'image', payload: Record<string, unknown>, metadata: Record<string, unknown>, expected?: ArraySelection): ArrayWindowData {
  // Runtime envelope already validates standard fields. Only known inert keys
  // are accepted here; no resource URL or free-form file metadata is consumed.
  const expectedKeys = ['media_type', 'choices', 'selected', 'view_kind', ...(kind === 'tree' ? ['tree'] : ['array', 'axes'])];
  if (!keys(payload, expectedKeys) || payload.view_kind !== kind || payload.media_type !== 'application/json'
    || !record(payload.choices) || !keys(payload.choices, ['variables']) || !Array.isArray(payload.choices.variables) || payload.choices.variables.length > 128
    || !keys(metadata, ['format', 'container', 'input_mode', 'value_semantics', 'source_bytes', 'read_bytes', 'read_requests', 'chunks_touched', 'decoded_chunk_bytes', 'catalog_truncated', 'attributes', 'nonfinite_values', 'coordinates', 'limits'])
    || !['h5', 'hdf5', 'hdf', 'nc', 'nc4', 'netcdf', 'mat'].includes(metadata.format as string)
    || metadata.container !== 'HDF5' || metadata.input_mode !== 'window' || metadata.value_semantics !== ARRAY_VALUE_SEMANTICS
    || !int(metadata.source_bytes, 8 * 1024 ** 3, 256) || !int(metadata.read_bytes, 8 * 1024 ** 2, 1) || !int(metadata.read_requests, 128, 1)
    || metadata.read_bytes < metadata.read_requests || !int(metadata.chunks_touched, 128) || !int(metadata.decoded_chunk_bytes, 16 * 1024 ** 2)
    || !int(metadata.nonfinite_values, 16384) || typeof metadata.catalog_truncated !== 'boolean'
    || metadata.coordinates !== 'zero-based dimension indices; not geospatial coordinates'
    || !record(metadata.limits) || !keys(metadata.limits, ['max_elements', 'max_chunk_bytes', 'max_decoded_bytes', 'max_nodes'])
    || metadata.limits.max_elements !== 16384 || metadata.limits.max_chunk_bytes !== 4194304 || metadata.limits.max_decoded_bytes !== 16777216 || metadata.limits.max_nodes !== 128
    || !record(metadata.attributes) || Object.keys(metadata.attributes).some(k => !['units', 'scale_factor', 'add_offset', '_FillValue', 'missing_value'].includes(k))
    || Object.entries(metadata.attributes).some(([k, v]) => !(k === 'units' ? label(v) : finite(v)))) fail();
  const variables: ArrayVariable[] = payload.choices.variables.map(v => {
    if (!record(v) || !keys(v, ['id', 'label', 'shape', 'dtype', 'chunks', 'selectable', 'reason']) || !id(v.id) || !label(v.label)
      || typeof v.dtype !== 'string' || (v.dtype !== 'unsupported' && !/^[<>=|][iufb][1248]$/.test(v.dtype))
      || typeof v.selectable !== 'boolean' || typeof v.reason !== 'string' || !reasons.has(v.reason) || v.selectable !== (v.reason === '')
      || !Array.isArray(v.shape) || v.shape.length > 8 || v.shape.some(n => !int(n))
      || v.chunks !== null && (!Array.isArray(v.chunks) || v.chunks.length !== v.shape.length || v.chunks.some(n => !int(n, undefined, 1)))) fail();
    if (v.selectable && (!v.shape.length || v.shape.some(n => n === 0) || product(v.shape) > Number.MAX_SAFE_INTEGER || v.dtype === 'unsupported'
      || v.chunks !== null && product(v.chunks as number[]) * Number(v.dtype.slice(-1)) > 4194304)) fail();
    return v as unknown as ArrayVariable;
  });
  if (new Set(variables.map(v => v.id)).size !== variables.length) fail();
  let selected: ArraySelection | null = null, array: ArrayWindowData['array'] = null, axes: ArrayWindowData['axes'] = [], tree: ArrayTreeNode[] = [];
  if (kind === 'tree') {
    if (!record(payload.selected) || Object.keys(payload.selected).length || !Array.isArray(payload.tree) || payload.tree.length > 128
      || metadata.chunks_touched !== 0 || metadata.decoded_chunk_bytes !== 0 || metadata.nonfinite_values !== 0 || Object.keys(metadata.attributes).length) fail();
    const seen = new Set<string>(), arrays = new Set<string>();
    tree = payload.tree.map(v => {
      if (!record(v) || !keys(v, ['path', 'node_type', 'attributes']) || typeof v.path !== 'string' || !v.path.startsWith('/') || !id(v.path.slice(1))
        || seen.has(v.path) || !['object', 'array'].includes(v.node_type as string) || !record(v.attributes) || !keys(v.attributes, ['label', 'depth', 'reason'])
        || !label(v.attributes.label) || !int(v.attributes.depth, 8) || typeof v.attributes.reason !== 'string' || !reasons.has(v.attributes.reason)) fail();
      seen.add(v.path);
      if (v.node_type === 'array') { const variable = variables.find(a => a.id === v.path!.toString().slice(1)); if (!variable || variable.label !== v.attributes.label || variable.reason !== v.attributes.reason) fail(); arrays.add(variable.id); }
      return v as unknown as ArrayTreeNode;
    });
    if (arrays.size !== variables.length) fail();
  } else {
    selected = validateArraySelection(kind, payload.selected);
    const variable = variables.find(v => v.id === selected!.variable);
    if (!variable) fail();
    selected = validateArraySelection(kind, selected, variable);
    if (expected && !arraySelectionsEqual(selected, validateArraySelection(kind, expected, variable))) fail();
    const chunkCounts: number[] = [];
    selected.selection.forEach((v, axis) => {
      if (typeof v === 'number') { chunkCounts.push(1); return; }
      const indices = Array.from({ length: Math.ceil((v.stop - v.start) / v.step) }, (_, i) => v.start + i * v.step);
      axes.push({ dimension: axis, indices }); chunkCounts.push(variable.chunks ? new Set(indices.map(i => Math.floor(i / variable.chunks![axis]!))).size : 1);
    });
    const chunks = variable.chunks ? product(chunkCounts) : 0, decoded = variable.chunks ? chunks * product(variable.chunks) * Number(variable.dtype.slice(-1)) : 0;
    if (JSON.stringify(payload.axes) !== JSON.stringify(axes) || metadata.chunks_touched !== chunks || metadata.decoded_chunk_bytes !== decoded
      || !record(payload.array) || !keys(payload.array, ['shape', 'dimensions', 'values'])
      || JSON.stringify(payload.array.shape) !== JSON.stringify(axes.map(a => a.indices.length))
      || JSON.stringify(payload.array.dimensions) !== JSON.stringify(axes.map(a => `index_${a.dimension}`))
      || !Array.isArray(payload.array.values) || payload.array.values.length !== product(axes.map(a => a.indices.length))
      || payload.array.values.some(v => v !== null && (!finite(v) || /[iub]/.test(variable.dtype) && !Number.isSafeInteger(v) || variable.dtype[1] === 'u' && v < 0 || variable.dtype[1] === 'b' && v !== 0 && v !== 1)) || payload.array.values.filter(v => v === null).length !== metadata.nonfinite_values) fail();
    array = payload.array as unknown as NonNullable<ArrayWindowData['array']>;
  }
  return { variables, selected, array, axes, tree, sourceBytes: metadata.source_bytes, readBytes: metadata.read_bytes, reads: metadata.read_requests,
    attributes: metadata.attributes as Record<string, string | number>, truncated: metadata.catalog_truncated };
}
