import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/sqliteTableData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/sqlite-table-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64),table=fixture.first.selected.table;
function result(key='tree'){
 const {contract_version,type,reader,kind,metadata,warnings,sampled,...payload}=structuredClone(fixture[key]);
 return {contract_version,plugin_id:'viz-sqlite-table',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
for(const key of Object.keys(fixture))test(`actual native SQLite fixture ${key} passes strict frontend contract`,()=>{
 const r=result(key),p=helpers.parseSqliteTable(r.kind,r.payload,r.metadata,key==='tree'?undefined:r.payload.selected);
 assert.equal(p.format,'sqlite');if(key==='tree')assert.equal(p.rows,null);else assert.deepEqual(p.rowIds,r.payload.table.row_ids);
});
test('int64 stays text, BLOB never decoded, typed omissions and null distinguish',()=>{
 const r=result('first'),p=helpers.parseSqliteTable('table',r.payload,r.metadata);
 assert.equal(p.rows[0][1].value,'-9223372036854775808');assert.equal(p.rows[1][1].value,'9223372036854775807');
 assert.equal(helpers.sqliteCellText(p.rows[0][4]),'BLOB · 3 字节（未解码）');assert.equal(helpers.sqliteCellText(p.rows[0][5]),'NULL');
 const s=result('second'),q=helpers.parseSqliteTable('table',s.payload,s.metadata);
 assert.match(helpers.sqliteCellText(q.rows[0][3]),/513.*超过单元格预算/);assert.equal(helpers.sqliteCellText(q.rows[1][2]),'非有限实数');assert.match(helpers.sqliteCellText(q.rows[1][3]),/不安全文本/);
});
const mutations=[
 ['kind',r=>r.payload.view_kind='array'],['extra SQL',r=>r.payload.sql='SELECT 1'],['extra meta',r=>r.metadata.host_path='/tmp/x'],
 ['source bool',r=>r.metadata.source_bytes=true],['source limit',r=>r.metadata.source_bytes=16777217],['format',r=>r.metadata.format='parquet'],['mode',r=>r.metadata.input_mode='window'],
 ['schema source',r=>r.metadata.schema_bytes=r.metadata.source_bytes+1],['schema budget',r=>r.metadata.schema_bytes=65537],['callbacks',r=>r.metadata.progress_callbacks=2001],['limit',r=>r.metadata.limits.max_columns=17],
 ['semantics',r=>r.metadata.value_semantics='coerced'],['count known',r=>r.metadata.total_rows_known=true],['source missing',r=>delete r.metadata.source_bytes],
 ['table order',r=>r.payload.choices.tables.reverse()],['unsafe table',r=>r.payload.choices.tables[0].label='not a table'],['duplicate token',r=>r.payload.choices.tables[0].id=r.payload.choices.tables[1].id],
 ['column bool',r=>r.payload.choices.tables[1].columns[0].id=true],['column label',r=>r.payload.choices.tables[1].columns[0].label='a;DROP'],['affinity',r=>r.payload.choices.tables[1].columns[0].affinity='UNKNOWN'],['primary key',r=>r.payload.choices.tables[1].columns[0].primary_key=2],
 ['columns false',r=>r.payload.table.column_ids[0]=false],['rowids numeric',r=>r.payload.table.row_ids[0]=-5],['rowids out of range',r=>r.payload.table.row_ids[0]='-9223372036854775809'],['rowids reverse',r=>r.payload.table.row_ids.reverse()],['rowids duplicate',r=>r.payload.table.row_ids[1]=r.payload.table.row_ids[0]],
 ['integer number',r=>r.payload.table.rows[0][1].value=9007199254740992],['integer overflow',r=>r.payload.table.rows[0][1].value='9223372036854775808'],['integer -0',r=>r.payload.table.rows[0][1].value='-0'],['integer +1',r=>r.payload.table.rows[0][1].value='+1'],
 ['real nonfinite',r=>r.payload.table.rows[0][2].value=Infinity],['real bool',r=>r.payload.table.rows[0][2].value=true],['text budget',r=>r.payload.table.rows[0][3].value='a'.repeat(513)],['text path',r=>r.payload.table.rows[0][3].value='/Users/a'],['text surrogate',r=>r.payload.table.rows[0][3].value='\ud800'],
 ['blob allocation',r=>r.payload.table.rows[0][4].bytes=65537],['blob data leak',r=>r.payload.table.rows[0][4].value='AAAA'],['null invalid',r=>r.payload.table.rows[0][5].value=0],['row length',r=>r.payload.table.rows[0].pop()],['row statistics',r=>r.metadata.rows_returned=0],['blob statistics',r=>r.metadata.blob_values=0],
 ['selection',r=>r.payload.selected.columns=[1]],['page offset',r=>r.payload.table.row_offset=1],['ordering',r=>r.payload.table.ordering='label'],['has more bool',r=>r.payload.table.has_more=1],
];
for(const [label,mutate] of mutations)test(`reject ${label}`,()=>{const r=result('first');mutate(r);assert.throws(()=>helpers.parseSqliteTable('table',r.payload,r.metadata))});
test('exact request, catalog equality and tree zero-user-data boundaries',()=>{
 const r=result('first');for(const patch of [{table:fixture.empty.selected.table},{columns:[1]},{row_offset:1},{row_limit:1}])assert.throws(()=>helpers.parseSqliteTable('table',r.payload,r.metadata,{...r.payload.selected,...patch}));
 const tree=result(),a=helpers.parseSqliteTable('tree',tree.payload,tree.metadata),b=structuredClone(a);b.tables[1].columns[0]=Object.fromEntries(Object.entries(b.tables[1].columns[0]).reverse());assert.equal(helpers.sqliteCatalogMatches(a,b),true);b.schemaBytes++;assert.equal(helpers.sqliteCatalogMatches(a,b),false);
 tree.payload.tree[0].attributes.columns=true;assert.throws(()=>helpers.parseSqliteTable('tree',tree.payload,tree.metadata));
});
for(const patch of [{columns:[false]},{columns:[0,0]},{columns:[]},{columns:Array.from({length:17},(_,i)=>i)},{row_offset:-1},{row_offset:100001},{row_limit:201},{row_limit:0},{table:'measurements'},{sql:'SELECT 1'}])test(`invalid selection ${JSON.stringify(patch)}`,()=>assert.throws(()=>helpers.validateSqliteSelection({...fixture.first.selected,...patch})));
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)}};
const defaultResponse=options=>result(options.kind==='tree'?'tree':options.table===fixture.empty.selected.table?'empty':options.columns.length===2?'columns':options.row_offset===0?'first':options.row_offset===2?'second':'last');
function mount(t,response=defaultResponse){
 const seen=[];const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./sqliteTableData':helpers,
 '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({options,signal,file,plugin});return response(options)}}};
 const source=readFileSync(new URL('../src/visualizations/extended/SqliteTablePreview.vue',import.meta.url),'utf8');
 const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'sqlite-table'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
 const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
 const props=vue.reactive({file:{file_id:'sqlite-file',filename:'synthetic.sqlite',size:fixture.tree.metadata.source_bytes,metadata:{dataset_file_version:'a'}},
 plugin:{id:'viz-sqlite-table',reader:'sqlite-table',adapter:'sqlite-table',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
 const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());
 return {props,state,seen,stop:()=>scope.stop()};
}
async function select(v){await flush();v.state.tableId.value=table;v.state.rowLimit.value=2;await flush()}
test('actual component metadata only, exact integer page, manual column choice and version-pinned pagination',async t=>{
 const v=mount(t);await select(v);assert.equal(v.state.error.value,'');assert.equal(v.seen.length,1);assert.equal(v.state.data.value,undefined);
 await v.state.loadPage();assert.equal(v.state.error.value,'');assert.equal(v.seen[1].options.version,version);assert.equal(v.state.data.value.rows[0][1].value,'-9223372036854775808');
 await v.state.page(1);assert.equal(v.state.data.value.rowIds[0],'9');assert.equal(v.seen[2].options.row_offset,2);
 await v.state.page(-1);assert.equal(v.state.data.value.rowIds[0],'-5');
 v.state.columns.value=[1,3];assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,4);await v.state.loadPage();assert.equal(v.state.error.value,'');assert.equal(v.state.data.value.rows[1][0].value,'9223372036854775807');
});
test('same identities no reload; disabling clears synchronously and does not read',async t=>{
 const v=mount(t);await select(v);await v.state.loadPage();
 for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush()}
 assert.equal(v.seen.length,2);v.props.plugin.enabled=false;assert.equal(v.state.data.value,undefined);assert.equal(v.state.catalog.value,undefined);assert.equal(v.seen.length,2);
});
for(const action of ['disable','unmount','file','table','columns','offset','limit','plugin-budget'])test(`late data after ${action} never publishes`,async t=>{
 const task=pending(),v=mount(t,options=>options.kind==='tree'?result():task.promise);await select(v);const request=v.state.loadPage();await flush();
 if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='other';else if(action==='table')v.state.tableId.value=fixture.empty.selected.table;else if(action==='columns')v.state.columns.value=[1];else if(action==='offset')v.state.rowOffset.value=2;else if(action==='limit')v.state.rowLimit.value=1;else v.props.plugin.limits.max_input_bytes--;
 assert.equal(v.seen[1].signal.aborted,true);task.resolve(result('first'));await request;assert.equal(v.state.data.value,undefined);
});
for(const change of ['kind','version','source','format','catalog','selection'])test(`component refuses wrong ${change} response`,async t=>{
 const v=mount(t,options=>{const r=defaultResponse(options);if(options.kind==='table'){if(change==='kind')r.kind='tree';if(change==='version')r.version='c'.repeat(64);if(change==='source')r.metadata.source_bytes++;if(change==='format')r.metadata.format='db';if(change==='catalog')r.payload.choices.tables[1].columns[0].affinity='TEXT';if(change==='selection')r.payload.selected.row_limit=1}return r});
 await select(v);await v.state.loadPage();assert.notEqual(v.state.error.value,'');assert.equal(v.state.data.value,undefined);
});
test('late catalog after close or disabled cannot populate',async t=>{const task=pending(),v=mount(t,()=>task.promise);await flush();v.props.plugin.enabled=false;task.resolve(result());await flush();assert.equal(v.state.catalog.value,undefined)});
test('catalog filename/source bound and invalid selection never submits table request',async t=>{
 const v=mount(t);await select(v);v.state.columns.value=[127];await v.state.loadPage();assert.equal(v.seen.length,1);assert.notEqual(v.state.error.value,'');
 const w=mount(t,()=>{const r=result();r.metadata.source_bytes++;return r});await flush();assert.equal(w.state.catalog.value,undefined);assert.notEqual(w.state.error.value,'');
});
