import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/nexusWindowData.ts';

const fixture = JSON.parse(readFileSync(new URL('./browser/nexus-window-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64);
function envelope(kind = 'tree') {
  const { contract_version, type: _type, reader: _reader, metadata, warnings, sampled, kind: view_kind, ...payload } = structuredClone(fixture[kind]);
  return { contract_version, kind: kind === 'tree' ? 'tree' : 'array', version, revision: 'b'.repeat(64), plugin_id: 'viz-nexus-window', payload: { ...payload, view_kind }, metadata, warnings, sampled };
}
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; };
const selection = kind => structuredClone(fixture[kind].selected);
const parsed = kind => { const r = envelope(kind); return data.parseNexusWindow(kind, r.payload, r.metadata, kind === 'tree' ? undefined : selection(kind)); };

for (const format of ['nxs', 'nx', 'h5', 'hdf5', 'hdf']) test(`registered ${format} extension accepts the same HDF5 NXdata contract`, () => {
  const r = envelope('tree'); r.metadata.format = format;
  assert.equal(data.parseNexusWindow('tree', r.payload, r.metadata).signals.length, 2);
});

test('actual reader payload keeps NXdata signals axes units and uncertainties', () => {
  assert.equal(parsed('tree').signals.length, 2); assert.equal(parsed('tree').array, null);
  const series = parsed('series'); assert.deepEqual(series.array.values, [2, 4, 6, 8]); assert.deepEqual(series.axes[0].values, [101, 102, 103, 104]); assert.equal(series.axes[0].unit, 'eV'); assert.deepEqual(series.errors, [1, 1, 1, 1]);
  const image = parsed('image'); assert.deepEqual(image.array.values, [8, 9, 10, 14, 15, 16]); assert.deepEqual(image.axes.map(a => a.values), [[100.5, 101], [201, 201.5, 202]]);
});
test('selection is canonical copied and comparison ignores request property ordering', () => {
  const expected = selection('series'); const reordered = { selection: [{ step: 2, stop: 10, start: 2 }], nxdata: expected.nxdata };
  const result = data.validateNexusSelection('series', reordered); reordered.selection[0].start = 4; assert.deepEqual(result, expected);
  const r = envelope('series'); assert.doesNotThrow(() => data.parseNexusWindow('series', r.payload, r.metadata, expected));
});
const changes = {
  'extra payload': r => { r.payload.url = 'https://invalid'; }, 'extra metadata': r => { r.metadata.path = '/private'; },
  'wrong reader view': r => { r.payload.view_kind = 'series'; }, 'wrong media': r => { r.payload.media_type = 'text/html'; },
  'bad coordinates': r => { r.payload.axes[0].values[0] = null; }, 'duplicate coordinate': r => { r.payload.axes[0].values[0] = 101; },
  'NaN coordinate': r => { r.payload.axes[0].values[0] = NaN; }, 'wrong coordinate unit': r => { r.payload.axes[0].unit = 'm'; },
  'bad index': r => { r.payload.axes[0].indices[0] = true; }, 'bad dimension': r => { r.payload.axes[0].dimension = false; },
  'axis path': r => { r.payload.axes[0].path = '/entry'; }, 'array wrong shape': r => { r.payload.array.shape = [3, 2]; },
  'array missing': r => { r.payload.array.values.pop(); }, 'array boolean': r => { r.payload.array.values[0] = true; },
  'array fractional integer': r => { r.payload.array.values[0] = .5; }, 'array out of dtype': r => { r.payload.array.values[0] = 65536; },
  'array null integer': r => { r.payload.array.values[0] = null; }, 'array unsafe': r => { r.payload.array.values[0] = 2 ** 53; },
  'errors negative': r => { r.payload.errors[0] = -1; }, 'errors missing': r => { r.payload.errors = null; },
  'duplicate choice': r => { r.payload.choices.signals.push(structuredClone(r.payload.choices.signals[0])); },
  'choice HTML': r => { r.payload.choices.signals[0].label = '<script>'; }, 'choice URL': r => { r.payload.choices.signals[0].unit = 'https://invalid'; },
  'choice path': r => { r.payload.choices.signals[0].id = '/entry'; }, 'choice raw header': r => { r.payload.choices.signals[0].header = {}; },
  'dtype object': r => { r.payload.choices.signals[0].dtype = '|O'; }, 'dtype vlen': r => { r.payload.choices.signals[0].dtype = '<U10'; },
  'dtype bool': r => { r.payload.choices.signals[0].dtype = '|b1'; }, 'dtype half float': r => { r.payload.choices.signals[0].dtype = '<f2'; },
  'shape bool': r => { r.payload.choices.signals[0].shape[0] = true; }, 'shape product': r => { r.payload.choices.signals[0].shape = [2 ** 31 - 1, 2 ** 31 - 1]; },
  'huge chunk': r => { r.payload.choices.signals[0].chunks = [99999, 99999]; }, 'axis unknown': r => { r.payload.choices.signals[0].axes[0].source = 'unknown'; },
  'axis index lie': r => { r.payload.choices.signals[0].axes[0].source = 'index'; }, 'axis vlen': r => { r.payload.choices.signals[0].axes[0].dtype = '|O'; },
  'errors descriptor': r => { r.payload.choices.signals[0].errors.chunks = [4]; },
  'selection overflow': r => { r.payload.selected.selection[0].stop = 9; }, 'selection mode': r => { r.payload.selected.decode = 'scale'; },
  'source overflow': r => { r.metadata.source_bytes = 8 * 1024 ** 3 + 1; }, 'read overflow': r => { r.metadata.read_bytes = 8 * 1024 ** 2 + 1; },
  'reads overflow': r => { r.metadata.read_requests = 129; }, 'attributes overflow': r => { r.metadata.attribute_bytes = 65537; },
  'decode overflow': r => { r.metadata.decoded_chunk_bytes = 16777217; }, 'output count lie': r => { r.metadata.output_values = 6; },
  'chunk count lie': r => { r.metadata.chunks_touched--; }, 'decode count lie': r => { r.metadata.decoded_chunk_bytes--; },
  'missing count lie': r => { r.metadata.nonfinite_values++; }, 'wrong semantics': r => { r.metadata.value_semantics = 'calibrated'; },
};
for (const [name, change] of Object.entries(changes)) test(`NXdata helper rejects ${name}`, () => { const r = envelope('image'); change(r); assert.throws(() => data.parseNexusWindow('image', r.payload, r.metadata)); });
test('tree has exact inert labels and no concealed arrays', () => {
  for (const change of [r => { r.payload.tree[0].attributes.script = 'bad'; }, r => { r.payload.array = {}; }, r => { r.metadata.output_values = 1; }]) {
    const r = envelope(); change(r); assert.throws(() => data.parseNexusWindow('tree', r.payload, r.metadata));
  }
});
test('explicit missing-coordinate fallback is an index, never a guessed unit', () => {
  const r = envelope('series'), sig = r.payload.choices.signals[1];
  sig.axes[0] = { label: 'index_0', source: 'index', unit: null, dtype: null, chunks: null };
  r.payload.axes[0] = { dimension: 0, indices: [2, 4, 6, 8], values: [2, 4, 6, 8], source: 'index', label: 'index_0', unit: null };
  r.payload.array.dimensions = ['index_0'];
  assert.equal(data.parseNexusWindow('series', r.payload, r.metadata).axes[0].source, 'index');
});

function mount(t, request = options => envelope(options.kind), render) {
  const seen = { requests: [], plots: [], purged: [], removed: [] }, previous = globalThis.document;
  globalThis.document = { createElement() { const element = { style: {}, dataset: {}, remove() { seen.removed.push(element); } }; return element; } };
  t.after(() => { globalThis.document = previous; });
  const library = { newPlot: async (element, traces, layout) => { seen.plots.push({ element, traces, layout }); await render?.(); }, purge: element => seen.purged.push(element), Plots: { resize() {} } };
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity,
    '../runtime': { requestVisualization: async (_file, _plugin, _operation, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } },
    './scientific/browserLibraries': { loadBrowserLibrary: async () => library }, './nexusWindowData': data };
  const source = readFileSync(new URL('../src/visualizations/extended/NexusWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'nexus-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'opaque-nexus', filename: 'synthetic.nxs', size: fixture.tree.metadata.source_bytes, metadata: { dataset_file_version: 'a' } },
    plugin: { id: 'viz-nexus-window', version: '1.0.0', enabled: true, capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = { replaceChildren() {} };
  const stop = () => scope.stop(); t.after(stop); return { state, props, seen, stop };
}
async function choose(view, kind) {
  view.state.selectedId.value = fixture[kind].selected.nxdata; await flush();
  view.state.slices.value = structuredClone(fixture[kind].selected.selection); await flush();
}
test('component starts tree-only, then explicit NXdata choice and version-pinned series renders units/errors', async t => {
  const v = mount(t); await flush(); assert.equal(v.seen.requests.length, 1); assert.deepEqual(v.seen.requests[0].options, { kind: 'tree' }); assert.equal(v.seen.plots.length, 0);
  await choose(v, 'series'); await v.state.loadWindow(); assert.equal(v.state.error.value, '');
  assert.deepEqual(v.seen.requests[1].options, { kind: 'series', version, ...selection('series') });
  const { traces: [trace], layout } = v.seen.plots[0]; assert.deepEqual(trace.x, [101, 102, 103, 104]); assert.deepEqual(trace.y, [2, 4, 6, 8]);
  assert.deepEqual(trace.error_y.array, [1, 1, 1, 1]); assert.equal(trace.error_y.visible, true); assert.equal(layout.xaxis.title.text, 'energy [eV]'); assert.equal(layout.yaxis.title.text, 'counts [counts]');
});
test('image uses separate declared coordinates without transpose and retains errors for hover', async t => {
  const v = mount(t); await flush(); await choose(v, 'image'); await v.state.loadWindow(); assert.equal(v.state.error.value, '');
  const { traces: [trace], layout } = v.seen.plots[0]; assert.deepEqual(trace.x, [201, 201.5, 202]); assert.deepEqual(trace.y, [100.5, 101]);
  assert.deepEqual(trace.z, [[8, 9, 10], [14, 15, 16]]); assert.deepEqual(trace.customdata, [[1, 1, 1], [1, 1, 1]]); assert.equal(trace.zsmooth, false);
  assert.equal(layout.xaxis.title.text, 'x [mm]'); assert.equal(layout.yaxis.title.text, 'y [mm]');
});
test('reloading catalog after selecting data works and clears the previous graph', async t => {
  const v = mount(t); await flush(); await choose(v, 'series'); await v.state.loadWindow(); await v.state.inspect(); await flush();
  assert.equal(v.seen.requests.length, 3); assert.equal(v.state.selectedId.value, ''); assert.equal(v.state.displayed.value, undefined);
  assert.ok(v.seen.purged.includes(v.seen.plots[0].element)); assert.equal(v.state.catalog.value.signals.length, 2);
});
test('same semantic file/plugin clones do not reload, actual version and capability changes do', async t => {
  const v = mount(t); await flush();
  for (let i = 0; i < 3; i++) { v.props.file = { ...v.props.file }; v.props.plugin = { ...v.props.plugin }; await flush(); }
  assert.equal(v.seen.requests.length, 1);
  v.props.file.metadata.dataset_file_version = 'b'; await flush(); assert.equal(v.seen.requests.length, 2);
  v.props.plugin.limits.max_input_bytes--; await flush(); assert.equal(v.seen.requests.length, 3);
});
test('invalid total output or out-of-bounds selection never requests data', async t => {
  const v = mount(t); await flush(); await choose(v, 'series'); v.state.slices.value[0].stop = 13; await flush(); await v.state.loadWindow();
  assert.equal(v.seen.requests.length, 1); assert.match(v.state.error.value, /无效/);
  const signal = { ...parsed('tree').signals[1], shape: [6000] };
  assert.throws(() => data.validateNexusSelection('series', { nxdata: signal.id, selection: [{ start: 0, stop: 6000, step: 1 }] }, signal));
});
for (const mismatch of ['version', 'choice', 'size', 'selection']) test(`component refuses ${mismatch} mismatch before render`, async t => {
  const v = mount(t, options => { const r = envelope(options.kind); if (options.kind !== 'tree') {
    if (mismatch === 'version') r.version = 'c'.repeat(64); if (mismatch === 'choice') r.payload.choices.signals[1].unit = 'changed';
    if (mismatch === 'size') r.metadata.source_bytes++; if (mismatch === 'selection') r.payload.selected.selection[0].start++;
  } return r; });
  await flush(); await choose(v, 'series'); await v.state.loadWindow(); assert.notEqual(v.state.error.value, ''); assert.equal(v.seen.plots.length, 0);
});
test('empty catalog retains a friendly fallback with no automatic array reads', async t => {
  const v = mount(t, () => { const r = envelope(); r.payload.tree = []; r.payload.choices.signals = []; r.metadata.skipped_nxdata = 1; return r; });
  await flush(); assert.equal(v.state.catalog.value.signals.length, 0); assert.equal(v.seen.requests.length, 1); assert.equal(v.state.error.value, '');
});
test('disabled plugin aborts and removes an existing plot', async t => {
  const v = mount(t); await flush(); await choose(v, 'image'); await v.state.loadWindow(); v.props.plugin.enabled = false; await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(v.seen.requests[1].signal.aborted, true); assert.equal(v.state.displayed.value, undefined); assert.equal(v.state.catalog.value, undefined);
  assert.ok(v.seen.purged.includes(v.seen.plots[0].element)); assert.ok(v.seen.removed.includes(v.seen.plots[0].element));
});
for (const action of ['unmount', 'disable', 'file-change']) test(`late data after ${action} never draws`, async t => {
  const task = pending(), v = mount(t, options => options.kind === 'tree' ? envelope() : task.promise);
  await flush(); await choose(v, 'series'); const loading = v.state.loadWindow(); await flush();
  if (action === 'unmount') v.stop(); else if (action === 'disable') v.props.plugin.enabled = false; else v.props.file.file_id = 'new';
  await flush(); task.resolve(envelope('series')); await loading; await flush();
  assert.equal(v.seen.requests[1].signal.aborted, true); assert.equal(v.seen.plots.length, 0); assert.equal(v.state.displayed.value, undefined);
});
test('late Plotly render is purged after scope disposal', async t => {
  const task = pending(), v = mount(t, undefined, () => task.promise); await flush(); await choose(v, 'series'); const loading = v.state.loadWindow(); await flush();
  assert.equal(v.seen.plots.length, 1); v.stop(); task.resolve(); await loading;
  assert.equal(v.state.displayed.value, undefined); assert.ok(v.seen.purged.includes(v.seen.plots[0].element)); assert.ok(v.seen.removed.includes(v.seen.plots[0].element));
});
test('changing explicit slice clears a stale plot without eager re-reading', async t => {
  const v = mount(t); await flush(); await choose(v, 'series'); await v.state.loadWindow(); v.state.slices.value[0].start = 3; await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(v.state.displayed.value, undefined); assert.ok(v.seen.purged.includes(v.seen.plots[0].element));
});
test('template documents fixed-string scope, metadata is inert and no URL/storage access exists', () => {
  const text = readFileSync(new URL('../src/visualizations/extended/NexusWindowPreview.vue', import.meta.url), 'utf8');
  assert.match(text, /固定长度字符串/); assert.match(text, /可变长/); assert.match(text, /原始数组/); assert.match(text, /标准差/);
  assert.doesNotMatch(text, /v-html|https?:\/\/|localStorage|sessionStorage/);
});
