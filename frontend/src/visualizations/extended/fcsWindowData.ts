/** Exact, inert FCS schema and local histogram of explicitly returned events. */
export const FCS_SEMANTICS = 'stored values; no bit mask, gain, antilog, calibration, timestep conversion, compensation, gating or sampling';
export const FCS_READ_SEMANTICS = 'event-interleaved row window read; only selected channels decoded; not whole-file validation';
export const FCS_LIMITS = { max_text_bytes: 262144, max_channels: 128, max_events: 8192, max_values: 16384, max_decoded_bytes: 131072, max_histogram_bins: 128 };
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).length === names.length && names.every(k => Object.prototype.hasOwnProperty.call(v, k));
const integer = (v: unknown, max = 8589934592, min = 0): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown, min = -Number.MAX_VALUE, max = Number.MAX_VALUE): v is number => typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max;
const safeText = (v: unknown, max = 128): v is string => typeof v === 'string' && [...v].length > 0 && [...v].length <= max && !/[\p{C}<>\\&]/u.test(v) && !/^[\/~]/.test(v) && !v.includes('://') && !v.includes('../');
function fail(): never { throw new Error('FCS 的结构、事件窗口或读取预算不在安全支持范围。'); }
export interface FcsChannel { id: number; name: string; stain: string | null; bits: number; range: number; exponent: number[]; gain: number | null; calibration: { factor: number; unit: string } | null; display: { scale: string; values: number[] } | null }
export interface FcsSelection { view: 'scatter' | 'histogram'; channels: number[]; event_offset: number; event_count: number; bins?: number }
export interface FcsTrace { channel: number; label: string; x: number[]; y: (number | null)[] }
export interface FcsCompensation { declarations: string[]; spillover: { channels: number[]; matrix: number[][] } | null }
export interface FcsData { channels: FcsChannel[]; selected: FcsSelection | null; traces: FcsTrace[]; fcsVersion: string; datatype: string; byteOrder: string; sourceBytes: number; readBytes: number; reads: number; textBytes: number; totalEvents: number; eventBytes: number; scannedBytes: number; decodedBytes: number; nonfinite: number; plottable: number; timestep: number | null; compensation: FcsCompensation }
export function validateFcsSelection(value: unknown, catalog?: FcsData): FcsSelection {
  if (!object(value) || !['scatter', 'histogram'].includes(value.view as string)) fail();
  const count = value.view === 'scatter' ? 2 : 1;
  if (!keys(value, ['view', 'channels', 'event_offset', 'event_count', ...(value.view === 'histogram' ? ['bins'] : [])]) || !Array.isArray(value.channels) || value.channels.length !== count
    || value.channels.some(c => !integer(c, 127)) || new Set(value.channels).size !== count || !integer(value.event_offset, 8589934591) || !integer(value.event_count, 8192, 1)
    || value.view === 'histogram' && !integer(value.bins, 128, 1) || catalog && (value.event_offset + value.event_count > catalog.totalEvents || value.channels.some(c => c >= catalog.channels.length))) fail();
  return { view: value.view as 'scatter' | 'histogram', channels: [...value.channels] as number[], event_offset: value.event_offset, event_count: value.event_count, ...(value.view === 'histogram' ? { bins: value.bins as number } : {}) };
}
function channel(value: unknown, index: number, datatype: string): FcsChannel {
  if (!object(value) || !keys(value, ['id', 'name', 'stain', 'bits', 'range', 'exponent', 'gain', 'calibration', 'display']) || !integer(value.id, 127) || value.id !== index
    || !safeText(value.name) || value.name.includes(',') || value.stain !== null && !safeText(value.stain) || !integer(value.bits, 64, 1)
    || !Array.isArray(value.exponent) || value.exponent.length !== 2 || value.exponent.some(v => !finite(v, 0, 1e12))
    || !(value.exponent.every(v => v === 0) || value.exponent.every(v => v > 0)) || value.gain !== null && !finite(value.gain, 1e-300, 1e12)) fail();
  if (datatype === 'I') { if (![8, 16, 32].includes(value.bits) || !integer(value.range, 2 ** 32, 1) || value.range !== 2 ** value.bits) fail(); }
  else if (value.bits !== (datatype === 'F' ? 32 : 64) || !finite(value.range, 1e-300) || value.exponent.some(v => v !== 0)) fail();
  if (value.exponent.some(v => v !== 0) && value.gain !== null && value.gain !== 1) fail();
  const cal = value.calibration, display = value.display;
  if (cal !== null && (!object(cal) || !keys(cal, ['factor', 'unit']) || !finite(cal.factor, 1e-300, 1e12) || !safeText(cal.unit, 64))) fail();
  if (display !== null && (!object(display) || !keys(display, ['scale', 'values']) || !['Linear', 'Logarithmic'].includes(display.scale as string) || !Array.isArray(display.values) || display.values.length !== 2
    || display.values.some(v => !finite(v, -1e12, 1e12)) || (display.scale === 'Linear' ? display.values[1] <= display.values[0] : display.values.some(v => v <= 0)))) fail();
  return value as unknown as FcsChannel;
}
export function parseFcsWindow(kind: 'tree' | 'series', payload: Record<string, unknown>, metadata: Record<string, unknown>, expected?: FcsSelection): FcsData {
  if (!keys(payload, ['media_type', 'view_kind', 'choices', 'selected', kind]) || payload.view_kind !== kind || payload.media_type !== 'application/json' || !object(payload.choices) || !keys(payload.choices, ['channels']) || !Array.isArray(payload.choices.channels)) fail();
  const m = metadata;
  if (!keys(m, ['format', 'fcs_version', 'datatype', 'byte_order', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'text_bytes', 'total_events', 'channel_count', 'event_bytes', 'scanned_event_bytes', 'decoded_values', 'decoded_bytes', 'nonfinite_values', 'plottable_events', 'timestep', 'compensation', 'value_semantics', 'read_semantics', 'limits'])
    || m.format !== 'fcs' || !['3.0', '3.1'].includes(m.fcs_version as string) || !['F', 'D', 'I'].includes(m.datatype as string) || !['big', 'little'].includes(m.byte_order as string) || m.input_mode !== 'window'
    || !integer(m.source_bytes, 8589934592, 59) || !integer(m.read_bytes, 8388608, 1) || !integer(m.read_requests, 128, 1) || m.read_bytes < m.read_requests
    || !integer(m.text_bytes, 262144, 1) || !integer(m.total_events, 8589934592, 1) || !integer(m.channel_count, 128, 1) || !integer(m.event_bytes, 1024, 1)
    || m.total_events * m.event_bytes + 58 + m.text_bytes > m.source_bytes || !integer(m.scanned_event_bytes, 8388608) || !integer(m.decoded_values, 16384) || !integer(m.decoded_bytes, 131072)
    || !integer(m.nonfinite_values, 16384) || !integer(m.plottable_events, 8192) || m.read_bytes < 58 + m.text_bytes + m.scanned_event_bytes
    || m.timestep !== null && !finite(m.timestep, 1e-300, 1e12) || m.value_semantics !== FCS_SEMANTICS || m.read_semantics !== FCS_READ_SEMANTICS || !object(m.limits) || !keys(m.limits, Object.keys(FCS_LIMITS))
    || Object.entries(FCS_LIMITS).some(([k, v]) => (m.limits as Record<string, unknown>)[k] !== v) || payload.choices.channels.length !== m.channel_count) fail();
  const channels = payload.choices.channels.map((v, i) => channel(v, i, m.datatype as string));
  if (new Set(channels.map(c => c.name)).size !== channels.length || new Set(channels.map(c => c.bits)).size !== 1 || m.event_bytes !== channels.length * channels[0]!.bits / 8) fail();
  const comp = m.compensation, declarations = ['$SPILLOVER', '$COMP', 'SPILL', 'SPILLOVER'];
  if (!object(comp) || !keys(comp, ['declarations', 'spillover']) || !Array.isArray(comp.declarations) || JSON.stringify(comp.declarations) !== JSON.stringify(declarations.filter(k => (comp.declarations as unknown[]).includes(k))) || (comp.spillover !== null) !== comp.declarations.includes('$SPILLOVER')) fail();
  const spill = comp.spillover;
  if (spill !== null && (!object(spill) || !keys(spill, ['channels', 'matrix']) || !Array.isArray(spill.channels) || spill.channels.length < 2 || spill.channels.length > channels.length
    || spill.channels.some(c => !integer(c, channels.length - 1)) || new Set(spill.channels).size !== spill.channels.length || !Array.isArray(spill.matrix) || spill.matrix.length !== spill.channels.length
    || spill.matrix.some(row => !Array.isArray(row) || row.length !== (spill.channels as number[]).length || row.some(v => !finite(v, -1e6, 1e6))))) fail();
  let selected: FcsSelection | null = null, traces: FcsTrace[] = [];
  if (kind === 'tree') {
    if (!object(payload.selected) || Object.keys(payload.selected).length || !Array.isArray(payload.tree) || payload.tree.length !== channels.length || [m.scanned_event_bytes, m.decoded_values, m.decoded_bytes, m.nonfinite_values, m.plottable_events].some(n => n !== 0)) fail();
    for (const [i, node] of payload.tree.entries()) if (!object(node) || !keys(node, ['path', 'node_type', 'attributes']) || node.path !== `/channel-${i}` || node.node_type !== 'channel'
      || !object(node.attributes) || !keys(node.attributes, ['name', 'bits']) || node.attributes.name !== channels[i]!.name || node.attributes.bits !== channels[i]!.bits) fail();
  } else {
    selected = validateFcsSelection(payload.selected);
    if (expected && JSON.stringify(selected) !== JSON.stringify(validateFcsSelection(expected)) || selected.event_offset + selected.event_count > m.total_events || selected.channels.some(c => c >= channels.length)
      || !Array.isArray(payload.series) || payload.series.length !== selected.channels.length) fail();
    let missing = 0;
    traces = payload.series.map((trace, i) => {
      const c = channels[selected!.channels[i]!]!;
      if (!object(trace) || !keys(trace, ['channel', 'label', 'x', 'y']) || trace.channel !== c.id || trace.label !== c.name || !Array.isArray(trace.x) || !Array.isArray(trace.y)
        || trace.x.length !== selected!.event_count || trace.y.length !== selected!.event_count || trace.x.some((v, n) => !integer(v, 8589934591) || v !== selected!.event_offset + n)) fail();
      for (const value of trace.y) {
        if (value === null) { if (m.datatype === 'I') fail(); missing++; }
        else if (m.datatype === 'I' ? !integer(value, c.range - 1) : !finite(value, m.datatype === 'F' ? -3.4028234663852886e38 : -Number.MAX_VALUE, m.datatype === 'F' ? 3.4028234663852886e38 : Number.MAX_VALUE)) fail();
      }
      return trace as unknown as FcsTrace;
    });
    const plottable = traces[0]!.y.reduce<number>((sum, _, i) => sum + Number(traces.every(t => t.y[i] !== null)), 0);
    if (m.scanned_event_bytes !== selected.event_count * m.event_bytes || m.decoded_values !== selected.event_count * selected.channels.length
      || m.decoded_bytes !== selected.event_count * selected.channels.length * channels[0]!.bits / 8 || m.nonfinite_values !== missing || m.plottable_events !== plottable) fail();
  }
  if (new TextEncoder().encode(JSON.stringify({ payload, metadata })).length > 2097152) fail();
  return { channels, selected, traces, fcsVersion: m.fcs_version as string, datatype: m.datatype as string, byteOrder: m.byte_order as string,
    sourceBytes: m.source_bytes, readBytes: m.read_bytes, reads: m.read_requests, textBytes: m.text_bytes, totalEvents: m.total_events, eventBytes: m.event_bytes,
    scannedBytes: m.scanned_event_bytes, decodedBytes: m.decoded_bytes, nonfinite: m.nonfinite_values, plottable: m.plottable_events, timestep: m.timestep as number | null, compensation: comp as unknown as FcsCompensation };
}
/** Strict topology/metadata identity, independent of property serialization order. */
export function fcsCatalogMatches(a: FcsData, b: FcsData): boolean {
  const serialize = (value: unknown): string => Array.isArray(value) ? '[' + value.map(serialize).join(',') + ']' : object(value) ? '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + serialize(value[k])).join(',') + '}' : JSON.stringify(value);
  return ['fcsVersion', 'datatype', 'byteOrder', 'sourceBytes', 'textBytes', 'totalEvents', 'eventBytes', 'timestep', 'channels', 'compensation'].every(k => serialize(a[k as keyof FcsData]) === serialize(b[k as keyof FcsData]));
}
export function fcsHistogram(values: (number | null)[], bins: number) {
  if (!Array.isArray(values) || !values.length || values.length > 8192 || values.some(v => v !== null && !finite(v)) || !integer(bins, 128, 1)) fail();
  const valid = values.filter((v): v is number => v !== null);
  if (!valid.length) return { centers: [], counts: [], edges: [], missing: values.length, constant: false };
  const low = Math.min(...valid), high = Math.max(...valid);
  if (low === high) return { centers: [low], counts: [valid.length], edges: [low, high], missing: values.length - valid.length, constant: true };
  const edges = Array.from({ length: bins + 1 }, (_, i) => low * (1 - i / bins) + high * (i / bins));
  if (edges.some((v, i) => !finite(v) || i > 0 && v <= edges[i - 1]!)) throw new Error('原值间隔不足以构造指定箱数，请减少直方图箱数。');
  const counts = Array<number>(bins).fill(0);
  for (const v of valid) { let index = 0; while (index < bins - 1 && v >= edges[index + 1]!) index++; counts[index] = counts[index]! + 1; }
  return { centers: counts.map((_, i) => edges[i]! / 2 + edges[i + 1]! / 2), counts, edges, missing: values.length - valid.length, constant: false };
}
