import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {parseDatabaseTable} from '../src/visualizations/extended/databaseTableData.ts';

const cases=JSON.parse(readFileSync(new URL('../../sandbox/tests/fixtures/database/scalar-cases.json',import.meta.url),'utf8'));
const fixture=JSON.parse(readFileSync(new URL('./browser/database-native-data.json',import.meta.url),'utf8')).first;
function candidate(type,value){
  const source=structuredClone(fixture),target=source.choices.tables.find(t=>t.id===source.selected.table);
  target.columns[source.selected.columns[0]].data_type=type.toUpperCase();
  for(const row of source.table.rows)row[0]={type,value};
  const {contract_version,reader,kind,type:unused,metadata,warnings,sampled,...payload}=source;
  return {payload:{...payload,view_kind:kind},metadata};
}
for(const [type,value] of cases.accepted)test(`database literal accepts unchanged ${type} ${JSON.stringify(value)}`,()=>{
  const c=candidate(type,value),data=parseDatabaseTable('table',c.payload,c.metadata);
  assert.equal(data.rows[0][0].value,value);
});
for(const [type,value] of cases.rejected)test(`database literal rejects ${type} ${JSON.stringify(value)}`,()=>{
  const c=candidate(type,value);assert.throws(()=>parseDatabaseTable('table',c.payload,c.metadata));
});
test('ordinary text is not interpreted as a temporal value',()=>{
  const c=candidate('text','foo 24:00:01');assert.equal(parseDatabaseTable('table',c.payload,c.metadata).rows[0][0].value,'foo 24:00:01');
});
