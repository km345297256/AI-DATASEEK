/** Inert seismic schema and explicit record/sample selection. */
export const SEISMIC_SEMANTICS = 'raw stored samples; SAC SCALE and instrument response not applied; no filtering, resampling, merging or gap filling';
export const SEISMIC_TIME_AXIS = "seconds relative to the selected record's first sample; UTC display rounded to microseconds";
export const SEISMIC_LIMITS = { max_samples: 16384, max_page_records: 16, max_record_bytes: 1048576, max_decoded_samples: 65535, max_decoded_bytes: 524280 };
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).length === names.length && names.every(k => Object.prototype.hasOwnProperty.call(v, k));
const integer = (v: unknown, max = 2 ** 31 - 1, min = 0): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown, min = -1e12, max = 1e12): v is number => typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max;
const encodings = ['INT16', 'INT32', 'FLOAT32', 'FLOAT64', 'STEIM1', 'STEIM2'];
function fail(): never { throw new Error('地震波形的结构、记录窗口或读取预算无效。'); }
export interface SeismicSelection { record: number; start_sample: number; sample_count: number }
export interface SeismicPage { record_offset: number; record_limit: number }
export interface SeismicRecord {
  id: number; label: string; variant: string; encoding: string; byte_order: string; samples: number; sample_interval: number;
  unit: string; unit_source: string; declared_scale: number | null; start_time: string | null; begin_seconds: number;
  time_adjustment_seconds: number; timing_quality: number | null; quality_flags: number; relation: string; gap_seconds: number | null;
}
export interface SeismicData {
  records: SeismicRecord[]; selected: SeismicSelection | null; x: number[] | null; y: (number | null)[] | null;
  format: string; sourceBytes: number; readBytes: number; reads: number; recordBytes: number; recordSlots: number;
  catalogOffset: number; catalogCount: number; catalogComplete: boolean; decodedSamples: number; decodedBytes: number;
}
export function validateSeismicSelection(input: unknown, r?: SeismicRecord): SeismicSelection {
  if (!record(input) || !keys(input, ['record', 'start_sample', 'sample_count']) || !integer(input.record, 33554431) || !integer(input.start_sample)
    || !integer(input.sample_count, 16384, 1) || r && (input.record !== r.id || input.start_sample + input.sample_count > r.samples)) fail();
  return { record: input.record, start_sample: input.start_sample, sample_count: input.sample_count };
}
export function validateSeismicPage(input: unknown): SeismicPage | Record<string, never> {
  if (!record(input)) fail();
  if (!Object.keys(input).length) return {};
  if (!keys(input, ['record_offset', 'record_limit']) || !integer(input.record_offset, 33554431) || !integer(input.record_limit, 16, 1)) fail();
  return { record_offset: input.record_offset, record_limit: input.record_limit };
}
export function seismicRecordsEqual(a: SeismicRecord, b: SeismicRecord): boolean {
  return Object.keys(a).filter(k => !['relation', 'gap_seconds'].includes(k)).every(k => a[k as keyof SeismicRecord] === b[k as keyof SeismicRecord]);
}
function date(value: unknown): boolean {
  if (value === null) return true;
  if (typeof value !== 'string' || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$/.test(value)) return false;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) && parsed.getUTCFullYear() >= 1900 && parsed.getUTCFullYear() <= 2200 && parsed.toISOString() === value.slice(0, 23) + 'Z';
}
export function parseSeismicWindow(kind: 'tree' | 'series', payload: Record<string, unknown>, metadata: Record<string, unknown>, expected?: SeismicSelection | SeismicPage | Record<string, never>): SeismicData {
  if (!keys(payload, ['media_type', 'view_kind', 'choices', 'selected', kind]) || payload.media_type !== 'application/json' || payload.view_kind !== kind
    || !record(payload.choices) || !keys(payload.choices, ['records']) || !Array.isArray(payload.choices.records)) fail();
  const m = metadata;
  if (!keys(m, ['format', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'record_bytes', 'record_slots', 'catalog_offset', 'catalog_count', 'catalog_complete', 'decoded_samples', 'decoded_bytes', 'nonfinite_values', 'value_semantics', 'time_axis', 'limits'])
    || typeof m.format !== 'string' || !['mseed', 'miniseed', 'sac'].includes(m.format) || m.input_mode !== 'window'
    || !integer(m.source_bytes, 8 * 1024 ** 3, 256) || !integer(m.read_bytes, 8388608, 1) || !integer(m.read_requests, 128, 1) || m.read_bytes < m.read_requests
    || !integer(m.record_bytes, 8 * 1024 ** 3, 256) || !integer(m.record_slots, 33554432, 1) || m.record_bytes * m.record_slots !== m.source_bytes
    || !integer(m.catalog_offset, m.record_slots - 1) || !integer(m.catalog_count, Math.min(16, m.record_slots - m.catalog_offset), 1) || payload.choices.records.length !== m.catalog_count
    || typeof m.catalog_complete !== 'boolean' || m.catalog_complete !== (m.catalog_offset === 0 && m.catalog_count === m.record_slots)
    || !integer(m.decoded_samples, 65535) || !integer(m.decoded_bytes, 524280) || !integer(m.nonfinite_values, 16384)
    || m.value_semantics !== SEISMIC_SEMANTICS || m.time_axis !== SEISMIC_TIME_AXIS || !record(m.limits) || !keys(m.limits, Object.keys(SEISMIC_LIMITS))
    || Object.entries(SEISMIC_LIMITS).some(([k, v]) => (m.limits as Record<string, unknown>)[k] !== v)
    || (m.format === 'sac' ? m.record_slots !== 1 : m.record_bytes > 1048576 || (m.record_bytes & (m.record_bytes - 1)) !== 0)) fail();
  const records: SeismicRecord[] = payload.choices.records.map((r, i) => {
    if (!record(r) || !keys(r, ['id', 'label', 'variant', 'encoding', 'byte_order', 'samples', 'sample_interval', 'unit', 'unit_source', 'declared_scale', 'start_time', 'begin_seconds', 'time_adjustment_seconds', 'timing_quality', 'quality_flags', 'relation', 'gap_seconds'])
      || !integer(r.id, Number(m.record_slots) - 1) || r.id !== Number(m.catalog_offset) + i || typeof r.label !== 'string' || !/^[A-Za-z0-9_.?-]{1,40}$/.test(r.label)
      || !['MiniSEED2', 'SAC6', 'SAC7'].includes(r.variant as string) || !encodings.includes(r.encoding as string) || !['big', 'little'].includes(r.byte_order as string)
      || !integer(r.samples, m.format === 'sac' ? 2 ** 31 - 1 : 65535, 1) || !finite(r.sample_interval, 1e-7, 1e6)
      || !['unknown', 'nm', 'nm/s', 'nm/s^2', 'V'].includes(r.unit as string) || !['not supplied', 'SAC IDEP'].includes(r.unit_source as string)
      || r.declared_scale !== null && !finite(r.declared_scale) || !date(r.start_time) || !finite(r.begin_seconds) || !finite(r.time_adjustment_seconds, -214749, 214749)
      || r.timing_quality !== null && !integer(r.timing_quality, 100) || !integer(r.quality_flags, 255)
      || !['uncompared', 'continuous', 'gap', 'overlap', 'rate-change'].includes(r.relation as string) || r.gap_seconds !== null && !finite(r.gap_seconds)
      || (r.relation === 'uncompared') !== (r.gap_seconds === null)) fail();
    if (m.format === 'sac') {
      if (!['SAC6', 'SAC7'].includes(r.variant as string) || r.encoding !== 'FLOAT32' || r.time_adjustment_seconds !== 0 || r.timing_quality !== null || r.quality_flags !== 0 || r.unit_source !== 'SAC IDEP'
        || 632 + r.samples * 4 + (r.variant === 'SAC7' ? 176 : 0) !== m.source_bytes) fail();
    } else if (r.variant !== 'MiniSEED2' || r.unit !== 'unknown' || r.unit_source !== 'not supplied' || r.declared_scale !== null || r.start_time === null || r.begin_seconds !== 0) fail();
    return r as unknown as SeismicRecord;
  });
  let selected: SeismicSelection | null = null, x: number[] | null = null, y: (number | null)[] | null = null;
  if (kind === 'tree') {
    const page = validateSeismicPage(payload.selected);
    if (expected && JSON.stringify(page) !== JSON.stringify(validateSeismicPage(expected)) || m.catalog_offset !== (page.record_offset ?? 0) || m.catalog_count !== Math.min(page.record_limit ?? 16, m.record_slots - m.catalog_offset)
      || [m.decoded_samples, m.decoded_bytes, m.nonfinite_values].some(n => n !== 0) || !Array.isArray(payload.tree) || payload.tree.length !== records.length) fail();
    for (const [i, node] of payload.tree.entries()) {
      if (!record(node) || !keys(node, ['path', 'node_type', 'attributes']) || node.path !== `/record-${records[i]!.id}` || node.node_type !== 'record'
        || !record(node.attributes) || !keys(node.attributes, ['label', 'encoding']) || node.attributes.label !== records[i]!.label || node.attributes.encoding !== records[i]!.encoding) fail();
    }
  } else {
    if (records.length !== 1 || !Array.isArray(payload.series) || payload.series.length !== 1) fail();
    const r = records[0]!; selected = validateSeismicSelection(payload.selected, r);
    if (expected && JSON.stringify(selected) !== JSON.stringify(validateSeismicSelection(expected, r)) || r.relation !== 'uncompared' || r.gap_seconds !== null) fail();
    const trace = payload.series[0];
    if (!record(trace) || !keys(trace, ['record', 'label', 'unit', 'x', 'y']) || trace.record !== r.id || trace.label !== r.label || trace.unit !== r.unit
      || !Array.isArray(trace.x) || !Array.isArray(trace.y) || trace.x.length !== selected.sample_count || trace.y.length !== selected.sample_count) fail();
    if (trace.x.some((v, i) => !finite(v, 0, 2 ** 31 * 1e6) || v !== (selected!.start_sample + i) * r.sample_interval)) fail();
    if (trace.y.some(v => v === null ? !r.encoding.startsWith('FLOAT') : r.encoding.startsWith('FLOAT') ? !finite(v, -Number.MAX_VALUE, Number.MAX_VALUE) : !integer(v, r.encoding === 'INT16' ? 32767 : 2147483647, r.encoding === 'INT16' ? -32768 : -2147483648))) fail();
    const decoded = r.encoding.startsWith('STEIM') ? r.samples : selected.sample_count, width = r.encoding === 'FLOAT64' ? 8 : r.encoding === 'INT16' ? 2 : 4;
    if (m.decoded_samples !== decoded || m.decoded_bytes !== decoded * width || m.nonfinite_values !== trace.y.filter(v => v === null).length) fail();
    x = trace.x as number[]; y = trace.y as (number | null)[];
  }
  if (new TextEncoder().encode(JSON.stringify({ payload, metadata })).length > 2097152) fail();
  return { records, selected, x, y, format: m.format, sourceBytes: m.source_bytes, readBytes: m.read_bytes, reads: m.read_requests,
    recordBytes: m.record_bytes, recordSlots: m.record_slots, catalogOffset: m.catalog_offset, catalogCount: m.catalog_count, catalogComplete: m.catalog_complete, decodedSamples: m.decoded_samples, decodedBytes: m.decoded_bytes };
}
