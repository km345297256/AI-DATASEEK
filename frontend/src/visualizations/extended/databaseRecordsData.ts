import {need,keys,int,bytes,safeText,same,intText,object} from './databasePreviewGuards.ts';
import {validateDecimal128} from './decimal128Value.ts';
export const RECORD_LIMITS={max_groups:32,max_records:100000,max_rows:50,max_offset:100000,max_depth:8,max_record_nodes:128,max_page_nodes:4096,max_scan_nodes:100000,max_text_bytes:512,max_document_bytes:1048576,max_decompressed_block_bytes:1048576,max_decompressed_bytes:33554432};
const containers=['document','array','hash','list','set','zset','entry'];
const bsonTypes=['document','array','null','boolean','integer','real','nonfinite','decimal128','text','date-ms','timestamp','objectid','binary','omitted','unsupported'];
const redisBytesTypes=['text','binary','omitted'];
const unsupported=['regex','javascript','javascript-scope','dbpointer','symbol','undefined','min-key','max-key'];
export type RecordCell = {type:string;value?:string|number|boolean|null;count?:number;bytes?:number;subtype?:string|null;reason?:string;name?:string;bid?:string;seconds?:string;increment?:string};
export interface RecordNode {id:number;parent:number|null;key:string|null;key_omitted:boolean;cell:RecordCell}
export interface DatabaseRecord {index:string;key:RecordCell|null;expires_at_ms:string|null;nodes:RecordNode[];truncated:boolean}
export interface RecordGroup {id:string;label:string;database:number|null;record_count:number;counts:Record<string,number>}
export interface RecordSelection {group_id:string;offset:number;limit:number}
export interface RecordsData {engine:'bson'|'redis-rdb';format:string;sourceBytes:number;groups:RecordGroup[];records:DatabaseRecord[]|null;selected:RecordSelection|null;hasMore:boolean;total:number;omitted:number;unsupported:number}
const token=(v:unknown)=>typeof v==='string'&&v.length===26&&/^g-[0-9a-f]{24}$/.test(v);
export function validateRecordSelection(v:unknown,groups?:RecordGroup[]):RecordSelection {
  keys(v,'group_id offset limit');need(token(v.group_id)&&int(v.offset,0,100000)&&int(v.limit,1,50)&&(!groups||groups.some(g=>g.id===v.group_id)));
  return {group_id:v.group_id,offset:v.offset,limit:v.limit};
}
export function validateRecordCell(c:unknown):asserts c is RecordCell {
  need(object(c)&&typeof c.type==='string');const t=c.type;
  if(containers.includes(t)){keys(c,'type count');need(int(c.count,0,127))}
  else if(t==='binary'){keys(c,'type bytes subtype');need(int(c.bytes,0,16777216)&&(c.subtype===null||typeof c.subtype==='string'&&c.subtype.length===2&&/^[0-9a-f]{2}$/.test(c.subtype)))}
  else if(t==='omitted'){keys(c,'type bytes reason');need(int(c.bytes,0,16777216)&&c.reason===(c.bytes>512?'text-budget':'unsafe-text'))}
  else if(t==='unsupported'){keys(c,'type name');need(unsupported.includes(c.name))}
  else if(t==='timestamp'){keys(c,'type seconds increment');need(intText(c.seconds,0n,4294967295n)&&intText(c.increment,0n,4294967295n))}
  else if(t==='decimal128'){keys(c,'type value bid');need(validateDecimal128(c.value,c.bid))}
  else{
    keys(c,'type value');
    if(t==='null')need(c.value===null);else if(t==='boolean')need(typeof c.value==='boolean');
    else if(t==='integer'||t==='date-ms')need(intText(c.value));
    else if(t==='real')need(typeof c.value==='number'&&Number.isFinite(c.value));
    else if(t==='nonfinite')need(['NaN','Infinity','-Infinity'].includes(c.value));
    else if(t==='objectid')need(typeof c.value==='string'&&c.value.length===24&&/^[0-9a-f]{24}$/.test(c.value));
    else need(t==='text'&&safeText(c.value));
  }
}
function validateRecord(r:unknown,engine:string):[number,number,number]{
  keys(r,'index key expires_at_ms nodes truncated');need(intText(r.index,0n,99999n)&&typeof r.truncated==='boolean');
  if(engine==='bson')need(r.key===null&&r.expires_at_ms===null);
  else{validateRecordCell(r.key);need(['text','omitted','binary'].includes(r.key.type)&&(r.expires_at_ms===null||intText(r.expires_at_ms)));if(r.key.type==='binary')need(r.key.subtype===null)}
  need(Array.isArray(r.nodes)&&r.nodes.length>=1&&r.nodes.length<=128);
  const depths:number[]=[],children:number[]=Array(r.nodes.length).fill(0),visibleFields=new Map<number,Set<string>>();let active:number[]=[],omitted=Number(r.key?.type==='omitted'),unknown=0;
  for(const [i,n] of r.nodes.entries()){
    keys(n,'id parent key key_omitted cell');need(int(n.id,i,i)&&typeof n.key_omitted==='boolean');
    let childIndex=0;
    if(i===0){need(n.parent===null&&n.key===null&&!n.key_omitted);depths.push(0);active.push(0)}
    else{need(int(n.parent,0,i-1)&&containers.includes(r.nodes[n.parent].cell.type)&&active.includes(n.parent));active=[...active.slice(0,active.indexOf(n.parent)+1),i];const depth=depths[n.parent]!+1;need(depth<=8);depths.push(depth);childIndex=children[n.parent]!;children[n.parent]!++;
      need(safeText(n.key)&&(!n.key_omitted||n.key==='字段名已省略'))}
    validateRecordCell(n.cell);const type=n.cell.type;
    const uniqueField=()=>{const fields=visibleFields.get(n.parent)??new Set<string>();need(!fields.has(n.key));fields.add(n.key);visibleFields.set(n.parent,fields)};
    if(engine==='bson'){
      need(bsonTypes.includes(type));if(type==='binary')need(n.cell.subtype!==null);
      if(i){const parentType=r.nodes[n.parent].cell.type;need(['document','array'].includes(parentType));
        if(parentType==='array')need(n.key===String(childIndex)&&!n.key_omitted);else if(!n.key_omitted)uniqueField()}
    }else{
      if(type==='binary')need(n.cell.subtype===null);
      if(i){const parentType=r.nodes[n.parent].cell.type;
        if(['list','set','hash'].includes(parentType)){need(n.parent===0&&redisBytesTypes.includes(type));if(parentType!=='hash')need(n.key===String(childIndex)&&!n.key_omitted);else if(!n.key_omitted)uniqueField()}
        else if(parentType==='zset')need(n.parent===0&&type==='entry'&&n.cell.count===2&&n.key===String(childIndex)&&!n.key_omitted);
        else{need(parentType==='entry'&&r.nodes[0].cell.type==='zset'&&[0,1].includes(childIndex));need(n.key===(childIndex===0?'member':'score')&&!n.key_omitted);need(childIndex===0?redisBytesTypes.includes(type):['real','nonfinite'].includes(type));if(childIndex===1&&type==='nonfinite')need(n.cell.value!=='NaN')}
      }
    }
    omitted+=Number(n.key_omitted)+Number(n.cell.type==='omitted');unknown+=Number(n.cell.type==='unsupported');
  }
  r.nodes.forEach((n:RecordNode,i:number)=>need(children[i]===(containers.includes(n.cell.type)?n.cell.count:0)));
  need(engine==='bson'?r.nodes[0].cell.type==='document':['text','binary','omitted','list','set','hash','zset'].includes(r.nodes[0].cell.type));
  need(r.truncated===!!omitted);return [r.nodes.length,omitted,unknown];
}
export function parseDatabaseRecords(kind:'tree'|'table',payload:unknown,metadata:unknown,expected?:RecordSelection):RecordsData {
  need(kind==='tree'||kind==='table');keys(payload,`view_kind media_type choices selected ${kind}`);need(payload.view_kind===kind&&payload.media_type==='application/json');keys(payload.choices,'groups');
  keys(metadata,'engine format container input_mode source_bytes format_version ordering total_records groups_count records_returned nodes_returned omitted_values unsupported_values checksum limits');
  const m=metadata;need(m.engine==='bson'||m.engine==='redis-rdb');const bson=m.engine==='bson';
  need(m.format===(bson?'bson':'rdb')&&m.container===(bson?'BSON document stream':'Redis RDB')&&m.input_mode==='whole'&&int(m.source_bytes,bson?5:18,16777216));
  need(m.ordering===(bson?'bson-document-order':'rdb-file-order')&&m.checksum===(bson?'not-applicable':'verified')&&(bson?m.format_version===null:m.format_version===11)&&same(m.limits,RECORD_LIMITS));
  const groups=payload.choices.groups;need(Array.isArray(groups)&&groups.length>=1&&groups.length<=32&&int(m.groups_count,groups.length,groups.length));
  let total=0;
  for(const g of groups){
    keys(g,'id label database record_count counts');need(token(g.id)&&(bson?g.database===null:int(g.database,0,1023))&&g.label===(bson?'Documents':`Redis DB ${g.database}`)&&int(g.record_count,0,100000)&&object(g.counts));
    need(Object.entries(g.counts).every(([k,n])=>(bson?['document']:['string','list','set','hash','zset']).includes(k)&&int(n,1,100000))&&Object.values<number>(g.counts).reduce((a,b)=>a+b,0)===g.record_count);total+=g.record_count;
  }
  need(new Set(groups.map(g=>g.id)).size===groups.length&&(bson?groups.length===1:groups.every((g,i)=>i===0||groups[i-1].database<g.database))&&int(m.total_records,total,total)&&total<=100000);
  need(int(m.records_returned,0,50)&&int(m.nodes_returned,0,4096)&&int(m.omitted_values,0,8192)&&int(m.unsupported_values,0,4096));
  let records:DatabaseRecord[]|null=null,selected:RecordSelection|null=null,hasMore=false;
  if(kind==='tree'){
    keys(payload.selected,'');need(same(payload.tree,groups.map(g=>({path:'/'+g.id,node_type:'group',attributes:{label:g.label,records:g.record_count}})))&&['records_returned','nodes_returned','omitted_values','unsupported_values'].every(k=>m[k]===0));
  }else{
    selected=validateRecordSelection(payload.selected,groups);if(expected)need(same(selected,validateRecordSelection(expected,groups)));
    const group=groups.find(g=>g.id===selected!.group_id)!,p=payload.table;keys(p,'group_id offset limit has_more records');
    need(p.group_id===selected.group_id&&p.offset===selected.offset&&p.limit===selected.limit&&typeof p.has_more==='boolean'&&Array.isArray(p.records));
    const count=Math.min(selected.limit,Math.max(0,group.record_count-selected.offset));need(p.records.length===count&&m.records_returned===count);
    const stats=p.records.map((r:unknown)=>validateRecord(r,m.engine));
    need(p.records.every((r:DatabaseRecord,i:number)=>r.index===String(selected!.offset+i))&&m.nodes_returned===stats.reduce((a:number,s:number[])=>a+s[0]!,0)&&m.omitted_values===stats.reduce((a:number,s:number[])=>a+s[1]!,0)&&m.unsupported_values===stats.reduce((a:number,s:number[])=>a+s[2]!,0));
    need(p.has_more===(selected.offset+count<group.record_count));records=p.records;hasMore=p.has_more;
  }
  need(bytes(JSON.stringify({payload,metadata}))<=2097152);
  return {engine:m.engine,format:m.format,sourceBytes:m.source_bytes,groups,records,selected,hasMore,total,omitted:m.omitted_values,unsupported:m.unsupported_values};
}
export const recordsCatalogMatches=(a:RecordsData,b:RecordsData)=>a.engine===b.engine&&a.format===b.format&&a.sourceBytes===b.sourceBytes&&a.total===b.total&&same(a.groups,b.groups);
export function recordCellText(c:RecordCell):string {
  if(containers.includes(c.type))return `${c.count} 个成员`;
  if(c.type==='null')return 'NULL';if(c.type==='text'&&c.value==='')return '空字符串';
  if(c.type==='binary')return `${c.bytes} 字节（未解码）${c.subtype===null?'':` · 子类型 ${c.subtype}`}`;
  if(c.type==='omitted')return `${c.bytes} 字节（${c.reason==='text-budget'?'超过文本预算':'不安全文本'}，已省略）`;
  if(c.type==='unsupported')return `${c.name}（不解释、不执行）`;
  if(c.type==='timestamp')return `逻辑时间：seconds=${c.seconds}，increment=${c.increment}`;
  if(c.type==='date-ms')return `${c.value}（UTC Unix 毫秒）`;
  if(c.type==='decimal128')return `${c.value} · BID ${c.bid}`;
  return String(c.value);
}
export function recordNodeDepths(record:DatabaseRecord):number[]{const out:number[]=[];for(const n of record.nodes)out.push(n.parent===null?0:out[n.parent]!+1);return out}
