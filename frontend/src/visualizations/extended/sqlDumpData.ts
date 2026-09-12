/** SQL literals are source text, never restored/converted database values. */
import {need,keys,int,bytes,safeText,safeName,same,object} from './databasePreviewGuards.ts';

export const SQL_DIALECTS = {postgres:'PostgreSQL',mysql:'MySQL',sqlite:'SQLite'} as const;
export type SqlDialect = keyof typeof SQL_DIALECTS;
export const SQL_DUMP_LIMITS = {max_statements:4096,max_statement_bytes:1048576,max_depth:32,max_tables:32,max_schema_columns:128,max_schema_bytes:131072,max_columns:16,max_rows:200,max_offset:100000,max_cell_bytes:512,max_number_bytes:128,max_source_rows:1000000};
export const SQL_DUMP_SEMANTICS = 'Source SQL literals only; COPY fields remain text; no execution, type coercion or restored database state';
export interface SqlDumpColumn {id:number;label:string;declared_type:string}
export interface SqlDumpTable {id:string;label:string;columns:SqlDumpColumn[]}
export interface SqlDumpSelection {dialect:SqlDialect;table:string;columns:number[];row_offset:number;row_limit:number}
export type SqlDumpCell = {type:'number-literal'|'text';value:string}|{type:'null';value:null}|{type:'boolean';value:boolean}|{type:'blob';bytes:number}|{type:'text-omitted';bytes:number;reason:'cell-budget'|'unsafe-text'};
export interface SqlDumpData {dialect:SqlDialect;sourceBytes:number;schemaBytes:number;statements:number;ignored:number;sourceRows:number;tables:SqlDumpTable[];
  rows:SqlDumpCell[][]|null;rowIds:string[];selected:SqlDumpSelection|null;hasMore:boolean}
const isDialect=(v:unknown):v is SqlDialect=>typeof v==='string'&&Object.prototype.hasOwnProperty.call(SQL_DIALECTS,v);
const token=(v:unknown):v is string=>typeof v==='string'&&v.length===26&&/^s-[0-9a-f]{24}$/.test(v);

export function validateSqlDumpSelection(v:unknown,data?:Pick<SqlDumpData,'tables'|'dialect'>):SqlDumpSelection {
  keys(v,'dialect table columns row_offset row_limit');
  need(isDialect(v.dialect)&&token(v.table)&&Array.isArray(v.columns)&&v.columns.length>=1&&v.columns.length<=16&&v.columns.every((c:unknown)=>int(c,0,127))&&new Set(v.columns).size===v.columns.length&&int(v.row_offset,0,100000)&&int(v.row_limit,1,200));
  if(data){const t=data.tables.find(t=>t.id===v.table);need(v.dialect===data.dialect&&t&&v.columns.every((c:number)=>c<t.columns.length))}
  return {dialect:v.dialect,table:v.table,columns:[...v.columns],row_offset:v.row_offset,row_limit:v.row_limit};
}

function cell(v:unknown):asserts v is SqlDumpCell {
  need(object(v));
  if(['number-literal','text','null','boolean'].includes(v.type)){
    keys(v,'type value');
    if(v.type==='number-literal')need(typeof v.value==='string'&&v.value.length<=128&&v.value===v.value.trim()&&/^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(v.value));
    else if(v.type==='text')need(safeText(v.value));
    else if(v.type==='null')need(v.value===null);
    else need(typeof v.value==='boolean');
  }else if(v.type==='blob'){keys(v,'type bytes');need(int(v.bytes,0,16777216))}
  else {keys(v,'type bytes reason');need(v.type==='text-omitted'&&int(v.bytes,0,16777216)&&v.reason===(v.bytes>512?'cell-budget':'unsafe-text'))}
}

export function parseSqlDump(kind:'tree'|'table',payload:unknown,metadata:unknown,expected?:SqlDumpSelection|{dialect:SqlDialect}):SqlDumpData {
  need(kind==='tree'||kind==='table');keys(payload,`view_kind media_type choices selected ${kind}`);
  need(payload.view_kind===kind&&payload.media_type==='application/json');keys(payload.choices,'tables');
  keys(metadata,'dialect format input_mode source_bytes statement_count ignored_statement_count source_rows schema_bytes value_semantics ordering limits');
  const m=metadata;
  need(isDialect(m.dialect)&&m.format==='sql'&&m.input_mode==='whole'&&int(m.source_bytes,1,16777216)&&int(m.schema_bytes,1,131072)&&int(m.statement_count,1,4096)&&int(m.ignored_statement_count,0,m.statement_count)&&int(m.source_rows,0,1000000)&&m.value_semantics===SQL_DUMP_SEMANTICS&&m.ordering==='source-literal-order'&&same(m.limits,SQL_DUMP_LIMITS));
  const tables=payload.choices.tables;need(Array.isArray(tables)&&tables.length>=1&&tables.length<=32);
  for(const t of tables){
    keys(t,'id label columns');need(token(t.id)&&safeName(t.label)&&Array.isArray(t.columns)&&t.columns.length>=1&&t.columns.length<=128);
    for(const [i,c] of t.columns.entries()){
      keys(c,'id label declared_type');need(int(c.id,i,i)&&safeName(c.label)&&typeof c.declared_type==='string'&&/^[A-Z][A-Z0-9_ ]{0,95}(?:\([0-9]{1,5}(?:, ?[0-9]{1,5})?\))?$/.test(c.declared_type));
    }
    need(new Set(t.columns.map((c:SqlDumpColumn)=>c.label.toLowerCase())).size===t.columns.length);
  }
  need(new Set(tables.map(t=>t.id)).size===tables.length&&new Set(tables.map(t=>t.label.toLowerCase())).size===tables.length&&tables.every((t,i)=>i===0||tables[i-1].label<t.label));
  let rows:SqlDumpCell[][]|null=null,rowIds:string[]=[],selected:SqlDumpSelection|null=null,hasMore=false;
  if(kind==='tree'){
    keys(payload.selected,'dialect');need(payload.selected.dialect===m.dialect);
    if(expected)need(same(payload.selected,expected));
    need(same(payload.tree,tables.map(t=>({path:'/'+t.id,node_type:'table',attributes:{label:t.label,columns:t.columns.length}}))));
  }else{
    selected=validateSqlDumpSelection(payload.selected,{tables,dialect:m.dialect});if(expected)need(same(selected,validateSqlDumpSelection(expected,{tables,dialect:m.dialect})));
    const t=payload.table,target=tables.find(t=>t.id===selected!.table);need(target);keys(t,'column_ids columns rows row_ids row_offset has_more');
    need(same(t.column_ids,selected.columns)&&same(t.columns,selected.columns.map(i=>target.columns[i].label))&&int(t.row_offset,selected.row_offset,selected.row_offset)&&typeof t.has_more==='boolean'&&Array.isArray(t.rows)&&t.rows.length<=selected.row_limit&&t.rows.length<=m.source_rows);
    need(Array.isArray(t.row_ids)&&same(t.row_ids,t.rows.map((_r:unknown,i:number)=>String(selected!.row_offset+i)))&&(!t.has_more||t.rows.length===selected.row_limit));
    for(const row of t.rows){need(Array.isArray(row)&&row.length===selected.columns.length);for(const c of row)cell(c)}
    rows=t.rows;rowIds=t.row_ids;hasMore=t.has_more;
  }
  need(bytes(JSON.stringify({payload,metadata}))<=2097152);
  return {dialect:m.dialect,sourceBytes:m.source_bytes,schemaBytes:m.schema_bytes,statements:m.statement_count,ignored:m.ignored_statement_count,sourceRows:m.source_rows,tables,rows,rowIds,selected,hasMore};
}

export const sqlDumpCatalogMatches=(a:SqlDumpData,b:SqlDumpData)=>a.dialect===b.dialect&&a.sourceBytes===b.sourceBytes&&a.schemaBytes===b.schemaBytes&&a.statements===b.statements&&a.ignored===b.ignored&&a.sourceRows===b.sourceRows&&same(a.tables,b.tables);
export function sqlDumpCellText(c:SqlDumpCell):string {
  if(c.type==='null')return 'NULL';if(c.type==='boolean')return c.value?'true':'false';
  if(c.type==='blob')return `二进制字面量 · ${c.bytes} 字节（未解码）`;
  if(c.type==='text-omitted')return `文本已省略 · ${c.bytes} 字节（${c.reason==='cell-budget'?'超过单元格预算':'不安全文本'}）`;
  if(c.type==='text'&&c.value==='')return '空字符串';return c.value;
}
