const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const integer = (value: unknown, max: number): value is number => Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= max;
const label = (value: unknown, max: number): value is string => typeof value === 'string' && [...value].length <= max && !/[<>\x00-\x1f\x7f]/.test(value);
function reject(): never { throw new Error('仪器数据响应的类型、单位或读取窗口无效。'); }
export interface NumericTrace { name: string; unit: string; x: number[]; y: number[] }
export interface SignalChannel { id: number; label: string; unit: string; sample_rate: number; selectable: boolean }
export interface SignalWindow {
  channels: SignalChannel[]; selected: number[]; start: number; duration: number;
  totalDuration: number; sourceBytes: number; readBytes: number; reads: number; format: string; traces: NumericTrace[];
}
export function parseSignalWindow(payload: Record<string, unknown>, metadata: Record<string, unknown>): SignalWindow {
  const choices = payload.choices, selected = payload.selected, rows = payload.series;
  if (!record(choices) || !Array.isArray(choices.channels) || !choices.channels.length || choices.channels.length > 256
    || !record(selected) || !Array.isArray(selected.channels) || !selected.channels.length || selected.channels.length > 8
    || !finite(selected.start_seconds) || selected.start_seconds < 0 || !finite(selected.duration_seconds) || selected.duration_seconds <= 0 || selected.duration_seconds > 60
    || !finite(metadata.total_duration_seconds) || metadata.total_duration_seconds <= 0
    || selected.start_seconds + selected.duration_seconds > metadata.total_duration_seconds + 1e-9
    || !integer(metadata.source_bytes, 8 * 1024 ** 3) || !metadata.source_bytes || !integer(metadata.read_bytes, 8 * 1024 ** 2)
    || !integer(metadata.read_requests, 128) || !metadata.read_requests || !['edf', 'bdf'].includes(metadata.format as string)
    || metadata.identity_fields_hidden !== true || metadata.annotations_hidden !== true || metadata.no_resampling !== true) reject();
  const channels: SignalChannel[] = choices.channels.map(value => {
    if (!record(value) || !integer(value.id, 255) || !label(value.label, 16) || !label(value.unit, 8)
      || !finite(value.sample_rate) || value.sample_rate <= 0 || typeof value.selectable !== 'boolean'
      || !['signal', 'annotation', 'status'].includes(value.channel_type as string) || value.selectable !== (value.channel_type === 'signal')) reject();
    return { id: value.id, label: value.label, unit: value.unit, sample_rate: value.sample_rate, selectable: value.selectable };
  });
  if (new Set(channels.map(value => value.id)).size !== channels.length || new Set(selected.channels).size !== selected.channels.length
    || selected.channels.some(id => !integer(id, 255) || !channels.some(value => value.id === id && value.selectable))
    || !Array.isArray(rows) || rows.length !== selected.channels.length) reject();
  const start = selected.start_seconds, duration = selected.duration_seconds;
  const selectedChannels = selected.channels as number[];
  let samples = 0;
  const traces = rows.map((value, index): NumericTrace => {
    const channel = channels.find(item => item.id === selectedChannels[index]);
    if (!record(value) || !channel || value.channel !== channel.id || value.label !== channel.label || value.unit !== channel.unit || value.sample_rate !== channel.sample_rate
      || !Array.isArray(value.x) || !Array.isArray(value.y) || !value.x.length || value.x.length !== value.y.length
      || value.x.some(x => !finite(x)) || value.y.some(y => !finite(y))) reject();
    const x = value.x as number[], y = value.y as number[];
    samples += x.length;
    const tolerance = Math.max(1e-9, 1 / channel.sample_rate * 1e-6);
    if (samples > 16384 || x[0]! < start - tolerance || x[x.length - 1]! >= start + duration + tolerance
      || x.some((time, i) => i > 0 && (time <= x[i - 1]! || Math.abs(time - x[i - 1]! - 1 / channel.sample_rate) > tolerance))) reject();
    return { name: `#${channel.id + 1} ${channel.label || '通道'}`, unit: channel.unit || '未声明单位', x, y };
  });
  return { channels, selected: selected.channels as number[], start, duration, traces, totalDuration: metadata.total_duration_seconds,
    sourceBytes: metadata.source_bytes, readBytes: metadata.read_bytes, reads: metadata.read_requests, format: metadata.format as string };
}
export interface McaSpectrum { trace: NumericTrace; xLabel: string; channels: number; calibrated: boolean; coefficients: number[] | null; liveTime: number | null; realTime: number | null }
export function parseMcaSpectrum(payload: Record<string, unknown>, metadata: Record<string, unknown>): McaSpectrum {
  const array = payload.array, calibrated = metadata.calibration_applied, axis = metadata.axis;
  if (!record(array) || !Array.isArray(array.shape) || array.shape.length !== 2 || !integer(array.shape[0], 8192) || !array.shape[0] || array.shape[1] !== 2
    || !Array.isArray(array.values) || array.values.length !== array.shape[0] * 2
    || metadata.format !== 'mca' || metadata.dialect !== 'amptek-pmca-ascii' || metadata.channels !== array.shape[0]
    || metadata.raw_counts !== true || metadata.fitting !== false || metadata.header_text_hidden !== true
    || typeof calibrated !== 'boolean' || axis !== (calibrated ? 'energy' : 'channel')) reject();
  const unit = metadata.energy_unit, coefficients = metadata.calibration_coefficients;
  if (calibrated ? !['eV', 'keV', 'MeV'].includes(unit as string) || !Array.isArray(coefficients) || coefficients.length !== 3 || !coefficients.every(finite) || coefficients[1] <= 0 || coefficients[2] !== 0 : unit !== null || coefficients !== null) reject();
  const values = array.values, x: number[] = [], y: number[] = [];
  for (let i = 0; i < values.length; i += 2) {
    const coordinate = values[i], count = values[i + 1];
    if (!finite(coordinate) || !integer(count, Number.MAX_SAFE_INTEGER) || (i > 0 && coordinate <= x[x.length - 1]!)
      || (!calibrated && coordinate !== i / 2)) reject();
    x.push(coordinate); y.push(count);
  }
  for (const time of [metadata.live_time_seconds, metadata.real_time_seconds]) if (time !== null && (!finite(time) || time < 0)) reject();
  return { trace: { name: '原始计数', unit: 'counts', x, y }, xLabel: calibrated ? `能量 (${unit})` : '通道索引',
    channels: array.shape[0], calibrated, coefficients: coefficients as number[] | null,
    liveTime: metadata.live_time_seconds as number | null, realTime: metadata.real_time_seconds as number | null };
}
