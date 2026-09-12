import { validateCziSelection, type CziData } from './cziData';
export { validateCziSelection } from './cziData';
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const exact = (v: unknown, keys: string[]): v is Record<string, unknown> => object(v) && Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k));
const integer = (v: unknown, low: number, high: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= low && v <= high;
const fail = (): never => { throw new Error('CZI 区域响应未通过结构、选择或范围预算检查。'); };
export const CZI_WINDOW_WARNINGS = [
  '仅支持未压缩 CZI 1.0 DV、显式单 scene 0 和原始分辨率；不支持压缩、金字塔、分卷或像素掩码附件。',
  '只读取目录及所选 ROI 的实际像素范围；缺失覆盖或重叠子块会拒绝，不补零、不重采样。',
  '不读取采集 XML、标注或附件；显示为像素坐标，不推测物理单位，灰度范围仅针对当前 ROI。',
];
export function parseCziWindowData(raw: unknown): CziData {
  if (!object(raw) || raw.contract_version !== 2 || raw.type !== 'czi-window' || typeof raw.kind !== 'string' || !['tree', 'image'].includes(raw.kind) || typeof raw.sampled !== 'boolean' || JSON.stringify(raw.warnings) !== JSON.stringify(CZI_WINDOW_WARNINGS)) return fail();
  const image = raw.kind === 'image', meta = raw.metadata;
  const common = ['format', 'variant', 'engine', 'scene', 'scene_shape', 'dimension_sizes', 'pixel_types', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'directory_bytes', 'subblocks', 'limits', 'metadata_hidden', 'coverage'];
  if (!exact(meta, common.concat(image ? ['display_range', 'normalization', 'output_shape', 'invalid_pixels'] : [])) || meta.format !== 'CZI' || meta.variant !== 'CZI 1.0 DV uncompressed full-resolution' || meta.engine !== 'dataseek-czi-dv-window-v1' || meta.scene !== 0 || meta.input_mode !== 'window' || meta.metadata_hidden !== true || meta.coverage !== (image ? 'complete and non-overlapping' : 'not decoded')) return fail();
  if (!integer(meta.source_bytes, 544, 8 * 1024 ** 3) || !integer(meta.read_bytes, 1, 32 * 1024 ** 2) || !integer(meta.read_requests, 1, 4096) || !integer(meta.directory_bytes, 160, 4 * 1024 ** 2 + 32) || !integer(meta.subblocks, 1, 16384)) return fail();
  const limits = meta.limits;
  if (!exact(limits, ['max_source_bytes', 'max_read_bytes', 'max_total_bytes', 'max_reads', 'max_directory_bytes', 'max_entries', 'max_roi_size']) || limits.max_source_bytes !== 8 * 1024 ** 3 || limits.max_directory_bytes !== 4 * 1024 ** 2 || limits.max_entries !== 16384 || limits.max_roi_size !== 1024 || !integer(limits.max_read_bytes, 1, 1024 ** 2) || !integer(limits.max_total_bytes, 1, 32 * 1024 ** 2) || !integer(limits.max_reads, 1, 4096) || meta.read_bytes > limits.max_total_bytes || meta.read_requests > limits.max_reads || meta.read_bytes > meta.read_requests * limits.max_read_bytes) return fail();
  if (!Array.isArray(meta.scene_shape) || meta.scene_shape.length !== 2 || !meta.scene_shape.every(v => integer(v, 1, 100000)) || !exact(meta.dimension_sizes, ['C', 'Z', 'T']) || Object.entries(meta.dimension_sizes).some(([k, v]) => !integer(v, 1, k === 'C' ? 64 : 4096))) return fail();
  const shape = meta.scene_shape as number[], dimensions = meta.dimension_sizes as CziData['dimensions'];
  if (!Array.isArray(meta.pixel_types) || meta.pixel_types.length !== dimensions.C || meta.pixel_types.some(v => typeof v !== 'string' || !['Gray8', 'Gray16', 'Gray32Float', 'Bgr24'].includes(v))) return fail();
  const channels = Array.from({ length: dimensions.C }, (_, i) => i);
  if (!exact(raw.choices, ['channels', 'z_count', 'time_count', 'max_roi_size']) || JSON.stringify(raw.choices.channels) !== JSON.stringify(channels) || raw.choices.z_count !== dimensions.Z || raw.choices.time_count !== dimensions.T || raw.choices.max_roi_size !== 1024 || !exact(raw.selected, ['indices', 'roi']) || !Array.isArray(raw.selected.indices) || !Array.isArray(raw.selected.roi)) return fail();
  const indices = raw.selected.indices as number[], roi = raw.selected.roi as number[];
  validateCziSelection(indices, roi, dimensions, shape);
  if (raw.sampled !== (image && (JSON.stringify(roi) !== JSON.stringify([0, 0, shape[1], shape[0]]) || Object.values(dimensions).some(v => v > 1)))) return fail();
  const result: CziData = { shape, dimensions, indices, roi, channels, metadata: meta };
  if (!image) {
    if (raw.media_type !== 'application/json' || 'data_base64' in raw || JSON.stringify(raw.tree) !== JSON.stringify([{ path: '/0', node_type: 'object', attributes: { label: 'Uncompressed CZI directory', children_count: 0 } }])) return fail();
    return result;
  }
  const color = meta.pixel_types[indices[0]!] === 'Bgr24';
  if (JSON.stringify(meta.output_shape) !== JSON.stringify([roi[3], roi[2]]) || !integer(meta.invalid_pixels, 0, roi[2]! * roi[3]! - 1) || !Array.isArray(meta.display_range) || meta.display_range.length !== 2 || !meta.display_range.every(v => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 3.5e38) || meta.display_range[0] > meta.display_range[1] || meta.normalization !== (color ? 'native uint8 BGR to RGB; no scaling' : 'ROI min-max to uint8 grayscale; original data unchanged') || color && (JSON.stringify(meta.display_range) !== '[0,255]' || meta.invalid_pixels !== 0)) return fail();
  if (raw.media_type !== 'image/png' || typeof raw.data_base64 !== 'string' || !raw.data_base64.length || raw.data_base64.length > 7 * 1024 ** 2 || !/^[A-Za-z0-9+/]+={0,2}$/.test(raw.data_base64) || raw.data_base64.length % 4) return fail();
  let bytes: Uint8Array;
  try { const decoded = atob(raw.data_base64); if (btoa(decoded) !== raw.data_base64) return fail(); bytes = Uint8Array.from(decoded, c => c.charCodeAt(0)); } catch { return fail(); }
  if (bytes.length < 33 || bytes.length > 5 * 1024 ** 2 || [...bytes.subarray(0, 16)].join(',') !== '137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82') return fail();
  const header = new DataView(bytes.buffer);
  if (header.getUint32(16) !== roi[2] || header.getUint32(20) !== roi[3] || [...bytes.subarray(24, 29)].join(',') !== `8,${color ? 2 : 6},0,0,0`) return fail();
  result.png = bytes; return result;
}
