export const RIPPLE_WARNING = '只显示单通道原始图像或单像元谱线；坐标仅按文件声明展开，不做能量校准、背景扣除、元素识别或定量。';
const ERROR = 'Ripple 响应或窗口选择无效，请重新打开预览。';
export type RippleKind = 'tree' | 'image' | 'series';
export type RippleOptions = Record<string, number>;
export interface RippleAxis { label: string; unit: string | null; origin: number; scale: number; calibration: 'index' | 'declared' | 'ev-per-chan'; origin_defaulted: boolean }
export interface RippleCube { width: number; height: number; depth: number; dtype: string; byte_order: string; data_offset: number; signal_type: string; axes: RippleAxis[] }
export interface RippleData { cube: RippleCube; sourceBytes: number; headerBytes: number; readBytes: number; reads: number; nulls: number; selected: RippleOptions; array: { shape: number[]; dimensions: string[]; dtype: string; values: (number | null)[] } | null; axes: { label: string; unit: string | null; values: number[] }[] }
const types: Record<string, [number, number | null, number]> = { int8: [1,-128,127], uint8: [1,0,255], int16: [2,-32768,32767], uint16: [2,0,65535], int32: [4,-2147483648,2147483647], uint32: [4,0,4294967295], float32: [4,null,3.4028234663852886e38], float64: [8,null,Number.MAX_VALUE] };
function assert(ok: unknown): asserts ok { if (!ok) throw new Error(ERROR); }
const object = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v);
function keys(v: unknown, names: string[]): asserts v is Record<string, any> { assert(object(v) && Object.keys(v).sort().join('|') === [...names].sort().join('|')); }
const int = (v: unknown, lo: number, hi: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= lo && v <= hi;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const same = (a: unknown, b: unknown): boolean => Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((v, i) => same(v, b[i])) : object(a) && object(b) ? Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k])) : a === b;
export function validateRippleOptions(kind: RippleKind, value: unknown, cube?: RippleCube): RippleOptions {
  assert(['tree','image','series'].includes(kind));
  const names = kind === 'tree' ? [] : kind === 'image' ? ['channel','x','y','width','height'] : ['x','y','channel_start','channel_count'];
  keys(value,names);
  if (kind !== 'tree') {
    assert(int(value.x,0,2**31-1) && int(value.y,0,2**31-1));
    if (cube) assert(value.x < cube.width && value.y < cube.height);
    if (kind === 'image') { assert(int(value.channel,0,1048575) && int(value.width,1,128) && int(value.height,1,128)); if (cube) assert(value.channel < cube.depth && value.x+value.width <= cube.width && value.y+value.height <= cube.height); }
    else { assert(int(value.channel_start,0,1048575) && int(value.channel_count,1,16384)); if (cube) assert(value.channel_start+value.channel_count <= cube.depth); }
  }
  return Object.fromEntries(names.map(k => [k,value[k]]));
}
export function parseRippleWindow(kind: RippleKind, payload: unknown, metadata: unknown, expected?: RippleOptions): RippleData {
  keys(payload,['view_kind','media_type','choices','selected',...(kind==='tree'?['tree']:['array','axes'])]);
  assert(payload.view_kind===kind && payload.media_type==='application/json');
  keys(metadata,['format','input_mode','source_bytes','header_bytes','data_bytes','read_bytes','read_requests','null_values','limits','record_by','value_semantics']);
  const m=metadata;
  assert(m.format==='rpl' && m.input_mode==='window' && int(m.source_bytes,2,8*1024**3) && int(m.header_bytes,1,65536) && int(m.data_bytes,1,8*1024**3) && m.source_bytes===m.header_bytes+m.data_bytes && int(m.read_bytes,m.header_bytes,8*1024**2) && int(m.read_requests,1,256) && int(m.null_values,0,16384) && m.record_by==='vector' && m.value_semantics==='raw-storage' && same(m.limits,{max_values:16384,max_depth:1048576,max_spectrum_channels:16384}));
  keys(payload.choices,['cube']); const c=payload.choices.cube;
  keys(c,['width','height','depth','dtype','byte_order','data_offset','signal_type','axes']);
  assert(int(c.width,1,2**31-1) && int(c.height,1,2**31-1) && int(c.depth,2,1048576) && typeof c.dtype==='string' && Object.prototype.hasOwnProperty.call(types,c.dtype) && ['little-endian','big-endian','dont-care'].includes(c.byte_order) && int(c.data_offset,0,1048576) && ['EELS','EDS_SEM','EDS_TEM','unspecified'].includes(c.signal_type));
  const type=types[c.dtype]!;
  assert((type[0]===1)===(c.byte_order==='dont-care') && c.data_offset+c.width*c.height*c.depth*type[0]===m.data_bytes && Array.isArray(c.axes) && c.axes.length===3);
  c.axes.forEach((desc: unknown,i: number) => {
    keys(desc,['label','unit','origin','scale','calibration','origin_defaulted']);
    assert(desc.label===['height','width','depth'][i] && (desc.unit===null || typeof desc.unit==='string' && /^[A-Za-z0-9 µμÅÅ°^()./− -]{1,64}$/.test(desc.unit) && !/(?:^\/|\/(?:Users|home|tmp|private|var)\/)/i.test(desc.unit)) && finite(desc.origin) && Math.abs(desc.origin)<=1e12 && finite(desc.scale) && Math.abs(desc.scale)>=1e-12 && Math.abs(desc.scale)<=1e12 && ['index','declared','ev-per-chan'].includes(desc.calibration) && typeof desc.origin_defaulted==='boolean');
    if(desc.origin_defaulted) assert(desc.origin===0);
    if(desc.calibration==='index') assert(same(desc,{label:['height','width','depth'][i],unit:null,origin:0,scale:1,calibration:'index',origin_defaulted:true}));
    if(desc.calibration==='ev-per-chan') assert(i===2 && desc.unit==='eV' && desc.scale>0);
    const size=[c.height,c.width,c.depth][i]!,end=desc.origin+(size-1)*desc.scale;
    assert(finite(end) && Math.abs(end)<=1e15 && (size<2 || desc.origin+desc.scale!==desc.origin && end-desc.scale!==end));
  });
  const cube=c as unknown as RippleCube,selected=validateRippleOptions(kind,payload.selected,cube);
  if(expected) assert(same(selected,validateRippleOptions(kind,expected,cube)));
  let array: RippleData['array']=null,axes: RippleData['axes']=[];
  if(kind==='tree') assert(same(payload.tree,[{path:'/cube',node_type:'array',shape:[c.height,c.width,c.depth],dtype:c.dtype}]) && m.null_values===0 && m.read_requests===1 && m.read_bytes===m.header_bytes);
  else {
    const shape=kind==='image'?[selected.height!,selected.width!]:[selected.channel_count!],dimensions=kind==='image'?['height','width']:['depth'];
    keys(payload.array,['shape','dimensions','dtype','values']);const a=payload.array;
    assert(same(a.shape,shape) && same(a.dimensions,dimensions) && a.dtype===c.dtype && Array.isArray(a.values) && a.values.length===shape.reduce((a,b)=>a*b,1) && a.values.length<=16384);
    assert(m.read_requests>=2 && m.read_bytes>=m.header_bytes+a.values.length*type[0]);
    assert(a.values.every((v: unknown)=>type[1]===null ? v===null || finite(v) && Math.abs(v)<=type[2] : int(v,type[1],type[2])) && a.values.filter((v: unknown)=>v===null).length===m.null_values);
    const parts=kind==='image'?[[0,selected.y!,selected.height!],[1,selected.x!,selected.width!]]:[[2,selected.channel_start!,selected.channel_count!]];
    axes=parts.map(([dim,start,count])=>{const d=cube.axes[dim!]!; return {label:d.label,unit:d.unit,values:Array.from({length:count!},(_,i)=>d.origin+(start!+i)*d.scale)};});
    assert(same(payload.axes,axes));
    axes.forEach(axis=>assert(axis.values.length<2 || axis.values.every((v,i)=>i===0 || v>axis.values[i-1]!) || axis.values.every((v,i)=>i===0 || v<axis.values[i-1]!)));
    array=a as RippleData['array'];
  }
  return {cube,sourceBytes:m.source_bytes,headerBytes:m.header_bytes,readBytes:m.read_bytes,reads:m.read_requests,nulls:m.null_values,selected,array,axes};
}
