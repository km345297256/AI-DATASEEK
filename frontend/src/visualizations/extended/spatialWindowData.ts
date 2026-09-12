export const SPATIAL_LIMITS={max_observations:8192,max_scalars:24576,max_sparse_entries:65536,max_chunk_bytes:4194304,max_decoded_chunk_bytes:16777216,max_numeric_bytes:4194304,max_attribute_buffer_bytes:4096};
export const SPATIAL_VALUE='X storage values; upstream processing unknown; no normalization, registration or unit conversion';
export const SPATIAL_SPARSE='absent entries are zero; sorted unique indices required in inspected segments';
export const SPATIAL_SCOPE='selected numeric windows; no whole-file validation';
export const SPATIAL_LABELS='ordinal only; obs and var labels not read';
export const SPATIAL_AXES='stored columns 0,1; no inversion';
export const SPATIAL_STRATEGIES={dense:'selected dense feature column and observation rows; HDF5 chunks may include other values',csr:'all sparse entries in selected observation rows, then select one feature',csc:'all sparse entries in selected feature column, then select observation rows'};
export type SpatialSelection={feature:number;observation_start:number;observation_count:number;decode:'raw'};
export type SpatialValues={observations:number[];x:(number|null)[];y:(number|null)[];values:(number|null)[]};
export interface SpatialData {sourceBytes:number;readBytes:number;reads:number;observations:number;features:number;storage:'dense'|'csr'|'csc';matrix:Record<string,any>;coordinates:Record<string,any>;spatial:SpatialValues|null;selected:SpatialSelection|null;numericBytes:number;decodedChunkBytes:number;sparseEntries:number;nullCoordinates:number;nullExpressions:number;plottable:number;attributeBytes:number}
const ERROR='空间窗口响应、结构或范围无效，请重新打开。';
function assert(ok:unknown):asserts ok{if(!ok)throw new Error(ERROR)}
const obj=(v:unknown):v is Record<string,any>=>v!==null&&typeof v==='object'&&!Array.isArray(v);
function keys(v:unknown,names:string):asserts v is Record<string,any>{assert(obj(v)&&Object.keys(v).sort().join('|')===names.split(' ').filter(Boolean).sort().join('|'))}
const int=(v:unknown,lo=0,hi=2147483647):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=lo&&v<=hi;
const finite=(v:unknown):v is number=>typeof v==='number'&&Number.isFinite(v);
const same=(a:any,b:any):boolean=>Array.isArray(a)&&Array.isArray(b)?a.length===b.length&&a.every((v,i)=>same(v,b[i])):obj(a)&&obj(b)?Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>Object.prototype.hasOwnProperty.call(b,k)&&same(a[k],b[k])):a===b;
const dtype=(v:unknown):v is string=>typeof v==='string'&&/^(?:[|][iu]1|[<>][iu][248]|[<>]f[48])$/.test(v);
function raw(value:unknown,d:string):boolean{const kind=d.slice(-2,-1),width=Number(d.slice(-1));if(value===null)return kind==='f';if(!finite(value))return false;if(kind==='f')return Math.abs(value)<=(width===4?3.4028234663852886e38:Number.MAX_VALUE);return int(value,kind==='u'?0:-(2**(width*8-1)),Math.min(Number.MAX_SAFE_INTEGER,2**(width*8-(kind==='i'?1:0))-1))}
export function validateSpatialSelection(value:unknown,data?:Pick<SpatialData,'features'|'observations'>):SpatialSelection{
 keys(value,'feature observation_start observation_count decode');assert(value.decode==='raw'&&int(value.feature)&&int(value.observation_start)&&int(value.observation_count,1,8192));
 if(data)assert(value.feature<data.features&&value.observation_start+value.observation_count<=data.observations);
 return {feature:value.feature,observation_start:value.observation_start,observation_count:value.observation_count,decode:'raw'};
}
export function parseSpatialWindow(kind:'tree'|'geometry',payload:unknown,metadata:unknown,expected?:SpatialSelection):SpatialData{
 assert(kind==='tree'||kind==='geometry');keys(payload,'view_kind media_type choices selected '+(kind==='tree'?'tree':'spatial'));assert(payload.view_kind===kind&&payload.media_type==='application/json');
 keys(metadata,'format h5ad_encoding input_mode source_bytes read_bytes read_requests matrix coordinates labels value_semantics sparse_semantics scope read_strategy attribute_buffer_bytes chunks_touched decoded_chunk_bytes numeric_values_read numeric_bytes_read sparse_entries_scanned null_coordinates null_expressions plottable_points limits');
 const m=metadata;assert(m.format==='h5ad'&&m.h5ad_encoding==='0.1.0'&&m.input_mode==='window'&&int(m.source_bytes,256,8*1024**3)&&int(m.read_bytes,1,8*1024**2)&&int(m.read_requests,1,128)
 &&m.labels===SPATIAL_LABELS&&m.value_semantics===SPATIAL_VALUE&&m.sparse_semantics===SPATIAL_SPARSE&&m.scope===SPATIAL_SCOPE&&same(m.limits,SPATIAL_LIMITS)
 &&int(m.attribute_buffer_bytes,1,4096)&&int(m.chunks_touched,0,128)&&int(m.decoded_chunk_bytes,0,16777216)&&int(m.numeric_values_read,0,200000)&&int(m.numeric_bytes_read,0,4194304)&&int(m.sparse_entries_scanned,0,65536)
 &&int(m.null_coordinates,0,16384)&&int(m.null_expressions,0,8192)&&int(m.plottable_points,0,8192));
 keys(m.matrix,'storage shape dtype nnz index_dtype pointer_dtype');const a=m.matrix;
 assert(['dense','csr','csc'].includes(a.storage)&&Array.isArray(a.shape)&&a.shape.length===2&&a.shape.every((v:unknown)=>int(v,1))&&dtype(a.dtype)&&m.read_strategy===SPATIAL_STRATEGIES[a.storage as keyof typeof SPATIAL_STRATEGIES]);
 const observations=a.shape[0] as number,features=a.shape[1] as number;
 if(a.storage==='dense')assert(a.nnz===null&&a.index_dtype===null&&a.pointer_dtype===null);
 else assert(int(a.nnz,0,Number.MAX_SAFE_INTEGER)&&dtype(a.index_dtype)&&'iu'.includes(a.index_dtype.slice(-2,-1)!)&&dtype(a.pointer_dtype)&&'iu'.includes(a.pointer_dtype.slice(-2,-1)!));
 keys(m.coordinates,'path shape dtype unit axis_order');const c=m.coordinates;
 assert(c.path==='/obsm/spatial'&&same(c.shape,[observations,2])&&dtype(c.dtype)&&c.unit===null&&c.axis_order===SPATIAL_AXES);
 assert(same(payload.choices,{observation_count:observations,feature_count:features,feature_labels:'zero-based ordinal'}));
 let spatial:SpatialValues|null=null,selected:SpatialSelection|null=null;
 if(kind==='tree'){
  keys(payload.selected,'');assert(same(payload.tree,[{path:'/spatial',node_type:'array',shape:[observations,2]},{path:'/X',node_type:'array',shape:[observations,features]}])
  &&['chunks_touched','decoded_chunk_bytes','numeric_values_read','numeric_bytes_read','sparse_entries_scanned','null_coordinates','null_expressions','plottable_points'].every(k=>m[k]===0));
 }else{
  selected=validateSpatialSelection(payload.selected,{observations,features});if(expected)assert(same(selected,validateSpatialSelection(expected,{observations,features})));
  const start=selected.observation_start,count=selected.observation_count; {
   keys(payload.spatial,'x y values observations');const s=payload.spatial;
   assert(Object.values(s).every(v=>Array.isArray(v)&&v.length===count)&&s.observations.every((v:unknown,i:number)=>int(v,start,start+count-1)&&v===start+i)
   &&['x','y'].every(k=>s[k].every((v:unknown)=>raw(v,c.dtype)))&&s.values.every((v:unknown)=>raw(v,a.dtype)));
   assert(m.null_coordinates===s.x.filter((v:unknown)=>v===null).length+s.y.filter((v:unknown)=>v===null).length&&m.null_expressions===s.values.filter((v:unknown)=>v===null).length
   &&m.plottable_points===s.x.filter((_:unknown,i:number)=>s.x[i]!==null&&s.y[i]!==null&&s.values[i]!==null).length);
   assert(m.numeric_values_read>=2*count&&m.numeric_bytes_read>=2*count*Number(c.dtype.slice(-1)));
   if(a.storage==='dense')assert(m.sparse_entries_scanned===0&&m.numeric_values_read===3*count&&m.numeric_bytes_read===count*(2*Number(c.dtype.slice(-1))+Number(a.dtype.slice(-1))));
   else{const n=m.sparse_entries_scanned,p=a.storage==='csr'?count+1:2;assert(n<=a.nnz&&m.numeric_values_read===2*count+2*n+p+2&&m.numeric_bytes_read===2*count*Number(c.dtype.slice(-1))+n*(Number(a.dtype.slice(-1))+Number(a.index_dtype.slice(-1)))+(p+2)*Number(a.pointer_dtype.slice(-1)))}
   spatial=s as SpatialValues;
  }
 }
 return {sourceBytes:m.source_bytes,readBytes:m.read_bytes,reads:m.read_requests,observations,features,storage:a.storage,matrix:a,coordinates:c,spatial,selected,numericBytes:m.numeric_bytes_read,decodedChunkBytes:m.decoded_chunk_bytes,sparseEntries:m.sparse_entries_scanned,nullCoordinates:m.null_coordinates,nullExpressions:m.null_expressions,plottable:m.plottable_points,attributeBytes:m.attribute_buffer_bytes};
}
export function spatialCatalogMatches(a:SpatialData,b:SpatialData):boolean{return a.sourceBytes===b.sourceBytes&&a.observations===b.observations&&a.features===b.features&&same(a.matrix,b.matrix)&&same(a.coordinates,b.coordinates)}
