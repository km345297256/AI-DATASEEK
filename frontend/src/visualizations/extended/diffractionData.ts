/** Dedicated, inert curve schema. Never guesses units or uncertainty columns. */
import type { VisualizationResult } from '../runtime';
export const DIFFRACTION_WARNINGS = ['仅显示声明坐标与原始存储强度；不换算单位、不按计数时间归一化、不扣背景。',
  '误差线仅来自文件显式声明，不推断标准差；不进行结构求解、Rietveld 精修或二维方位积分。',
  '本插件有界读取整个 XML；目录解析全部受限扫描，不是范围读取，选择后才绘制单扫描。'];
const semantics = 'declared axes and stored intensity; no unit conversion, counting-time normalization, background subtraction or uncertainty inference';
const uncertainty = 'declared uncertainty; distribution unspecified';
const limits = { max_scans: 32, max_points: 16384, max_total_points: 65536 };
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).length === names.length && names.every(k => Object.prototype.hasOwnProperty.call(v, k));
const int = (v: unknown, min: number, max: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max;
const number = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 1e150;
function require(v: unknown): asserts v { if (!v) throw new Error('衍射或散射响应未通过格式、单位、扫描或预算校验。'); }
export interface DiffractionScan { id: number; label: string; points: number; x_quantity: string; x_unit: string; y_quantity: string; y_unit: string; x_error: string | null; y_error: string | null; axis_mode: string }
export interface DiffractionTrace { scan: number; x: number[]; y: number[]; x_error: number[] | null; y_error: number[] | null }
export interface DiffractionData { scans: DiffractionScan[]; trace?: DiffractionTrace; sourceBytes: number; format: string; dialect: string; totalPoints: number }
export function validateDiffractionSelection(scan: unknown, total: number) { require(int(scan, 0, total - 1) && total <= 32); return { scan }; }
export function diffractionScansEqual(a: DiffractionScan[], b: DiffractionScan[]) {
  const names: (keyof DiffractionScan)[] = ['id', 'label', 'points', 'x_quantity', 'x_unit', 'y_quantity', 'y_unit', 'x_error', 'y_error', 'axis_mode'];
  return a.length === b.length && a.every((scan, index) => names.every(key => scan[key] === b[index]?.[key]));
}
export function parseDiffractionData(result: VisualizationResult, kind: 'tree' | 'series', options: Record<string, unknown>, fileSize?: number, filename?: string): DiffractionData {
  require(result.contract_version === 2 && result.kind === kind && typeof result.version === 'string' && /^[0-9a-f]{64}$/.test(result.version)
    && typeof result.revision === 'string' && /^[0-9a-f]{64}$/.test(result.revision) && result.sampled === false
    && JSON.stringify(result.warnings) === JSON.stringify(DIFFRACTION_WARNINGS));
  const p = result.payload, m = result.metadata;
  require(record(p) && keys(p, ['media_type', 'view_kind', 'choices', 'selected', kind]) && p.media_type === 'application/json' && p.view_kind === kind);
  require(record(m) && keys(m, ['format', 'dialect', 'input_mode', 'source_bytes', 'scan_count', 'total_points', 'returned_points', 'limits', 'value_semantics'])
    && typeof m.format === 'string' && ['xrdml', 'xml'].includes(m.format) && typeof m.dialect === 'string' && ['xrdml-1d', 'cansas1d-1.0', 'cansas1d-1.1'].includes(m.dialect)
    && (m.format === 'xrdml') === (m.dialect === 'xrdml-1d') && m.input_mode === 'whole' && int(m.source_bytes, 1, 16777216)
    && int(m.scan_count, 1, 32) && int(m.total_points, 2, 65536) && int(m.returned_points, 0, 16384) && m.value_semantics === semantics);
  require(record(m.limits) && keys(m.limits, Object.keys(limits)) && Object.entries(limits).every(([k, v]) => (m.limits as Record<string, unknown>)[k] === v));
  require(fileSize === undefined || int(fileSize, 1, 16777216) && fileSize === m.source_bytes);
  require(filename === undefined || filename.split('.').pop()?.toLowerCase() === m.format);
  require(record(p.choices) && keys(p.choices, ['scans']) && Array.isArray(p.choices.scans) && p.choices.scans.length === m.scan_count);
  const scans = p.choices.scans.map((s, i) => {
    require(record(s) && keys(s, ['id', 'label', 'points', 'x_quantity', 'x_unit', 'y_quantity', 'y_unit', 'x_error', 'y_error', 'axis_mode'])
      && int(s.id, 0, 31) && s.id === i && s.label === `Scan ${i + 1}` && int(s.points, 2, 16384)
      && typeof s.axis_mode === 'string' && ['linear-declared', 'explicit'].includes(s.axis_mode)
      && [null, uncertainty].includes(s.x_error as string | null) && [null, uncertainty].includes(s.y_error as string | null));
    if (m.dialect === 'xrdml-1d') require(typeof s.x_quantity === 'string' && ['2Theta', 'Omega'].includes(s.x_quantity) && s.x_unit === 'deg'
      && typeof s.y_quantity === 'string' && ['counts', 'intensities'].includes(s.y_quantity) && s.y_unit === 'counts' && s.x_error === null && s.y_error === null);
    else require(s.x_quantity === 'Q' && typeof s.x_unit === 'string' && ['1/A', '1/nm', '1/cm', '1/m'].includes(s.x_unit) && s.y_quantity === 'I'
      && typeof s.y_unit === 'string' && ['1/cm', '1/mm', '1/m', 'a.u.', 'none', 'counts', 'cps', 'counts/s', 'counts/sec'].includes(s.y_unit) && s.axis_mode === 'explicit');
    return s as unknown as DiffractionScan;
  });
  require(scans.reduce((total, s) => total + s.points, 0) === m.total_points);
  require(record(p.selected) && record(options));
  let trace: DiffractionTrace | undefined;
  if (kind === 'tree') {
    require(keys(p.selected, []) && keys(options, []) && m.returned_points === 0 && JSON.stringify(p.tree) === JSON.stringify(scans.map(s => ({ path: `/scan-${s.id}`, node_type: 'scan', attributes: { label: s.label } }))));
  } else {
    require(keys(options, ['scan']) && keys(p.selected, ['scan']) && int(options.scan, 0, scans.length - 1) && p.selected.scan === options.scan);
    const scan = scans[options.scan]!;
    require(m.returned_points === scan.points && Array.isArray(p.series) && p.series.length === 1);
    const t = p.series[0]; require(record(t) && keys(t, ['scan', 'x', 'y', 'x_error', 'y_error']) && t.scan === scan.id);
    for (const axis of ['x', 'y'] as const) {
      const values = t[axis]; require(Array.isArray(values) && values.length === scan.points && values.every(number));
      const error = t[`${axis}_error`];
      if (scan[`${axis}_error`] === null) require(error === null);
      else require(Array.isArray(error) && error.length === values.length && error.every(v => number(v) && v >= 0));
    }
    trace = t as unknown as DiffractionTrace;
    require(trace.x.every((x, i, xs) => !i || x > xs[i - 1]!) || trace.x.every((x, i, xs) => !i || x < xs[i - 1]!));
    if (m.dialect.startsWith('cansas')) require(trace.x.every(x => x >= 0));
  }
  require(new TextEncoder().encode(JSON.stringify(result)).length <= 2097152);
  return { scans, trace, sourceBytes: m.source_bytes, format: m.format, dialect: m.dialect, totalPoints: m.total_points };
}
