const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const fail = (): never => { throw new Error('CZI 预览响应未通过结构或预算检查。'); };
const exact = (v: unknown, fields: string[]): v is Record<string, unknown> => object(v) && Object.keys(v).length === fields.length && Object.keys(v).every(k => fields.includes(k));
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
export interface CziData { shape: number[]; dimensions: { C: number; Z: number; T: number }; indices: number[]; roi: number[]; channels: number[]; metadata: Record<string, unknown>; png?: Uint8Array; }
export function validateCziSelection(indices: number[], roi: number[], dims: CziData['dimensions'], shape: number[]) {
  if (indices.length !== 3 || indices.some((v, i) => !Number.isSafeInteger(v) || v < 0 || v >= [dims.C, dims.Z, dims.T][i]!) || roi.length !== 4 || roi.some(v => !Number.isSafeInteger(v)) || roi[0]! < 0 || roi[1]! < 0 || roi[2]! < 1 || roi[3]! < 1 || roi[2]! > 1024 || roi[3]! > 1024 || roi[0]! + roi[2]! > shape[1]! || roi[1]! + roi[3]! > shape[0]!) throw new Error('CZI 通道、Z/T 或 ROI 越界；窗口宽高不得超过 1024。');
}
export function parseCziData(raw: unknown): CziData {
  if (!object(raw) || typeof raw.kind !== 'string' || !['tree', 'image'].includes(raw.kind) || !object(raw.metadata)) return fail();
  const meta = raw.metadata, image = raw.kind === 'image';
  const common = ['format', 'engine', 'scene', 'scene_shape', 'dimension_sizes', 'pixel_types', 'input_mode', 'input_bytes', 'subblocks', 'decoded_block_budget', 'selection_mode'];
  if (!exact(meta, common.concat(image ? ['display_range', 'normalization', 'output_shape', 'roi_origin', 'invalid_pixels'] : [])) || meta.format !== 'CZI' || meta.engine !== 'pylibCZIrw 6.1.0' || meta.scene !== 0 || meta.input_mode !== 'whole' || !Number.isSafeInteger(meta.input_bytes) || Number(meta.input_bytes) < 1 || Number(meta.input_bytes) > 64 * 1024 * 1024 || !Number.isSafeInteger(meta.subblocks) || Number(meta.subblocks) < 1 || Number(meta.subblocks) > 4096 || meta.decoded_block_budget !== 16 * 1024 * 1024 || meta.selection_mode !== 'explicit C/Z/T and pixel ROI') return fail();
  if (!Array.isArray(meta.scene_shape) || meta.scene_shape.length !== 2 || meta.scene_shape.some(v => !Number.isSafeInteger(v) || v < 1 || v > 100000) || !exact(meta.dimension_sizes, ['C', 'Z', 'T'])) return fail();
  const dimensions = meta.dimension_sizes as CziData['dimensions'], shape = meta.scene_shape as number[];
  if (Object.entries(dimensions).some(([k, v]) => !Number.isSafeInteger(v) || v < 1 || v > (k === 'C' ? 64 : 4096)) || !Array.isArray(meta.pixel_types) || meta.pixel_types.length !== dimensions.C || meta.pixel_types.some(v => typeof v !== 'string' || !['Gray8', 'Gray16', 'Gray32Float', 'Bgr24'].includes(v))) return fail();
  const channels = Array.from({ length: dimensions.C }, (_, i) => i);
  if (!exact(raw.choices, ['channels', 'z_count', 'time_count', 'max_roi_size']) || JSON.stringify(raw.choices.channels) !== JSON.stringify(channels) || raw.choices.z_count !== dimensions.Z || raw.choices.time_count !== dimensions.T || raw.choices.max_roi_size !== 1024 || !exact(raw.selected, ['indices', 'roi']) || !Array.isArray(raw.selected.indices) || !Array.isArray(raw.selected.roi)) return fail();
  const indices = raw.selected.indices as number[], roi = raw.selected.roi as number[];
  validateCziSelection(indices, roi, dimensions, shape);
  const result: CziData = { shape, dimensions, indices, roi, channels, metadata: meta };
  if (!image) {
    if (raw.media_type !== 'application/json' || 'data_base64' in raw || JSON.stringify(raw.tree) !== JSON.stringify([{ path: '/0', node_type: 'object', attributes: { label: 'Single-scene CZI', children_count: 0 } }])) return fail();
    return result;
  }
  if (meta.roi_origin !== 'scene top-left; x right, y down' || typeof meta.normalization !== 'string' || !['ROI min-max to uint8 grayscale; original data unchanged', 'native uint8 BGR to RGB; no scaling'].includes(meta.normalization) || !Number.isSafeInteger(meta.invalid_pixels) || Number(meta.invalid_pixels) < 0 || Number(meta.invalid_pixels) > roi[2]! * roi[3]! || !Array.isArray(meta.display_range) || meta.display_range.length !== 2 || !meta.display_range.every(finite) || meta.display_range[0] > meta.display_range[1] || JSON.stringify(meta.output_shape) !== JSON.stringify([roi[3], roi[2]])) return fail();
  if (raw.media_type !== 'image/png' || typeof raw.data_base64 !== 'string' || !raw.data_base64.length || raw.data_base64.length > 7 * 1024 * 1024 || !/^[A-Za-z0-9+/]+={0,2}$/.test(raw.data_base64) || raw.data_base64.length % 4) return fail();
  let bytes: Uint8Array;
  try { bytes = Uint8Array.from(atob(raw.data_base64), c => c.charCodeAt(0)); } catch { return fail(); }
  if (bytes.length < 33 || bytes.length > 5 * 1024 * 1024 || [...bytes.subarray(0, 16)].join(',') !== '137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82') return fail();
  const header = new DataView(bytes.buffer);
  if (header.getUint32(16) !== roi[2] || header.getUint32(20) !== roi[3] || bytes[24] !== 8 || ![2, 6].includes(bytes[25]!)) return fail();
  result.png = bytes; return result;
}
