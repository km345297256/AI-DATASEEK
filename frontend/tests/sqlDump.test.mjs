import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/sqlDumpData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/sql-dump-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64);
function result(key='tree'){
  const {contract_version,type,reader,kind,metadata,warnings,sampled,...payload}=structuredClone(fixture[key]);
  return {contract_version,plugin_id:'viz-sql-dump',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
for(const key of Object.keys(fixture))test(`SQL dump ${key} accepts native SQLite dump literal payload`,()=>{
  const r=result(key),p=helpers.parseSqlDump(r.kind,r.payload,r.metadata,r.payload.selected);
  assert.equal(p.dialect,'sqlite');assert.equal(p.sourceBytes,fixture.tree.metadata.source_bytes);
});
const mutations=[
  ['auto dialect',r=>r.metadata.dialect='auto'],['wrong format',r=>r.metadata.format='dump'],['leaked path',r=>r.metadata.path='/Users/private'],
  ['executable SQL',r=>r.payload.sql='DROP TABLE t'],['boolean source size',r=>r.metadata.source_bytes=true],['oversized source',r=>r.metadata.source_bytes=16777217],
  ['relaxed bounds',r=>r.metadata.limits.max_rows=201],['wrong semantics',r=>r.metadata.value_semantics='restored'],['wrong ordering',r=>r.metadata.ordering='primary-key'],
  ['unsafe table',r=>r.payload.choices.tables[0].label='https://evil.example'],['duplicate table',r=>r.payload.choices.tables[0].id=r.payload.choices.tables[1].id],
  ['boolean column id',r=>r.payload.choices.tables[0].columns[0].id=false],['unsafe declared type',r=>r.payload.choices.tables[0].columns[0].declared_type='load_extension()'],
  ['row id negative',r=>r.payload.table.row_ids[0]='-1'],['numeric row id',r=>r.payload.table.row_ids[0]=0],['duplicate row ids',r=>r.payload.table.row_ids[1]='0'],
  ['wrong selection',r=>r.payload.selected.row_offset=8],['unsupported source count',r=>r.metadata.source_rows=1],['extra cell data',r=>r.payload.table.rows[0][0].html='<img>'],
  ['unbounded text',r=>r.payload.table.rows[0][0]={type:'text',value:'x'.repeat(513)}],['unsafe text',r=>r.payload.table.rows[0][0]={type:'text',value:'/private/secret'}],
  ['unpaired surrogate',r=>r.payload.table.rows[0][0]={type:'text',value:'\ud800'}],['floating number conversion',r=>r.payload.table.rows[0][0]={type:'number-literal',value:9007199254740992}],
  ['numeric expression',r=>r.payload.table.rows[0][0]={type:'number-literal',value:'1+1'}],['literal NaN',r=>r.payload.table.rows[0][0]={type:'number-literal',value:'NaN'}],
  ['null as zero',r=>r.payload.table.rows[0][0]={type:'null',value:0}],['BLOB raw content',r=>r.payload.table.rows[0][0]={type:'blob',bytes:2,data:'AAAA'}],
];
for(const [label,mutate] of mutations)test(`SQL dump rejects ${label}`,()=>{const r=result('first');mutate(r);assert.throws(()=>helpers.parseSqlDump('table',r.payload,r.metadata))});
test('literal display preserves integer/decimal/exponent text, NULL/empty/boolean; no float charts',()=>{
  for(const value of ['9007199254740993','+0001.230000000000000001','1e+400','.001','1.'])assert.equal(helpers.sqlDumpCellText({type:'number-literal',value}),value);
  assert.equal(helpers.sqlDumpCellText({type:'null',value:null}),'NULL');assert.equal(helpers.sqlDumpCellText({type:'text',value:''}),'空字符串');
  assert.equal(helpers.sqlDumpCellText({type:'boolean',value:false}),'false');
  assert.equal('sqlDumpPagePlot' in helpers,false);
});
for(const patch of [{dialect:'auto'},{columns:[]},{columns:[0,0]},{columns:[true]},{columns:Array.from({length:17},(_,i)=>i)},{row_limit:201},{row_offset:100001},{sql:'SELECT 1'}])test(`bad SQL selection ${JSON.stringify(patch)}`,()=>assert.throws(()=>helpers.validateSqlDumpSelection({...fixture.first.selected,...patch})));
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)}};
function defaultResponse(options){return result(options.kind==='tree'?'tree':options.table===fixture.empty.selected.table?'empty':options.columns.length===2?'columns':options.row_offset===0?'first':'second')}
function mount(t,response=defaultResponse){
  const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./sqlDumpData':helpers,
    '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,options,signal});return response(options)}}};
  const source=readFileSync(new URL('../src/visualizations/extended/SQLDumpPreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'sql-dump'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
  const props=vue.reactive({file:{file_id:'sql-file',filename:'synthetic.sql',size:fixture.tree.metadata.source_bytes},plugin:{id:'viz-sql-dump',reader:'sql-dump',adapter:'database-dump',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {props,state,seen,stop:()=>scope.stop()};
}
async function select(v){v.state.dialect.value='sqlite';await v.state.inspect();await flush();v.state.tableId.value=fixture.first.selected.table;v.state.columns.value=[...fixture.first.selected.columns];v.state.rowLimit.value=2;await flush()}
test('component does not infer dialect or automatically request tree/page on mount or selection',async t=>{
  const v=mount(t);await flush();assert.equal(v.seen.length,0);assert.equal(v.state.dialect.value,'');await v.state.inspect();assert.equal(v.seen.length,0);
  v.state.dialect.value='sqlite';await flush();assert.equal(v.seen.length,0);await v.state.inspect();assert.equal(v.seen.length,1);assert.equal(v.state.data.value,undefined);
});
test('manual directory, exact literal page and version-pinned paging',async t=>{
  const v=mount(t);await select(v);assert.equal(v.seen.length,1);assert.equal(v.state.error.value,'');
  assert.deepEqual(v.seen[0].options,{kind:'tree',dialect:'sqlite'});
  await v.state.loadPage();assert.equal(v.state.error.value,'');assert.equal(v.seen[1].options.version,version);assert.deepEqual(v.state.data.value.rows,fixture.first.table.rows);
  await v.state.page(1);assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.rows,fixture.second.table.rows);
  v.state.columns.value=[...fixture.columns.selected.columns];v.state.rowOffset.value=0;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,3);
  await v.state.loadPage();assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.rows,fixture.columns.table.rows);
});
for(const action of ['disable','unmount','file','dialect','columns','offset','limit','table','plugin-budget'])test(`late SQL response after ${action} cannot publish`,async t=>{
  const p=pending(),v=mount(t,o=>o.kind==='tree'?result():p.promise);await select(v);const request=v.state.loadPage();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='changed';else if(action==='dialect')v.state.dialect.value='postgres';else if(action==='columns')v.state.columns.value=[0];else if(action==='offset')v.state.rowOffset.value=2;else if(action==='limit')v.state.rowLimit.value=1;else if(action==='table')v.state.tableId.value=fixture.empty.selected.table;else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen[1].signal.aborted,true);p.resolve(result('first'));await request;assert.equal(v.state.data.value,undefined);
});
test('identical identities retain selection; disabling erases state without request',async t=>{
  const v=mount(t);await select(v);await v.state.loadPage();v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,2);
  v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.state.data.value,undefined);assert.equal(v.state.dialect.value,'');assert.equal(v.seen.length,2);
});
for(const change of ['kind','version','source','dialect','catalog','selection','stats'])test(`SQL wrong ${change} result is refused`,async t=>{
  const v=mount(t,o=>{const r=defaultResponse(o);if(o.kind==='table'){if(change==='kind')r.kind='tree';if(change==='version')r.version='c'.repeat(64);if(change==='source')r.metadata.source_bytes++;if(change==='dialect')r.metadata.dialect='postgres';if(change==='catalog')r.payload.choices.tables[0].columns[0].declared_type='INTEGER';if(change==='selection')r.payload.selected.row_limit=1;if(change==='stats')r.metadata.source_rows++}return r});
  await select(v);await v.state.loadPage();assert.notEqual(v.state.error.value,'');assert.equal(v.state.data.value,undefined);
});
