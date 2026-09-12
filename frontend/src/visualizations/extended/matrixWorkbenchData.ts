import type { VisualizationResult } from '../runtime';

export const MATRIX_WARNING='预览仅显示选定分量与索引切片；等步长抽样及统计不代表整个源数组。稀疏结构模式按显示网格分箱非零项；非有限值显示为空。';
export const MATRIX_LIMITS={max_input_bytes:134217728,max_output_bytes:8388608,max_arrays:512,max_dimensions:16,max_points:512,max_values:262144,max_curve_points:4096};
export interface MatrixArray {id:string;name:string;shape:number[];dtype:string;sparse:boolean}
export interface MatrixCurve {variable:string;title:string;kind:'line'|'scatter';x:number[];y:number[]}
export interface MatrixPlane {values:(number|null)[][];rows:number[];columns:number[];shape:number[];sampled:boolean;row_step:number;column_step:number;row_range:number[];column_range:number[];structure:boolean;nonzero:number;finite_count:number;count:number;minimum:number|null;maximum:number|null;mean:number|null;standard_deviation:number|null;row_profile:(number|null)[];column_profile:(number|null)[];non_finite:number}
export interface MatrixData {arrays:MatrixArray[];plane:MatrixPlane|null;curves:MatrixCurve[]}
export interface MatrixSelection extends Record<string,unknown> {variable:string;axes:number[];indices:number[];component:string;row_range:number[]|null;column_range:number[]|null;max_points:number;structure:boolean}
const fail=():never=>{throw new Error('矩阵响应与当前文件或选择不一致。');};
const need=(x:unknown):void=>{if(!x)fail();};
const rec=(x:unknown):x is Record<string,unknown>=>!!x&&typeof x==='object'&&!Array.isArray(x);
const exact=(x:unknown,fields:string):x is Record<string,unknown>=>rec(x)&&Object.keys(x).sort().join(' ')==fields.split(' ').sort().join(' ');
const int=(x:unknown,lo=0,hi=Number.MAX_SAFE_INTEGER):x is number=>typeof x==='number'&&Number.isSafeInteger(x)&&x>=lo&&x<=hi;
const num=(x:unknown):x is number=>typeof x==='number'&&Number.isFinite(x);
const text=(x:unknown)=>typeof x==='string'&&x.length>=1&&x.length<=128&&!/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\/i.test(x);
const same=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);
const range=(r:number[],step:number)=>Array.from({length:Math.ceil((r[1]-r[0])/step)},(_,i)=>r[0]+i*step);

export function matrixSelection(info:MatrixArray,selection:MatrixSelection):MatrixSelection {
 need(exact(selection,'variable axes indices component row_range column_range max_points structure'));
 need(selection.variable===info.id&&Array.isArray(selection.axes)&&selection.axes.length===2&&selection.axes.every(n=>int(n,0,15))&&selection.axes[0]!==selection.axes[1]);
 need(Array.isArray(selection.indices)&&selection.indices.length===info.shape.length&&selection.indices.every((n,i)=>int(n,0,info.shape[i]-1)));
 need(info.shape.length>=2?selection.axes.every(n=>n<info.shape.length):same(selection.axes,[0,1]));
 need(!info.sparse||same(selection.axes,[0,1]));need(['real','imaginary','magnitude','phase'].includes(selection.component)&&typeof selection.structure==='boolean'&&int(selection.max_points,16,512));
 const rows=info.shape.length>=2?info.shape[selection.axes[0]]:1,cols=info.shape.length>=2?info.shape[selection.axes[1]]:info.shape[0]??1;
 for(const [r,n] of [[selection.row_range,rows],[selection.column_range,cols]] as [number[]|null,number][])need(r===null||Array.isArray(r)&&r.length===2&&int(r[0],0,n-1)&&int(r[1],r[0]+1,n));
 return structuredClone(selection);
}
export function matrixCatalogIdentity(data:MatrixData){return JSON.stringify(data.arrays);}

export function parseMatrixData(result:VisualizationResult,kind:'tree'|'image'|'series',options:Record<string,unknown>,size?:number,filename?:string,pluginId='viz-matrix-workbench'):MatrixData {
 need(result.contract_version===2&&result.plugin_id===pluginId&&/^[a-f0-9]{64}$/.test(result.version)&&/^[a-f0-9]{64}$/.test(result.revision));
 need(result.kind===(kind==='image'?'array':kind)&&same(result.warnings,[MATRIX_WARNING])&&typeof result.sampled==='boolean');
 const p=result.payload,m=result.metadata;
 need(exact(p,'media_type matrix selected view_kind')&&p.media_type==='application/json'&&p.view_kind===kind&&rec(p.selected)&&same(p.selected,options));
 need(exact(m,'format input_mode source_bytes limits matrix_semantics')&&['npy','npz','mat','mtx'].includes(String(m.format))&&m.input_mode==='whole'&&int(m.source_bytes,1,134217728));
 need(size===undefined||size===m.source_bytes);need(!filename||filename.toLowerCase().endsWith(`.${m.format}`));
 const limits=m.limits;need(rec(limits)&&Object.keys(MATRIX_LIMITS).every(k=>limits[k]===MATRIX_LIMITS[k as keyof typeof MATRIX_LIMITS])&&Object.keys(limits).length===Object.keys(MATRIX_LIMITS).length);
 need(m.matrix_semantics==='selected component; sampled statistics; sparse structure bins nonzero entries');
 need(exact(p.matrix,'arrays plane curves'));const data=p.matrix as unknown as MatrixData;
 need(Array.isArray(data.arrays)&&data.arrays.length>=1&&data.arrays.length<=512);
 data.arrays.forEach((a,i)=>{need(exact(a,'id name shape dtype sparse')&&a.id===`array-${i}`&&text(a.name)&&typeof a.sparse==='boolean');need(Array.isArray(a.shape)&&a.shape.length<=16&&a.shape.every(n=>int(n,1,2147483647))&&a.shape.reduce((a,b)=>a*b,1)<=Number.MAX_SAFE_INTEGER&&(!a.sparse||a.shape.length===2));need(/^(bool|u?int(8|16|32|64)|float(16|32|64|128)|complex(64|128|256))$/.test(a.dtype));});
 need(Array.isArray(data.curves)&&data.curves.length<=6);
 if(kind!=='series')need(data.curves.length===0);else{need(Object.keys(options).length===0);const seen=new Set<string>();data.curves.forEach(c=>{need(exact(c,'variable title kind x y'));const allowed:Record<string,string[]>={S:['奇异值谱','已保存奇异值的累计能量比例'],singular_values:['奇异值谱','已保存奇异值的累计能量比例'],residual_history:['迭代残差'],eigenvalues:['特征值分布']};need(allowed[c.variable]?.includes(c.title)&&!seen.has(`${c.variable}:${c.title}`));seen.add(`${c.variable}:${c.title}`);need(c.kind===(c.variable==='eigenvalues'?'scatter':'line')&&Array.isArray(c.x)&&Array.isArray(c.y)&&c.x.length>=1&&c.x.length<=4096&&c.y.length===c.x.length&&c.x.every(num)&&c.y.every(num));need(c.kind==='scatter'||c.x.every((n,i)=>n===i));});}
 if(kind!=='image'){need(data.plane===null&&result.sampled===false);if(kind==='tree')need(Object.keys(options).length===0);return data;}
 const a=data.arrays.find(a=>a.id===options.variable);need(a);const s=matrixSelection(a!,options as MatrixSelection),v=data.plane;
 need(exact(v,'values rows columns shape sampled row_step column_step row_range column_range structure nonzero finite_count count minimum maximum mean standard_deviation row_profile column_profile non_finite'));
 const plane=v!,row=s.row_range??[0,a!.shape.length>=2?a!.shape[s.axes[0]]:1],col=s.column_range??[0,a!.shape.length>=2?a!.shape[s.axes[1]]:a!.shape[0]??1];
 const rs=Math.max(1,Math.ceil((row[1]-row[0])/s.max_points)),cs=Math.max(1,Math.ceil((col[1]-col[0])/s.max_points)),rows=range(row,rs),cols=range(col,cs);
 need(same(plane.rows,rows)&&same(plane.columns,cols)&&same(plane.shape,a!.shape)&&same(plane.row_range,row)&&same(plane.column_range,col)&&plane.row_step===rs&&plane.column_step===cs);
 need(plane.sampled===(rs>1||cs>1)&&result.sampled===plane.sampled&&plane.structure===s.structure&&int(plane.nonzero));
 need(Array.isArray(plane.values)&&plane.values.length===rows.length&&plane.values.every(r=>Array.isArray(r)&&r.length===cols.length&&r.every(n=>n===null||num(n))));
 const flat=plane.values.flat(),finite=flat.filter((n):n is number=>n!==null);need(flat.length<=262144&&plane.count===flat.length&&plane.finite_count===finite.length&&plane.non_finite===flat.length-finite.length);
 if(s.structure)need(flat.every(n=>n===0||n===1));
 // Reductions, not argument spreading: the 512² contract exceeds engine call limits.
 need(plane.minimum===(finite.length?finite.reduce((a,b)=>Math.min(a,b)):null)&&plane.maximum===(finite.length?finite.reduce((a,b)=>Math.max(a,b)):null));
 for(const n of [plane.mean,plane.standard_deviation])need(finite.length?num(n):n===null);need(plane.standard_deviation===null||plane.standard_deviation>=0);
 for(const [profile,length] of [[plane.row_profile,rows.length],[plane.column_profile,cols.length]] as [(number|null)[],number][])need(Array.isArray(profile)&&profile.length===length&&profile.every(n=>n===null||num(n)));
 return data;
}

/** Split null gaps instead of drawing a fictitious connecting segment. */
export function matrixProfileSegments(values:(number|null)[]):string[] {
 const finite=values.filter((v):v is number=>v!==null&&Number.isFinite(v));if(!finite.length)return [];
 const low=Math.min(...finite),high=Math.max(...finite),scale=Math.max(Math.abs(low),Math.abs(high))||1,span=(high/scale-low/scale)||1;
 const segments:string[]=[],current:string[]=[];values.forEach((v,i)=>{if(v===null){if(current.length)segments.push(current.splice(0).join(' '));}else current.push(`${10+i*380/Math.max(1,values.length-1)},${90-(v/scale-low/scale)/span*80}`);});if(current.length)segments.push(current.join(' '));return segments;
}
export function matrixScatterPoints(x:number[],y:number[]):number[][] {
 if(!x.length)return [];const xs=Math.max(...x.map(Math.abs))||1,ys=Math.max(...y.map(Math.abs))||1,xx=x.map(v=>v/xs),yy=y.map(v=>v/ys),xmin=Math.min(...xx),ymin=Math.min(...yy),dx=Math.max(...xx)-xmin||1,dy=Math.max(...yy)-ymin||1;
 return xx.map((v,i)=>[10+(v-xmin)/dx*380,90-(yy[i]-ymin)/dy*80]);
}
