export const SQLITE_LIMITS={max_tables:32,max_schema_columns:128,max_columns:16,max_rows:200,max_offset:100000,max_cell_bytes:512,max_record_bytes:65536,max_schema_bytes:65536,max_sql_bytes:8192,max_progress_callbacks:2000,progress_interval:1000,sqlite_heap_bytes:33554432};
export const SQLITE_SEMANTICS='SQLite storage classes; exact integer text; rowid ascending; no user SQL, joins, aggregation or type coercion';
export interface SqliteColumn{id:number;label:string;affinity:'INTEGER'|'REAL'|'TEXT'|'BLOB'|'NUMERIC';nullable:boolean;primary_key:number}
export interface SqliteTable{id:string;label:string;columns:SqliteColumn[];ordering:'rowid-ascending'}
export interface SqliteSelection{table:string;columns:number[];row_offset:number;row_limit:number}
export type SqliteCell={type:'integer'|'text';value:string}|{type:'real';value:number}|{type:'null'|'nonfinite';value:null}|{type:'blob';bytes:number}|{type:'text-omitted';bytes:number;reason:'cell-budget'|'unsafe-text'};
export interface SqliteData{format:string;sourceBytes:number;tables:SqliteTable[];schemaBytes:number;callbacks:number;rows:SqliteCell[][]|null;rowIds:string[];selected:SqliteSelection|null;hasMore:boolean;omitted:number;blobs:number;nonfinite:number}
const ERROR='SQLite 目录、分页或数据类型无效，请重新打开预览。';
function assert(ok:unknown):asserts ok{if(!ok)throw new Error(ERROR)}
const object=(v:unknown):v is Record<string,any>=>v!==null&&typeof v==='object'&&!Array.isArray(v);
function keys(v:unknown,names:string):asserts v is Record<string,any>{assert(object(v)&&Object.keys(v).sort().join('|')===names.split(' ').filter(Boolean).sort().join('|'))}
const int=(v:unknown,lo=0,hi=Number.MAX_SAFE_INTEGER):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=lo&&v<=hi;
const name=(v:unknown):v is string=>typeof v==='string'&&/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(v);
const token=(v:unknown):v is string=>typeof v==='string'&&/^t-[0-9a-f]{24}$/.test(v);
const same=(a:any,b:any):boolean=>Array.isArray(a)&&Array.isArray(b)?a.length===b.length&&a.every((v,i)=>same(v,b[i])):object(a)&&object(b)?Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>Object.prototype.hasOwnProperty.call(b,k)&&same(a[k],b[k])):a===b;
const int64=(v:unknown):v is string=>typeof v==='string'&&v.length<=20&&/^(?:0|-[1-9][0-9]*|[1-9][0-9]*)$/.test(v)&&BigInt(v)>=-9223372036854775808n&&BigInt(v)<=9223372036854775807n;
const bytes=(v:string)=>new TextEncoder().encode(v).length;
const safe=(v:unknown):v is string=>typeof v==='string'&&bytes(v)<=512&&!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(v)&&!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
export function validateSqliteSelection(value:unknown,data?:Pick<SqliteData,'tables'>):SqliteSelection{
 keys(value,'table columns row_offset row_limit');assert(token(value.table)&&Array.isArray(value.columns)&&value.columns.length>=1&&value.columns.length<=16&&value.columns.every((c:unknown)=>int(c,0,127))&&new Set(value.columns).size===value.columns.length&&int(value.row_offset,0,100000)&&int(value.row_limit,1,200));
 if(data){const t=data.tables.find(t=>t.id===value.table);assert(t&&value.columns.every((c:number)=>c<t.columns.length))}
 return {table:value.table,columns:[...value.columns],row_offset:value.row_offset,row_limit:value.row_limit};
}
function cell(value:unknown):asserts value is SqliteCell{
 assert(object(value));const typ=value.type;
 if(['integer','real','text','null','nonfinite'].includes(typ)){
  keys(value,'type value');if(typ==='integer')assert(int64(value.value));else if(typ==='real')assert(typeof value.value==='number'&&Number.isFinite(value.value));else if(typ==='text')assert(safe(value.value));else assert(value.value===null);
 }else if(typ==='blob'){keys(value,'type bytes');assert(int(value.bytes,0,65536))}
 else{keys(value,'type bytes reason');assert(typ==='text-omitted'&&int(value.bytes,0,65536)&&value.reason===(value.bytes>512?'cell-budget':'unsafe-text'))}
}
export function parseSqliteTable(kind:'tree'|'table',payload:unknown,metadata:unknown,expected?:SqliteSelection):SqliteData{
 assert(kind==='tree'||kind==='table');keys(payload,`view_kind media_type choices selected ${kind}`);assert(payload.view_kind===kind&&payload.media_type==='application/json');keys(payload.choices,'tables');
 const tables=payload.choices.tables;assert(Array.isArray(tables)&&tables.length>=1&&tables.length<=32);
 for(const t of tables){
  keys(t,'id label columns ordering');assert(token(t.id)&&name(t.label)&&!t.label.toLowerCase().startsWith('sqlite_')&&t.ordering==='rowid-ascending'&&Array.isArray(t.columns)&&t.columns.length>=1&&t.columns.length<=128);
  for(const [i,c]of t.columns.entries()){keys(c,'id label affinity nullable primary_key');assert(int(c.id,i,i)&&name(c.label)&&['INTEGER','REAL','TEXT','BLOB','NUMERIC'].includes(c.affinity)&&typeof c.nullable==='boolean'&&int(c.primary_key,0,t.columns.length))}
  const labels=t.columns.map((c:SqliteColumn)=>c.label.toLowerCase());assert(new Set(labels).size===labels.length&&!['rowid','_rowid_','oid'].every(k=>labels.includes(k)));
  const pks=t.columns.filter((c:SqliteColumn)=>c.primary_key).map((c:SqliteColumn)=>c.primary_key).sort((a:number,b:number)=>a-b);assert(pks.every((v:number,i:number)=>v===i+1));
 }
 assert(new Set(tables.map(t=>t.id)).size===tables.length&&new Set(tables.map(t=>t.label.toLowerCase())).size===tables.length&&tables.every((t,i)=>i===0||tables[i-1].label<t.label));
 keys(metadata,'format container input_mode source_bytes table_count schema_bytes value_semantics ordering total_rows_known progress_callbacks rows_returned nonfinite_values omitted_texts blob_values limits');const m=metadata;
 assert(['sqlite','sqlite3','db'].includes(m.format)&&m.container==='SQLite 3'&&m.input_mode==='whole'&&int(m.source_bytes,512,16777216)&&int(m.table_count,tables.length,tables.length)&&int(m.schema_bytes,1,Math.min(65536,m.source_bytes))&&m.value_semantics===SQLITE_SEMANTICS&&m.ordering==='rowid-ascending'&&m.total_rows_known===false&&int(m.progress_callbacks,0,2000)&&int(m.rows_returned,0,200)&&same(m.limits,SQLITE_LIMITS));
 assert(['nonfinite_values','omitted_texts','blob_values'].every(k=>int(m[k],0,3200)));
 let rows:SqliteCell[][]|null=null,rowIds:string[]=[],selected:SqliteSelection|null=null,hasMore=false;
 if(kind==='tree'){
  keys(payload.selected,'');assert(same(payload.tree,tables.map(t=>({path:'/'+t.id,node_type:'table',attributes:{label:t.label,columns:t.columns.length}})))&&['rows_returned','nonfinite_values','omitted_texts','blob_values'].every(k=>m[k]===0));
 }else{
  selected=validateSqliteSelection(payload.selected,{tables});if(expected)assert(same(selected,validateSqliteSelection(expected,{tables})));
  const t=payload.table,target=tables.find(t=>t.id===selected!.table);assert(target);keys(t,'columns column_ids rows row_ids row_offset has_more ordering');
  assert(same(t.columns,selected.columns.map(i=>target.columns[i].label))&&same(t.column_ids,selected.columns)&&int(t.row_offset,selected.row_offset,selected.row_offset)&&t.ordering==='rowid-ascending'&&typeof t.has_more==='boolean'&&Array.isArray(t.rows)&&t.rows.length<=selected.row_limit&&m.rows_returned===t.rows.length);
  assert(Array.isArray(t.row_ids)&&t.row_ids.length===t.rows.length&&t.row_ids.every(int64)&&t.row_ids.every((v:string,i:number)=>i===0||BigInt(t.row_ids[i-1])<BigInt(v))&&(!t.has_more||t.rows.length===selected.row_limit));
  const count:Record<string,number>={nonfinite:0,'text-omitted':0,blob:0};
  for(const row of t.rows){assert(Array.isArray(row)&&row.length===selected.columns.length);for(const c of row){cell(c);if(Object.prototype.hasOwnProperty.call(count,c.type))count[c.type]++}}
  assert(m.nonfinite_values===count.nonfinite&&m.omitted_texts===count['text-omitted']&&m.blob_values===count.blob);
  rows=t.rows;rowIds=t.row_ids;hasMore=t.has_more;
 }
 assert(bytes(JSON.stringify({payload,metadata}))<=2097152);
 return {format:m.format,sourceBytes:m.source_bytes,tables,schemaBytes:m.schema_bytes,callbacks:m.progress_callbacks,rows,rowIds,selected,hasMore,omitted:m.omitted_texts,blobs:m.blob_values,nonfinite:m.nonfinite_values};
}
export function sqliteCatalogMatches(a:SqliteData,b:SqliteData){return a.sourceBytes===b.sourceBytes&&a.format===b.format&&a.schemaBytes===b.schemaBytes&&same(a.tables,b.tables)}
export function sqliteCellText(c:SqliteCell):string{
 if(c.type==='null')return 'NULL';if(c.type==='nonfinite')return '非有限实数';if(c.type==='blob')return `BLOB · ${c.bytes} 字节（未解码）`;
 if(c.type==='text-omitted')return `文本已省略 · ${c.bytes} 字节（${c.reason==='cell-budget'?'超过单元格预算':'不安全文本'}）`;
 return String(c.value);
}
