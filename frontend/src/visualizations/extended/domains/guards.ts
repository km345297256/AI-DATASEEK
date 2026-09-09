/** Bounded, local-only inputs for trusted scientific renderer adapters. */
export const MAX_POINTS = 100_000;
export const MAX_PIXELS = 16_777_216;
export const MAX_DECODE_BYTES = 128 * 1024 * 1024;
const textDecoder = new TextDecoder('utf-8', { fatal: true });
export function decodeText(bytes: ArrayBuffer): string { return textDecoder.decode(bytes); }
export function safeName(value: unknown, max = 160): string {
  return typeof value === 'string' ? value.replace(/[\x00-\x1f<>]/g, '').slice(0, max) : '';
}
function record(value: unknown): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('数据对象格式不正确。');
  return value as Record<string, any>;
}
export function boundedGeoJSON(bytes: ArrayBuffer) {
  const root = record(JSON.parse(decodeText(bytes)));
  if (root.crs) throw new Error('GeoJSON 仅接受 RFC 7946 的 WGS84 经纬度；请先转换坐标系。');
  const input = root.type === 'FeatureCollection' ? root.features : root.type === 'Feature' ? [root] : [{ type: 'Feature', geometry: root }];
  if (!Array.isArray(input) || input.length > 10_000) throw new Error('GeoJSON 要素数量超出 10,000 上限。');
  let count = 0;
  const depths: Record<string, number> = { Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3 };
  const coords = (value: unknown, depth: number): any => {
    if (!Array.isArray(value)) throw new Error('GeoJSON 坐标格式不正确。');
    if (depth) { if (value.length > MAX_POINTS) throw new Error('坐标数量超限。'); return value.map((item) => coords(item, depth - 1)); }
    if (++count > MAX_POINTS || value.length < 2 || value.length > 3 || !value.every((n) => typeof n === 'number' && Number.isFinite(n))
      || Math.abs(value[0]) > 180 || Math.abs(value[1]) > 90 || (value[2] !== undefined && Math.abs(value[2]) > 1e8)) throw new Error('GeoJSON 坐标无效或超过 100,000 点预算。');
    return [...value];
  };
  const features = input.map((value: unknown, index: number) => {
    const feature = record(value), geometry = record(feature.geometry);
    if (!Object.prototype.hasOwnProperty.call(depths, geometry.type)) throw new Error('暂不支持 GeometryCollection 或非标准几何。');
    // Never pass arbitrary style, popup HTML, icon URL or external resources downstream.
    return { type: 'Feature' as const, id: index, properties: { name: safeName(feature.properties?.name) }, geometry: { type: geometry.type, coordinates: coords(geometry.coordinates, depths[geometry.type]!) } };
  });
  if (!count) throw new Error('数据中没有可显示的坐标。');
  return { type: 'FeatureCollection' as const, features };
}
export function geoCenter(data: ReturnType<typeof boundedGeoJSON>): [number, number] {
  let x = 0, y = 0, n = 0;
  const walk = (value: any) => { if (typeof value[0] === 'number') { x += value[0]; y += value[1]; n++; } else value.forEach(walk); };
  data.features.forEach((feature) => walk(feature.geometry.coordinates));
  return [x / n, y / n];
}

/** Allow only numerical position packets; never hand arbitrary CZML URI/material fields to Cesium. */
export function boundedCzml(bytes: ArrayBuffer): Record<string, unknown>[] {
  const packets = JSON.parse(decodeText(bytes));
  if (!Array.isArray(packets) || !packets.length || packets.length > 1001 || packets[0]?.id !== 'document' || packets[0]?.version !== '1.0') throw new Error('CZML 需要 version 1.0 文档包，最多 1,000 个位置对象。');
  let inspected = 0;
  const rejectResources = (value: any, depth = 0) => {
    if (++inspected > 500_000 || depth > 12) throw new Error('CZML 嵌套深度或字段数量超限。');
    if (typeof value === 'string' && /^(?:https?|ftp|file|data|blob):/i.test(value)) throw new Error('CZML 外部或嵌入 URI 资源不开放。');
    if (value && typeof value === 'object') for (const [key, item] of Object.entries(value)) {
      if (/^(?:uri|url|href|image|gltf|reference|references)$/i.test(key)) throw new Error('CZML 仅允许纯位置数据，不能包含 URI 或资源引用。');
      rejectResources(item, depth + 1);
    }
  };
  rejectResources(packets);
  let total = 0;
  const result: Record<string, unknown>[] = [{ id: 'document', version: '1.0' }];
  const ids = new Set<string>();
  for (const candidate of packets.slice(1)) {
    const packet = record(candidate);
    if (Object.keys(packet).some((key) => !['id', 'name', 'position', 'availability', 'point', 'path'].includes(key))) throw new Error('CZML 首期仅允许位置、时间及轨迹；模型、URI、引用和外部资源不开放。');
    const position = record(packet.position);
    if (Object.keys(position).some((key) => !['epoch', 'cartographicDegrees'].includes(key))) throw new Error('CZML 仅接受 cartographicDegrees，不支持外部引用或自定义插值。');
    const values = position.cartographicDegrees;
    const timeSeries = typeof position.epoch === 'string';
    const stride = timeSeries ? 4 : 3;
    if (!Array.isArray(values) || values.length % stride || (!timeSeries && values.length !== 3)
      || !values.every((n) => typeof n === 'number' && Number.isFinite(n)) || (total += values.length / stride) > MAX_POINTS) throw new Error('CZML 位置数据无效或超出预算。');
    let previous = -Infinity;
    for (let i = 0; i < values.length; i += stride) {
      const j = i + (timeSeries ? 1 : 0);
      if (Math.abs(values[j]) > 180 || Math.abs(values[j + 1]) > 90 || Math.abs(values[j + 2]) > 1e8 || (timeSeries && values[i] <= previous)) throw new Error('CZML 经纬度或采样时间无效。');
      if (timeSeries) previous = values[i];
    }
    if (timeSeries && (!/^\d{4}-\d\d-\d\dT/.test(position.epoch) || !Number.isFinite(Date.parse(position.epoch)))) throw new Error('CZML epoch 必须是明确 ISO 日期。');
    const id = safeName(packet.id, 80);
    if (!id || id === 'document' || ids.has(id)) throw new Error('CZML 对象 ID 缺失或重复。');
    ids.add(id);
    const next: Record<string, unknown> = { id, name: safeName(packet.name), position: { ...(timeSeries ? { epoch: position.epoch } : {}), cartographicDegrees: values }, point: { pixelSize: 8, color: { rgba: [255, 130, 40, 255] } } };
    if (timeSeries && values.length >= 8) {
      const start = new Date(Date.parse(position.epoch) + values[0] * 1000).toISOString();
      const stop = new Date(Date.parse(position.epoch) + values[values.length - 4] * 1000).toISOString();
      next.availability = `${start}/${stop}`;
      next.path = { width: 2, material: { solidColor: { color: { rgba: [255, 180, 40, 255] } } } };
    }
    result.push(next);
  }
  if (result.length < 2) throw new Error('CZML 没有可显示的位置对象。');
  return result;
}

export function validateRaster(width: number, height: number, channels = 1, bytesPerSample = 8) {
  if (![width, height, channels, bytesPerSample].every((n) => Number.isSafeInteger(n) && n > 0) || width * height > MAX_PIXELS || width * height * channels * bytesPerSample > MAX_DECODE_BYTES) throw new Error('图像超过 16M 像素或 128 MiB 解码预算。');
}

export function validateNiftiOrNrrd(bytes: ArrayBuffer, filename: string) {
  if (/\.nii$/i.test(filename)) {
    if (bytes.byteLength < 352) throw new Error('NIfTI 文件头不完整。');
    const view = new DataView(bytes), little = view.getInt32(0, true) === 348;
    if (view.getInt32(0, little) !== 348 || new TextDecoder().decode(bytes.slice(344, 348)) !== 'n+1\0') throw new Error('首期仅支持单文件、未压缩 NIfTI-1 (.nii)。');
    const dimensions = view.getInt16(40, little), bits = view.getInt16(72, little), datatype = view.getInt16(70, little), offset = view.getFloat32(108, little);
    const supportedTypes: Record<number, number> = { 2: 8, 4: 16, 8: 32, 16: 32, 64: 64, 256: 8, 512: 16, 768: 32 };
    if (dimensions < 2 || dimensions > 4 || supportedTypes[datatype] !== bits || !Number.isSafeInteger(offset) || offset < 352) throw new Error('NIfTI 维度或数据类型不支持。');
    let voxels = 1;
    for (let i = 1; i <= dimensions; i++) { const length = view.getInt16(40 + i * 2, little); if (length < 1) throw new Error('NIfTI 维度无效。'); voxels *= length; }
    if (voxels > MAX_PIXELS || voxels * Math.max(bits / 8, 4) > MAX_DECODE_BYTES || offset + voxels * bits / 8 > bytes.byteLength) throw new Error('NIfTI 解码预算超限或数据截断。');
    return;
  }
  if (!/\.nrrd$/i.test(filename)) throw new Error('首期仅支持 .nii 和内嵌 raw 编码 .nrrd；压缩或关联文件需独立资源协议。');
  const prefix = new TextDecoder().decode(bytes.slice(0, Math.min(8192, bytes.byteLength))), boundary = /\r?\n\r?\n/.exec(prefix);
  if (!prefix.startsWith('NRRD000') || !boundary) throw new Error('NRRD 文件头不完整或超限。');
  const fields = new Map<string, string>();
  for (const line of prefix.slice(0, boundary.index).split(/\r?\n/).slice(1)) {
    if (line.startsWith('#') || !line.trim()) continue;
    const split = line.indexOf(':'); if (split < 1) throw new Error('NRRD 文件头格式无效。');
    fields.set(line.slice(0, split).trim().toLowerCase(), line.slice(split + 1).trim());
  }
  if (fields.has('data file') || fields.has('datafile') || fields.get('encoding') !== 'raw' || fields.has('byte skip') || fields.has('line skip')) throw new Error('NRRD 仅允许内嵌 raw 数据，禁止外部文件、压缩和偏移跳读。');
  const widths: Record<string, number> = { uchar: 1, 'unsigned char': 1, uint8: 1, int8: 1, char: 1, short: 2, 'short int': 2, int16: 2, ushort: 2, 'unsigned short': 2, uint16: 2, int: 4, int32: 4, uint: 4, uint32: 4, float: 4, double: 8 };
  const bpp = widths[fields.get('type') || ''];
  const sizes = (fields.get('sizes') || '').trim().split(/\s+/).map(Number);
  if (!bpp || sizes.length < 2 || sizes.length > 4 || Number(fields.get('dimension')) !== sizes.length || !sizes.every((n) => Number.isSafeInteger(n) && n > 0)) throw new Error('NRRD 数据类型或维度不支持。');
  const voxels = sizes.reduce((a, b) => a * b, 1);
  if (voxels > MAX_PIXELS || voxels * Math.max(4, bpp) > MAX_DECODE_BYTES || boundary.index + boundary[0].length + voxels * bpp > bytes.byteLength) throw new Error('NRRD 解码预算超限或数据截断。');
}

export function fitsWcsHeader(bytes: ArrayBuffer) {
  if (bytes.byteLength < 2880) throw new Error('FITS 文件头不完整。');
  const header = new Map<string, string>(); let ended = false, headerLength = 0;
  const prefix = new TextDecoder('ascii').decode(bytes.slice(0, Math.min(bytes.byteLength, 80 * 2048)));
  for (let offset = 0; offset + 80 <= prefix.length; offset += 80) {
    const card = prefix.slice(offset, offset + 80), key = card.slice(0, 8).trim();
    if (key === 'END') { ended = true; headerLength = Math.ceil((offset + 80) / 2880) * 2880; break; }
    if (card[8] === '=') header.set(key, card.slice(10).split('/')[0]!.trim().replace(/^'|'$/g, '').trim());
  }
  if (!ended || header.get('SIMPLE') !== 'T' || Number(header.get('NAXIS')) !== 2) throw new Error('天球预览首期支持 primary HDU 二维 FITS 图像。');
  const width = Number(header.get('NAXIS1')), height = Number(header.get('NAXIS2'));
  validateRaster(width, height);
  const bitpix = Number(header.get('BITPIX'));
  if (![8, 16, 32, -32, -64].includes(bitpix) || headerLength + width * height * Math.abs(bitpix) / 8 > bytes.byteLength) throw new Error('FITS 数据类型不支持或图像数据截断。');
  if (!/^RA---/.test(header.get('CTYPE1') || '') || !/^DEC--/.test(header.get('CTYPE2') || '') || !['CRVAL1', 'CRVAL2', 'CRPIX1', 'CRPIX2'].every((key) => header.has(key) && Number.isFinite(Number(header.get(key))))) throw new Error('缺少可识别 RA/DEC WCS；请使用普通 FITS 图像插件。');
  if (Math.abs(Number(header.get('CRVAL2'))) > 90 || Math.abs(Number(header.get('CRVAL1'))) > 360) throw new Error('FITS WCS 天球坐标范围无效。');
  const scale = (key: string) => header.has(key) && Number.isFinite(Number(header.get(key))) && Number(header.get(key)) !== 0;
  if (!(scale('CDELT1') && scale('CDELT2')) && !(scale('CD1_1') && scale('CD2_2'))) throw new Error('FITS WCS 缺少明确的像素比例 CDELT/CD 矩阵。');
  return { width, height, ra: Number(header.get('CRVAL1')), dec: Number(header.get('CRVAL2')) };
}

export function chromosomeSizes(text: string): { text: string; first: string; sizes: Map<string, number> } {
  if (!text || text.length > 65536) throw new Error('请提供明确参考组装的 chrom.sizes（染色体名与长度，每行一条）。');
  const sizes = new Map<string, number>();
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const fields = line.trim().split(/\s+/), length = Number(fields[1]);
    if (fields.length !== 2 || !/^[\w.:-]{1,80}$/.test(fields[0]!) || !Number.isSafeInteger(length) || length < 1 || length > 2 ** 32 || sizes.has(fields[0]!) || sizes.size >= 1000) throw new Error('chrom.sizes 含无效、重复或超限染色体。');
    sizes.set(fields[0]!, length);
  }
  if (!sizes.size) throw new Error('chrom.sizes 不能为空。');
  return { text: [...sizes].map(([name, size]) => `${name}\t${size}`).join('\n'), first: sizes.keys().next().value!, sizes };
}

export function checkedGenomicText(bytes: ArrayBuffer, extension: string, reference: Map<string, number>) {
  const lines = decodeText(bytes).split(/\r?\n/), output: string[] = []; let count = 0;
  const vcf = extension === 'vcf';
  for (const line of lines) {
    if (!line.trim()) continue;
    if (line.startsWith('#')) {
      if (vcf && (/^##fileformat=VCFv4\.[0-9]$/.test(line) || line.startsWith('#CHROM\t'))) output.push(line);
      continue;
    }
    if (/^(track|browser)\s/.test(line)) continue;
    const fields = line.split('\t'), chromosome = fields[0] || '', max = reference.get(chromosome), start = Number(fields[1]), end = vcf ? start : Number(fields[2]);
    if (++count > 50_000 || max === undefined || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < (vcf ? 1 : 0) || end < start || end > max) throw new Error('轨道坐标超出指定参考组装或记录超过 50,000 条。');
    if (vcf) {
      if (fields.length < 8 || !/^[ACGTN]+$/i.test(fields[3]!) || !fields[4]!.split(',').every((alt) => /^(?:[ACGTN]+|[.*]|<(?:DEL|INS|DUP|INV|CNV|NON_REF|BND)>)$/i.test(alt))) throw new Error('VCF 记录格式无效；首期不开放断点 URL 或自定义关联字段。');
      output.push([chromosome, start, safeName(fields[2]) || '.', fields[3], fields[4], fields[5] === '.' || Number.isFinite(Number(fields[5])) ? fields[5] : '.', /^[\w.;-]{1,160}$/.test(fields[6]!) ? fields[6] : '.', '.'].join('\t'));
    } else {
      output.push([chromosome, start, end, safeName(fields[3]) || `feature-${count}`, '0', ['+', '-'].includes(fields[5]!) ? fields[5] : '.'].join('\t'));
    }
  }
  if (!count || (vcf && !output.some((line) => line.startsWith('##fileformat=')))) throw new Error('轨道为空或缺少 VCF 文件头。');
  // Deliberately bounded display subset: no arbitrary INFO HTML, URLs or session directives.
  if (vcf) return ['##fileformat=VCFv4.2', '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO', ...output.filter((line) => !line.startsWith('#'))].join('\n');
  return output.join('\n');
}
