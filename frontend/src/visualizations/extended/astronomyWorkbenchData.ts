/** Domain validation for the Cordis scientific-image workbench, never URLs. */
export const ASTRONOMY_WARNING = '读取有界原始文件并应用文件声明的标度；显示拉伸不修改数据。源检测仅为局部峰候选，不是天体分类或测光。无公网底图。';
export const ASTRONOMY_LIMITS = { source_bytes:33554432,decoded_bytes:134217728,plane_values:2097152,datasets:64,render_side:1024,table_rows:50,table_columns:32,spectrum_points:2000,sources:2000 };
export type AstronomyKind = 'tree'|'image'|'table'|'series';
export type AstronomyOptions = Record<string, unknown>;
export interface AstronomyColumn {index:number;name:string;format:string;unit:string}
export interface AstronomyDataset {index:number;name:string;kind:'empty'|'image'|'table'|'spectrum';shape:number[];plane_shape:number[];dtype:string;channels:number;columns:AstronomyColumn[];row_count:number;wcs:{celestial:boolean;axis_types:string[];units:string[]}}
export interface AstronomyData {datasets:AstronomyDataset[];geospatial:Record<string,unknown>|null;selected:AstronomyOptions;result:Record<string,any>;sourceBytes:number;format:'fits'|'tiff'}
const ERROR = '科学影像响应或选择无效，请重新检查文件。';
function need(v:unknown): asserts v {if(!v)throw new Error(ERROR);}
const object = (v:unknown):v is Record<string,any> => v !== null && typeof v === 'object' && !Array.isArray(v);
function keys(v:unknown,names:string): asserts v is Record<string,any> {need(object(v)&&Object.keys(v).sort().join('|')===names.split(' ').filter(Boolean).sort().join('|'));}
const integer = (v:unknown,l:number,h:number):v is number => typeof v === 'number'&&Number.isSafeInteger(v)&&v>=l&&v<=h;
const finite = (v:unknown):v is number => typeof v === 'number'&&Number.isFinite(v)&&Math.abs(v)<=1e100;
const number = (v:unknown):boolean => v === null||finite(v);
const label = (v:unknown):v is string => typeof v === 'string'&&v.length<=128&&!/[<>\x00-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
export function sameAstronomy(a:unknown,b:unknown):boolean {return Array.isArray(a)&&Array.isArray(b)?a.length===b.length&&a.every((v,i)=>sameAstronomy(v,b[i])):object(a)&&object(b)?Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>Object.prototype.hasOwnProperty.call(b,k)&&sameAstronomy(a[k],b[k])):a===b;}
export function validateAstronomyOptions(kind:AstronomyKind,value:unknown):AstronomyOptions {
  need(['tree','image','table','series'].includes(kind)&&object(value)); if(kind==='tree'){keys(value,'');return {};}
  need(integer(value.dataset,0,63));
  if(kind==='series') keys(value,'dataset');
  else if(kind==='table'){keys(value,'dataset row_offset column_offset');need(integer(value.row_offset,0,2147483647)&&integer(value.column_offset,0,255));}
  else {
    const fields:Record<string,string>={render:'stretch interval low high colour_map invert',pixel:'x y',region:'bounds',sources:'threshold_sigma'};
    need(typeof value.action==='string'&&Object.prototype.hasOwnProperty.call(fields,value.action));keys(value,'dataset slices band action '+fields[value.action]);
    need(Array.isArray(value.slices)&&value.slices.length<=6&&value.slices.every((v:unknown)=>integer(v,0,2147483647))&&integer(value.band,0,16));
    if(value.action==='render') {need(['linear','log','sqrt','asinh'].includes(value.stretch)&&['zscale','percentile','manual'].includes(value.interval)&&['gray','viridis','heat','cool'].includes(value.colour_map)&&typeof value.invert==='boolean');need(value.interval==='manual'?finite(value.low)&&finite(value.high)&&value.high>value.low:value.low===null&&value.high===null);}
    else if(value.action==='pixel') need(integer(value.x,0,2147483647)&&integer(value.y,0,2147483647));
    else if(value.action==='region') need(Array.isArray(value.bounds)&&value.bounds.length===4&&value.bounds.every((v:unknown)=>integer(v,0,2147483647))&&value.bounds[2]>value.bounds[0]&&value.bounds[3]>value.bounds[1]);
    else need(finite(value.threshold_sigma)&&value.threshold_sigma>=1&&value.threshold_sigma<=50);
  }
  return structuredClone(value);
}
function world(value:unknown){need(value===null||Array.isArray(value)&&value.length===2&&value.every(finite));}
function stats(value:unknown,count:number){keys(value,'valid_count missing_count minimum maximum mean median std sum');need(integer(value.valid_count,0,count)&&integer(value.missing_count,0,count)&&value.valid_count+value.missing_count===count);need(['minimum','maximum','mean','median','std','sum'].every(k=>value.valid_count===0?value[k]===null:finite(value[k])));if(value.valid_count)need(value.minimum<=value.maximum&&value.std>=0);}
function column(c:unknown,i?:number){keys(c,'index name format unit');need(integer(c.index,0,255)&&(i===undefined||c.index===i)&&label(c.name)&&label(c.format)&&label(c.unit));}
function cell(v:unknown):boolean{return number(v)||typeof v==='boolean'||label(v)||Array.isArray(v)&&v.length<=100&&v.every(x=>number(x)||typeof x==='boolean'||label(x));}
export function parseAstronomyWorkbench(kind:AstronomyKind,payload:unknown,metadata:unknown,expected:unknown):AstronomyData {
  keys(payload,'view_kind media_type choices selected workbench'+(kind==='tree'?' tree':''));need(payload.view_kind===kind&&payload.media_type==='application/json');
  const selected=validateAstronomyOptions(kind,payload.selected);need(sameAstronomy(selected,validateAstronomyOptions(kind,expected)));
  keys(metadata,'format input_mode source_bytes unpacked_bytes limits value_semantics');const m=metadata;
  need(['fits','tiff'].includes(m.format)&&m.input_mode==='whole'&&integer(m.source_bytes,1,33554432)&&integer(m.unpacked_bytes,1,33554432)&&sameAstronomy(m.limits,ASTRONOMY_LIMITS)&&m.value_semantics==='file-declared scaling; zero-based indices; upper-exclusive regions');
  keys(payload.choices,'datasets geospatial');const choices=payload.choices;need(Array.isArray(choices.datasets)&&choices.datasets.length>0&&choices.datasets.length<=64);let totalColumns=0;
  choices.datasets.forEach((d:unknown,i:number)=>{keys(d,'index name kind shape plane_shape dtype channels columns row_count wcs');need(d.index===i&&label(d.name)&&['empty','image','table','spectrum'].includes(d.kind)&&Array.isArray(d.shape)&&d.shape.length<=8&&d.shape.every((n:unknown)=>integer(n,0,2147483647))&&Array.isArray(d.plane_shape)&&(d.kind==='image'?d.plane_shape.length===2&&d.plane_shape.every((n:unknown)=>integer(n,1,2147483647)):d.plane_shape.length===0)&&label(d.dtype)&&integer(d.channels,1,16)&&integer(d.row_count,0,2147483647)&&Array.isArray(d.columns)&&d.columns.length<=256);d.columns.forEach((c:unknown,j:number)=>column(c,j));totalColumns+=d.columns.length;keys(d.wcs,'celestial axis_types units');need(typeof d.wcs.celestial==='boolean'&&Array.isArray(d.wcs.axis_types)&&d.wcs.axis_types.length<=8&&d.wcs.axis_types.every(label)&&Array.isArray(d.wcs.units)&&d.wcs.units.length<=8&&d.wcs.units.every(label));});need(totalColumns<=256);
  const g=choices.geospatial;if(g!==null){keys(g,'crs bounds bounds_wgs84 resolution bands nodata');need(label(g.crs)&&integer(g.bands,1,16)&&number(g.nodata));for(const [key,n] of [['bounds',4],['resolution',2]] as const)need(Array.isArray(g[key])&&g[key].length===n&&g[key].every(finite));need(g.bounds_wgs84===null||Array.isArray(g.bounds_wgs84)&&g.bounds_wgs84.length===4&&g.bounds_wgs84.every(finite));}
  const datasets=choices.datasets as AstronomyDataset[], w=payload.workbench;need(object(w));
  if(kind==='tree'){keys(w,'action');need(w.action==='inspect'&&sameAstronomy(payload.tree,datasets.map(d=>({path:'/datasets/'+d.index,node_type:'array',attributes:{label:d.name}}))));}
  else {
    const d=datasets[selected.dataset as number];need(d);
    if(kind==='table'){
      keys(w,'action columns rows row_offset column_offset total_rows total_columns');need(d.kind==='table'&&w.action==='table'&&w.row_offset===selected.row_offset&&w.column_offset===selected.column_offset&&w.total_rows===d.row_count&&w.total_columns===d.columns.length&&sameAstronomy(w.columns,d.columns.slice(w.column_offset,w.column_offset+32))&&Array.isArray(w.rows)&&w.rows.length===Math.min(50,d.row_count-w.row_offset));w.rows.forEach((r:unknown)=>need(Array.isArray(r)&&r.length===w.columns.length&&r.every(cell)));
    }else if(kind==='series'){
      keys(w,'action indices values stride total_points');need(d.kind==='spectrum'&&w.action==='spectrum'&&d.shape.length===1&&w.total_points===d.shape[0]&&w.stride===Math.max(1,Math.ceil(w.total_points/2000))&&Array.isArray(w.indices)&&Array.isArray(w.values)&&w.values.length===w.indices.length&&w.indices.length===Math.ceil(w.total_points/w.stride)&&w.indices.length<=2000&&w.indices.every((n:unknown,i:number)=>n===i*w.stride)&&w.values.every(number));
    }else {
      need(d.kind==='image'&&w.action===selected.action&&integer(w.width,1,2147483647)&&integer(w.height,1,2147483647)&&sameAstronomy([w.height,w.width],d.plane_shape)&&[1,3].includes(w.channels)&&w.width*w.height*w.channels<=2097152);
      const a=w.action,basic='action width height channels '; if(m.format==='fits')need(sameAstronomy([w.height,w.width],d.shape.slice(-2))&&selected.band===1&&Array.isArray(selected.slices)&&selected.slices.length===d.shape.length-2&&selected.slices.every((v,i)=>integer(v,0,d.shape[i]!-1))&&w.channels===1);else need(Array.isArray(selected.slices)&&selected.slices.length===0&&integer(selected.band,0,d.channels)&&(selected.band!==0||d.channels>=3)&&w.channels===(selected.band===0?3:1));
      if(a==='render') {
        keys(w,basic+'image_base64 render_width render_height display_limits statistics histogram wcs');need(integer(w.render_width,1,Math.min(1024,w.width))&&integer(w.render_height,1,Math.min(1024,w.height))&&typeof w.image_base64==='string'&&w.image_base64.length<=4000000&&/^[A-Za-z0-9+/]+={0,2}$/.test(w.image_base64));
        const bytes=atob(w.image_base64);need(bytes.startsWith('\x89PNG\r\n\x1a\n')&&bytes.length>=33);const view=new DataView(Uint8Array.from(bytes.slice(16,24),c=>c.charCodeAt(0)).buffer);need(view.getUint32(0)===w.render_width&&view.getUint32(4)===w.render_height);
        need(Array.isArray(w.display_limits)&&w.display_limits.length===w.channels&&w.display_limits.every((p:unknown)=>Array.isArray(p)&&p.length===2&&p.every(finite)&&p[1]>p[0]));stats(w.statistics,w.width*w.height*w.channels);
        keys(w.histogram,'counts edges sample_count');const h=w.histogram;need(Array.isArray(h.counts)&&h.counts.length===96&&h.counts.every((n:unknown)=>integer(n,0,500000))&&Array.isArray(h.edges)&&h.edges.length===97&&h.edges.every(finite)&&h.edges.every((n:number,i:number)=>!i||n>h.edges[i-1])&&integer(h.sample_count,0,500000)&&h.counts.reduce((a:number,b:number)=>a+b,0)<=h.sample_count);keys(w.wcs,'corners');need(w.wcs.corners===null||Array.isArray(w.wcs.corners)&&w.wcs.corners.length===4);(w.wcs.corners||[]).forEach(world);
      }else if(a==='pixel'){keys(w,basic+'x y value world');need(w.x===selected.x&&w.y===selected.y&&integer(w.x,0,w.width-1)&&integer(w.y,0,w.height-1));need(w.channels===1?number(w.value):Array.isArray(w.value)&&w.value.length===3&&w.value.every(number));world(w.world);}
      else if(a==='region'){keys(w,basic+'bounds pixel_count statistics');need(sameAstronomy(w.bounds,selected.bounds)&&w.bounds[2]<=w.width&&w.bounds[3]<=w.height&&w.pixel_count===(w.bounds[2]-w.bounds[0])*(w.bounds[3]-w.bounds[1])*w.channels);stats(w.statistics,w.pixel_count);}
      else {keys(w,basic+'background noise threshold sources truncated');need(finite(w.background)&&finite(w.noise)&&w.noise>=0&&finite(w.threshold)&&w.threshold===w.background+(selected.threshold_sigma as number)*w.noise&&typeof w.truncated==='boolean'&&Array.isArray(w.sources)&&w.sources.length<=2000);const seen=new Set<string>();w.sources.forEach((p:unknown,i:number)=>{keys(p,'id x y peak snr world');need(p.id===i+1&&integer(p.x,0,w.width-1)&&integer(p.y,0,w.height-1)&&finite(p.peak)&&finite(p.snr)&&!seen.has(`${p.x},${p.y}`));seen.add(`${p.x},${p.y}`);world(p.world);});}
    }
  }
  return {datasets,geospatial:g,selected,result:w,sourceBytes:m.source_bytes,format:m.format};
}
/** Preserve missing-value gaps instead of drawing through an invalid sample. */
export function astronomySpectrumSegments(indices:number[],values:(number|null)[]):string[] {
  const valid=values.filter((v):v is number=>v!==null&&Number.isFinite(v));if(!valid.length)return [];
  const lo=Math.min(...valid),hi=Math.max(...valid),width=Math.max(1,indices[indices.length-1]??1),segments:string[]=[];let points:string[]=[];
  values.forEach((v,i)=>{if(v===null){if(points.length)segments.push(points.join(' '));points=[];}else points.push(`${indices[i]!/width*1000},${400-(v-lo)/(hi-lo||1)*380}`);});if(points.length)segments.push(points.join(' '));return segments;
}
