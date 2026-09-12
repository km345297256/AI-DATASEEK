const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const exact = (v: unknown, fields: string[]): v is Record<string, unknown> => record(v) && Object.keys(v).length === fields.length && Object.keys(v).every(key => fields.includes(key));
const integer = (v: unknown, min: number, max: number): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const fail = (): never => { throw new Error('仪器图像响应未通过格式、选择或资源预算检查。'); };
const versionPattern = /^[a-f0-9]{64}$/;
const dtypeBytes: Record<string, number> = { int8: 1, uint8: 1, int16: 2, uint16: 2, int32: 4, uint32: 4, float32: 4, float64: 8 };
const notices = ['仅显示显式选择的帧和像素窗口；不执行校准、拟合或重采样。', '灰度图按当前 ROI 有限原值范围映射；非有限值显示为黑色，原始数据不变。'];
const tree = [{ path: '/0', node_type: 'object', attributes: { label: 'Instrument image dataset', children_count: 0 } }];
export interface InstrumentImageData {
  kind: 'tree' | 'image'; format: 'esrf-edf' | 'princeton-spe'; shape: number[]; frames: number;
  frame: number; roi: number[]; dtype: string; byteOrder: string; sourceBytes: number; readBytes: number;
  reads: number; headerBytes: number; version: string; png?: Uint8Array; range?: number[]; invalid?: number;
}
export function validateInstrumentSelection(frame: unknown, roi: unknown, frames: number, shape: number[]) {
  if (!integer(frame, 0, frames - 1) || !Array.isArray(roi) || roi.length !== 4
    || !integer(roi[0], 0, shape[1]! - 1) || !integer(roi[1], 0, shape[0]! - 1)
    || !integer(roi[2], 1, 1024) || !integer(roi[3], 1, 1024)
    || roi[0] + roi[2] > shape[1]! || roi[1] + roi[3] > shape[0]!) throw new Error('帧索引或 ROI 越界；宽高各不得超过 1024 像素。');
}
function crc32(bytes: Uint8Array): number {
  let value = 0xffffffff;
  for (const byte of bytes) { value ^= byte; for (let i = 0; i < 8; i++) value = value >>> 1 ^ (value & 1 ? 0xedb88320 : 0); }
  return (value ^ 0xffffffff) >>> 0;
}
function decodePng(encoded: unknown, width: number, height: number): Uint8Array {
  if (typeof encoded !== 'string' || !encoded.length || encoded.length > 2 * 1024 ** 2 || encoded.length % 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) return fail();
  let binary: string;
  try { binary = atob(encoded); } catch { return fail(); }
  if (btoa(binary) !== encoded || binary.length > 2 * 1024 ** 2) return fail();
  const bytes = Uint8Array.from(binary, c => c.charCodeAt(0)), view = new DataView(bytes.buffer);
  if ([...bytes.subarray(0, 8)].join(',') !== '137,80,78,71,13,10,26,10') return fail();
  let offset = 8;
  for (const kind of ['IHDR', 'IDAT', 'IEND']) {
    if (offset + 12 > bytes.length) return fail();
    const length = view.getUint32(offset);
    if (length > bytes.length - offset - 12 || String.fromCharCode(...bytes.subarray(offset + 4, offset + 8)) !== kind
      || crc32(bytes.subarray(offset + 4, offset + 8 + length)) !== view.getUint32(offset + 8 + length)) return fail();
    if (kind === 'IHDR' && (length !== 13 || view.getUint32(offset + 8) !== width || view.getUint32(offset + 12) !== height || [...bytes.subarray(offset + 16, offset + 21)].join(',') !== '8,0,0,0,0')) return fail();
    if (kind === 'IDAT' && !length || kind === 'IEND' && length) return fail();
    offset += length + 12;
  }
  if (offset !== bytes.length) return fail();
  // The API additionally verifies the bounded zlib stream and row lengths.
  // Browser-side validation checks container integrity before Blob creation.
  return bytes;
}
export function parseInstrumentImageData(raw: unknown): InstrumentImageData {
  if (!record(raw) || !['tree', 'image'].includes(raw.kind as string) || raw.contract_version !== 2
    || !['instrument', 'instrument-window'].includes(raw.type as string) || raw.reader !== undefined && raw.reader !== 'instrument-window'
    || typeof raw.version !== 'string' || !versionPattern.test(raw.version) || raw.revision !== undefined && (typeof raw.revision !== 'string' || !versionPattern.test(raw.revision))
    || raw.view_kind !== undefined && raw.view_kind !== raw.kind) return fail();
  const image = raw.kind === 'image';
  const keys = ['contract_version', 'type', 'reader', 'kind', 'view_kind', 'version', 'revision', 'media_type', 'metadata', 'choices', 'selected', 'warnings', 'sampled', image ? 'data_base64' : 'tree'];
  if (Object.keys(raw).some(key => !keys.includes(key)) || raw.media_type !== (image ? 'image/png' : 'application/json')
    || typeof raw.sampled !== 'boolean' || JSON.stringify(raw.warnings) !== JSON.stringify(notices)) return fail();
  const common = ['format', 'dialect', 'format_version', 'frame_count', 'frame_shape', 'dtype', 'byte_order', 'input_mode', 'source_bytes', 'read_bytes', 'read_requests', 'header_bytes', 'header_text_hidden', 'calibration', 'storage_layout', 'selection_mode'];
  const meta = raw.metadata;
  if (!exact(meta, common.concat(image ? ['output_shape', 'display_range', 'normalization', 'invalid_pixels', 'roi_origin'] : []))
    || !['esrf-edf', 'princeton-spe'].includes(meta.format as string) || meta.input_mode !== 'window'
    || meta.header_text_hidden !== true || meta.calibration !== 'none; raw detector values'
    || meta.storage_layout !== 'row-major; x fastest' || meta.selection_mode !== 'explicit frame and pixel ROI'
    || typeof meta.dtype !== 'string' || !Object.prototype.hasOwnProperty.call(dtypeBytes, meta.dtype) || !['little', 'big'].includes(meta.byte_order as string)) return fail();
  const edf = meta.format === 'esrf-edf', shape = meta.frame_shape, frames = meta.frame_count;
  if (!Array.isArray(shape) || shape.length !== 2 || !shape.every(v => integer(v, 1, edf ? 100000 : 65535))
    || !integer(frames, 1, edf ? 256 : 1000000) || !integer(meta.source_bytes, 1, 8 * 1024 ** 3)
    || !integer(meta.header_bytes, 1, 1024 ** 2) || !integer(meta.read_bytes, meta.header_bytes, 32 * 1024 ** 2) || !integer(meta.read_requests, 1, 2048)) return fail();
  const headerReads = edf ? meta.header_bytes / 512 : 1;
  if (edf ? meta.dialect !== 'edf-inline-2d-uncompressed' || meta.format_version !== null || meta.header_bytes % 512 || meta.header_bytes < frames * 512 || meta.header_bytes > frames * 65536
    : meta.dialect !== 'spe-2.x-fixed-4100' || !finite(meta.format_version) || meta.format_version < 2 || meta.format_version >= 3 || meta.header_bytes !== 4100 || meta.byte_order !== 'little' || !['int16', 'uint16', 'int32', 'float32'].includes(meta.dtype)) return fail();
  const pixelsBytes = frames * shape[0] * shape[1] * dtypeBytes[meta.dtype]!;
  if (!Number.isSafeInteger(pixelsBytes) || meta.source_bytes !== meta.header_bytes + pixelsBytes
    || !exact(raw.choices, ['frame_count', 'max_roi_size']) || raw.choices.frame_count !== frames || raw.choices.max_roi_size !== 1024
    || !exact(raw.selected, ['frame', 'roi'])) return fail();
  validateInstrumentSelection(raw.selected.frame, raw.selected.roi, frames, shape);
  const frame = raw.selected.frame as number, roi = raw.selected.roi as number[];
  if (raw.sampled !== (!image || frames !== 1 || JSON.stringify(roi) !== JSON.stringify([0, 0, shape[1], shape[0]]))) return fail();
  const result: InstrumentImageData = { kind: image ? 'image' : 'tree', format: meta.format as InstrumentImageData['format'], shape, frames,
    frame, roi, dtype: meta.dtype, byteOrder: meta.byte_order as string, sourceBytes: meta.source_bytes,
    readBytes: meta.read_bytes, reads: meta.read_requests, headerBytes: meta.header_bytes, version: raw.version };
  if (!image) {
    if (meta.read_bytes !== meta.header_bytes || meta.read_requests !== headerReads || JSON.stringify(raw.tree) !== JSON.stringify(tree)
      || frame !== 0 || JSON.stringify(roi) !== JSON.stringify([0, 0, Math.min(256, shape[1]), Math.min(256, shape[0])])) return fail();
    return result;
  }
  const count = roi[2]! * roi[3]!;
  if (meta.read_bytes !== meta.header_bytes + count * dtypeBytes[meta.dtype]! || meta.read_requests <= headerReads || meta.read_requests > headerReads + roi[3]!
    || JSON.stringify(meta.output_shape) !== JSON.stringify([roi[3], roi[2]]) || meta.normalization !== 'ROI min-max to uint8 grayscale; original data unchanged'
    || meta.roi_origin !== 'frame top-left; x right, y down' || !integer(meta.invalid_pixels, 0, count)
    || !Array.isArray(meta.display_range) || meta.display_range.length !== 2 || !meta.display_range.every(finite) || meta.display_range[0] > meta.display_range[1]
    || meta.invalid_pixels === count && JSON.stringify(meta.display_range) !== '[0,0]') return fail();
  if (!meta.dtype.startsWith('float')) {
    const bits = dtypeBytes[meta.dtype]! * 8, unsigned = meta.dtype.startsWith('u');
    if (meta.invalid_pixels !== 0 || !meta.display_range.every(v => integer(v, unsigned ? 0 : -(2 ** (bits - 1)), unsigned ? 2 ** bits - 1 : 2 ** (bits - 1) - 1))) return fail();
  }
  result.range = meta.display_range; result.invalid = meta.invalid_pixels; result.png = decodePng(raw.data_base64, roi[2]!, roi[3]!);
  return result;
}

export function assertInstrumentImageBinding(data: InstrumentImageData, previous: InstrumentImageData, frame: number, roi: number[]) {
  if (data.kind !== 'image' || !data.png || data.version !== previous.version) throw new Error('仪器图像文件版本或视图类型变化，请重新检查结构。');
  if (data.frame !== frame || JSON.stringify(data.roi) !== JSON.stringify(roi) || data.format !== previous.format
    || JSON.stringify(data.shape) !== JSON.stringify(previous.shape) || data.frames !== previous.frames || data.dtype !== previous.dtype
    || data.byteOrder !== previous.byteOrder || data.sourceBytes !== previous.sourceBytes || data.headerBytes !== previous.headerBytes) throw new Error('仪器图像响应与请求的帧、ROI 或布局不一致。');
}
