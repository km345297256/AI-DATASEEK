import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import * as data from '../src/visualizations/extended/geometryData.ts';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/geometry-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64);
const modes=['gro-trajectory','simulation-mesh'];
const prefix=mode=>mode==='gro-trajectory'?'gro':'mesh';
const option=mode=>mode==='gro-trajectory'?{frame:1}:{field:'p-0',component:1};
function envelope(mode,kind='tree',variant='geometry') {
 const {type,reader,contract_version,kind:view_kind,metadata,warnings,sampled,...payload}=structuredClone(fixture[`${prefix(mode)}_${kind==='tree'?'tree':variant}`]);
 return {contract_version,kind:kind==='tree'?'tree':'geometry',version,revision,metadata,warnings,sampled,payload:{...payload,view_kind},plugin_id:`viz-${mode}`};
}
for(const mode of modes) {
 test(`${mode} real-reader selected geometry and fixed units`,()=>{
  const value=data.parseGeometryData(envelope(mode,'geometry'),mode,'geometry',option(mode)),scene=data.sceneGeometry(value),local=data.localScene(scene);
  assert.equal(data.geometryCatalogIdentity(value),data.geometryCatalogIdentity(data.parseGeometryData(envelope(mode),mode,'tree',{})));
  if(mode==='gro-trajectory'){assert.deepEqual(value.trajectory.positions[0],[0.01,0,0]);assert.deepEqual(value.trajectory.velocities[0],[-0.001,0.002,0]);assert.equal(scene.edges.length,0);assert.equal(value.frames[1].time_ps,0.25);}
  else {assert.deepEqual(scene.values,[2,5,8,11,14]);assert.equal(scene.edges.length,9);assert.equal(scene.association,'point');}
  assert.ok([...local.positions].every(Number.isFinite));assert.equal(local.positions[0],0);
 });
 const mutations={version:r=>r.version='invalid',revision:r=>r.revision='',sampled:r=>r.sampled=true,warnings:r=>r.warnings=['guess'],format:r=>r.metadata.format='exe',source:r=>r.metadata.source_bytes=16777217,metadata:r=>r.metadata.path='/Users/secret',payload:r=>r.payload.url='https://invalid',selected:r=>r.payload.selected.extra=1,media:r=>r.payload.media_type='text/html',kind:r=>r.kind='series'};
 for(const [name,mutate] of Object.entries(mutations))test(`${mode} rejects ${name}`,()=>{const r=envelope(mode,'geometry');mutate(r);assert.throws(()=>data.parseGeometryData(r,mode,'geometry',option(mode)));});
 for(const [name,kwargs] of [['size',[1,undefined]],['filename',[undefined,'x.csv']]])test(`${mode} source binding ${name}`,()=>assert.throws(()=>data.parseGeometryData(envelope(mode,'geometry'),mode,'geometry',option(mode),...kwargs)));
}
test('VTU cell values at arithmetic mean of vertices, no interpolation',()=>{
 const value=data.parseGeometryData(envelope('simulation-mesh','geometry','cell'),'simulation-mesh','geometry',{field:'c-0',component:0}),s=data.sceneGeometry(value);
 assert.deepEqual(s.markers,[[.25,.25,.25],[.5,.5,.5]]);assert.deepEqual(s.values,[-2,8]);assert.equal(s.association,'cell');
});
test('VTU null field values stay null and geometry-only has no invented field',()=>{
 const v=data.parseGeometryData(envelope('simulation-mesh','geometry','null'),'simulation-mesh','geometry',{field:'p-1',component:0});assert.equal(data.sceneGeometry(v).values[2],null);
 const only=data.parseGeometryData(envelope('simulation-mesh','geometry','only'),'simulation-mesh','geometry',{field:null,component:0});assert.equal(data.sceneGeometry(only).values,null);
});
test('equal-axis local origin applied before float32 conversion',()=>{
 const s={points:[[1e12,1e12,1e12],[1e12+.01,1e12+.02,1e12+.04]],markers:[[1e12,1e12,1e12],[1e12+.01,1e12+.02,1e12+.04]],edges:[],values:null,association:'atoms'},v=data.localScene(s);
 assert.equal(v.positions[0],0);assert.ok(v.positions[3]>.2&&v.positions[3]<.3);assert.ok(v.positions[4]>.4&&v.positions[4]<.6);assert.equal(v.positions[5],1);
});
for(const [name,mutate] of Object.entries({coordinates:r=>r.payload.trajectory.positions[0][0]=NaN,atom:r=>r.payload.trajectory.atoms[0].atom_name='<img>',velocity:r=>r.payload.trajectory.velocities=null,time:r=>r.payload.choices.frames[1].time_ps=Infinity,box:r=>r.payload.choices.frames[1].box[3]=1,shape:r=>r.payload.trajectory.positions.pop()}))test(`GRO invalid ${name}`,()=>{const r=envelope('gro-trajectory','geometry');mutate(r);assert.throws(()=>data.parseGeometryData(r,'gro-trajectory','geometry',{frame:1}));});
for(const [name,mutate] of Object.entries({indices:r=>r.payload.mesh.cells[0].points[0]=99,duplicate:r=>r.payload.mesh.cells[0].points[0]=1,type:r=>r.payload.mesh.cells[0].type=42,association:r=>r.payload.mesh.field.association='cell',component:r=>r.payload.mesh.field.component=0,label:r=>r.payload.choices.fields[0].name='/Users/secret',nan:r=>r.payload.mesh.field.values[0]=NaN}))test(`VTU invalid ${name}`,()=>{const r=envelope('simulation-mesh','geometry');mutate(r);assert.throws(()=>data.parseGeometryData(r,'simulation-mesh','geometry',{field:'p-0',component:1}));});
const flush=async()=>{for(let i=0;i<60;i++)await Promise.resolve();await vue.nextTick();};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)};};
function mount(t,mode,request=o=>envelope(mode,o.kind)) {
 const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'../runtime':{requestVisualization:async(f,p,op,o,signal)=>{seen.push({o,signal});return request(o,signal);}},'./domains/lifecycle':{displayError:e=>e.message},'./GeometryScene.vue':{default:{}},'./geometryData':data};
 const source=readFileSync(new URL('../src/visualizations/extended/BoundedGeometryPreview.vue',import.meta.url),'utf8'),compiled=compileScript(parse(source).descriptor,{id:'geometry'}).content;
 const module={exports:{}};new Function('require','module','exports',ts.transpileModule(compiled,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(name=>{assert.ok(name in modules,name);return modules[name];},module,module.exports);
 const scope=vue.effectScope(),props=vue.reactive({mode,file:{file_id:'opaque',filename:mode==='gro-trajectory'?'synthetic.gro':'synthetic.vtu',size:fixture[`${prefix(mode)}_tree`].metadata.source_bytes},plugin:{id:`viz-${mode}`,enabled:true,version:'1.0.0',capabilities:{operations:['preview'],shared:false,input_mode:'whole'}}});
 const state=scope.run(()=>module.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {scope,props,state,seen};
}
async function choose(v){if(v.props.mode==='gro-trajectory')v.state.frameId.value=1;else{v.state.fieldId.value='p-0';v.state.componentId.value=1;}await flush();}
for(const mode of modes){
 test(`${mode} metadata first, explicit pinned geometry, clones preserve scene`,async t=>{const v=mount(t,mode);await flush();assert.deepEqual(v.seen[0].o,{kind:'tree'});assert.equal(v.state.displayed.value,undefined);await choose(v);await v.state.loadGeometry();assert.equal(v.state.error.value,'');assert.ok(v.state.displayed.value);assert.deepEqual(v.seen[1].o,{kind:'geometry',version,...option(mode)});v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,2);assert.ok(v.state.displayed.value);});
 for(const action of ['unmount','disable','file','selection'])test(`${mode} ${action} cancels and ignores late response`,async t=>{const p=pending(),v=mount(t,mode,o=>o.kind==='geometry'?p.promise:envelope(mode));await flush();await choose(v);const loading=v.state.loadGeometry();await flush();const req=v.seen.at(-1);if(action==='unmount')v.scope.stop();else if(action==='disable')v.props.plugin.enabled=false;else if(action==='file')v.props.file.file_id='other';else if(mode==='gro-trajectory')v.state.frameId.value=0;else v.state.componentId.value=0;await flush();assert.equal(req.signal.aborted,true);p.resolve(envelope(mode,'geometry'));await loading;assert.equal(v.state.displayed.value,undefined);});
 for(const action of ['version','catalog','selection'])test(`${mode} refuses changed ${action}`,async t=>{const v=mount(t,mode,o=>{const r=envelope(mode,o.kind);if(o.kind==='geometry'){if(action==='version')r.version='c'.repeat(64);if(action==='catalog')r.metadata.source_bytes++;if(action==='selection')r.payload.selected={};}return r;});await flush();await choose(v);await v.state.loadGeometry();assert.ok(v.state.error.value);assert.equal(v.state.displayed.value,undefined);});
}
