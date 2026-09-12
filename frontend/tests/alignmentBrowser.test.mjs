import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/alignmentBrowserData.ts';
import * as bio from '../src/visualizations/extended/sequenceBrowserData.ts';

const fixture=JSON.parse(readFileSync(new URL('./browser/alignment-browser-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64),selection=fixture.table.selected;
function result(key='tree'){
 const {contract_version,type,reader,kind,metadata,warnings,sampled,...payload}=structuredClone(fixture[key]);
 return {contract_version,plugin_id:'viz-alignment-browser',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
test('native SAM fixture preserves coverage and CIGAR events',()=>{
 const c=helpers.parseAlignment(result()),d=helpers.parseAlignment(result('table'),selection);
 assert.equal(c.references[0].name,'chr1');assert.equal(d.reads.length,5);assert.equal(d.scanComplete,true);
 assert.equal(d.coverage.reduce((n,b)=>n+b.covered_bases,0),38);
 assert.deepEqual(d.reads[0].insertions,[{position:105,length:2,sequence:'AA'}]);
 assert.deepEqual(d.reads[0].mismatches,[{position:102,query:'G',reference:'C'}]);
 assert.equal(d.reads[3].mismatch_available,false);
});
for(const [label,mutate] of [
 ['wrong kind',r=>r.kind='tree'],['extra path',r=>r.payload.path='/private/x'],['extra metadata',r=>r.metadata.path='/private/x'],
 ['source bool',r=>r.metadata.source_bytes=true],['source budget',r=>r.metadata.source_bytes=67108865],['reference private',r=>r.payload.choices.references[0].name='/Users/secret'],
 ['reference index',r=>r.payload.choices.references[0].id=true],['selection',r=>r.payload.selected.start++],['scan claim',r=>r.sampled=true],
 ['read count',r=>r.payload.alignment.matched_reads++],['NaN depth',r=>r.payload.alignment.coverage[0].depth=NaN],
 ['wrong depth',r=>r.payload.alignment.coverage[0].depth=4],['wrong width',r=>r.payload.alignment.coverage[0].end++],
 ['CIGAR blocks',r=>r.payload.alignment.reads[0].blocks[0].end--],['CIGAR deletion',r=>r.payload.alignment.reads[0].deletions[0].length++],
 ['CIGAR insertion',r=>r.payload.alignment.reads[0].insertions[0].position--],['mismatch intron',r=>r.payload.alignment.reads[0].mismatches[0].position=116],
 ['missing MD',r=>r.payload.alignment.reads[0].md=null],['false flag',r=>r.payload.alignment.reads[0].reverse=true],
 ['read path',r=>r.payload.alignment.reads[0].name='/private/secret'],['unknown field',r=>r.payload.alignment.reads[0].sequence='unbounded'],
])test(`alignment rejects ${label}`,()=>{const r=result('table');mutate(r);assert.throws(()=>helpers.parseAlignment(r,selection))});
for(const patch of [{reference:true},{reference:256},{start:-1},{end:5001},{end:100},{max_reads:2001},{bins:19},{bins:1001}])test(`alignment selection rejects ${JSON.stringify(patch)}`,()=>assert.throws(()=>helpers.validateAlignmentSelection({...selection,...patch},helpers.parseAlignment(result()).references)));
test('packing and zoom keep original genomic coordinates',()=>{
 const d=helpers.parseAlignment(result('table'),selection);const packed=helpers.packAlignmentReads(d.reads,100,160);assert.equal(packed.length,5);assert.ok(new Set(packed.map(p=>p.lane)).size>=3);
 assert.deepEqual(helpers.alignmentWindow(100,160,5000,.5),{start:115,end:145});assert.deepEqual(helpers.alignmentWindow(0,10,10,2),{start:0,end:10});
});
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();await vue.nextTick()};
const deferred=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)}};
function mount(t,response=options=>result(options.kind)){
 const seen=[];const pluginCatalog=vue.shallowRef(null),relatedFiles=vue.ref([]);
 const modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./alignmentBrowserData':helpers,'./sequenceBrowserData':bio,
  '../../composables/useFilePanel':{useFilePanel:()=>({relatedFiles})},'../catalog':{useVisualizationCatalog:()=>({catalog:pluginCatalog})},
  '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,operation,options,signal});return response(options,file,plugin)}}};
 const source=readFileSync(new URL('../src/visualizations/extended/AlignmentBrowserPreview.vue',import.meta.url),'utf8');
 const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'alignment-browser'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
 const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
 const props=vue.reactive({file:{file_id:'alignment-fixture',filename:'fixture.sam',size:fixture.tree.metadata.source_bytes},plugin:{id:'viz-alignment-browser',reader:'alignment-browser',adapter:'alignment-browser',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:67108864,max_output_bytes:4194304}}});
 const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());
 return {state,seen,props,relatedFiles,pluginCatalog,stop:()=>scope.stop()};
}
async function select(v){await flush();v.state.start.value=selection.start;v.state.end.value=selection.end;v.state.maxReads.value=selection.max_reads;await flush()}
test('component reads only header first; selection is explicit and pinned',async t=>{const v=mount(t,options=>{const r=result(options.kind);if(options.kind==='table'){r.payload.selected.bins=500;r.payload.alignment.coverage=Array.from({length:60},(_,i)=>{const left=100+i,right=101+i,covered=r.payload.alignment.reads.reduce((n,r)=>n+r.blocks.reduce((s,b)=>s+Math.max(0,Math.min(right,b.end)-Math.max(left,b.start)),0),0);return {start:left,end:right,covered_bases:covered,depth:covered}});}return r});await select(v);assert.equal(v.state.error.value,'');assert.equal(v.seen.length,1);assert.equal(v.state.data.value,undefined);await v.state.loadRegion();assert.equal(v.state.error.value,'');assert.equal(v.seen[1].options.version,version);assert.equal(v.state.data.value.reads.length,5);assert.equal(v.state.pairs.value.length,1);v.state.logScale.value=true;assert.equal(v.seen.length,2)});
test('equivalent catalog/file clones do not reread, disabled state clears',async t=>{const v=mount(t);await select(v);for(let i=0;i<3;i++){v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush()}assert.equal(v.seen.length,1);v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.seen.length,1)});
for(const action of ['unmount','disable','file','version','start','end','maxReads','plugin-budget'])test(`late alignment response after ${action} does not publish`,async t=>{const pending=deferred(),v=mount(t,options=>options.kind==='tree'?result():pending.promise);await select(v);const request=v.state.loadRegion();await flush();if(action==='unmount')v.stop();else if(action==='disable')v.props.plugin.enabled=false;else if(action==='file')v.props.file.file_id='other';else if(action==='version')v.props.plugin.version='2';else if(action==='start')v.state.start.value=101;else if(action==='end')v.state.end.value=159;else if(action==='maxReads')v.state.maxReads.value=2;else v.props.plugin.limits.max_input_bytes--;assert.equal(v.seen[1].signal.aborted,true);pending.resolve(result('table'));await request;assert.equal(v.state.data.value,undefined)});
test('stale version and invalid form never publish data',async t=>{const v=mount(t,options=>{const r=result(options.kind);if(options.kind==='table')r.version='c'.repeat(64);return r});await select(v);await v.state.loadRegion();assert.match(v.state.error.value,/版本/);assert.equal(v.state.data.value,undefined);v.state.start.value=-1;await v.state.loadRegion();assert.equal(v.seen.length,2)});
test('annotation composition uses separate authorized plugin/version and disabling clears immediately',async t=>{
 const annotations=JSON.parse(readFileSync(new URL('./browser/main-bio-data.json',import.meta.url),'utf8'));
 const v=mount(t,(options,file,plugin)=>{
  if(plugin.reader==='alignment-browser')return result(options.kind==='tree'?'tree':'table_ui');
  const raw=structuredClone(annotations[options.kind==='tree'?'bed_tree':'bed']);
  const {contract_version,type,reader,kind,metadata,warnings,sampled,...payload}=raw;
  if(kind==='map'){payload.selected={chromosome:0,start:100,end:160};payload.tracks=payload.tracks.filter(f=>f.start<160&&f.end>100)}
  return {contract_version,plugin_id:plugin.id,kind:kind==='map'?'features':'tree',version:'c'.repeat(64),revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
 });await select(v);await v.state.loadRegion();assert.equal(v.state.error.value,'');
 const plugin={...structuredClone(vue.toRaw(v.props.plugin)),id:'viz-genome-tracks',reader:'genome-tracks',adapter:'genome-tracks',extensions:['bed']};
 v.pluginCatalog.value={plugins:[plugin]};v.relatedFiles.value=[{file_id:'authorized-annotation',filename:'features.bed',size:annotations.bed.metadata.source_bytes}];v.state.annotationId.value='authorized-annotation';await v.state.loadAnnotations();
 assert.equal(v.state.overlayError.value,'');assert.equal(v.state.annotations.value.length,1);assert.equal(v.seen[2].file.file_id,'authorized-annotation');assert.equal(v.seen[3].options.version,'c'.repeat(64));assert.notEqual(v.seen[3].options.version,v.seen[1].options.version);
 v.pluginCatalog.value={plugins:[{...plugin,enabled:false}]};assert.equal(v.state.annotations.value.length,0);assert.equal(v.seen[3].signal.aborted,true);
});
