/** Strict inert GRIB2 window payloads; no metadata-driven code or URL access. */
export const GRIB_WARNING = '按单条 GRIB2 消息解码原始气象场及缺失位图；不合并变量、层或时刻，不额外换算单位、插值、排序经度或请求在线底图。';
export const GRIB_SEMANTICS = 'ecCodes decoded values; explicit bitmap; longitude [0,360); no extra unit conversion or interpolation';
export interface GribMessage { id: string; byte_length: number; label: string; short_name: string; unit: string; discipline: number; parameter_category: number; parameter_number: number; reference_time: string; forecast_time: number; forecast_unit: number; surface_type: number; surface_scale: number | null; surface_value: number | null; shape: number[]; first: number[]; step: number[]; scanning_mode: number; earth_shape: number; bitmap: boolean; missing_count: number; packing_bits: number }
export interface GribSelection { message: string; roi: number[] }
export interface GribData { messages: GribMessage[]; selected: { offset: number } | GribSelection; array: { shape: number[]; dimensions: string[]; values: (number | null)[] } | null; axes: { name: string; unit: string; values: number[] }[]; sourceBytes: number; readBytes: number; reads: number; pageOffset: number; nextOffset: number | null; skipped: number; decoded: number; missing: number }
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: unknown, names: string): v is Record<string, unknown> => record(v) && Object.keys(v).length === names.split(' ').length && names.split(' ').every(k => Object.prototype.hasOwnProperty.call(v, k));
const int = (v: unknown, max = 8 * 1024 ** 3, min = 0): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown, max = Number.MAX_VALUE): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= max;
const label = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && [...v].length <= 128 && !/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\/i.test(v);
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const near = (a: number, b: number) => Math.abs(a - b) <= 1e-6;
const modulo = (n: number) => ((n % 360) + 360) % 360;
function fail(): never { throw new Error('GRIB2 消息、坐标、编码或窗口预算无效。'); }
export function gribMessageOffset(value: unknown): number {
  if (typeof value !== 'string' || !/^g-[0-9a-f]{16}$/.test(value)) fail();
  const offset = Number.parseInt(value.slice(2), 16);
  if (!int(offset, 8 * 1024 ** 3 - 1)) fail();
  return offset;
}
export function validateGribSelection(input: unknown, message?: GribMessage): GribSelection {
  if (!keys(input, 'message roi')) fail();
  gribMessageOffset(input.message);
  if (!Array.isArray(input.roi) || input.roi.length !== 4 || input.roi.some(n => !int(n, 16384)) || input.roi[2] < 1 || input.roi[3] < 1 || input.roi[2] * input.roi[3] > 16384) fail();
  if (message && (message.id !== input.message || input.roi[0] + input.roi[2] > message.shape[1]! || input.roi[1] + input.roi[3] > message.shape[0]!)) fail();
  return { message: input.message as string, roi: [...input.roi] };
}
export function parseGribWindow(kind: 'tree' | 'image', payload: unknown, metadata: unknown, expected: unknown = {}): GribData {
  if (!keys(payload, 'media_type choices selected view_kind ' + (kind === 'tree' ? 'tree' : 'array axes')) || payload.media_type !== 'application/json' || payload.view_kind !== kind
    || !keys(payload.choices, 'messages') || !Array.isArray(payload.choices.messages)
    || !keys(metadata, 'format edition grid_type packing decoder input_mode value_semantics source_bytes read_bytes read_requests page_offset next_offset scanned_messages skipped_messages decoded_points output_values missing_values')) fail();
  const meta = metadata;
  if (typeof meta.format !== 'string' || !['grib', 'grb', 'grib2', 'grb2'].includes(meta.format) || meta.edition !== 2 || meta.grid_type !== 'regular_ll' || meta.packing !== 'grid_simple'
    || meta.decoder !== 'ecCodes' || meta.input_mode !== 'window' || meta.value_semantics !== GRIB_SEMANTICS || !int(meta.source_bytes, 8 * 1024 ** 3, 20)
    || !int(meta.read_bytes, 8 * 1024 ** 2, 1) || !int(meta.read_requests, 128, 1) || meta.read_bytes < meta.read_requests || !int(meta.page_offset, meta.source_bytes - 1)
    || meta.next_offset !== null && !int(meta.next_offset, meta.source_bytes - 1, meta.page_offset + 1) || !int(meta.scanned_messages, 8, 1)
    || !int(meta.skipped_messages, meta.scanned_messages) || !int(meta.decoded_points, 16384) || !int(meta.output_values, 16384) || !int(meta.missing_values, 16384)
    || payload.choices.messages.length !== meta.scanned_messages - meta.skipped_messages) fail();
  const sourceBytes = meta.source_bytes;
  let end = meta.page_offset;
  const messages = payload.choices.messages.map(value => {
    if (!keys(value, 'id byte_length label short_name unit discipline parameter_category parameter_number reference_time forecast_time forecast_unit surface_type surface_scale surface_value shape first step scanning_mode earth_shape bitmap missing_count packing_bits')) fail();
    const offset = gribMessageOffset(value.id);
    if (offset < end || !int(value.byte_length, 1048576, 179) || offset + value.byte_length > sourceBytes) fail();
    if (![value.label, value.short_name, value.unit].every(label) || ![value.discipline, value.parameter_category, value.parameter_number, value.forecast_unit, value.surface_type].every(n => int(n, 255))
      || !int(value.forecast_time, 2 ** 31 - 1) || ![0, 1, 2, 10, 11, 12, 13].includes(value.forecast_unit as number)
      || value.surface_scale !== null && !int(value.surface_scale, 127, -127) || value.surface_value !== null && !int(value.surface_value, 2 ** 32 - 2)
      || typeof value.reference_time !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value.reference_time)
      || !Number.isFinite(Date.parse(value.reference_time)) || new Date(value.reference_time).toISOString().replace('.000Z', 'Z') !== value.reference_time
      || !Array.isArray(value.shape) || value.shape.length !== 2 || value.shape.some(n => !int(n, 16384, 1)) || value.shape[0] * value.shape[1] > 16384
      || !Array.isArray(value.first) || value.first.length !== 2 || value.first.some(n => !finite(n, 360)) || value.first[0] < 0 || value.first[0] >= 360 || Math.abs(value.first[1]) > 90
      || !Array.isArray(value.step) || value.step.length !== 2 || value.step.some(n => !finite(n, 360) || n === 0)
      || !int(value.scanning_mode, 255) || (value.scanning_mode & 31) !== 0 || !int(value.earth_shape, 9) || typeof value.bitmap !== 'boolean'
      || !int(value.missing_count, value.shape[0] * value.shape[1]) || !value.bitmap && value.missing_count !== 0 || !int(value.packing_bits, 32)) fail();
    const v = value as unknown as GribMessage;
    if ((v.step[0]! < 0) !== !!(v.scanning_mode & 128) || (v.step[1]! > 0) !== !!(v.scanning_mode & 64)
      || Math.abs(v.first[1]! + (v.shape[0]! - 1) * v.step[1]!) > 90 + 1e-6 || Math.abs((v.shape[1]! - 1) * v.step[0]!) >= 360) fail();
    end = offset + v.byte_length;
    return { ...v, shape: [...v.shape], first: [...v.first], step: [...v.step] };
  });
  if (meta.next_offset !== null && end > meta.next_offset) fail();
  let selected: GribData['selected'], array: GribData['array'] = null, axes: GribData['axes'] = [];
  if (kind === 'tree') {
    if (!record(expected) || Object.keys(expected).some(k => k !== 'offset') || !int(expected.offset ?? 0, 8 * 1024 ** 3 - 1)
      || !keys(payload.selected, 'offset') || !int(payload.selected.offset) || payload.selected.offset !== (expected.offset ?? 0)
      || meta.page_offset !== payload.selected.offset || meta.decoded_points || meta.output_values || meta.missing_values
      || !same(payload.tree, messages.map(v => ({ path: '/' + v.id, node_type: 'array', attributes: { label: v.short_name } })))) fail();
    selected = { offset: payload.selected.offset };
  } else {
    selected = validateGribSelection(payload.selected);
    if (!same(selected, validateGribSelection(expected)) || messages.length !== 1 || messages[0]!.id !== selected.message || meta.page_offset !== gribMessageOffset(selected.message)) fail();
    const message = messages[0]!, [x, y, width, height] = selected.roi;
    const end = meta.page_offset + message.byte_length;
    if (meta.next_offset !== (end < meta.source_bytes ? end : null)) fail();
    validateGribSelection(selected, message);
    if (!keys(payload.array, 'shape dimensions values') || !same(payload.array.shape, [height, width]) || !same(payload.array.dimensions, ['latitude', 'longitude'])
      || !Array.isArray(payload.array.values) || payload.array.values.length !== width! * height! || payload.array.values.some(v => v !== null && !finite(v))
      || !Array.isArray(payload.axes) || payload.axes.length !== 2) fail();
    array = { shape: [height!, width!], dimensions: ['latitude', 'longitude'], values: [...payload.array.values] };
    axes = payload.axes.map((a, dimension) => {
      const slot = dimension === 0 ? 1 : 0, count = dimension === 0 ? height! : width!, start = dimension === 0 ? y! : x!;
      const name = dimension === 0 ? 'latitude' : 'longitude', unit = dimension === 0 ? 'degrees_north' : 'degrees_east';
      if (!keys(a, 'name unit values') || a.name !== name || a.unit !== unit || !Array.isArray(a.values) || a.values.length !== count) fail();
      const values = a.values as number[];
      if (values.some((n, index) => !finite(n, 360) || !near(n, slot === 0 ? modulo(message.first[slot]! + (start + index) * message.step[slot]!) : message.first[slot]! + (start + index) * message.step[slot]!))
        || values.some((n, i) => i > 0 && !near(n - values[i - 1]!, message.step[slot]!))) fail();
      return { name, unit, values: [...values] };
    });
    if (meta.scanned_messages !== 1 || meta.skipped_messages || meta.decoded_points !== message.shape[0]! * message.shape[1]!
      || meta.output_values !== width! * height! || meta.missing_values !== array.values.filter(v => v === null).length || meta.missing_values > message.missing_count) fail();
  }
  return { messages, selected, array, axes, sourceBytes: meta.source_bytes, readBytes: meta.read_bytes, reads: meta.read_requests,
    pageOffset: meta.page_offset, nextOffset: meta.next_offset as number | null, skipped: meta.skipped_messages, decoded: meta.decoded_points, missing: meta.missing_values };
}
