import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {intText} from '../src/visualizations/extended/databasePreviewGuards.ts';
import {parseSqlDump,validateSqlDumpSelection} from '../src/visualizations/extended/sqlDumpData.ts';
import {parsePgDump} from '../src/visualizations/extended/pgDumpData.ts';
for(const text of ['1\n','1\r','1 ',' 1','-0','+1','01','1\u2028','1\u2029'])test(`integer rejects noncanonical lexical form ${JSON.stringify(text)}`,()=>assert.equal(intText(text),false));
const sql=JSON.parse(readFileSync(new URL('./browser/sql-dump-data.json',import.meta.url),'utf8'));
function normalized(p){const {kind,contract_version,type,reader,metadata,warnings,sampled,...payload}=structuredClone(p);return {kind,payload:{...payload,view_kind:kind},metadata}}
test('SQL number literal and opaque token reject final newline rather than trimming it',()=>{
  const r=normalized(sql.first);r.payload.table.rows[0][0].value+='\n';assert.throws(()=>parseSqlDump('table',r.payload,r.metadata));
  assert.throws(()=>validateSqlDumpSelection({...sql.first.selected,table:sql.first.selected.table+'\n'}));
});
test('Postgres opaque object references reject newline suffix',()=>{
  const f=JSON.parse(readFileSync(new URL('./browser/pg-dump-data.json',import.meta.url),'utf8')),r=normalized(f.treeCustom);r.payload.tree[0].path+='\n';assert.throws(()=>parsePgDump(r.payload,r.metadata));
});
