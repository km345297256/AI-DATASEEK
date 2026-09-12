import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/rippleWindowData.ts';
const fixture = JSON.parse(readFileSync(new URL('./browser/domain-expansion-ripple-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64), imageOptions = fixture.image.selected, seriesOptions = fixture.series.selected;
const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function result(kind = 'tree', calibrated = true) {
  const { contract_version, type, reader, metadata, warnings, sampled, kind: privateKind, ...payload } = structuredClone((calibrated ? fixture : fixture.index)[kind]);
  return { contract_version, kind: kind === 'tree' ? 'tree' : 'array', plugin_id: 'viz-ripple-window', version, revision: 'b'.repeat(64), metadata, warnings, sampled, payload: { ...payload, view_kind: kind } };
}
for (const calibrated of [true,false]) test(`true NumPy fixture ${calibrated ? 'declared physical' : 'index'} coordinates and read counts`, () => {
  for (const kind of ['tree','image','series']) {
    const r = result(kind, calibrated), data = helpers.parseRippleWindow(kind,r.payload,r.metadata,r.payload.selected);
    assert.equal(data.sourceBytes,data.headerBytes+392); assert.equal(data.cube.dtype,'float32');
    if (kind==='tree') { assert.equal(data.array,null); assert.equal(data.readBytes,data.headerBytes); assert.equal(data.reads,1); }
    else { assert.deepEqual(data.array.values,fixture[kind].array.values); assert.equal(data.reads,2); }
    if (kind==='image') { assert.deepEqual(data.axes[0].values,calibrated?[17,14]:[1,2]); assert.deepEqual(data.axes[1].values,calibrated?[12,14,16]:[1,2,3]); }
    if (kind==='series') { assert.deepEqual(data.axes[0].values,calibrated?[105,110,115,120]:[1,2,3,4]); assert.equal(data.axes[0].unit,calibrated?'eV':null); }
  }
});
const mutations = [
  ['wrong view',r=>r.payload.view_kind='series'],['URL payload',r=>r.payload.url='https://example.invalid'],['host path',r=>r.metadata.path='/Users/private'],
  ['wrong format',r=>r.metadata.format='dm3'],['wrong input',r=>r.metadata.input_mode='whole'],['source bool',r=>r.metadata.source_bytes=true],
  ['source budget',r=>r.metadata.source_bytes=8589934593],['read budget',r=>r.metadata.read_bytes=8388609],['read count',r=>r.metadata.read_requests=257],
  ['pair accounting',r=>r.metadata.data_bytes++],['header budget',r=>r.metadata.header_bytes=65537],['null accounting',r=>r.metadata.null_values=1],
  ['calibrated values',r=>r.metadata.value_semantics='calibrated'],['record-by',r=>r.metadata.record_by='image'],['limit expansion',r=>r.metadata.limits.max_values++],
  ['resource choices',r=>r.payload.choices.resource='foreign'],['dimensions',r=>r.payload.choices.cube.width++],['dtype64 int',r=>r.payload.choices.cube.dtype='int64'],
  ['byte order unspecified',r=>r.payload.choices.cube.byte_order='dont-care'],['signal type',r=>r.payload.choices.cube.signal_type='quantified EELS'],
  ['axis extra',r=>r.payload.choices.cube.axes[0].url='https://example.invalid'],['unit injection',r=>r.payload.choices.cube.axes[0].unit='<img src=x>'],
  ['axis bool',r=>r.payload.choices.cube.axes[0].origin=true],['axis infinite',r=>r.payload.choices.cube.axes[0].scale=Infinity],
  ['axis zero',r=>r.payload.choices.cube.axes[0].scale=0],['axis default flag',r=>r.payload.choices.cube.axes[0].origin_defaulted=1],
  ['array dtype',r=>r.payload.array.dtype='float64'],['array count',r=>r.payload.array.values.pop()],['array boolean',r=>r.payload.array.values[0]=true],
  ['array infinity',r=>r.payload.array.values[0]=Infinity],['array shape',r=>r.payload.array.shape=[3,2]],['array labels',r=>r.payload.array.dimensions=['x','y']],
  ['axis location',r=>r.payload.axes[0].values[0]=1],['axis extra field',r=>r.payload.axes[0].href='/private/a'],['option extra',r=>r.payload.selected.normalize=true],
];
for (const [name,mutate] of mutations) test(`Ripple rejects ${name}`,()=>{const r=result('image');mutate(r);assert.throws(()=>helpers.parseRippleWindow('image',r.payload,r.metadata));});
test('strict tree shape and metadata-only read accounting',()=>{
  for(const mutate of [r=>r.payload.tree[0].shape[0]=true,r=>r.metadata.read_requests=2,r=>r.metadata.read_bytes++,r=>r.payload.array={values:[1]}]) { const r=result();mutate(r);assert.throws(()=>helpers.parseRippleWindow('tree',r.payload,r.metadata)); }
});
test('selection exact matches in either key order, but foreign channel or ROI is rejected',()=>{
  const r=result('image'); assert.deepEqual(helpers.parseRippleWindow('image',r.payload,r.metadata,{height:2,width:3,y:1,x:1,channel:3}).array.values,[113,123,133,213,223,233]);
  for(const [key,value] of [['channel',2],['x',0],['y',0],['width',2],['height',1]]) assert.throws(()=>helpers.parseRippleWindow('image',r.payload,r.metadata,{...imageOptions,[key]:value}));
});
test('unmarked coordinate defaults may not masquerade as physical calibration',()=>{
  for(const mutate of [r=>r.payload.choices.cube.axes[0].origin=1,r=>r.payload.choices.cube.axes[0].unit='nm',r=>r.payload.choices.cube.axes[0].origin_defaulted=false]) {
    const r=result('tree',false);mutate(r);assert.throws(()=>helpers.parseRippleWindow('tree',r.payload,r.metadata));
  }
});
for(const [kind,options] of [['tree',[]],['tree',{x:0}],['image',{}],['image',{...imageOptions,channel:true}],['image',{...imageOptions,channel:8}],['image',{...imageOptions,width:129}],['image',{...imageOptions,x:4}],['series',{...seriesOptions,channel_count:16385}],['series',{...seriesOptions,channel_start:6}],['series',{...seriesOptions,x:-1}]]) test(`invalid options ${kind} ${JSON.stringify(options)}`,()=>assert.throws(()=>helpers.validateRippleOptions(kind,options,fixture.tree.choices.cube)));
function mount(t,{response=options=>result(options.kind),libraryWait,plotWait,calibrated=true}={}) {
  const seen={requests:[],libraries:[],plots:[],purged:[],observers:[],elements:[]};
  const lib={async newPlot(element,traces,layout,config){seen.plots.push({element,traces,layout,config});if(plotWait)await plotWait;element.rendered=true;},purge(element){seen.purged.push(element);element.rendered=false;},Plots:{resize(){}}};
  const FakeResizeObserver=class {constructor(){this.disconnected=false;seen.observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
  const document={createElement(){const el={dataset:{},style:{},rendered:false,removed:false,remove(){this.removed=true;}};seen.elements.push(el);return el;}};
  const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./rippleWindowData':helpers,
    './scientific/browserLibraries':{loadBrowserLibrary:async(name,signal)=>{seen.libraries.push({name,signal});if(libraryWait)await libraryWait;return lib;}},
    '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.requests.push({file:structuredClone(vue.toRaw(file)),operation,options,signal});return response(options);}}};
  const source=readFileSync(new URL('../src/visualizations/extended/RippleWindowPreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'ripple-window'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module={exports:{}};new Function('require','module','exports','document','ResizeObserver',compiled)(id=>{assert.ok(id in modules,id);return modules[id];},module,module.exports,document,FakeResizeObserver);
  const props=vue.reactive({file:{file_id:'dataset-preview:ripple-a',filename:'cube.rpl',size:result('tree',calibrated).metadata.header_bytes,metadata:{dataset_file_version:'a'}},plugin:{id:'viz-ripple-window',version:'1',adapter:'ripple-window',reader:'ripple-window',enabled:true,capabilities:{operations:['preview'],input_mode:'window',shared:false},limits:{max_input_bytes:8388608,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>module.exports.default.setup(props,{expose(){}}));state.target.value={replaceChildren(){}};t.after(()=>scope.stop());return {state,props,seen,stop:()=>scope.stop()};
}
function select(v,kind='image'){v.state.view.value=kind;for(const [key,value]of Object.entries(kind==='image'?imageOptions:seriesOptions))v.state[{channel_start:'channelStart',channel_count:'channelCount'}[key]??key].value=value;}
test('explicit pinned windows preserve descending physical height without index reversal',async t=>{
  const v=mount(t);await flush();assert.equal(v.state.error.value,'');assert.equal(v.seen.requests.length,1);assert.equal(v.seen.libraries.length,0);select(v);await v.state.loadWindow();
  assert.equal(v.state.error.value,'');assert.equal(v.seen.requests[1].options.version,version);assert.deepEqual(v.seen.plots[0].traces[0].z,[[113,123,133],[213,223,233]]);assert.deepEqual(v.seen.plots[0].traces[0].y,[17,14]);assert.equal(v.seen.plots[0].layout.yaxis.autorange,undefined);
  select(v,'series');assert.equal(v.state.displayed.value,undefined);await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.deepEqual(v.seen.plots[1].traces[0].x,[105,110,115,120]);assert.deepEqual(v.seen.plots[1].traces[0].y,[121,122,123,124]);assert.equal(v.seen.plots[1].layout.xaxis.title.text,'depth [eV]');assert.equal(v.seen.elements[0].removed,true);assert.equal(v.seen.observers[0].disconnected,true);
});
test('undeclared units use downward source row indices, never guessed energy',async t=>{
  const v=mount(t,{calibrated:false,response:options=>result(options.kind,false)});await flush();select(v);await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.equal(v.seen.plots[0].layout.yaxis.autorange,'reversed');select(v,'series');await v.state.loadWindow();assert.deepEqual(v.seen.plots[1].traces[0].x,[1,2,3,4]);assert.equal(v.seen.plots[1].layout.xaxis.title.text,'depth');
});
test('catalog clone polling avoids re-reads and disable purges synchronously',async t=>{
  const v=mount(t);await flush();select(v);await v.state.loadWindow();for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();}assert.equal(v.seen.requests.length,2);v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.elements[0].removed,true);assert.equal(v.seen.observers[0].disconnected,true);
});
for(const action of ['disable','unmount','selection','file','limits'])test(`late window after ${action} cannot revive`,async t=>{
  const task=pending(),v=mount(t,{response:options=>options.kind==='tree'?result():task.promise});await flush();select(v);const running=v.state.loadWindow();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='selection')v.state.channel.value=2;else if(action==='file')v.props.file.file_id='dataset-preview:ripple-b';else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen.requests[1].signal.aborted,true);task.resolve(result('image'));await running;assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.plots.length,0);
});
test('late initial metadata after disable cannot revive catalog',async t=>{const task=pending(),v=mount(t,{response:()=>task.promise});await flush();v.props.plugin.enabled=false;task.resolve(result());await flush();assert.equal(v.state.catalog.value,undefined);assert.equal(v.seen.requests[0].signal.aborted,true);});
for(const phase of ['library','newPlot'])test(`late ${phase} cleaned even after DOM ref clears`,async t=>{
  const task=pending(),v=mount(t,phase==='library'?{libraryWait:task.promise}:{plotWait:task.promise});await flush();select(v);const running=v.state.loadWindow();await flush();v.state.target.value=undefined;v.stop();task.resolve();await running;assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.libraries[0].signal.aborted,true);for(const element of v.seen.elements){assert.equal(element.removed,true);assert.equal(element.rendered,false);assert.ok(v.seen.purged.includes(element));}
});
for(const change of ['version','public-kind','channel','cube','source','header'])test(`component rejects wrong ${change} binding`,async t=>{
  const v=mount(t,{response:options=>{const r=result(options.kind);if(options.kind==='tree'&&change==='header'){r.metadata.header_bytes++;r.metadata.source_bytes++;r.metadata.read_bytes++;}if(options.kind!=='tree'){if(change==='version')r.version='c'.repeat(64);if(change==='public-kind')r.kind='series';if(change==='channel')r.payload.selected.channel=2;if(change==='cube')r.payload.choices.cube.signal_type='EELS';if(change==='source'){r.payload.choices.cube.data_offset++;r.metadata.data_bytes++;r.metadata.source_bytes++;}}return r;}});
  await flush();select(v);await v.state.loadWindow();assert.equal(v.state.displayed.value,undefined);assert.notEqual(v.state.error.value,'');assert.equal(v.seen.plots.length,0);
});
test('invalid ROI rejects before network window request',async t=>{const v=mount(t);await flush();v.state.x.value=4;await v.state.loadWindow();assert.equal(v.seen.requests.length,1);assert.equal(v.seen.plots.length,0);assert.notEqual(v.state.error.value,'');});
