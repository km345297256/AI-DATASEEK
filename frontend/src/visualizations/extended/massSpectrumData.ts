export const MASS_WARNING = '仅显示显式选择的单条质谱；保留原始 m/z、强度和声明单位，不归一化、扣背景、寻峰、插值或静默抽样。';
const reasons = ['supported', 'unsupported-representation', 'unsupported-encoding', 'ambiguous-metadata', 'point-budget', 'empty-spectrum'];
function check(ok: unknown): asserts ok { if (!ok) throw new Error('质谱响应类型、科学语义或资源预算不一致。'); }
function record(value: unknown): Record<string, unknown> { check(value !== null && typeof value === 'object' && !Array.isArray(value)); return value as Record<string, unknown>; }
function keys(value: Record<string, unknown>, expected: string[]) { check(Object.keys(value).length === expected.length && expected.every(k => Object.prototype.hasOwnProperty.call(value, k))); }
const integer = (v: unknown, low: number, high: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= low && v <= high;
const numeric = (v: unknown, low = -1e308, high = 1e308): v is number => typeof v === 'number' && Number.isFinite(v) && v >= low && v <= high;
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export function massSpectrumIndex(value: unknown) { check(typeof value === 'string' && /^s-[0-9]{6}$/.test(value)); const n = Number(value.slice(2)); check(n < 1024); return n; }
export interface MassSpectrumDescriptor { id: string; index: number; points: number; representation: 'centroid' | 'profile' | 'unknown'; mz_unit: string | null; intensity_unit: string | null; retention_time: number | null; time_unit: 's' | 'min' | null; precursor_mz: number | null; ms_level: number | null; selectable: boolean; reason: string }
export interface MassSpectrumData { spectra: MassSpectrumDescriptor[]; values?: number[]; total: number; offset: number; nextOffset: number | null; sourceBytes: number; decodedBytes: number; selected: Record<string, unknown> }
function descriptor(raw: unknown) {
  const v = record(raw);
  keys(v, ['id', 'index', 'points', 'representation', 'mz_unit', 'intensity_unit', 'retention_time', 'time_unit', 'precursor_mz', 'ms_level', 'selectable', 'reason']);
  check(integer(v.index, 0, 1023) && v.index === massSpectrumIndex(v.id) && integer(v.points, 0, 2 ** 31 - 1));
  check(typeof v.representation === 'string' && ['centroid', 'profile', 'unknown'].includes(v.representation));
  check(v.mz_unit === null || v.mz_unit === 'MS:1000040');
  check(v.intensity_unit === null || typeof v.intensity_unit === 'string' && /^[A-Z]{2,8}:[0-9]{1,12}$/.test(v.intensity_unit));
  check(v.retention_time === null || numeric(v.retention_time, 0, 1e12));
  check(v.time_unit === null || v.time_unit === 's' || v.time_unit === 'min');
  check((v.retention_time === null) === (v.time_unit === null));
  check(v.precursor_mz === null || numeric(v.precursor_mz, 0)); check(v.ms_level === null || integer(v.ms_level, 1, 100));
  check(typeof v.selectable === 'boolean' && typeof v.reason === 'string' && reasons.includes(v.reason));
  check(v.selectable === (v.reason === 'supported'));
  if (v.selectable) check(v.points >= 1 && v.points <= 16384 && v.representation !== 'unknown');
  if (v.reason === 'empty-spectrum') check(v.points === 0);
  if (v.reason === 'point-budget') check(v.points > 16384);
  return v as unknown as MassSpectrumDescriptor;
}
export function parseMassSpectrum(kind: 'tree' | 'series', raw: unknown, metadata: unknown, expected: Record<string, unknown> = {}): MassSpectrumData {
  check(kind === 'tree' || kind === 'series');
  const payload = record(raw), meta = record(metadata), selected = record(payload.selected);
  keys(payload, ['view_kind', 'media_type', 'choices', 'selected', kind === 'tree' ? 'tree' : 'array']);
  check(payload.view_kind === kind && payload.media_type === 'application/json');
  keys(meta, ['format', 'dialect', 'input_mode', 'source_bytes', 'total_spectra', 'offset', 'next_offset', 'decoded_bytes', 'output_points']);
  check(meta.format === 'mgf' || meta.format === 'mzml');
  check(meta.dialect === (meta.format === 'mgf' ? 'mgf-ions-2column' : 'mzml-1.1-inline-float') && meta.input_mode === 'whole');
  check(integer(meta.source_bytes, 1, 16 * 1024 ** 2) && integer(meta.total_spectra, 1, 1024));
  check(integer(meta.offset, 0, meta.total_spectra - 1) && integer(meta.decoded_bytes, 0, 16384 * 16) && integer(meta.output_points, 0, 16384));
  const choices = record(payload.choices); keys(choices, ['spectra']); check(Array.isArray(choices.spectra));
  check(choices.spectra.length <= 64); const spectra = choices.spectra.map(descriptor);
  let values: number[] | undefined;
  if (kind === 'tree') {
    keys(selected, ['offset']); keys(expected, Object.prototype.hasOwnProperty.call(expected, 'offset') ? ['offset'] : []);
    check(integer(selected.offset, 0, 1023) && selected.offset % 64 === 0 && selected.offset === (expected.offset ?? 0));
    check(meta.offset === selected.offset && spectra.length === Math.min(64, meta.total_spectra - meta.offset));
    check(spectra.every((v, i) => v.index === (meta.offset as number) + i));
    check(meta.next_offset === (meta.offset + 64 < meta.total_spectra ? meta.offset + 64 : null));
    check(meta.decoded_bytes === 0 && meta.output_points === 0);
    check(equal(payload.tree, spectra.map(v => ({ path: '/' + v.id, node_type: 'array', attributes: { label: 'Spectrum ' + (v.index + 1) } }))));
  } else {
    keys(selected, ['spectrum']); keys(expected, ['spectrum']); check(selected.spectrum === expected.spectrum);
    const index = massSpectrumIndex(selected.spectrum);
    check(spectra.length === 1 && spectra[0]!.selectable && spectra[0]!.index === index && meta.offset === index && meta.next_offset === null);
    const item = spectra[0]!, array = record(payload.array); keys(array, ['shape', 'dimensions', 'values']);
    check(equal(array.shape, [item.points, 2]) && equal(array.dimensions, ['m/z', 'intensity']));
    check(meta.output_points === item.points && (meta.format === 'mgf' ? meta.decoded_bytes === 0 : [8, 12, 16].some(n => item.points * n === meta.decoded_bytes)));
    check(Array.isArray(array.values) && array.values.length === item.points * 2 && array.values.every((v, i) => numeric(v, i % 2 ? -1e308 : 0)));
    values = array.values as number[];
    if (item.representation === 'profile') check(values.every((v, i) => i % 2 || i < 2 || v > values![i - 2]!));
  }
  return { spectra, values, total: meta.total_spectra, offset: meta.offset, nextOffset: meta.next_offset as number | null, sourceBytes: meta.source_bytes, decodedBytes: meta.decoded_bytes, selected };
}
export function massSpectrumTrace(data: MassSpectrumData) {
  check(data.values && data.spectra.length === 1);
  const item = data.spectra[0]!, x: (number | null)[] = [], y: (number | null)[] = [];
  for (let i = 0; i < data.values.length; i += 2) {
    const mz = data.values[i]!, intensity = data.values[i + 1]!;
    if (item.representation === 'centroid') { x.push(mz, mz, null); y.push(0, intensity, null); }
    else { x.push(mz); y.push(intensity); }
  }
  return { type: 'scatter' as const, mode: 'lines' as const, x, y, connectgaps: false, line: { shape: 'linear' as const, simplify: false, width: 1.5 }, hovertemplate: 'm/z=%{x}<br>强度=%{y}<extra></extra>' };
}
const units: Record<string, string> = { 'MS:1000131': 'detector counts', 'MS:1000132': '% of base peak', 'MS:1000814': 'counts/s', 'MS:1000905': '% of base peak ×100' };
export function massIntensityLabel(item: MassSpectrumDescriptor) { return item.intensity_unit ? (units[item.intensity_unit] ?? item.intensity_unit) : '单位未声明'; }
