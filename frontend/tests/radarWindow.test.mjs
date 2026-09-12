import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/radarWindowData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/domain-expansion-radar-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),options=fixture.positive.image.selected;
const flush=async()=>{for(let i=0;i<60;i++)await Promise.resolve();await vue.nextTick();};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:value=>resolve(value)};};
function result(kind='tree',group='positive'){
  const {contract_version,type,reader,metadata,warnings,sampled,kind:privateKind,...payload}=structuredClone(fixture[group][kind]);
  return {contract_version,kind:kind==='tree'?'tree':'array',plugin_id:'viz-radar-window',version,revision:'b'.repeat(64),metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
function parsed(kind='image',group='positive'){const r=result(kind,group);return helpers.parseRadarWindow(kind,r.payload,r.metadata,kind==='tree'?{}:options);}
for(const group of ['positive','negative'])test(`true HDF5 fixture ${group}: storage codes distinct from declared calibration and reserved values`,()=>{
  const tree=parsed('tree',group),data=parsed('image',group);assert.equal(tree.values,null);assert.equal(tree.metadata.numeric_bytes_read,0);
  assert.deepEqual(data.values,[255,0,18,19,26,27,28,29]);assert.deepEqual(data.range,[1375,1625,1875,2125]);assert.deepEqual(data.rays,[1,2]);assert.equal(data.sweeps[0].a1gate,3);
  const before=structuredClone(data.values),raw=helpers.radarDisplay(data,false),physical=helpers.radarDisplay(data,true);
  assert.deepEqual(raw.z,[[null,null,18,19],[26,27,28,29]]);assert.deepEqual(raw.flags,[[1,2,null,null],[null,null,null,null]]);
  assert.deepEqual(physical.z,group==='positive'?[[null,null,-23,-22.5],[-19,-18.5,-18,-17.5]]:[[null,null,-36.5,-36.75],[-38.5,-38.75,-39,-39.25]]);
  assert.deepEqual(data.values,before);assert.match(raw.label,/无物理单位/);assert.match(physical.label,/dBZ/);
});
const mutations=[['wrongview',r=>r.payload.view_kind='series'],['URL',r=>r.payload.url='https://example.invalid'],['path',r=>r.metadata.path='/private/a'],['format',r=>r.metadata.format='nc'],['version',r=>r.metadata.odim_version='ODIM_H5/V2_3'],['object',r=>r.metadata.object='PPI'],['bool source',r=>r.metadata.source_bytes=true],['oversource',r=>r.metadata.source_bytes=8589934593],['overread',r=>r.metadata.read_bytes=8388609],['overrequests',r=>r.metadata.read_requests=129],['headerbudget',r=>r.metadata.attribute_bytes=32769],['limits',r=>r.metadata.limits.max_rays=true],['decodedbudget',r=>r.metadata.decoded_chunk_bytes++],['chunkcount',r=>r.metadata.chunks_touched++],['numericbytes',r=>r.metadata.numeric_bytes_read++],['calibrationclaim',r=>r.metadata.value_semantics='calibrated'],['unitinjection',r=>r.payload.choices.sweeps[0].quantities[0].unit='<img src=x>'],['zerogain',r=>r.payload.choices.sweeps[0].quantities[0].gain=0],['boolgain',r=>r.payload.choices.sweeps[0].quantities[0].gain=true],['infiniteoffset',r=>r.payload.choices.sweeps[0].quantities[0].offset=Infinity],['reservedalias',r=>r.payload.choices.sweeps[0].quantities[0].undetect=255],['fractionreserved',r=>r.payload.choices.sweeps[0].quantities[0].nodata=254.5],['a1gate',r=>r.payload.choices.sweeps[0].a1gate=8],['rstart',r=>r.payload.choices.sweeps[0].rstart_m=-1],['rscale',r=>r.payload.choices.sweeps[0].rscale_m=0],['boolray',r=>r.payload.choices.sweeps[0].nrays=true],['elevation',r=>r.payload.choices.sweeps[0].elevation=91],['dtype',r=>r.payload.array.dtype='<i8'],['dimension',r=>r.payload.array.dimensions=['y','x']],['boolvalue',r=>r.payload.array.values[0]=true],['fractionvalue',r=>r.payload.array.values[0]=1.5],['overflowvalue',r=>r.payload.array.values[0]=256],['badshape',r=>r.payload.array.shape=[4,2]],['missingvalue',r=>r.payload.array.values.pop()],['rangeedge',r=>r.payload.radar.range_m[0]=1250],['rotation',r=>r.payload.radar.ray_indices=[4,5]],['nullcount',r=>r.payload.radar.nodata_count=0],['undetectcount',r=>r.payload.radar.undetect_count=2],['physicaldecode',r=>r.payload.selected.decode='physical']];
for(const [name,change]of mutations)test(`strict Radar rejects ${name}`,()=>{const r=result('image');change(r);assert.throws(()=>helpers.parseRadarWindow('image',r.payload,r.metadata));});
test('tree requires metadata-only array-free result',()=>{for(const change of [r=>r.payload.array={},r=>r.metadata.numeric_bytes_read=1,r=>r.payload.tree[0].shape[0]=true]){const r=result();change(r);assert.throws(()=>helpers.parseRadarWindow('tree',r.payload,r.metadata));}});
for(const edit of [{sweep:true},{quantity:'constructor'},{ray_count:129},{ray_start:7},{gate_start:11},{gate_count:0},{decode:'calibrated'},{extra:1}])test(`strict selection ${JSON.stringify(edit)}`,()=>assert.throws(()=>helpers.validateRadarOptions('image',{...options,...edit},parsed('tree').sweeps)));
test('request/result binding ignores key order but not a different valid window',()=>{
  const r=result('image');assert.ok(helpers.parseRadarWindow('image',r.payload,r.metadata,{decode:'raw',gate_count:4,gate_start:1,ray_count:2,ray_start:1,quantity:'DBZH',sweep:1}));
  assert.throws(()=>helpers.parseRadarWindow('image',r.payload,r.metadata,{...options,ray_start:0}));
});
function mount(t,{response=req=>result(req.kind),libraryWait,plotWait}={}){
  const seen={requests:[],libraries:[],plots:[],purged:[],observers:[],elements:[]};
  const lib={async newPlot(element,traces,layout){seen.plots.push({element,traces,layout});if(plotWait)await plotWait;element.rendered=true;},purge(element){seen.purged.push(element);element.rendered=false;},Plots:{resize(){}}};
  const observer=class {constructor(){this.disconnected=false;seen.observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
  const document={createElement(){const el={dataset:{},style:{},rendered:false,removed:false,remove(){this.removed=true;}};seen.elements.push(el);return el;}};
  const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./radarWindowData':helpers,
    './scientific/browserLibraries':{loadBrowserLibrary:async(name,signal)=>{seen.libraries.push({name,signal});if(libraryWait)await libraryWait;return lib;}},
    '../runtime':{requestVisualization:async(file,plugin,operation,request,signal)=>{seen.requests.push({request,signal});return response(request);}}};
  const source=readFileSync(new URL('../src/visualizations/extended/RadarWindowPreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'radar-window'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module={exports:{}};new Function('require','module','exports','document','ResizeObserver',compiled)(id=>{assert.ok(id in modules,id);return modules[id];},module,module.exports,document,observer);
  const props=vue.reactive({file:{file_id:'dataset-preview:radar-a',filename:'synthetic.h5',size:fixture.positive.tree.metadata.source_bytes,metadata:{dataset_file_version:'a'}},plugin:{id:'viz-radar-window',version:'1',adapter:'radar-window',reader:'radar-window',enabled:true,capabilities:{operations:['preview'],input_mode:'window',shared:false},limits:{max_input_bytes:8388608,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>module.exports.default.setup(props,{expose(){}}));state.target.value={replaceChildren(){}};t.after(()=>scope.stop());return {state,props,seen,stop:()=>scope.stop()};
}
function select(v){v.state.rayStart.value=1;v.state.rayCount.value=2;v.state.gateStart.value=1;v.state.gateCount.value=4;}
test('actual Vue: tree-first exact window, cached local calibration and rapid toggles do not read again',async t=>{
  const v=mount(t);await flush();assert.equal(v.state.error.value,'');assert.equal(v.state.busy.value,false);assert.equal(v.seen.requests.length,1);assert.equal(v.seen.plots.length,0);
  select(v);await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.deepEqual(v.seen.requests[1].request,{kind:'image',version,...options});assert.deepEqual(v.seen.plots[0].traces[0].z,[[null,null,18,19],[26,27,28,29]]);
  v.state.calibrated.value=true;await flush();assert.deepEqual(v.seen.plots.at(-1).traces[0].z,[[null,null,-23,-22.5],[-19,-18.5,-18,-17.5]]);assert.equal(v.seen.elements[0].removed,true);
  v.state.calibrated.value=false;v.state.calibrated.value=true;v.state.calibrated.value=false;await flush();assert.equal(v.state.error.value,'');assert.equal(v.seen.requests.length,2);assert.deepEqual(v.seen.plots.at(-1).traces[0].z,[[null,null,18,19],[26,27,28,29]]);
});
test('cloned catalogs do not reread; disable purges synchronously',async t=>{
  const v=mount(t);await flush();select(v);await v.state.loadWindow();for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();}assert.equal(v.seen.requests.length,2);
  v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.elements[0].removed,true);assert.equal(v.seen.observers[0].disconnected,true);
});
for(const action of ['disable','unmount','selection','file','limits','cancel'])test(`late window after ${action} cannot revive`,async t=>{
  const task=pending(),v=mount(t,{response:req=>req.kind==='tree'?result():task.promise});await flush();select(v);const running=v.state.loadWindow();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='selection')v.state.rayStart.value=0;else if(action==='file')v.props.file.file_id='dataset-preview:radar-b';else if(action==='limits')v.props.plugin.limits.max_input_bytes--;else v.state.cancel();
  assert.equal(v.seen.requests[1].signal.aborted,true);task.resolve(result('image'));await running;assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.plots.length,0);
});
for(const phase of ['library','newPlot'])test(`late ${phase} cleaned even after ref clears`,async t=>{
  const task=pending(),v=mount(t,phase==='library'?{libraryWait:task.promise}:{plotWait:task.promise});await flush();select(v);const running=v.state.loadWindow();await flush();v.state.target.value=undefined;v.stop();task.resolve();await running;
  assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.libraries[0].signal.aborted,true);for(const el of v.seen.elements){assert.equal(el.removed,true);assert.equal(el.rendered,false);assert.ok(v.seen.purged.includes(el));}
});
for(const change of ['version','public-kind','selection','gain','source','format'])test(`component rejects ${change} before rendering`,async t=>{
  const v=mount(t,{response:req=>{const r=result(req.kind);if(req.kind==='image'){if(change==='version')r.version='c'.repeat(64);if(change==='public-kind')r.kind='series';if(change==='selection')r.payload.selected.sweep=2;if(change==='gain')r.payload.choices.sweeps[0].quantities[0].gain=3;if(change==='source')r.metadata.source_bytes++;if(change==='format')r.metadata.format='hdf5';}return r;}});
  await flush();select(v);await v.state.loadWindow();assert.equal(v.state.displayed.value,undefined);assert.ok(v.state.error.value);assert.equal(v.seen.plots.length,0);
});
test('invalid ROI never reaches API; disabled late tree never revives',async t=>{
  const v=mount(t);await flush();v.state.rayStart.value=8;await v.state.loadWindow();assert.ok(v.state.error.value);assert.equal(v.seen.requests.length,1);
  const task=pending(),late=mount(t,{response:()=>task.promise});await flush();late.props.plugin.enabled=false;task.resolve(result());await flush();assert.equal(late.state.catalog.value,undefined);
});
