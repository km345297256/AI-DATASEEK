/** Inert, fixed scientific contracts. Nothing here resolves a URL or renderer. */
import type { VisualizationResult } from '../runtime';
export type BioReader = 'sequence-browser' | 'genome-tracks' | 'blast-hits';
export type BioKind = 'tree' | 'table' | 'map';
export const BIO_WARNINGS: Record<BioReader, string> = {
  'sequence-browser': '仅有界读取未压缩 FASTA / FASTQ；序列以大写显示，GC/N 是字符占比。FASTQ 质量按用户明确选择的编码换算，不自动猜测；模体为字面匹配。',
  'genome-tracks': '仅绘制当前文件的已存储坐标与信号；内部坐标为 0 基半开区间，界面显示 1 基位置。参考基因组未指定，不获取外部轨道、不插值信号；详情有界截取并移除路径。',
  'blast-hits': '只读取标准 12 列 BLAST tabular，或明确声明附加 qlen 的 13 列。未声明 Query 全长时覆盖度未知，图示范围仅为观测命中；不以命中跨度猜测全长。',
};
export const BIO_LIMITS = {
  'sequence-browser': {max_records:256,max_bases:8000000,max_window:1000,max_matches:10000},
  'genome-tracks': {max_chromosomes:256,max_records:100000,max_features:3000},
  'blast-hits': {max_queries:256,max_hits:20000,max_page:500},
};
const formats: Record<BioReader, string[]> = {
  'sequence-browser': ['fa','fasta','fna','ffn','frn','faa','fastq','fq'],
  'genome-tracks': ['vcf','gff','gff3','gtf','bed','bedgraph','wig'],
  'blast-hits': ['blast','blast6','m8','blasttab','tab'],
};
export interface SequenceRecord { id:number;name:string;length:number;gc_count:number;n_count:number;gc_bins:number[];qualities:boolean }
/** All track coordinates are zero-based half-open, including zero-length BED insertions. */
export interface GenomeChromosome {id:number;name:string;start:number;end:number;records:number}
export interface GenomeFeature {id:number;chromosome:number;start:number;end:number;label:string;track:'variant'|'annotation'|'interval'|'signal';strand:'+'|'-'|'.'|'?';value:number|null;detail:string}
export interface BlastQuery {id:number;name:string;length:number|null;extent:number;hits:number}
export interface BlastHit {id:number;query:number;subject:string;identity:number;alignment_length:number;mismatches:number;gap_opens:number;qstart:number;qend:number;sstart:number;send:number;evalue:string;bitscore:number;coverage:number|null}
export interface BioData {
  reader:BioReader; metadata:Record<string,any>; selected:Record<string,any>;
  records:SequenceRecord[];chromosomes:GenomeChromosome[];queries:BlastQuery[];
  sequence?:{bases:string;qualities:number[]|null;quality_stats:{sum:number;low_count:number}|null;search:{positions:number[];total:number;truncated:boolean}};
  tracks?:GenomeFeature[];
  hits?:{rows:BlastHit[];total:number;unknown_coverage:number};
}
type Obj = Record<string, any>;
function need(ok: unknown): asserts ok {if(!ok)throw new Error('生物数据结果与所选文件、目录或区域不一致。');}
function keys(v:unknown,names:string):v is Obj {const n=names?names.split(' '):[];return !!v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).length===n.length&&n.every(k=>Object.prototype.hasOwnProperty.call(v,k));}
const int=(v:unknown,lo:number,hi:number):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=lo&&v<=hi;
const num=(v:unknown,lo=-1e12,hi=1e12):v is number=>typeof v==='number'&&Number.isFinite(v)&&v>=lo&&v<=hi;
const name=(v:unknown,max=128,empty=false):v is string=>typeof v==='string'&&(empty||v.length>0)&&v.length<=max&&!/[<>\x00-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
function canonical(v:unknown,depth=0):unknown {need(depth<=12);if(Array.isArray(v))return v.map(x=>canonical(x,depth+1));if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,canonical(x,depth+1)]));return v;}
const same=(a:unknown,b:unknown)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
export function bioCatalogIdentity(d:BioData) {return JSON.stringify(canonical([d.reader,d.metadata,d.records,d.chromosomes,d.queries]));}
export function bioSelection(reader:BioReader,kind:BioKind,o:Record<string,any>):Record<string,unknown> {
  if(kind==='tree'){need(keys(o,''));return {};}
  need(kind===(reader==='genome-tracks'?'map':'table'));
  if(reader==='sequence-browser')need(keys(o,'record start count motif quality_encoding')&&int(o.record,0,255)&&int(o.start,1,8000000)&&int(o.count,1,1000)&&typeof o.motif==='string'&&/^[A-Z*.\-]{0,64}$/.test(o.motif)&&(o.quality_encoding===null||['phred33','phred64'].includes(o.quality_encoding)));
  else if(reader==='genome-tracks')need(keys(o,'chromosome start end')&&int(o.chromosome,0,255)&&int(o.start,0,2147483646)&&int(o.end,o.start+1,2147483647));
  else need(keys(o,'query min_identity min_coverage offset count')&&(o.query===null||int(o.query,0,255))&&num(o.min_identity,0,100)&&num(o.min_coverage,0,100)&&int(o.offset,0,20000)&&int(o.count,1,500));
  return {...o};
}
export function parseBioData(r:VisualizationResult,reader:BioReader,kind:BioKind,options:Record<string,unknown>,size?:number,filename?:string,expectedPluginId?:string):BioData {
  const field=kind==='tree'?'tree':reader==='sequence-browser'?'sequence':reader==='genome-tracks'?'tracks':'hits';
  const publicKind=kind==='map'?'features':kind;
  // requestVisualization has already bound plugin_id to the enabled descriptor;
  // this domain validator checks the inert payload, not a hard-coded plugin ID.
  need(r.contract_version===2&&r.kind===publicKind&&/^[a-f0-9]{64}$/.test(r.version)&&/^[a-f0-9]{64}$/.test(r.revision)&&r.sampled===false&&same(r.warnings,[BIO_WARNINGS[reader]]));
  need(expectedPluginId===undefined||r.plugin_id===expectedPluginId);
  const p:Obj=r.payload,m:Obj=r.metadata;bioSelection(reader,kind,options);
  need(keys(p,`media_type choices selected view_kind ${field}`)&&p.media_type==='application/json'&&p.view_kind===kind&&same(p.selected,options));
  need(keys(m,'format input_mode source_bytes records total_units labels_redacted limits')&&formats[reader].includes(m.format)&&m.input_mode==='whole'&&int(m.source_bytes,1,16777216)&&(size===undefined||m.source_bytes===size)&&(filename===undefined||filename.split('.').pop()?.toLowerCase()===m.format));
  need(int(m.records,1,100000)&&int(m.total_units,1,8000000)&&int(m.labels_redacted,0,1000000)&&same(m.limits,BIO_LIMITS[reader]));
  const d:BioData={reader,metadata:m,selected:p.selected,records:[],chromosomes:[],queries:[]};
  let tree:unknown[]=[];
  if(reader==='sequence-browser'){
    need(keys(p.choices,'records')&&Array.isArray(p.choices.records)&&p.choices.records.length>=1&&p.choices.records.length<=256);
    d.records=p.choices.records.map((x:unknown,i:number)=>{
      need(keys(x,'id name length gc_count n_count gc_bins qualities')&&int(x.id,i,i)&&name(x.name)&&int(x.length,1,8000000)&&int(x.gc_count,0,x.length)&&int(x.n_count,0,x.length-x.gc_count)&&typeof x.qualities==='boolean'&&x.qualities===['fq','fastq'].includes(m.format));
      need(Array.isArray(x.gc_bins)&&x.gc_bins.length===Math.min(x.length,120));const n=x.length,bins=x.gc_bins.length;
      need(x.gc_bins.every((b:unknown,j:number)=>int(b,0,Math.floor((j+1)*n/bins)-Math.floor(j*n/bins)))&&x.gc_bins.reduce((s:number,b:number)=>s+b,0)===x.gc_count);return x as SequenceRecord;
    });
    need(m.records===d.records.length&&m.total_units===d.records.reduce((s,x)=>s+x.length,0));
    tree=d.records.map(x=>({path:`/records/${x.id}`,node_type:'array',attributes:{label:x.name}}));
    if(kind!=='tree'){
      const o=p.selected,c=d.records[o.record];need(c&&o.start<=c.length&&(c.qualities?['phred33','phred64'].includes(o.quality_encoding):o.quality_encoding===null));const s=p.sequence,n=Math.min(o.count,c.length-o.start+1);
      need(keys(s,'bases qualities quality_stats search')&&typeof s.bases==='string'&&/^[A-Z*.\-]+$/.test(s.bases)&&s.bases.length===n);
      if(c.qualities){const hi=o.quality_encoding==='phred33'?93:62;need(Array.isArray(s.qualities)&&s.qualities.length===n&&s.qualities.every((v:unknown)=>int(v,0,hi))&&keys(s.quality_stats,'sum low_count')&&int(s.quality_stats.sum,0,hi*c.length)&&int(s.quality_stats.low_count,0,c.length));if(n===c.length)need(s.quality_stats.sum===s.qualities.reduce((a:number,b:number)=>a+b,0)&&s.quality_stats.low_count===s.qualities.filter((q:number)=>q<20).length);}
      else need(s.qualities===null&&s.quality_stats===null);
      const search=s.search;need(keys(search,'positions total truncated')&&Array.isArray(search.positions)&&int(search.total,0,c.length)&&search.positions.length===Math.min(search.total,10000)&&typeof search.truncated==='boolean'&&search.truncated===(search.total>10000));
      need(search.positions.every((v:unknown,i:number)=>int(v,1,c.length-o.motif.length+1)&&(i===0||v>search.positions[i-1])));if(!o.motif)need(search.total===0);
      for(const pos of search.positions){const a=pos-o.start;if(a>=0&&a<=n-o.motif.length)need(s.bases.slice(a,a+o.motif.length)===o.motif);}
      d.sequence=s as BioData['sequence'];
    }
  } else if(reader==='genome-tracks') {
    need(keys(p.choices,'chromosomes')&&Array.isArray(p.choices.chromosomes)&&p.choices.chromosomes.length>=1&&p.choices.chromosomes.length<=256);
    d.chromosomes=p.choices.chromosomes.map((x:unknown,i:number)=>{need(keys(x,'id name start end records')&&int(x.id,i,i)&&name(x.name)&&int(x.start,0,2147483647)&&int(x.end,x.start,2147483647)&&int(x.records,1,100000));return x as GenomeChromosome;});
    need(new Set(d.chromosomes.map(x=>x.name)).size===d.chromosomes.length&&d.chromosomes.reduce((s,x)=>s+x.records,0)===m.records&&m.total_units===m.records);
    tree=d.chromosomes.map(x=>({path:`/chromosomes/${x.id}`,node_type:'array',attributes:{label:x.name}}));
    if(kind!=='tree'){
      const o=p.selected,c=d.chromosomes[o.chromosome];need(c&&Array.isArray(p.tracks)&&p.tracks.length<=Math.min(c.records,3000));const ids=new Set<number>();
      d.tracks=p.tracks.map((x:unknown)=>{need(keys(x,'id chromosome start end label track strand value detail')&&int(x.id,0,m.records-1)&&!ids.has(x.id)&&int(x.chromosome,c.id,c.id)&&int(x.start,c.start,c.end)&&int(x.end,x.start,c.end));ids.add(x.id);
        const expectedTrack=['wig','bedgraph'].includes(m.format)?'signal':m.format==='vcf'?'variant':m.format==='bed'?'interval':'annotation';
        need(x.start===x.end?o.start<=x.start&&x.start<o.end:x.start<o.end&&x.end>o.start);need(name(x.label)&&name(x.detail,256,true)&&x.track===expectedTrack&&['+','-','.','?'].includes(x.strand)&&(x.value===null||num(x.value))&&(x.track!=='signal'||x.value!==null));return x as GenomeFeature;});
    }
  } else {
    need(keys(p.choices,'queries')&&Array.isArray(p.choices.queries)&&p.choices.queries.length>=1&&p.choices.queries.length<=256);
    d.queries=p.choices.queries.map((x:unknown,i:number)=>{need(keys(x,'id name length extent hits')&&int(x.id,i,i)&&name(x.name)&&int(x.extent,1,2147483647)&&(x.length===null||int(x.length,x.extent,2147483647))&&int(x.hits,1,20000));return x as BlastQuery;});
    need(new Set(d.queries.map(x=>x.name)).size===d.queries.length&&d.queries.reduce((s,x)=>s+x.hits,0)===m.records&&m.records===m.total_units&&m.records<=20000);
    tree=d.queries.map(x=>({path:`/queries/${x.id}`,node_type:'array',attributes:{label:x.name}}));
    if(kind!=='tree'){
      const o=p.selected,b=p.hits;need(o.query===null||o.query<d.queries.length);need(keys(b,'rows total unknown_coverage')&&int(b.total,0,m.records)&&int(b.unknown_coverage,0,m.records)&&o.offset<=b.total&&Array.isArray(b.rows)&&b.rows.length===Math.min(o.count,b.total-o.offset));const ids=new Set<number>();
      for(const x of b.rows){need(keys(x,'id query subject identity alignment_length mismatches gap_opens qstart qend sstart send evalue bitscore coverage')&&int(x.id,0,m.records-1)&&!ids.has(x.id)&&int(x.query,0,d.queries.length-1)&&(o.query===null||o.query===x.query)&&name(x.subject));ids.add(x.id);const q=d.queries[x.query]!;
        need(num(x.identity,o.min_identity,100)&&int(x.alignment_length,1,2147483647)&&int(x.mismatches,0,x.alignment_length)&&int(x.gap_opens,0,x.alignment_length)&&['qstart','qend','sstart','send'].every(k=>int(x[k],1,k.startsWith('q')?q.extent:2147483647)));
        const expected=q.length===null?null:(Math.abs(x.qend-x.qstart)+1)*100/q.length;need(expected===null?x.coverage===null:num(x.coverage,0,100)&&Math.abs(x.coverage-expected)<1e-10);need(o.min_coverage===0||x.coverage!==null&&x.coverage>=o.min_coverage);
        need(num(x.bitscore,0)&&typeof x.evalue==='string'&&x.evalue.length<=64&&/^\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?$/.test(x.evalue));}
      d.hits=b as BioData['hits'];
    }
  }
  if(kind==='tree')need(same(p.tree,tree));
  need(new TextEncoder().encode(JSON.stringify(r)).length<=2097152);return d;
}
/** A single-base hit still has width; reverse HSPs begin at the smaller endpoint. */
export function blastRange(hit:BlastHit,query:BlastQuery) {const extent=query.length??query.extent;return {left:(Math.min(hit.qstart,hit.qend)-1)/extent,width:(Math.abs(hit.qend-hit.qstart)+1)/extent,reverse:hit.qstart>hit.qend,knownLength:query.length!==null};}
/** Bound highlight work to the displayed window, not motif length × all matches. */
export function sequenceBases(data:BioData) {
  if(!data.sequence)return [];
  const {sequence,selected}=data,motif=selected.motif as string;
  let cursor=0;const positions=sequence.search.positions;
  return [...sequence.bases].map((base,i)=>{const position=selected.start+i;while(cursor<positions.length&&positions[cursor]!+motif.length<=position)cursor++;return {base,position,quality:sequence.qualities?.[i],hit:motif.length>0&&cursor<positions.length&&positions[cursor]!<=position};});
}
