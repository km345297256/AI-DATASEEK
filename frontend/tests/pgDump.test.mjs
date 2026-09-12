import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/pgDumpData.ts';
const version='a'.repeat(64),revision='b'.repeat(64);
function result(count=3){return {kind:'tree',version,revision,payload:{view_kind:'tree',media_type:'application/json',choices:{},selected:{},tree:Array.from({length:count},(_,i)=>({path:'/o-'+i.toString(16).padStart(24,'0'),node_type:'group',attributes:{object_type:i%2?'TABLE':'INDEX',schema:'science lab',name:'测量 data '+i}}))},metadata:{engine:'pg-dump',format:'dump',container:'PostgreSQL custom archive',archive_version:'1.16.0',source_bytes:2048,input_mode:'whole',objects_returned:count,objects_total:count,data_verified:false,limits:structuredClone(helpers.PG_LIMITS)}}}
test('PG directory preserves only approved bounded object metadata',()=>{const r=result(),p=helpers.parsePgDump(r.payload,r.metadata);assert.equal(p.objects.length,3);assert.equal(p.archiveVersion,'1.16.0');assert.equal(p.objects[0].attributes.name,'测量 data 0')});
for(const [name,mutate] of [
  ['SQL',r=>r.payload.sql='DROP DATABASE test'],['owner',r=>r.metadata.owner='secret'],['path',r=>r.metadata.path='/Users/private'],
  ['data integrity promise',r=>r.metadata.data_verified=true],['unknown version',r=>r.metadata.archive_version='1.17.0'],['wrong budget',r=>r.metadata.limits.max_objects=1025],
  ['source boolean',r=>r.metadata.source_bytes=true],['oversize source',r=>r.metadata.source_bytes=16777217],['unapproved format',r=>r.metadata.format='bak'],
  ['count mismatch',r=>r.metadata.objects_total=4],['page options',r=>r.payload.selected.offset=1],['tree extra SQL',r=>r.payload.tree[0].sql='SELECT 1'],
  ['duplicate object id',r=>r.payload.tree[1].path=r.payload.tree[0].path],['raw object id',r=>r.payload.tree[0].path='/private/foo'],
  ['unsafe name',r=>r.payload.tree[0].attributes.name='/Users/private'],['HTML name',r=>r.payload.tree[0].attributes.name='<img>'],
  ['unsafe schema',r=>r.payload.tree[0].attributes.schema='https://private'],['surrogate',r=>r.payload.tree[0].attributes.name='\ud800'],
  ['overlong label',r=>r.payload.tree[0].attributes.name='x'.repeat(129)],['unknown type',r=>r.payload.tree[0].attributes.object_type='SCRIPT'],
  ['node metadata extra',r=>r.payload.tree[0].attributes.owner='secret'],['wrong view',r=>r.payload.view_kind='table'],
  ['database name',r=>r.payload.tree[0].attributes.object_type='DATABASE'],['comment tag',r=>r.payload.tree[0].attributes.object_type='COMMENT'],['user mapping',r=>r.payload.tree[0].attributes.object_type='USER MAPPING'],
])test(`PG rejects ${name}`,()=>{const r=result();mutate(r);assert.throws(()=>helpers.parsePgDump(r.payload,r.metadata))});
test('PG limits entire TOC and permits fixed omission labels',()=>{const r=result(1024);assert.equal(helpers.parsePgDump(r.payload,r.metadata).objects.length,1024);assert.throws(()=>{const v=result(1025);helpers.parsePgDump(v.payload,v.metadata)});r.payload.tree[0].attributes.name='名称已隐藏';assert.doesNotThrow(()=>helpers.parsePgDump(r.payload,r.metadata))});
const fixtureURL=new URL('./browser/pg-dump-data.json',import.meta.url);
test('native PG custom and tar payloads satisfy frontend schema',()=>{
  assert.ok(existsSync(fixtureURL),'Native PG fixtures are required');
  const fixtures=JSON.parse(readFileSync(fixtureURL,'utf8'));
  for(const p of Object.values(fixtures)){const {kind,contract_version,type,reader,metadata,warnings,sampled,...payload}=p;const parsed=helpers.parsePgDump({...payload,view_kind:kind},metadata);assert.ok(parsed.objects.length>0)}
});
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
function mount(t,response=async()=>result()){
  const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./pgDumpData':helpers,'../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,operation,options,signal});return response(options)}}};
  const source=readFileSync(new URL('../src/visualizations/extended/PostgresDumpPreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'pg-dump'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
  const props=vue.reactive({file:{file_id:'pg-file',filename:'synthetic.dump',size:2048},plugin:{id:'viz-postgres-dump',reader:'pg-dump',adapter:'database-dump',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {props,state,seen,stop:()=>scope.stop()};
}
test('PG filters and paginates entirely locally without fetching SQL or data',async t=>{const v=mount(t,async()=>result(250));await flush();assert.equal(v.state.error.value,'');assert.equal(v.state.visible.value.length,100);v.state.page.value=2;assert.equal(v.state.visible.value.length,50);v.state.type.value='TABLE';assert.equal(v.state.page.value,0);assert.equal(v.state.filtered.value.length,125);v.state.search.value='data 2';assert.ok(v.state.filtered.value.length<125);assert.equal(v.seen.length,1);assert.deepEqual(v.seen[0].options,{kind:'tree'})});
for(const action of ['disable','unmount','file','plugin-budget'])test(`PG stale response after ${action} cannot publish`,async t=>{
  let resolve;const pending=new Promise(r=>resolve=r),v=mount(t,()=>pending);await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='changed';else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen[0].signal.aborted,true);resolve(result());await flush();if(action==='file'||action==='plugin-budget')assert.equal(v.seen.length,2);else assert.equal(v.state.data.value,undefined);
});
test('PG identical identity replacements do not reload and disabling removes data',async t=>{const v=mount(t);await flush();v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,1);v.props.plugin.enabled=false;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,1)});
for(const [name,mutate] of [['kind',r=>r.kind='table'],['version',r=>r.version='bad'],['source',r=>r.metadata.source_bytes++],['format',r=>r.metadata.format='backup']])test(`PG wrong ${name} rejected`,async t=>{const v=mount(t,async()=>{const r=result();mutate(r);return r});await flush();assert.equal(v.state.data.value,undefined);assert.notEqual(v.state.error.value,'')});
