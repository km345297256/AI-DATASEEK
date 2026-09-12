/** Trusted database-view payloads; no SQL, HTML or executable chart configuration. */
export const DATABASE_LIMITS = {max_tables:32,max_schema_columns:128,max_columns:16,max_rows:200,max_offset:100000,max_cell_bytes:512,max_schema_bytes:65536};
export const DATABASE_SEMANTICS = 'Engine-native scalar values; exact integer and decimal text; no user SQL, views, macros, joins or type coercion';
export const DATABASE_ENGINES = {duckdb:'DuckDB',dbf:'dBASE DBF',access:'Microsoft Access'} as const;
export const DATABASE_ORDERINGS = {duckdb:'duckdb-rowid-ascending',dbf:'dbf-record-order',access:'access-export-order'} as const;
const FORMATS: Record<string, string> = {duckdb:'duckdb',ddb:'duckdb',dbf:'dbf',mdb:'access',accdb:'access'};
const DATA_TYPES = ['INTEGER','DECIMAL','REAL','BOOLEAN','TEXT','BLOB','DATE','TIME','TIMESTAMP','UUID','UNSUPPORTED'];
export interface DatabaseColumn {id:number;label:string;data_type:string;nullable:boolean|null;primary_key:number|null;previewable:boolean}
export interface DatabaseTable {id:string;label:string;columns:DatabaseColumn[];ordering:string}
export interface DatabaseSelection {table:string;columns:number[];row_offset:number;row_limit:number}
export type DatabaseCell = {type:'integer'|'decimal'|'text'|'date'|'time'|'timestamp'|'uuid';value:string}
  | {type:'real';value:number} | {type:'boolean';value:boolean} | {type:'null'|'nonfinite';value:null}
  | {type:'blob';bytes:number} | {type:'text-omitted';bytes:number;reason:'cell-budget'|'unsafe-text'};
export interface DatabaseData {engine:keyof typeof DATABASE_ENGINES;format:string;sourceBytes:number;schemaBytes:number;tables:DatabaseTable[];
  rows:DatabaseCell[][]|null;rowIds:string[];selected:DatabaseSelection|null;hasMore:boolean;omitted:number;blobs:number;nonfinite:number}
const ERROR = '数据库目录、分页或数据类型无效，请重新打开预览。';
function need(ok:unknown):asserts ok {if(!ok)throw new Error(ERROR)}
const object=(v:unknown):v is Record<string,any>=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const owns=(v:object,k:PropertyKey)=>Object.prototype.hasOwnProperty.call(v,k);
function keys(v:unknown,names:string):asserts v is Record<string,any> {need(object(v)&&Object.keys(v).sort().join('|')===names.split(' ').filter(Boolean).sort().join('|'))}
const int=(v:unknown,lo=0,hi=Number.MAX_SAFE_INTEGER):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=lo&&v<=hi;
const bytes=(v:string)=>new TextEncoder().encode(v).length;
const safe=(v:unknown):v is string=>typeof v==='string'&&bytes(v)<=512&&!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(v)&&!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
const name=(v:unknown):v is string=>safe(v)&&bytes(v)<=128&&v===v.trim()&&/^[\p{L}\p{N}_][\p{L}\p{N}_ .()-]{0,127}$/u.test(v);
const token=(v:unknown):v is string=>typeof v==='string'&&/^t-[0-9a-f]{24}$/.test(v);
const same=(a:any,b:any):boolean=>Array.isArray(a)&&Array.isArray(b)?a.length===b.length&&a.every((v,i)=>same(v,b[i])):object(a)&&object(b)?Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>Object.prototype.hasOwnProperty.call(b,k)&&same(a[k],b[k])):a===b;
const exactInteger=(v:unknown):v is string=>typeof v==='string'&&v.length<=40&&/^(?:0|-[1-9][0-9]*|[1-9][0-9]*)$/.test(v)&&BigInt(v)>=-(2n**127n)&&BigInt(v)<2n**128n;
const exactDecimal=(v:unknown):v is string=>typeof v==='string'&&v.length<=41&&/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(v)&&v.replace(/[-.]/g,'').replace(/^0+/,'').length<=38;
export function validateDatabaseSelection(value:unknown,data?:Pick<DatabaseData,'tables'>):DatabaseSelection {
  keys(value,'table columns row_offset row_limit');
  need(token(value.table)&&Array.isArray(value.columns)&&value.columns.length>=1&&value.columns.length<=16&&value.columns.every((c:unknown)=>int(c,0,127))&&new Set(value.columns).size===value.columns.length&&int(value.row_offset,0,100000)&&int(value.row_limit,1,200));
  if(data){const t=data.tables.find(t=>t.id===value.table);need(t&&value.columns.every((c:number)=>t.columns[c]?.previewable===true))}
  return {table:value.table,columns:[...value.columns],row_offset:value.row_offset,row_limit:value.row_limit};
}
/** Lexical/components only; preserve extended/BC years, nanoseconds and 24:00. */
function scalarText(kind:string,value:string):boolean {
  if(value.length>64||value.trim()!==value)return false;
  if(kind==='date'){
    const m=/^([0-9]{4,10})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])(?: \(BC\))?$/.exec(value);
    return !!m&&Number(m[1])>0&&Number(m[3])<=[31,29,31,30,31,30,31,31,30,31,30,31][Number(m[2])-1]!;
  }
  if(kind==='time'){
    const m=/^([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?$/.exec(value);if(!m)return false;
    const h=Number(m[1]),minute=Number(m[2]),s=Number(m[3]);
    return h<=24&&minute<=59&&s<=59&&(h!==24||minute===0&&s===0&&!(m[4]??'').replace(/0/g,''));
  }
  if(kind==='timestamp'){
    const m=/^(.+)[ T]([0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?)$/.exec(value);
    return !!m&&scalarText('date',m[1]!)&&scalarText('time',m[2]!);
  }
  return kind==='uuid'&&/^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(value);
}
function cell(v:unknown):asserts v is DatabaseCell {
  need(object(v));const type=v.type;
  if(['integer','decimal','real','boolean','text','date','time','timestamp','uuid','null','nonfinite'].includes(type)){
    keys(v,'type value');
    if(type==='integer')need(exactInteger(v.value));
    else if(type==='decimal')need(exactDecimal(v.value));
    else if(type==='real')need(typeof v.value==='number'&&Number.isFinite(v.value));
    else if(type==='boolean')need(typeof v.value==='boolean');
    else if(type==='null'||type==='nonfinite')need(v.value===null);
    else need(safe(v.value)&&(type==='text'||scalarText(type,v.value)));
  }else if(type==='blob'){keys(v,'type bytes');need(int(v.bytes,0,16777216))}
  else{keys(v,'type bytes reason');need(type==='text-omitted'&&int(v.bytes,0,16777216)&&v.reason===(v.bytes>512?'cell-budget':'unsafe-text'))}
}
export function parseDatabaseTable(kind:'tree'|'table',payload:unknown,metadata:unknown,expected?:DatabaseSelection):DatabaseData {
  need(kind==='tree'||kind==='table');keys(payload,`view_kind media_type choices selected ${kind}`);
  need(payload.view_kind===kind&&payload.media_type==='application/json');keys(payload.choices,'tables');
  keys(metadata,'engine format container input_mode source_bytes table_count schema_bytes value_semantics ordering total_rows_known rows_returned nonfinite_values omitted_texts blob_values limits');
  const m=metadata,engine=m.engine as keyof typeof DATABASE_ENGINES;
  need(owns(DATABASE_ENGINES,engine)&&typeof m.format==='string'&&owns(FORMATS,m.format)&&FORMATS[m.format]===engine&&m.container===DATABASE_ENGINES[engine]&&m.input_mode==='whole');
  need(int(m.source_bytes,32,16777216)&&int(m.schema_bytes,1,65536)&&m.value_semantics===DATABASE_SEMANTICS&&m.ordering===DATABASE_ORDERINGS[engine]&&m.total_rows_known===false&&int(m.rows_returned,0,200)&&same(m.limits,DATABASE_LIMITS));
  need(['nonfinite_values','omitted_texts','blob_values'].every(k=>int(m[k],0,3200)));
  const tables=payload.choices.tables;need(Array.isArray(tables)&&tables.length>=1&&tables.length<=32&&int(m.table_count,tables.length,tables.length));
  for(const t of tables){
    keys(t,'id label columns ordering');need(token(t.id)&&name(t.label)&&t.ordering===DATABASE_ORDERINGS[engine]&&Array.isArray(t.columns)&&t.columns.length>=1&&t.columns.length<=128);
    for(const [i,c] of t.columns.entries()){
      keys(c,'id label data_type nullable primary_key previewable');need(int(c.id,i,i)&&name(c.label)&&DATA_TYPES.includes(c.data_type)&&(c.nullable===null||typeof c.nullable==='boolean')&&(c.primary_key===null||int(c.primary_key,0,t.columns.length))&&typeof c.previewable==='boolean'&&(c.data_type!=='UNSUPPORTED'||!c.previewable));
    }
    need(new Set(t.columns.map((c:DatabaseColumn)=>c.label.toLowerCase())).size===t.columns.length);
    const pks=t.columns.filter((c:DatabaseColumn)=>c.primary_key).map((c:DatabaseColumn)=>c.primary_key!).sort((a:number,b:number)=>a-b);need(pks.every((v:number,i:number)=>v===i+1));
  }
  need(new Set(tables.map(t=>t.id)).size===tables.length&&new Set(tables.map(t=>t.label.toLowerCase())).size===tables.length&&tables.every((t,i)=>i===0||tables[i-1].label<t.label));
  let rows:DatabaseCell[][]|null=null,rowIds:string[]=[],selected:DatabaseSelection|null=null,hasMore=false;
  if(kind==='tree'){
    keys(payload.selected,'');need(same(payload.tree,tables.map(t=>({path:'/'+t.id,node_type:'table',attributes:{label:t.label,columns:t.columns.length}})))&&['rows_returned','nonfinite_values','omitted_texts','blob_values'].every(k=>m[k]===0));
  }else{
    selected=validateDatabaseSelection(payload.selected,{tables});if(expected)need(same(selected,validateDatabaseSelection(expected,{tables})));
    const t=payload.table,target=tables.find(t=>t.id===selected!.table);need(target);keys(t,'columns column_ids rows row_ids row_offset has_more ordering');
    need(same(t.columns,selected.columns.map(i=>target.columns[i].label))&&same(t.column_ids,selected.columns)&&int(t.row_offset,selected.row_offset,selected.row_offset)&&t.ordering===DATABASE_ORDERINGS[engine]&&typeof t.has_more==='boolean'&&Array.isArray(t.rows)&&t.rows.length<=selected.row_limit&&m.rows_returned===t.rows.length);
    need(Array.isArray(t.row_ids)&&t.row_ids.length===t.rows.length&&t.row_ids.every((v:unknown)=>exactInteger(v)&&BigInt(v)>=0n)&&t.row_ids.every((v:string,i:number)=>i===0||BigInt(t.row_ids[i-1])<BigInt(v))&&(!t.has_more||t.rows.length===selected.row_limit));
    const count:Record<string,number>={nonfinite:0,'text-omitted':0,blob:0};
    for(const row of t.rows){
      need(Array.isArray(row)&&row.length===selected.columns.length);
      for(const [index,c] of row.entries()){
        cell(c);const dtype=target.columns[selected.columns[index]!].data_type;
        const allowed=[dtype.toLowerCase(),'null'];if(dtype==='TEXT')allowed.push('text-omitted');if(['REAL','DATE','TIME','TIMESTAMP'].includes(dtype))allowed.push('nonfinite');
        need(allowed.includes(c.type));if(owns(count,c.type))count[c.type]++;
      }
    }
    need(m.nonfinite_values===count.nonfinite&&m.omitted_texts===count['text-omitted']&&m.blob_values===count.blob);
    rows=t.rows;rowIds=t.row_ids;hasMore=t.has_more;
  }
  need(bytes(JSON.stringify({payload,metadata}))<=2097152);
  return {engine,format:m.format,sourceBytes:m.source_bytes,schemaBytes:m.schema_bytes,tables,rows,rowIds,selected,hasMore,omitted:m.omitted_texts,blobs:m.blob_values,nonfinite:m.nonfinite_values};
}
export const databaseCatalogMatches=(a:DatabaseData,b:DatabaseData)=>a.engine===b.engine&&a.format===b.format&&a.sourceBytes===b.sourceBytes&&a.schemaBytes===b.schemaBytes&&same(a.tables,b.tables);
export function databaseCellText(c:DatabaseCell):string {
  if(c.type==='null')return 'NULL';if(c.type==='nonfinite')return '非有限实数';if(c.type==='blob')return `BLOB · ${c.bytes} 字节（未解码）`;
  if(c.type==='text-omitted')return `文本已省略 · ${c.bytes} 字节（${c.reason==='cell-budget'?'超过单元格预算':'不安全文本'}）`;
  if(c.type==='boolean')return c.value?'true':'false';
  if(c.type==='text'&&c.value==='')return '空字符串';
  return String(c.value);
}
/** No implicit decimal/string/NULL coercion. Exact integers beyond JS precision stay in the table. */
export function databasePagePlot(data:DatabaseData|undefined,column:number) {
  const rows=data?.rows??[];const points:{index:number;id:string;value:number}[]=[];
  rows.forEach((row,index)=>{
    const c=row[column];if(!c)return;
    if(c.type==='real'&&Number.isFinite(c.value))points.push({index,id:data!.rowIds[index]!,value:c.value});
    else if(c.type==='integer'&&BigInt(c.value)>=BigInt(Number.MIN_SAFE_INTEGER)&&BigInt(c.value)<=BigInt(Number.MAX_SAFE_INTEGER))points.push({index,id:data!.rowIds[index]!,value:Number(c.value)});
  });
  const scale=Math.max(0,...points.map(p=>Math.abs(p.value)))||1;
  return {points:points.map(p=>({...p,x:30+(p.index+.5)*640/Math.max(1,rows.length),y:110-85*(p.value/scale)})),skipped:rows.length-points.length,scale};
}
