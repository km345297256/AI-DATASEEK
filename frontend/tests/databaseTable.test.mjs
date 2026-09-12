import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/databaseTableData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/database-native-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64);
function result(key='tree'){
  const {contract_version,type,reader,kind,metadata,warnings,sampled,...payload}=structuredClone(fixture[key]);
  return {contract_version,plugin_id:'viz-duckdb-table',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
for(const key of Object.keys(fixture))test(`native database ${key} obeys strict frontend schema`,()=>{
  const r=result(key),p=helpers.parseDatabaseTable(r.kind,r.payload,r.metadata,key==='tree'?undefined:r.payload.selected);
  assert.equal(p.engine,'duckdb');assert.equal(p.sourceBytes,fixture.tree.metadata.source_bytes);
});
const mutations=[
  ['wrong engine',r=>r.metadata.engine='sqlite'],['wrong format',r=>r.metadata.format='mdb'],['leaked path',r=>r.metadata.path='/Users/private'],
  ['SQL',r=>r.payload.sql='DROP TABLE t'],['source bool',r=>r.metadata.source_bytes=true],['oversized source',r=>r.metadata.source_bytes=16777217],
  ['wrong bounds',r=>r.metadata.limits.max_rows=201],['wrong semantics',r=>r.metadata.value_semantics='coerced'],['wrong ordering',r=>r.metadata.ordering='primary-key'],
  ['unsafe table label',r=>r.payload.choices.tables[0].label='https://evil.example'],['duplicate table id',r=>r.payload.choices.tables[0].id=r.payload.choices.tables[1].id],
  ['boolean column id',r=>r.payload.choices.tables[0].columns[0].id=false],['unknown type',r=>r.payload.choices.tables[0].columns[0].data_type='SCRIPT'],
  ['nullable truthy',r=>r.payload.choices.tables[0].columns[0].nullable=1],['previewable truthy',r=>r.payload.choices.tables[0].columns[0].previewable='yes'],
  ['row id negative',r=>r.payload.table.row_ids[0]='-1'],['row id number',r=>r.payload.table.row_ids[0]=0],['row id duplicate',r=>r.payload.table.row_ids[1]=r.payload.table.row_ids[0]],
  ['wrong selection',r=>r.payload.selected.row_offset=8],['bad count',r=>r.metadata.rows_returned=0],['extra cell field',r=>r.payload.table.rows[0][0].html='<img>'],
  ['unbounded text',r=>r.payload.table.rows[0][0]={type:'text',value:'x'.repeat(513)}],['unsafe text',r=>r.payload.table.rows[0][0]={type:'text',value:'/private/secret'}],
  ['unpaired surrogate',r=>r.payload.table.rows[0][0]={type:'text',value:'\ud800'}],['numeric big integer',r=>r.payload.table.rows[0][0]={type:'integer',value:9007199254740992}],
  ['integer overflow',r=>r.payload.table.rows[0][0]={type:'integer',value:(2n**128n).toString()}],['decimal exponent',r=>r.payload.table.rows[0][0]={type:'decimal',value:'1e30'}],
  ['decimal floating',r=>r.payload.table.rows[0][0]={type:'decimal',value:0.1}],['real infinity',r=>r.payload.table.rows[0][0]={type:'real',value:Infinity}],
  ['null masquerading zero',r=>r.payload.table.rows[0][0]={type:'null',value:0}],['BLOB contents',r=>r.payload.table.rows[0][0]={type:'blob',bytes:2,data:'AAAA'}],
];
for(const [label,mutate] of mutations)test(`database rejects ${label}`,()=>{const r=result('first');mutate(r);assert.throws(()=>helpers.parseDatabaseTable('table',r.payload,r.metadata))});
test('empty string, null, boolean, integers and decimal formatting are distinct',()=>{
  assert.equal(helpers.databaseCellText({type:'null',value:null}),'NULL');assert.equal(helpers.databaseCellText({type:'text',value:''}),'空字符串');
  assert.equal(helpers.databaseCellText({type:'boolean',value:false}),'false');assert.equal(helpers.databaseCellText({type:'decimal',value:'12345678901234567890.0001'}),'12345678901234567890.0001');
});
test('local chart never coerces decimal/big integers/null/text; extreme finite real stays bounded',()=>{
  const data={rows:[[{type:'integer',value:'7'}],[{type:'integer',value:'9007199254740993'}],[{type:'decimal',value:'0.1'}],[{type:'null',value:null}],[{type:'text',value:'8'}],[{type:'real',value:1.7976931348623157e308}],[{type:'real',value:-1.7976931348623157e308}]],rowIds:['0','1','2','3','4','5','6']};
  const p=helpers.databasePagePlot(data,0);assert.equal(p.skipped,4);assert.equal(p.points.length,3);assert.ok(p.points.every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.y>=25&&p.y<=195));
});
for(const patch of [{columns:[]},{columns:[0,0]},{columns:[true]},{columns:Array.from({length:17},(_,i)=>i)},{row_limit:201},{row_offset:100001},{sql:'SELECT 1'}])test(`bad database selection ${JSON.stringify(patch)}`,()=>assert.throws(()=>helpers.validateDatabaseSelection({...fixture.first.selected,...patch})));
test('non-previewable columns are not selectable and nullable unknown is preserved',()=>{
  const r=result('tree'),p=helpers.parseDatabaseTable('tree',r.payload,r.metadata),t=p.tables.find(t=>t.id===fixture.first.selected.table);
  t.columns[0].previewable=false;assert.throws(()=>helpers.validateDatabaseSelection({...fixture.first.selected,columns:[0]},p));
});
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)}};
function defaultResponse(options){return result(options.kind==='tree'?'tree':options.table===fixture.empty.selected.table?'empty':options.columns.length===2?'columns':options.row_offset===0?'first':'second')}
function mount(t,response=defaultResponse){
  const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./databaseTableData':helpers,
    '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,options,signal});return response(options)}}};
  const source=readFileSync(new URL('../src/visualizations/extended/DatabaseTablePreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'database-table'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
  const props=vue.reactive({file:{file_id:'database-file',filename:'synthetic.duckdb',size:fixture.tree.metadata.source_bytes},plugin:{id:'viz-duckdb-table',reader:'database-table',adapter:'database-table',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {props,state,seen,stop:()=>scope.stop()};
}
async function select(v){await flush();v.state.tableId.value=fixture.first.selected.table;v.state.columns.value=[...fixture.first.selected.columns];v.state.rowLimit.value=2;await flush()}
test('component metadata first, manual selected columns, version-pinned pagination, no automatic row scan',async t=>{
  const v=mount(t);await select(v);assert.equal(v.seen.length,1);assert.equal(v.state.data.value,undefined);assert.equal(v.state.error.value,'');
  await v.state.loadPage();assert.equal(v.state.error.value,'');assert.equal(v.seen[1].options.version,version);assert.deepEqual(v.state.data.value.rows,fixture.first.table.rows);
  await v.state.page(1);assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.rows,fixture.second.table.rows);
  v.state.columns.value=[...fixture.columns.selected.columns];v.state.rowOffset.value=0;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,3);
  await v.state.loadPage();assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.rows,fixture.columns.table.rows);
});
for(const action of ['disable','unmount','file','columns','offset','limit','table','plugin-budget'])test(`database response after ${action} cannot publish`,async t=>{
  const p=pending(),v=mount(t,o=>o.kind==='tree'?result():p.promise);await select(v);const request=v.state.loadPage();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='changed';else if(action==='columns')v.state.columns.value=[0];else if(action==='offset')v.state.rowOffset.value=2;else if(action==='limit')v.state.rowLimit.value=1;else if(action==='table')v.state.tableId.value=fixture.empty.selected.table;else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen[1].signal.aborted,true);p.resolve(result('first'));await request;assert.equal(v.state.data.value,undefined);
});
test('unchanged identities do not reload; closing immediately clears catalog and rows',async t=>{
  const v=mount(t);await select(v);await v.state.loadPage();v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,2);
  v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,2);
});
for(const change of ['kind','version','source','format','catalog','selection'])test(`database wrong ${change} result is refused`,async t=>{
  const v=mount(t,o=>{const r=defaultResponse(o);if(o.kind==='table'){if(change==='kind')r.kind='tree';if(change==='version')r.version='c'.repeat(64);if(change==='source')r.metadata.source_bytes++;if(change==='format')r.metadata.format='ddb';if(change==='catalog')r.payload.choices.tables[0].columns[0].nullable=null;if(change==='selection')r.payload.selected.row_limit=1}return r});
  await select(v);await v.state.loadPage();assert.notEqual(v.state.error.value,'');assert.equal(v.state.data.value,undefined);
});
