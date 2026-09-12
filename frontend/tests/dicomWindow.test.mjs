import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/dicomWindowData.ts';
const fixtures=JSON.parse(readFileSync(new URL('./browser/domain-expansion-dicom-data.json',import.meta.url),'utf8'));
const oracle=JSON.parse(readFileSync(new URL('./browser/domain-expansion-dicom-oracle.json',import.meta.url),'utf8'));
const version='a'.repeat(64),options=fixtures.mono2.image.selected;
const flush=async()=>{for(let i=0;i<50;i++)await Promise.resolve();await vue.nextTick();};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)};};
function result(kind='tree',group='mono2'){
  const {contract_version,type,reader,metadata,warnings,sampled,kind:privateKind,...payload}=structuredClone(fixtures[group][kind]);
  return {contract_version,kind:kind==='tree'?'tree':'array',plugin_id:'viz-dicom-window',version,revision:'b'.repeat(64),metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
const parsed=(kind='image',group='mono2')=>{const r=result(kind,group);return helpers.parseDicomWindow(kind,r.payload,r.metadata,kind==='image'?options:{});};
test('real reader metadata and raw stored integer ROI remain distinct from display conversion',()=>{
  const tree=parsed('tree'),image=parsed();assert.equal(tree.values,null);assert.equal(tree.readBytes,572);assert.equal(tree.headerBytes,626);assert.deepEqual(image.values,[1110,1120,1210,1220]);assert.equal(image.readBytes,584);assert.equal(image.reads,44);assert.equal(image.image.rescale.slope,2);assert.equal(image.image.rescale.intercept,-1000);
});
for(const reference of oracle.display)test(`pydicom LINEAR oracle ${reference.mono} center=${reference.center} width=${reference.width}`,()=>{
  const data=parsed('image',reference.mono==='MONOCHROME1'?'mono1':'mono2'),before=[...data.values];
  assert.deepEqual([...helpers.dicomRgba(data,reference.center,reference.width)],reference.rgba);assert.deepEqual(data.values,before);
});
test('padding becomes transparent, undefined windows use explicitly labeled ROI range',()=>{
  const data=parsed();data.image.window=null;data.image.padding={low:1110,high:1120};
  assert.deepEqual([...helpers.dicomRgba(data,1350,1000)].slice(0,8),[0,0,0,0,0,0,0,0]);assert.match(helpers.defaultDicomWindow(data).source,/当前 ROI/);
  assert.deepEqual(helpers.defaultDicomWindow(data),{center:1430.5,width:21,source:'当前 ROI 范围（非文件窗设置）'});
});
const mutations=[['view',r=>r.payload.view_kind='tree'],['payloadURL',r=>r.payload.url='https://example.invalid'],['PHI',r=>r.metadata.PatientName='PHI'],['format',r=>r.metadata.format='nifti'],['source bool',r=>r.metadata.source_bytes=true],['read over',r=>r.metadata.read_bytes=8388609],['reads over',r=>r.metadata.read_requests=129],['limits',r=>r.metadata.limits.max_roi=true],['pixel bytes',r=>r.metadata.pixel_bytes++],['header bytes',r=>r.metadata.header_bytes++],['privacy',r=>r.metadata.privacy='fully anonymized'],['hidden',r=>r.metadata.metadata_hidden=false],['dtype',r=>r.payload.array.dtype='float64'],['dimensions',r=>r.payload.array.dimensions=['x','y']],['shape bool',r=>r.payload.array.shape=[true,2]],['pixel bool',r=>r.payload.array.values[0]=true],['pixel fraction',r=>r.payload.array.values[0]=1.1],['pixel overflow',r=>r.payload.array.values[0]=65536],['bits',r=>r.payload.choices.image.bits_stored=17],['RGB',r=>r.payload.choices.image.photometric='RGB'],['enhanced',r=>r.payload.choices.image.sop_class='enhanced CT'],['multi CT',r=>r.payload.choices.image.sop_class='CT'],['rescale0',r=>r.payload.choices.image.rescale.slope=0],['rescale unit',r=>r.payload.choices.image.rescale.unit='<img src=x>'],['window function',r=>r.payload.choices.image.window.function='SIGMOID'],['window0',r=>r.payload.choices.image.window.width=0],['paddingcount',r=>r.metadata.padding_pixels=1],['false confirm',r=>r.payload.selected.confirm_deidentified=false]];
for(const [name,mutate]of mutations)test(`strict DICOM rejects ${name}`,()=>{const r=result('image');mutate(r);assert.throws(()=>helpers.parseDicomWindow('image',r.payload,r.metadata));});
test('strict exact request selection cannot silently choose another frame or ROI',()=>{
  const r=result('image');assert.deepEqual(helpers.parseDicomWindow('image',r.payload,r.metadata,{roi:[1,1,2,2],confirm_deidentified:true,frame:1}).values,[1110,1120,1210,1220]);
  for(const expected of [{...options,frame:0},{...options,roi:[0,1,2,2]},{...options,confirm_deidentified:1}])assert.throws(()=>helpers.parseDicomWindow('image',r.payload,r.metadata,expected));
});
for(const value of [{}, {...options,confirm_deidentified:false},{...options,confirm_deidentified:1},{...options,frame:true},{...options,frame:2},{...options,roi:[1,1,129,1]},{...options,roi:[4,0,1,1]},{...options,window:100}])test(`request ${JSON.stringify(value)}`,()=>assert.throws(()=>helpers.validateDicomOptions('image',value,parsed('tree').image)));
for(const [c,w]of [[true,10],[0,0],[Infinity,1],[0,NaN],[0,2e12+1]])test(`local invalid window ${c}/${w}`,()=>assert.throws(()=>helpers.dicomRgba(parsed(),c,w)));
function mount(t,response=req=>result(req.kind)){
  const seen={requests:[],canvases:[]};const document={createElement(name){assert.equal(name,'canvas');const c={style:{},dataset:{},width:0,height:0,removed:false,remove(){this.removed=true;},getContext(){return {putImageData(image){c.rgba=[...image.data];}};}};seen.canvases.push(c);return c;}};
  const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./dicomWindowData':helpers,'./domains/lifecycle':{displayError:e=>e.message},'../runtime':{requestVisualization:async(file,plugin,operation,request,signal)=>{seen.requests.push({file:structuredClone(vue.toRaw(file)),request,signal});return response(request);}}};
  const source=readFileSync(new URL('../src/visualizations/extended/DicomWindowPreview.vue',import.meta.url),'utf8');const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'dicom'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module={exports:{}};new Function('require','module','exports','document','ImageData',compiled)(id=>{assert.ok(id in modules,id);return modules[id];},module,module.exports,document,class {constructor(data,w,h){this.data=data;this.width=w;this.height=h;}});
  const props=vue.reactive({file:{file_id:'file-a',filename:'synthetic.dcm',size:674,metadata:{dataset_file_version:'v1'}},plugin:{id:'viz-dicom-window',version:'1',adapter:'dicom-window',reader:'dicom-window',enabled:true,capabilities:{operations:['preview'],input_mode:'window',shared:false},limits:{max_input_bytes:8388608,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>module.exports.default.setup(props,{expose(){}}));state.target.value={replaceChildren(){}};t.after(()=>scope.stop());return {props,state,seen,stop:()=>scope.stop()};
}
function select(v){v.state.frame.value=1;v.state.x.value=v.state.y.value=1;v.state.width.value=v.state.height.value=2;v.state.confirmed.value=true;}
test('actual Vue tree then confirm+explicit ROI; contrast updates cached values without reading',async t=>{
  const v=mount(t);await flush();assert.equal(v.state.error.value,'');assert.equal(v.seen.requests.length,1);assert.equal(v.seen.canvases.length,0);assert.equal(v.state.confirmed.value,false);
  select(v);await v.state.loadWindow();assert.equal(v.state.error.value,'');assert.deepEqual(v.seen.requests[1].request,{kind:'image',version,...options});assert.deepEqual(v.seen.canvases[0].rgba,oracle.display.find(r=>r.mono==='MONOCHROME2'&&r.center===1350).rgba);
  v.state.center.value=1250;v.state.windowWidth.value=1;assert.equal(v.seen.requests.length,2);assert.deepEqual(v.seen.canvases.at(-1).rgba,oracle.display.find(r=>r.mono==='MONOCHROME2'&&r.center===1250).rgba);assert.equal(v.seen.canvases[0].width,0);assert.equal(v.seen.canvases[0].removed,true);
});
test('confirmation is required before any pixel API request',async t=>{const v=mount(t);await flush();await v.state.loadWindow();assert.equal(v.seen.requests.length,1);assert.equal(v.seen.canvases.length,0);assert.ok(v.state.error.value);});
test('cloned catalogs do not reread; unconfirm and disable clear captured canvas immediately',async t=>{
  const v=mount(t);await flush();select(v);await v.state.loadWindow();for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();}assert.equal(v.seen.requests.length,2);
  v.state.confirmed.value=false;assert.equal(v.state.displayed.value,undefined);assert.equal(v.seen.canvases[0].width,0);assert.equal(v.seen.canvases[0].removed,true);v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);
});
for(const action of ['disable','unmount','selection','unconfirm','file','limits','cancel'])test(`late pixels after ${action} cannot revive`,async t=>{
  const wait=pending(),v=mount(t,req=>req.kind==='tree'?result():wait.promise);await flush();select(v);const running=v.state.loadWindow();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='selection')v.state.frame.value=0;else if(action==='unconfirm')v.state.confirmed.value=false;else if(action==='file')v.props.file.file_id='file-b';else if(action==='limits')v.props.plugin.limits.max_input_bytes--;else v.state.cancel();
  assert.equal(v.seen.requests[1].signal.aborted,true);wait.resolve(result('image'));await running;assert.equal(v.seen.canvases.length,0);assert.equal(v.state.displayed.value,undefined);
});
test('late tree cannot revive disabled metadata; unmount clears canvas even with cleared target ref',async t=>{
  const wait=pending(),v=mount(t,()=>wait.promise);await flush();v.props.plugin.enabled=false;wait.resolve(result());await flush();assert.equal(v.state.catalog.value,undefined);
  const second=mount(t);await flush();select(second);await second.state.loadWindow();second.state.target.value=undefined;second.stop();assert.equal(second.seen.canvases[0].width,0);assert.equal(second.seen.canvases[0].height,0);assert.equal(second.seen.canvases[0].removed,true);assert.equal(second.state.displayed.value,undefined);
});
for(const change of ['version','kind','frame','rescale','source'])test(`wrong ${change} rejected before canvas`,async t=>{
  const v=mount(t,req=>{const r=result(req.kind);if(req.kind==='image'){if(change==='version')r.version='c'.repeat(64);if(change==='kind')r.kind='media';if(change==='frame')r.payload.selected.frame=0;if(change==='rescale')r.payload.choices.image.rescale.slope=3;if(change==='source'){r.metadata.header_bytes++;r.metadata.source_bytes++;}}return r;});await flush();select(v);await v.state.loadWindow();assert.ok(v.state.error.value);assert.equal(v.seen.canvases.length,0);
});
