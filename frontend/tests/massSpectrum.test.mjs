import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/massSpectrumData.ts';
const fixture = JSON.parse(readFileSync(new URL('./browser/mass-spectrum-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64);
function envelope(kind = 'tree', prefix = 'mzml') {
  const { contract_version, type, reader, metadata, warnings, sampled, kind: view_kind, ...payload } = structuredClone(fixture[prefix + '_' + kind]);
  return { contract_version, kind: kind === 'tree' ? 'tree' : 'array', version, revision: 'b'.repeat(64), plugin_id: 'viz-mass-spectrum', payload: { ...payload, view_kind }, metadata, warnings, sampled };
}
const selection = { spectrum: 's-000000' };
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; };

for (const prefix of ['mzml', 'mgf']) test(`${prefix} actual values, explicit units and representation`, () => {
  const r = envelope('series', prefix), value = data.parseMassSpectrum('series', r.payload, r.metadata, selection), trace = data.massSpectrumTrace(value);
  assert.equal(value.spectra[0].representation, prefix === 'mgf' ? 'centroid' : 'profile');
  assert.equal(trace.line.simplify, false); assert.equal(trace.connectgaps, false); assert.equal(trace.type, 'scatter');
  if (prefix === 'mgf') { assert.deepEqual(trace.x, [150.5,150.5,null,100.25,100.25,null,200.75,200.75,null]); assert.deepEqual(trace.y, [0,10,null,0,25,null,0,5,null]); assert.equal(data.massIntensityLabel(value.spectra[0]), '单位未声明'); }
  else { assert.deepEqual(trace.x, [100,100.25,100.5,100.75]); assert.deepEqual(trace.y, [-1,3,7,2]); assert.equal(data.massIntensityLabel(value.spectra[0]), 'detector counts'); }
});
const changes = {
  'unknown payload': r => r.payload.url = 'https://invalid', 'html': r => r.payload.media_type = 'text/html', 'wrong view': r => r.payload.view_kind = 'image',
  'metadata extra': r => r.metadata.path = '/private', 'source': r => r.metadata.source_bytes = 16777217, 'total': r => r.metadata.total_spectra = true,
  'offset': r => r.metadata.offset = 1, 'next': r => r.metadata.next_offset = 64, 'decoded': r => r.metadata.decoded_bytes = 1,
  'output': r => r.metadata.output_points = 5, 'format': r => r.metadata.format = 'mzxml', 'mode': r => r.metadata.input_mode = 'window',
  'id': r => r.payload.choices.spectra[0].id = '/private', 'index': r => r.payload.choices.spectra[0].index = true,
  'duplicate': r => r.payload.choices.spectra.push(r.payload.choices.spectra[0]), 'points': r => r.payload.choices.spectra[0].points = 16385,
  'representation': r => r.payload.choices.spectra[0].representation = 'unknown', 'unit': r => r.payload.choices.spectra[0].intensity_unit = '<svg>',
  'mzunit': r => r.payload.choices.spectra[0].mz_unit = 'seconds', 'retention': r => r.payload.choices.spectra[0].retention_time = -1,
  'timeunit': r => r.payload.choices.spectra[0].time_unit = null, 'level': r => r.payload.choices.spectra[0].ms_level = true,
  'reason': r => r.payload.choices.spectra[0].reason = 'other', 'enabled': r => r.payload.choices.spectra[0].selectable = false,
  'selected': r => r.payload.selected.spectrum = 's-000001', 'selected extra': r => r.payload.selected.path = '/private',
  'shape': r => r.payload.array.shape = [2,4], 'dimensions': r => r.payload.array.dimensions = ['time','count'],
  'null': r => r.payload.array.values[0] = null, 'nan': r => r.payload.array.values[0] = NaN, 'bool': r => r.payload.array.values[0] = true,
  'negative mz': r => r.payload.array.values[0] = -1, 'unordered profile': r => r.payload.array.values[2] = 99, 'length': r => r.payload.array.values.pop(),
};
for (const [name, change] of Object.entries(changes)) test(`strict schema refuses ${name}`, () => { const r = envelope('series'); change(r); assert.throws(() => data.parseMassSpectrum('series', r.payload, r.metadata, selection)); });

function mount(t, request = options => envelope(options.kind), render) {
  const seen = { requests: [], plots: [], purged: [], removed: [] }, previous = globalThis.document;
  globalThis.document = { createElement() { const element = { style: {}, dataset: {}, remove() { seen.removed.push(element); } }; return element; } }; t.after(() => { globalThis.document = previous; });
  const library = { newPlot: async (element, traces, layout) => { seen.plots.push({element,traces,layout}); await render?.(); }, purge: element => seen.purged.push(element), Plots: { resize() {} } };
  const modules = { vue, '../../composables/usePreviewLoad': {usePreviewLoad}, '../previewIdentity': identity, '../runtime': { requestVisualization: async (_f,_p,_o,options,signal) => { seen.requests.push({options,signal}); return request(options,signal); } }, './scientific/browserLibraries': {loadBrowserLibrary: async () => library}, './massSpectrumData': data };
  const source = readFileSync(new URL('../src/visualizations/extended/MassSpectrumPreview.vue', import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, {id:'mass'}).content, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module = {exports:{}}; new Function('require','module','exports',code)(id => {assert.ok(id in modules,id); return modules[id];},module,module.exports);
  const scope = vue.effectScope(), props = vue.reactive({file:{file_id:'opaque-mass',filename:'synthetic.mzml',size:fixture.mzml_tree.metadata.source_bytes,metadata:{dataset_file_version:'a'}},plugin:{id:'viz-mass-spectrum',version:'1.0.0',enabled:true,capabilities:{operations:['preview'],shared:false,input_mode:'whole'},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const state = scope.run(() => module.exports.default.setup(props,{expose(){}})); state.target.value = {replaceChildren(){}}; const stop = () => scope.stop(); t.after(stop); return {state,props,seen,stop};
}
async function choose(v) { v.state.selectedId.value = 's-000000'; await flush(); }
test('tree first, explicit version-pinned spectrum then scientific profile plot and purge', async t => {
  const v = mount(t); await flush(); assert.deepEqual(v.seen.requests[0].options,{kind:'tree',offset:0}); assert.equal(v.seen.plots.length,0);
  await choose(v); await v.state.loadSpectrum(); assert.equal(v.state.error.value,''); assert.deepEqual(v.seen.requests[1].options,{kind:'series',version,...selection});
  assert.deepEqual(v.seen.plots[0].traces[0].y,[-1,3,7,2]); v.stop(); assert.equal(v.seen.purged.length,1); assert.equal(v.seen.removed.length,1);
});
test('same file/plugin polling clones do not reload or discard selected spectrum', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadSpectrum(); v.props.file=structuredClone(vue.toRaw(v.props.file)); v.props.plugin=structuredClone(vue.toRaw(v.props.plugin)); await flush();
  assert.equal(v.seen.requests.length,2); assert.equal(v.seen.plots.length,1); assert.equal(v.state.selectedId.value,'s-000000');
});
for (const change of ['version','size','descriptor','sampled','selected']) test(`component rejects mismatched ${change}`, async t => {
  const v=mount(t,options=>{const r=envelope(options.kind); if(options.kind==='series'){if(change==='version')r.version='c'.repeat(64);if(change==='size')r.metadata.source_bytes++;if(change==='descriptor')r.payload.choices.spectra[0].ms_level=2;if(change==='sampled')r.sampled=true;if(change==='selected')r.payload.selected.spectrum='s-000001';}return r;});
  await flush();await choose(v);await v.state.loadSpectrum();assert.ok(v.state.error.value);assert.equal(v.seen.plots.length,0);
});
for(const action of ['unmount','disable','file-change'])test(`pending request canceled on ${action}`,async t=>{
  const blocked=pending(),v=mount(t,options=>options.kind==='series'?blocked.promise:envelope());await flush();await choose(v);const loading=v.state.loadSpectrum();await flush();const request=v.seen.requests.at(-1);
  if(action==='unmount')v.stop();else if(action==='disable')v.props.plugin.enabled=false;else v.props.file.file_id='other';await flush();assert.equal(request.signal.aborted,true);blocked.resolve(envelope('series'));await loading;assert.equal(v.seen.plots.length,0);assert.equal(v.state.displayed.value,undefined);
});
test('late Plotly completion after unmount is purged again',async t=>{const blocked=pending(),v=mount(t,undefined,()=>blocked.promise);await flush();await choose(v);const loading=v.state.loadSpectrum();await flush();assert.equal(v.seen.plots.length,1);v.stop();blocked.resolve();await loading;assert.ok(v.seen.purged.length>=2);});
test('nonzero page carries version and clears selection without decoding',async t=>{
  const page=(offset)=>{const raw=structuredClone(fixture[offset?'page_second':'page_first']);const {contract_version,type,reader,metadata,warnings,sampled,kind:view_kind,...payload}=raw;return {contract_version,kind:'tree',version,metadata,warnings,sampled,payload:{...payload,view_kind}};};
  const v=mount(t,options=>page(options.offset));v.props.file.size=fixture.page_first.metadata.source_bytes;await flush();await v.state.inspect(64);assert.equal(v.state.error.value,'');assert.deepEqual(v.seen.requests.at(-1).options,{kind:'tree',version,offset:64});assert.equal(v.state.catalog.value.spectra.length,1);assert.equal(v.seen.plots.length,0);
});
