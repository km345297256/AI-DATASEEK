import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/physicalDatabaseData.ts';
const originals=JSON.parse(readFileSync(new URL('./browser/physical-database-data.json',import.meta.url),'utf8'));
function result(reader='mysql-sdi'){
  const {kind,contract_version,type,metadata,warnings,sampled,reader:r,...rest}=structuredClone(originals[reader]);
  return {kind:'table',version:'a'.repeat(64),revision:'b'.repeat(64),payload:{...rest,view_kind:kind},metadata,warnings,sampled};
}
for(const reader of Object.keys(originals)){
  test(`${reader} native oracle satisfies exact frontend projection`,()=>{const r=result(reader),p=helpers.parsePhysical(r.payload,r.metadata,reader);assert.equal(p.rows.length,reader==='mysql-sdi'?8:3)});
  for(const [name,mutate] of [
    ['SQL',r=>r.payload.sql='SELECT 1'],['wrong kind',r=>r.payload.view_kind='table'],['execution options',r=>r.payload.selected.restore=true],
    ['true consistency',r=>r.metadata.logical_state_verified=true],['wrong version',r=>r.metadata.tool_version='99'],['path',r=>r.metadata.path='/Users/private'],
    ['budget',r=>r.metadata.limits.max_rows++],['size bool',r=>r.metadata.source_bytes=true],['oversize',r=>r.metadata.source_bytes=16777217],
    ['wrong format',r=>r.metadata.format='dmp'],['wrong count',r=>r.metadata.rows_returned++],['boolean count',r=>r.metadata.rows_returned=true],
    ['column mapping',r=>r.payload.table.columns.reverse()],['extra cell',r=>r.payload.table.rows[0].push('extra')],
    ['wrong row type',r=>r.payload.table.rows[0]={}],['unsafe label/key',r=>r.payload.table.rows[0][0]='/Users/private'],
  ])test(`${reader} rejects ${name}`,()=>{const r=result(reader);mutate(r);assert.throws(()=>helpers.parsePhysical(r.payload,r.metadata,reader))});
}
test('SST sequence precision, tombstones, empty and binary values stay distinct',()=>{
  const r=result('sst-records');r.payload.table.rows[0][2]='72057594037927935';
  const p=helpers.parsePhysical(r.payload,r.metadata,'sst-records');assert.equal(p.rows[0][2],'72057594037927935');assert.equal(p.rows[1][3],'deletion');assert.equal(p.rows[2][4],'610062');
  r.payload.table.rows[0][2]='72057594037927936';assert.throws(()=>helpers.parsePhysical(r.payload,r.metadata,'sst-records'));
});
for(const mutate of [r=>r.payload.table.rows[0][0]+='\n',r=>r.payload.table.rows[1][4]='01',r=>r.payload.table.rows[0][6]='both',r=>r.payload.table.rows[0][1]='true'])test('SST rejects malformed physical value binding',()=>{const r=result('sst-records');mutate(r);assert.throws(()=>helpers.parsePhysical(r.payload,r.metadata,'sst-records'))});
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
function mount(t,response=async()=>result()){
  const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./physicalDatabaseData':helpers,'../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,operation,options,signal});return response(options)}}};
  const source=readFileSync(new URL('../src/visualizations/extended/PhysicalDatabasePreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'physical-database'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
  const props=vue.reactive({file:{file_id:'physical-file',filename:'synthetic.ibd',size:originals['mysql-sdi'].metadata.source_bytes},plugin:{id:'viz-mysql-sdi',reader:'mysql-sdi',adapter:'physical-database',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {props,state,seen,stop:()=>scope.stop()};
}
test('physical directory search/page changes never trigger more reads',async t=>{const v=mount(t);await flush();assert.equal(v.state.error.value,'');v.state.search.value='decimal';assert.equal(v.state.filtered.value.length,1);v.state.page.value=1;v.state.search.value='';assert.equal(v.state.page.value,0);assert.equal(v.seen.length,1)});
for(const action of ['disable','unmount','file','plugin-budget'])test(`physical response after ${action} cannot publish`,async t=>{
  let resolve;const pending=new Promise(r=>resolve=r),v=mount(t,()=>pending);await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='changed';else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen[0].signal.aborted,true);resolve(result());await flush();if(action==='file'||action==='plugin-budget')assert.equal(v.seen.length,2);else assert.equal(v.state.data.value,undefined);
});
test('physical identical metadata does not reload',async t=>{const v=mount(t);await flush();v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,1)});
for(const [name,mutate] of [['kind',r=>r.kind='tree'],['version',r=>r.version='bad'],['source',r=>r.metadata.source_bytes++],['format',r=>r.metadata.format='sst']])test(`physical wrong ${name} rejected`,async t=>{const v=mount(t,async()=>{const r=result();mutate(r);return r});await flush();assert.equal(v.state.data.value,undefined);assert.notEqual(v.state.error.value,'')});
