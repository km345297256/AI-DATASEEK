import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/spatialWindowData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/domain-expansion-spatial-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64);
function result(kind='tree',storage='dense',window='window'){
 const {contract_version,type,reader,kind:privateKind,metadata,warnings,sampled,...payload}=structuredClone(fixture.cases[storage][kind==='tree'?'tree':window]);
 return {contract_version,plugin_id:'viz-spatial-window',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
for(const storage of ['dense','csr','csc'])for(const kind of ['tree','geometry'])test(`official AnnData ${storage} ${kind} remains compatible`,()=>{
 const r=result(kind,storage),p=helpers.parseSpatialWindow(kind,r.payload,r.metadata,r.payload.selected);
 assert.equal(p.storage,storage);assert.equal(p.coordinates.unit,null);
 if(kind==='geometry')assert.deepEqual(p.spatial,{observations:[1,2],x:[2,3],y:[20,30],values:[0,5]});
 else assert.equal(p.numericBytes,0);
});
const mutations=[
 ['wrong view',r=>r.payload.view_kind='series'],['extra path',r=>r.payload.host_path='/private/a'],
 ['format',r=>r.metadata.format='hdf5'],['unit',r=>r.metadata.coordinates.unit='um'],['axis order',r=>r.metadata.coordinates.axis_order='reverse'],
 ['source bool',r=>r.metadata.source_bytes=true],['input budget',r=>r.metadata.read_bytes=8388609],['count budget',r=>r.metadata.read_requests=129],
 ['chunk budget',r=>r.metadata.decoded_chunk_bytes=16777217],['attribute budget',r=>r.metadata.attribute_buffer_bytes=4097],
 ['raw semantics',r=>r.metadata.value_semantics='normalized'],['scope',r=>r.metadata.scope='whole file'],['strategy',r=>r.metadata.read_strategy='full dense'],
 ['identity field',r=>r.payload.spatial.names=['PATIENT']],['obs order',r=>r.payload.spatial.observations.reverse()],['obs bool',r=>r.payload.spatial.observations[0]=true],
 ['coord count',r=>r.payload.spatial.x.pop()],['coord inf',r=>r.payload.spatial.x[0]=Infinity],['expr inf',r=>r.payload.spatial.values[0]=Infinity],['expr bool',r=>r.payload.spatial.values[0]=true],
 ['float32 bound',r=>r.payload.spatial.values[0]=1e100],['null count',r=>r.metadata.null_expressions=1],['plottable',r=>r.metadata.plottable_points=0],
 ['decoded bytes',r=>r.metadata.numeric_bytes_read=0],['numeric count',r=>r.metadata.numeric_values_read=0],['sparse count',r=>r.metadata.sparse_entries_scanned=65537],
 ['feature labels',r=>r.payload.choices.feature_labels='gene names'],['feature choice',r=>r.payload.choices.feature_count=4],['spatial shape',r=>r.metadata.coordinates.shape=[4,3]],
 ['selection extra',r=>r.payload.selected.normalize=true],['selection bool',r=>r.payload.selected.feature=true],
];
for(const storage of ['dense','csr','csc'])for(const[name,mutate]of mutations)test(`${storage} rejects ${name}`,()=>{const r=result('geometry',storage);mutate(r);assert.throws(()=>helpers.parseSpatialWindow('geometry',r.payload,r.metadata));});
test('catalog identity ignores key ordering but binds dtype/storage/shape/source',()=>{
 const r=result(),a=helpers.parseSpatialWindow('tree',r.payload,r.metadata),b=structuredClone(a);
 b.matrix=Object.fromEntries(Object.entries(b.matrix).reverse());assert.equal(helpers.spatialCatalogMatches(a,b),true);
 for(const mutate of [v=>v.sourceBytes++,v=>v.matrix.dtype='>f4',v=>v.matrix.shape[1]++,v=>v.coordinates.dtype='<f4']){
  const c=structuredClone(a);mutate(c);assert.equal(helpers.spatialCatalogMatches(a,c),false);
 }
});
test('selection binding exact and ordinal limits reject before request',()=>{
 const r=result('geometry'),selection=r.payload.selected;
 for(const change of [{feature:0},{observation_start:0},{observation_count:1}])assert.throws(()=>helpers.parseSpatialWindow('geometry',r.payload,r.metadata,{...selection,...change}));
 for(const change of [{feature:3},{observation_start:4},{observation_count:8193},{decode:'normalized'}])assert.throws(()=>helpers.validateSpatialSelection({...selection,...change},{observations:4,features:3}));
});
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:value=>resolve(value)}};
function mount(t,{response=options=>result(options.kind,'dense',options.feature===0?'first':'window'),libraryWait,plotWait}={}){
 const seen={requests:[],plots:[],purged:[],elements:[],observers:[]};
 const lib={async newPlot(element,traces,layout){seen.plots.push({element,traces,layout});if(plotWait)await plotWait;element.rendered=true},purge(element){seen.purged.push(element);element.rendered=false},Plots:{resize(){}}};
 const document={createElement(){const v={dataset:{},style:{},removed:false,remove(){this.removed=true}};seen.elements.push(v);return v}};
 const ResizeObserver=class{constructor(){seen.observers.push(this)}observe(){}disconnect(){this.disconnected=true}};
 const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./spatialWindowData':helpers,
 '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.requests.push({file:structuredClone(vue.toRaw(file)),options,signal});return response(options)}},
 './scientific/browserLibraries':{loadBrowserLibrary:async()=>{if(libraryWait)await libraryWait;return lib}}};
 const source=readFileSync(new URL('../src/visualizations/extended/SpatialWindowPreview.vue',import.meta.url),'utf8');
 const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'spatial-window'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const module={exports:{}};new Function('require','module','exports','document','ResizeObserver',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},module,module.exports,document,ResizeObserver);
 const props=vue.reactive({file:{file_id:'file-spatial',filename:'synthetic.h5ad',size:fixture.cases.dense.tree.metadata.source_bytes,metadata:{dataset_file_version:'a'}},
 plugin:{id:'viz-spatial-window',adapter:'spatial-window',reader:'spatial-window',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'window',shared:false},limits:{max_input_bytes:8388608,max_output_bytes:2097152}}});
 const scope=vue.effectScope(),state=scope.run(()=>module.exports.default.setup(props,{expose(){}}));state.target.value={replaceChildren(){}};t.after(()=>scope.stop());
 return {props,state,seen,stop:()=>scope.stop()};
}
test('actual component first reads metadata only; explicit feature window preserves raw color and axes',async t=>{
 const v=mount(t);await flush();assert.equal(v.state.error.value,'');assert.equal(v.seen.requests.length,1);assert.equal(v.seen.plots.length,0);
 await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.equal(v.seen.requests[1].options.version,version);
 const p=v.seen.plots[0];assert.deepEqual(p.traces[0].x,[1,2,3,4]);assert.deepEqual(p.traces[0].y,[10,20,30,40]);assert.deepEqual(p.traces[0].marker.color,[0,3,0,7]);assert.equal(p.traces[0].mode,'markers');assert.notEqual(p.layout.yaxis.autorange,'reversed');assert.match(p.layout.xaxis.title.text,/单位未知/);
 v.state.feature.value=1;v.state.observationStart.value=1;v.state.observationCount.value=2;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.requests.length,2);assert.equal(v.seen.elements[0].removed,true);
 await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.spatial.values,[0,5]);
});
test('same revision clones do not reload and disable purges synchronously',async t=>{
 const v=mount(t);await flush();await v.state.loadWindow();
 for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush()}
 assert.equal(v.seen.requests.length,2);v.props.plugin.enabled=false;assert.equal(v.state.data.value,undefined);assert.equal(v.state.catalog.value,undefined);assert.equal(v.seen.elements[0].removed,true);assert.equal(v.seen.observers[0].disconnected,true);
});
for(const action of ['disable','unmount','file','feature','start','count','limits'])test(`late geometry after ${action} never revives`,async t=>{
 const task=pending(),v=mount(t,{response:options=>options.kind==='tree'?result():task.promise});await flush();const running=v.state.loadWindow();await flush();
 if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='different';else if(action==='feature')v.state.feature.value=1;else if(action==='start')v.state.observationStart.value=1;else if(action==='count')v.state.observationCount.value=1;else v.props.plugin.limits.max_input_bytes--;
 assert.equal(v.seen.requests[1].signal.aborted,true);task.resolve(result('geometry','dense','first'));await running;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.plots.length,0);
});
test('late tree response after disabled does not expose catalog',async t=>{const task=pending(),v=mount(t,{response:()=>task.promise});await flush();v.props.plugin.enabled=false;task.resolve(result());await flush();assert.equal(v.state.catalog.value,undefined)});
for(const phase of ['library','newPlot'])test(`late ${phase} remains purged after unmount`,async t=>{
 const task=pending(),v=mount(t,phase==='library'?{libraryWait:task.promise}:{plotWait:task.promise});await flush();const run=v.state.loadWindow();await flush();v.state.target.value=undefined;v.stop();task.resolve();await run;assert.equal(v.state.data.value?.spatial?.values.length??4,4);for(const el of v.seen.elements){assert.equal(el.removed,true);assert.equal(el.rendered,false);assert.ok(v.seen.purged.includes(el))}
});
for(const change of ['version','kind','source','dtype','coordinates','selection','filename'])test(`component refuses wrong ${change} identity`,async t=>{
 const v=mount(t,{response:options=>{const r=result(options.kind,'dense','first');if(options.kind==='tree'&&change==='filename')r.metadata.source_bytes++;
 if(options.kind!=='tree'){if(change==='version')r.version='c'.repeat(64);if(change==='kind')r.kind='series';if(change==='source')r.metadata.source_bytes++;if(change==='dtype')r.metadata.matrix.dtype='>f4';if(change==='coordinates')r.metadata.coordinates.dtype='>f8';if(change==='selection')r.payload.selected.feature=1}return r}});
 await flush();await v.state.loadWindow();assert.notEqual(v.state.error.value,'');assert.equal(v.seen.plots.length,0);
});
test('invalid user request has no geometry network call',async t=>{const v=mount(t);await flush();v.state.feature.value=3;await v.state.loadWindow();assert.equal(v.seen.requests.length,1);assert.notEqual(v.state.error.value,'')});
