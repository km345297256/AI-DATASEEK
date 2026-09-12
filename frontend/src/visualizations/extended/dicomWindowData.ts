export const DICOM_WARNING = '仅用于已脱敏科学图像探索，不用于诊断；单文件不代表完整临床序列。标签声明和用户确认不保证像素真正脱敏，本工具不会自动脱敏。';
const ERROR = 'DICOM 响应或窗口选择无效，请重新打开预览。';
export type DicomKind = 'tree' | 'image';
export interface DicomOptions { frame: number; roi: number[]; confirm_deidentified: true }
export interface DicomImage { rows: number; columns: number; frames: number; bits_allocated: number; bits_stored: number; pixel_representation: number; photometric: 'MONOCHROME1' | 'MONOCHROME2'; sop_class: string; rescale: { slope: number; intercept: number; unit: string; declared: boolean }; window: { center: number; width: number; function: 'LINEAR' } | null; padding: { low: number; high: number } | null }
export interface DicomData { image: DicomImage; selected: DicomOptions | Record<string, never>; values: number[] | null; shape: number[] | null; sourceBytes: number; headerBytes: number; readBytes: number; reads: number; paddingPixels: number }
function assert(v: unknown): asserts v { if (!v) throw new Error(ERROR); }
const object = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v);
function keys(v: unknown, names: string[]): asserts v is Record<string, any> { assert(object(v) && Object.keys(v).sort().join('|') === [...names].sort().join('|')); }
const int = (v: unknown, low: number, high: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= low && v <= high;
const finite = (v: unknown, bound: number): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= bound;
const same = (a: unknown, b: unknown): boolean => Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((v,i) => same(v,b[i])) : object(a) && object(b) ? Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.prototype.hasOwnProperty.call(b,k) && same(a[k],b[k])) : a === b;
const dtype = (image: DicomImage) => `${image.pixel_representation ? 'int' : 'uint'}${image.bits_allocated}`;
const bounds = (image: DicomImage) => image.pixel_representation ? [-(2**(image.bits_stored-1)),2**(image.bits_stored-1)-1] : [0,2**image.bits_stored-1];
export function validateDicomOptions(kind: DicomKind, value: unknown, image?: DicomImage): DicomOptions | Record<string, never> {
  assert(kind === 'tree' || kind === 'image'); keys(value,kind === 'tree' ? [] : ['frame','roi','confirm_deidentified']);
  if(kind === 'tree') return {};
  assert(value.confirm_deidentified === true && int(value.frame,0,4095) && Array.isArray(value.roi) && value.roi.length === 4 && value.roi.slice(0,2).every((v: unknown) => int(v,0,65534)) && value.roi.slice(2).every((v: unknown) => int(v,1,128)));
  if(image) assert(value.frame < image.frames && value.roi[0]+value.roi[2] <= image.columns && value.roi[1]+value.roi[3] <= image.rows);
  return {frame:value.frame,roi:[...value.roi],confirm_deidentified:true};
}
export function parseDicomWindow(kind: DicomKind, payload: unknown, metadata: unknown, expected?: unknown): DicomData {
  keys(payload,['view_kind','media_type','choices','selected',kind === 'tree' ? 'tree' : 'array']); assert(payload.view_kind === kind && payload.media_type === 'application/json');
  keys(payload.choices,['image']); const c = payload.choices.image;
  keys(c,['rows','columns','frames','bits_allocated','bits_stored','pixel_representation','photometric','sop_class','rescale','window','padding']);
  assert(int(c.rows,1,65535) && int(c.columns,1,65535) && int(c.frames,1,4096) && [8,16].includes(c.bits_allocated) && int(c.bits_stored,1,c.bits_allocated) && int(c.pixel_representation,0,1) && ['MONOCHROME1','MONOCHROME2'].includes(c.photometric) && ['CT','MR','SC','SC-multiframe-8','SC-multiframe-16'].includes(c.sop_class));
  if(['CT','MR','SC'].includes(c.sop_class)) assert(c.frames === 1);
  if(c.sop_class.startsWith('SC-multiframe')) assert(c.pixel_representation === 0 && c.bits_allocated === (c.sop_class.endsWith('8') ? 8 : 16));
  keys(c.rescale,['slope','intercept','unit','declared']); const rescale = c.rescale;
  assert(finite(rescale.slope,1e6) && rescale.slope !== 0 && finite(rescale.intercept,1e9) && ['HU','OD','US','MGML','Z_EFF','ED','EDW','HU_MOD','PCT','unspecified'].includes(rescale.unit) && typeof rescale.declared === 'boolean');
  if(!rescale.declared) assert(same(rescale,{slope:1,intercept:0,unit:'unspecified',declared:false}));
  const image = c as unknown as DicomImage, [low,high] = bounds(image);
  assert(finite(low!*rescale.slope+rescale.intercept,1e12) && finite(high!*rescale.slope+rescale.intercept,1e12));
  if(c.window !== null) { keys(c.window,['center','width','function']); validateDicomWindowLevel(c.window.center,c.window.width); assert(c.window.function === 'LINEAR'); }
  if(c.padding !== null) { keys(c.padding,['low','high']); assert(int(c.padding.low,low!,high!) && int(c.padding.high,c.padding.low,high!)); }
  const selected = validateDicomOptions(kind,payload.selected,image); if(expected !== undefined) assert(same(selected,validateDicomOptions(kind,expected,image)));
  keys(metadata,['format','transfer_syntax','input_mode','source_bytes','read_bytes','read_requests','header_bytes','pixel_bytes','padding_pixels','metadata_hidden','privacy','value_semantics','limits']); const m = metadata;
  assert(m.format === 'dicom' && ['implicit-little','explicit-little'].includes(m.transfer_syntax) && m.input_mode === 'window' && m.metadata_hidden === true && m.privacy === 'source-declarations-and-user-confirmation-only' && m.value_semantics === 'raw-stored-integers' && same(m.limits,{max_roi:128,max_frames:4096,max_header_bytes:1048576}));
  assert(int(m.source_bytes,152,8*1024**3) && int(m.read_bytes,132,8*1024**2) && int(m.read_requests,1,128) && int(m.header_bytes,152,1048576) && int(m.pixel_bytes,1,4294967294) && int(m.padding_pixels,0,16384));
  const bytes = c.rows*c.columns*c.frames*(c.bits_allocated/8);
  assert(m.pixel_bytes === bytes && m.source_bytes === m.header_bytes+bytes+bytes%2 && m.read_bytes <= m.read_requests*1048576);
  let values: number[] | null = null, shape: number[] | null = null;
  if(kind === 'tree') assert(same(payload.tree,[{path:'/pixels',node_type:'array',shape:[c.frames,c.rows,c.columns],dtype:dtype(image)}]) && m.padding_pixels === 0 && m.read_bytes <= m.header_bytes);
  else {
    const opts = selected as DicomOptions, a = payload.array; keys(a,['shape','dimensions','dtype','values']);
    shape = [opts.roi[3]!,opts.roi[2]!]; assert(same(a.shape,shape) && same(a.dimensions,['row','column']) && a.dtype === dtype(image) && Array.isArray(a.values) && a.values.length === shape[0]!*shape[1]! && a.values.every((v: unknown) => int(v,low!,high!)));
    const count = c.padding ? a.values.filter((v: number) => v >= c.padding.low && v <= c.padding.high).length : 0;
    assert(m.padding_pixels === count && m.read_bytes >= 132+a.values.length*(c.bits_allocated/8)); values = a.values as number[];
  }
  return {image,selected,values,shape,sourceBytes:m.source_bytes,headerBytes:m.header_bytes,readBytes:m.read_bytes,reads:m.read_requests,paddingPixels:m.padding_pixels};
}
export function validateDicomWindowLevel(center: unknown, width: unknown): void { assert(finite(center,1e12) && finite(width,2e12) && width >= 1); }
export function defaultDicomWindow(data: DicomData): {center:number;width:number;source:string} {
  if(data.image.window) return {...data.image.window,source:'文件声明'};
  assert(data.values !== null); const p = data.image.padding, s = data.image.rescale;
  const values = data.values.filter(v => !p || v < p.low || v > p.high).map(v => v*s.slope+s.intercept);
  if(!values.length) return {center:0,width:1,source:'区域全部为 padding'};
  const low = Math.min(...values), high = Math.max(...values);
  return {center:(low+high+1)/2,width:high-low+1,source:'当前 ROI 范围（非文件窗设置）'};
}
/** DICOM PS3.3 C.11.2.1.2.1 LINEAR, after declared rescale; no data mutation. */
export function dicomRgba(data: DicomData, center: number, width: number): Uint8ClampedArray {
  validateDicomWindowLevel(center,width); assert(data.values !== null && data.values.length <= 16384);
  const output = new Uint8ClampedArray(data.values.length*4), r = data.image.rescale, p = data.image.padding;
  for(let i = 0; i < data.values.length; i++) {
    const stored = data.values[i]!; if(p && stored >= p.low && stored <= p.high) continue;
    const x = stored*r.slope+r.intercept, lower = center-.5-(width-1)/2, upper = center-.5+(width-1)/2;
    let gray = x <= lower ? 0 : x > upper ? 255 : ((x-(center-.5))/(width-1)+.5)*255;
    if(data.image.photometric === 'MONOCHROME1') gray = 255-gray;
    output[i*4] = output[i*4+1] = output[i*4+2] = Math.round(gray); output[i*4+3] = 255;
  }
  return output;
}
