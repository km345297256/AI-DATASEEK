import type { VisualizationResult } from '../runtime';

export interface AlignmentSelection { reference:number; start:number; end:number; max_reads:number; bins:number }
export interface AlignmentReference { id:number; name:string; length:number }
export interface Span { start:number; end:number }
export interface AlignmentRead extends Span {
 name:string; reverse:boolean; mapq:number; cigar:string; flag:number; paired:boolean; duplicate:boolean; secondary:boolean;
 supplementary:boolean; read1:boolean; read2:boolean; mate_start:number|null; mate_reference:string|null; template_length:number;
 read_group:string|null; nm:number|null; md:string|null; blocks:Span[]; mismatches:Array<{position:number;query:string;reference:string}>;
 insertions:Array<{position:number;length:number;sequence:string}>; deletions:Array<Span&{length:number}>; splices:Array<Span&{length:number}>;
 mismatch_available:boolean; detail_truncated:boolean;
}
export interface AlignmentData {
 references:AlignmentReference[]; sortOrder:string; readGroups:number; sourceBytes:number; format:string;
 selected:AlignmentSelection|null; reads:AlignmentRead[]; coverage:Array<Span&{covered_bases:number;depth:number}>;
 recordsScanned:number; matchedReads:number; scanComplete:boolean; readsTruncated:boolean;
}
function need(ok:unknown):asserts ok { if(!ok) throw new Error('比对预览协议或数据选择无效。'); }
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
const exact=(v:unknown,keys:string)=>object(v)&&Object.keys(v).sort().join(' ' )===keys.split(' ').sort().join(' ');
const int=(v:unknown,lo=0,hi=2147483647):v is number=>typeof v==='number'&&Number.isInteger(v)&&v>=lo&&v<=hi;
const label=(v:unknown):v is string=>typeof v==='string'&&v.length>0&&v.length<=256&&!/[\x00-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|[A-Za-z]:\\|https?:\/\/|file:\/\/)/.test(v);
export function validateAlignmentSelection(value:AlignmentSelection,refs:AlignmentReference[]):AlignmentSelection {
 need(int(value.reference,0,255)&&refs[value.reference]&&int(value.start)&&int(value.end,1,refs[value.reference]!.length));
 need(value.end>value.start&&value.end-value.start<=2000000&&int(value.max_reads,1,2000)&&int(value.bins,20,1000));return {...value};
}
export function parseAlignment(result:VisualizationResult,expected?:AlignmentSelection):AlignmentData {
 const p=result.payload,m=result.metadata;need((result.kind==='tree'||result.kind==='table')&&p.view_kind===(expected?'table':'tree'));
 need(exact(p,`view_kind media_type choices selected ${expected?'alignment':'tree'}`)&&p.media_type==='application/json');
 need(exact(m,'format source_bytes input_mode coordinate_system limits')&&exact(m.limits,'references records region_width reads bins query_bases'));
 need(JSON.stringify(Object.entries(m.limits as Record<string,unknown>).sort())===JSON.stringify(Object.entries({references:256,records:100000,region_width:2000000,reads:2000,bins:1000,query_bases:4000000}).sort()));
 need(object(p.choices)&&object(p.selected)&&['sam','bam','cram'].includes(String(m.format))&&int(m.source_bytes,1,64*1024**2)&&m.coordinate_system==='0-based-half-open'&&m.input_mode==='whole');
 const c=p.choices;need(Array.isArray(c.references)&&c.references.length>0&&c.references.length<=256&&['unknown','unsorted','coordinate','queryname'].includes(String(c.sort_order))&&int(c.read_groups,0,10000));
 need(exact(c,'references sort_order read_groups'));
 const refs=c.references as AlignmentReference[];refs.forEach((r,i)=>need(object(r)&&r.id===i&&label(r.name)&&int(r.length,1)));need(new Set(refs.map(r=>r.name)).size===refs.length);
 const out:AlignmentData={references:refs,sortOrder:String(c.sort_order),readGroups:c.read_groups,sourceBytes:m.source_bytes,format:String(m.format),selected:null,reads:[],coverage:[],recordsScanned:0,matchedReads:0,scanComplete:true,readsTruncated:false};
 if(!expected){need(result.kind==='tree'&&Object.keys(p.selected).length===0&&result.sampled===false&&Array.isArray(p.tree)&&p.tree.length===refs.length);return out}
 validateAlignmentSelection(expected,refs);need(Object.keys(p.selected).length===5&&Object.entries(expected).every(([key,v])=>p.selected && (p.selected as Record<string,unknown>)[key]===v));
 need(result.kind==='table'&&object(p.alignment));const a=p.alignment;
 need(exact(a,'reads coverage records_scanned matched_reads scan_complete reads_truncated'));
 need(int(a.records_scanned,0,100000)&&int(a.matched_reads,0,a.records_scanned)&&typeof a.scan_complete==='boolean'&&typeof a.reads_truncated==='boolean');
 need(Array.isArray(a.reads)&&a.reads.length===Math.min(a.matched_reads,expected.max_reads)&&a.reads_truncated===(a.matched_reads>a.reads.length)&&result.sampled===(!a.scan_complete||a.reads_truncated));
 const span=(s:unknown,lo=0,hi=2147483647)=>object(s)&&int(s.start,lo,hi)&&int(s.end,(s.start as number)+1,hi);
 for(const r of a.reads){
  need(exact(r,'name start end reverse mapq cigar flag paired duplicate secondary supplementary read1 read2 mate_start mate_reference template_length read_group nm md blocks mismatches insertions deletions splices mismatch_available detail_truncated'));
  need(object(r)&&span(r)&&label(r.name)&&int(r.flag,0,65535)&&int(r.mapq,0,255)&&typeof r.cigar==='string'&&r.cigar.length<=2048&&/^(?:[1-9][0-9]{0,8}[MIDNSHP=X])+$/.test(r.cigar));
  need((r.start as number)<expected.end&&(r.end as number)>expected.start&&(r.end as number)<=refs[expected.reference]!.length);
  for(const [key,bit] of Object.entries({reverse:16,paired:1,duplicate:1024,secondary:256,supplementary:2048,read1:64,read2:128}))need(r[key]===!!(r.flag&bit));
  need(r.mate_start===null||int(r.mate_start));need(r.mate_reference===null||label(r.mate_reference));need(int(r.template_length,-2147483648));need(r.read_group===null||label(r.read_group));need(r.nm===null||int(r.nm));
  need(r.md===null||typeof r.md==='string'&&r.md.length<=2048&&/^[0-9ACGTNacgtn^]+$/.test(r.md));need(typeof r.mismatch_available==='boolean'&&typeof r.detail_truncated==='boolean');
  for(const key of ['blocks','deletions','splices']){const spans=r[key];need(Array.isArray(spans)&&spans.length<=(key==='blocks'?2048:100));for(const s of spans as Record<string,unknown>[])need(span(s,r.start as number,r.end as number)&&(key==='blocks'||s.length===(s.end as number)-(s.start as number)))}
  need(Array.isArray(r.mismatches)&&r.mismatches.length<=500&&(r.mismatch_available||r.mismatches.length===0));for(const q of r.mismatches)need(object(q)&&int(q.position,r.start as number,(r.end as number)-1)&&typeof q.query==='string'&&/^[A-Z=]$/.test(q.query)&&typeof q.reference==='string'&&/^[A-Z=]$/.test(q.reference));
  need(Array.isArray(r.insertions)&&r.insertions.length<=100);for(const x of r.insertions)need(object(x)&&int(x.position,r.start as number,r.end as number)&&int(x.length,1,200000)&&typeof x.sequence==='string'&&x.sequence.length<=Math.min(x.length,100)&&/^[A-Z=]*$/.test(x.sequence));
  verifyReadGeometry(r as unknown as AlignmentRead);
 }
 const width=Math.ceil((expected.end-expected.start)/expected.bins);need(Array.isArray(a.coverage)&&a.coverage.length===Math.ceil((expected.end-expected.start)/width));
 a.coverage.forEach((b,i)=>{need(object(b)&&b.start===expected.start+i*width&&b.end===Math.min(expected.end,expected.start+(i+1)*width)&&int(b.covered_bases,0,4000000)&&typeof b.depth==='number'&&Number.isFinite(b.depth)&&Math.abs(b.depth-b.covered_bases/((b.end as number)-(b.start as number)))<1e-8)});
 need(a.coverage.every(b=>exact(b,'start end covered_bases depth')&&b.covered_bases<=(a.matched_reads as number)*(b.end-b.start))&&a.coverage.reduce((n,b)=>n+b.covered_bases,0)<=4000000);
 const shown=alignmentCoveredBins(a.reads as unknown as AlignmentRead[],expected.start,expected.end,width);
 a.coverage.forEach((b,i)=>need(b.covered_bases>=shown[i]!&&(a.reads_truncated||b.covered_bases===shown[i])));
 return {...out,selected:{...expected},reads:a.reads as unknown as AlignmentRead[],coverage:a.coverage as AlignmentData['coverage'],recordsScanned:a.records_scanned,matchedReads:a.matched_reads,scanComplete:a.scan_complete,readsTruncated:a.reads_truncated};
}
export function alignmentCoveredBins(reads:AlignmentRead[],start:number,end:number,width:number){
 const count=Math.ceil((end-start)/width),partial=Array<number>(count).fill(0),delta=Array<number>(count+1).fill(0);
 for(const read of reads)for(const block of read.blocks){const lo=Math.max(start,block.start),hi=Math.min(end,block.end);if(hi<=lo)continue;
  const first=Math.floor((lo-start)/width),last=Math.floor((hi-1-start)/width);
  if(first===last)partial[first]!+=hi-lo;else{partial[first]!+=start+(first+1)*width-lo;partial[last]!+=hi-(start+last*width);delta[first+1]!++;delta[last]!--;}
 }
 let depth=0;for(let i=0;i<count;i++){depth+=delta[i]!;partial[i]!+=depth*(Math.min(end,start+(i+1)*width)-(start+i*width))}return partial;
}
function verifyReadGeometry(read:AlignmentRead){
 let pos=read.start;const blocks:Span[]=[],insertions:Array<{position:number;length:number}>=[],deletions:Array<Span&{length:number}>=[],splices:Array<Span&{length:number}>=[];
 for(const match of read.cigar.matchAll(/([1-9][0-9]{0,8})([MIDNSHP=X])/g)){const n=Number(match[1]),code=match[2]!;
  if('M=X'.includes(code)){blocks.push({start:pos,end:pos+n});pos+=n}
  else if(code==='I')insertions.push({position:pos,length:n});
  else if(code==='D'||code==='N'){(code==='D'?deletions:splices).push({start:pos,end:pos+n,length:n});pos+=n}
 }
 const same=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);
 need(pos===read.end&&same(blocks,read.blocks)&&same(deletions.slice(0,100),read.deletions)&&same(splices.slice(0,100),read.splices)&&same(insertions.slice(0,100),read.insertions.map(i=>({position:i.position,length:i.length}))));
 need(!read.mismatch_available||read.md!==null);need(read.mismatches.every(m=>blocks.some(b=>b.start<=m.position&&m.position<b.end)));
}
export function packAlignmentReads(reads:AlignmentRead[],start:number,end:number){
 const ends:number[]=[];const gap=(end-start)*.003;
 return reads.map((read,index)=>({read,index})).sort((a,b)=>a.read.start-b.read.start||a.index-b.index).map(entry=>{
  let lane=ends.findIndex(right=>right+gap<=Math.max(start,entry.read.start));if(lane<0)lane=ends.length;
  ends[lane]=Math.min(end,entry.read.end);return {...entry,lane};
 });
}
export function alignmentWindow(start:number,end:number,length:number,factor:number,anchor=.5):Span {
 const width=Math.min(length,2000000,Math.max(20,Math.round((end-start)*factor)));
 const left=Math.max(0,Math.min(length-width,Math.round(start+(end-start)*anchor-width*anchor)));
 return {start:left,end:left+width};
}
