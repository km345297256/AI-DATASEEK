const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const fail = (): never => { throw new Error('OME-Zarr 响应未通过结构、类型或窗口边界校验。'); };
const exact = (v: unknown, keys: string[]): v is Record<string, unknown> => obj(v) && Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k));
const integer = (v: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const units = new Set('angstrom attometer centimeter decimeter exameter femtometer foot gigameter hectometer inch kilometer megameter meter micrometer mile millimeter nanometer parsec petameter picometer terameter yard yoctometer yottameter zeptometer zettameter attosecond centisecond day decisecond exasecond femtosecond gigasecond hectosecond hour kilosecond megasecond microsecond millisecond minute nanosecond petasecond picosecond second terasecond yoctosecond yottasecond zeptosecond zettasecond'.split(' '));
export interface OmeAxis { name: 't' | 'c' | 'z' | 'y' | 'x'; type: 'time' | 'channel' | 'space'; unit: string | null }
export interface OmeLevel { level: number; shape: number[]; chunks: number[]; dtype: string; scale: number[]; translation: number[]; compression: string; fill_value: number | string | null }
export interface OmeSelection { level: number; indices: number[]; roi: number[] }
export interface OmeData { axes: OmeAxis[]; levels: OmeLevel[]; selected: OmeSelection; values?: (number | null)[]; metadata: Record<string, unknown> }

export function validateOmeSelection(selection: OmeSelection, data: Pick<OmeData, 'axes' | 'levels'>): OmeSelection {
  if (!exact(selection, ['level', 'indices', 'roi']) || !integer(selection.level, 0, data.levels.length - 1) || !Array.isArray(selection.indices) || !Array.isArray(selection.roi)) return fail();
  const level = data.levels[selection.level]!;
  if (selection.indices.length !== data.axes.length - 2 || selection.indices.some((v, i) => !integer(v, 0, level.shape[i]! - 1)) || selection.roi.length !== 4 || selection.roi.some(v => !integer(v, 0, 1e9)) || selection.roi[2]! < 1 || selection.roi[3]! < 1 || selection.roi[2]! > 128 || selection.roi[3]! > 128 || selection.roi[0]! + selection.roi[2]! > level.shape.slice(-1)[0]! || selection.roi[1]! + selection.roi[3]! > level.shape.slice(-2)[0]!) return fail();
  return { level: selection.level, indices: [...selection.indices], roi: [...selection.roi] };
}

export function parseOmeZarr(raw: unknown, expectedKind: 'tree' | 'image', requested?: OmeSelection): OmeData {
  if (!obj(raw) || raw.kind !== expectedKind || raw.type !== 'ome-zarr' || raw.contract_version !== 2 || raw.media_type !== 'application/json' || raw.sampled !== (expectedKind === 'image') || !exact(raw.choices, ['axes', 'levels', 'max_roi_size']) || raw.choices.max_roi_size !== 128 || !Array.isArray(raw.choices.axes) || !Array.isArray(raw.choices.levels)) return fail();
  const axes = raw.choices.axes as OmeAxis[], levels = raw.choices.levels as OmeLevel[], canonical = ['t', 'c', 'z', 'y', 'x'];
  if (axes.length < 2 || axes.length > 5 || !same(axes.slice(-2).map(a => a?.name), ['y', 'x']) || new Set(axes.map(a => a?.name)).size !== axes.length) return fail();
  axes.forEach((axis, i) => {
    if (!exact(axis, ['name', 'type', 'unit']) || !canonical.includes(axis.name) || axis.type !== (axis.name === 't' ? 'time' : axis.name === 'c' ? 'channel' : 'space') || (axis.unit !== null && (typeof axis.unit !== 'string' || !units.has(axis.unit))) || (axis.name === 'c' && axis.unit !== null) || (i && canonical.indexOf(axes[i - 1]!.name) >= canonical.indexOf(axis.name))) return fail();
    const time = axis.unit?.endsWith('second') || ['minute', 'hour', 'day'].includes(axis.unit ?? '');
    if (axis.unit !== null && ((axis.type === 'time' && !time) || (axis.type === 'space' && time))) return fail();
  });
  if (!levels.length || levels.length > 16) return fail();
  levels.forEach((level, index) => {
    if (!exact(level, ['level', 'shape', 'chunks', 'dtype', 'scale', 'translation', 'compression', 'fill_value']) || level.level !== index || !integer(level.level) || typeof level.dtype !== 'string' || !/^(?:\|[iu]1|[<>](?:[iu][248]|f[248]))$/.test(level.dtype) || !['none', 'zlib', 'gzip', 'blosc'].includes(level.compression)) return fail();
    for (const key of ['shape', 'chunks', 'scale', 'translation'] as const) {
      const values = level[key];
      if (!Array.isArray(values) || values.length !== axes.length || values.some(v => (key === 'shape' || key === 'chunks') ? !integer(v, 1, 1e9) : !finite(v) || (key === 'scale' && v <= 0))) return fail();
    }
    if (level.chunks.reduce((a, b) => a * b, 1) * Number(level.dtype.slice(-1)[0]) > 4 * 1024 ** 2 || level.shape.reduce((a, b) => a * BigInt(b), 1n) > 9223372036854775807n || (level.fill_value !== null && !finite(level.fill_value) && (typeof level.fill_value !== 'string' || !['NaN', 'Infinity', '-Infinity'].includes(level.fill_value)))) return fail();
    if (index) axes.forEach((axis, i) => {
      const previous = levels[index - 1]!;
      if (axis.type === 'space' ? level.shape[i]! > previous.shape[i]! || level.scale[i]! < previous.scale[i]! : level.shape[i] !== levels[0]!.shape[i] || level.scale[i] !== levels[0]!.scale[i]) fail();
    });
  });
  const selected = validateOmeSelection(raw.selected as OmeSelection, { axes, levels });
  if (requested && !same(selected, requested)) return fail();
  const meta = raw.metadata;
  if (!exact(meta, ['format', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'resource_count', 'loaded_chunks', 'decoded_chunk_bytes', 'missing_chunks', 'value_semantics', 'invalid_values', 'value_range', 'scope']) || meta.format !== 'OME-NGFF 0.4 / Zarr v2' || meta.input_mode !== 'window' || meta.missing_chunks !== 'rejected' || meta.value_semantics !== 'raw stored values; no fill-value masking or intensity normalization' || meta.scope !== 'registered local dataset image') return fail();
  for (const [key, min, max] of [['source_bytes', 1, 8 * 1024 ** 3], ['read_bytes', 1, 32 * 1024 ** 2], ['read_requests', 1, 256], ['resource_count', 2, 2048], ['loaded_chunks', 0, 64], ['decoded_chunk_bytes', 0, 32 * 1024 ** 2], ['invalid_values', 0, 16384]] as const) if (!integer(meta[key], min, max)) return fail();
  const result: OmeData = { axes, levels, selected, metadata: meta };
  if (expectedKind === 'tree') {
    const expected = levels.map((level, i) => ({ path: `/${i}`, node_type: 'array', attributes: { label: `Level ${i}`, shape: level.shape, dtype: level.dtype } }));
    if (!same(raw.tree, expected) || 'array' in raw || meta.loaded_chunks !== 0 || meta.decoded_chunk_bytes !== 0 || meta.invalid_values !== 0 || meta.value_range !== null || !same(selected, { level: 0, indices: Array(axes.length - 2).fill(0), roi: [0, 0, Math.min(128, levels[0]!.shape.slice(-1)[0]!), Math.min(128, levels[0]!.shape.slice(-2)[0]!)] })) return fail();
  } else {
    const { roi } = selected, level = levels[selected.level]!;
    if ('tree' in raw || !exact(raw.array, ['shape', 'values', 'dtype']) || !same(raw.array.shape, [roi[3], roi[2]]) || raw.array.dtype !== level.dtype || !Array.isArray(raw.array.values) || raw.array.values.length !== roi[2]! * roi[3]!) return fail();
    const values = raw.array.values as (number | null)[], numeric = values.filter((v): v is number => v !== null), type = level.dtype[1]!, bytes = Number(level.dtype.slice(-1)[0]);
    if (numeric.some(v => !finite(v))) return fail();
    if ('iu'.includes(type)) {
      const min = type === 'i' ? -(2 ** (bytes * 8 - 1)) : 0, max = 2 ** (bytes * 8 - (type === 'i' ? 1 : 0)) - 1;
      if (numeric.length !== values.length || numeric.some(v => !integer(v, min, max))) return fail();
    } else if (numeric.some(v => Math.abs(v) > ({ 2: 65504, 4: 3.4028234663852886e38, 8: Number.MAX_VALUE }[bytes] ?? 0))) return fail();
    const cy = level.chunks.slice(-2)[0]!, cx = level.chunks.slice(-1)[0]!;
    const count = (Math.floor((roi[1]! + roi[3]! - 1) / cy) - Math.floor(roi[1]! / cy) + 1) * (Math.floor((roi[0]! + roi[2]! - 1) / cx) - Math.floor(roi[0]! / cx) + 1);
    if (meta.loaded_chunks !== count || meta.decoded_chunk_bytes !== count * level.chunks.reduce((a, b) => a * b, 1) * bytes || meta.invalid_values !== values.length - numeric.length || !same(meta.value_range, numeric.length ? [Math.min(...numeric), Math.max(...numeric)] : null)) return fail();
    result.values = values;
  }
  return result;
}

export function omePixelGray(value: number | null, range: number[] | null): number | null {
  if (value === null || !range) return null;
  const scale = Math.max(1, Math.abs(range[0]!), Math.abs(range[1]!));
  const span = range[1]! / scale - range[0]! / scale;
  return span ? Math.round(Math.max(0, Math.min(1, (value / scale - range[0]! / scale) / span)) * 255) : 0;
}
