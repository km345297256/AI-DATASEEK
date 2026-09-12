import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as data from '../src/visualizations/extended/arrayWindowData.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';

const identifier = 'v-' + 'a'.repeat(32), version = 'a'.repeat(64);
const variable = () => ({ id: identifier, label: 'temperature', shape: [4], dtype: '<f8', chunks: [2], selectable: true, reason: '' });
const slice = (start = 0, stop = 4, step = 1) => ({ start, stop, step });
const selection = () => ({ variable: identifier, selection: [slice()], decode: 'raw' });
function fixture(kind = 'tree', options = selection()) {
  const metadata = { format: 'nc', container: 'HDF5', input_mode: 'window', value_semantics: data.ARRAY_VALUE_SEMANTICS,
    source_bytes: 1024 ** 3, read_bytes: 16384, read_requests: 1, chunks_touched: 0, decoded_chunk_bytes: 0, catalog_truncated: false,
    attributes: {}, nonfinite_values: 0, coordinates: 'zero-based dimension indices; not geospatial coordinates',
    limits: { max_elements: 16384, max_chunk_bytes: 4194304, max_decoded_bytes: 16777216, max_nodes: 128 } };
  const payload = { media_type: 'application/json', view_kind: kind, choices: { variables: [variable()] }, selected: kind === 'tree' ? {} : options };
  if (kind === 'tree') payload.tree = [{ path: '/' + identifier, node_type: 'array', attributes: { label: 'temperature', depth: 0, reason: '' } }];
  else {
    const s = options.selection[0], indices = Array.from({ length: Math.ceil((s.stop - s.start) / s.step) }, (_, i) => s.start + i * s.step);
    payload.axes = [{ dimension: 0, indices }]; payload.array = { shape: [indices.length], dimensions: ['index_0'], values: indices.map(i => [1.0000000000000002, null, -9999, 4][i]) };
    metadata.chunks_touched = new Set(indices.map(i => Math.floor(i / 2))).size; metadata.decoded_chunk_bytes = metadata.chunks_touched * 16;
    metadata.nonfinite_values = payload.array.values.filter(v => v === null).length;
    metadata.attributes = { scale_factor: 0.1, add_offset: 273.15, _FillValue: -9999, units: 'K' };
  }
  return { contract_version: 2, plugin_id: 'viz-array-window', kind: kind === 'tree' ? 'tree' : 'array', version, revision: 'b'.repeat(64), payload, metadata, warnings: [], sampled: false };
}
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };

test('tree is numeric metadata only and raw values retain Float64 precision/fill/null', () => {
  const tree = fixture(); assert.equal(data.parseArrayWindow('tree', tree.payload, tree.metadata).array, null);
  const raw = fixture('series'); const parsed = data.parseArrayWindow('series', raw.payload, raw.metadata, selection());
  assert.deepEqual(parsed.array.values, [1.0000000000000002, null, -9999, 4]);
  assert.deepEqual(parsed.axes[0].indices, [0, 1, 2, 3]); assert.equal(parsed.attributes.scale_factor, 0.1);
});
test('request selection comparison is independent of JSON property order', () => {
  const raw = fixture('series'); const expected = { decode: 'raw', selection: [{ step: 1, stop: 4, start: 0 }], variable: identifier };
  assert.doesNotThrow(() => data.parseArrayWindow('series', raw.payload, raw.metadata, expected));
});
test('large finite floats remain representable but unsafe integer arrays do not', () => {
  const raw = fixture('series'); raw.payload.array.values[0] = 1e100; assert.doesNotThrow(() => data.parseArrayWindow('series', raw.payload, raw.metadata));
  raw.payload.choices.variables[0].dtype = '<i8'; assert.throws(() => data.parseArrayWindow('series', raw.payload, raw.metadata));
});

const changes = [
  ['extra payload key', r => { r.payload.url = 'bad'; }], ['extra metadata', r => { r.metadata.source_path = '/private'; }],
  ['wrong raw semantics', r => { r.metadata.value_semantics = 'CF decoded'; }], ['read bytes', r => { r.metadata.read_bytes = 8388609; }],
  ['read count', r => { r.metadata.read_requests = 129; }], ['size type', r => { r.metadata.source_bytes = true; }],
  ['decode budget', r => { r.metadata.decoded_chunk_bytes = 16777217; }], ['node budget', r => { r.metadata.limits.max_nodes = 129; }],
  ['variable duplicate', r => { r.payload.choices.variables.push(variable()); }], ['variable URL', r => { r.payload.choices.variables[0].label = 'https://bad'; }],
  ['variable opaque ID', r => { r.payload.choices.variables[0].id = '/group'; }], ['unsupported dtype', r => { r.payload.choices.variables[0].dtype = '|O'; }],
  ['nonboolean selectable', r => { r.payload.choices.variables[0].selectable = 1; }], ['unbounded chunk', r => { r.payload.choices.variables[0].chunks = [1000000]; }],
  ['unbound value shape', r => { r.payload.array.shape = [5]; }], ['bool value', r => { r.payload.array.values[0] = true; }],
  ['infinite value', r => { r.payload.array.values[0] = Infinity; }], ['masked finite fill count', r => { r.metadata.nonfinite_values = 2; }],
  ['wrong axis', r => { r.payload.axes[0].indices[0] = 5; }], ['extra axis field', r => { r.payload.axes[0].url = 'bad'; }],
  ['wrong kind', r => { r.payload.view_kind = 'image'; }], ['wrong selected', r => { r.payload.selected.decode = 'cf'; }],
];
for (const [name, mutate] of changes) test(`array parser rejects ${name}`, () => { const raw = fixture('series'); mutate(raw); assert.throws(() => data.parseArrayWindow('series', raw.payload, raw.metadata)); });
for (const [name, selectionValue] of [['empty', []], ['bool', [true]], ['negative', [-1]], ['zero step', [slice(0, 4, 0)]], ['reverse', [slice(4, 2)]], ['output budget', [slice(0, 16385)]], ['too many axes', [slice(), slice()]], ['extra option', [{ ...slice(), url: 'bad' }]]]) {
  test(`selection rejects ${name}`, () => { assert.throws(() => data.validateArraySelection('series', { ...selection(), selection: selectionValue })); });
}

function mount(t, request, render) {
  const seen = { requests: [], plots: [], purged: [], removed: [], resize: [] }, previous = globalThis.document;
  globalThis.document = { createElement() { const element = { style: {}, remove() { seen.removed.push(element); } }; return element; } };
  t.after(() => { globalThis.document = previous; });
  const library = { newPlot: async (element, traces, layout) => { seen.plots.push({ element, traces, layout }); await render?.(); }, purge: element => seen.purged.push(element), Plots: { resize: element => seen.resize.push(element) } };
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../runtime': { requestVisualization: async (_file, _plugin, _operation, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } }, '../previewIdentity': identity, './scientific/browserLibraries': { loadBrowserLibrary: async () => library }, './arrayWindowData': data };
  const source = readFileSync(new URL('../src/visualizations/extended/ArrayWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'array-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: 'data.nc', size: 1024 ** 3, metadata: { dataset_file_version: 'a' } }, plugin: { id: 'viz-array-window', version: '1', enabled: true, capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = { replaceChildren() {} };
  const stop = () => scope.stop(); t.after(stop);
  return { state, props, seen, stop };
}
const requestFixture = options => options.kind === 'tree' ? fixture() : fixture(options.kind, { variable: options.variable, selection: options.selection, decode: options.decode });

function gridFixture(kind = 'tree', options) {
  const raw = fixture(); raw.payload.choices.variables[0].shape = [2, 3]; raw.payload.choices.variables[0].chunks = [1, 3];
  if (kind !== 'tree') {
    raw.kind = 'array'; raw.payload.view_kind = 'image'; delete raw.payload.tree;
    raw.payload.selected = { variable: options.variable, selection: options.selection, decode: 'raw' };
    raw.payload.axes = [{ dimension: 0, indices: [0, 1] }, { dimension: 1, indices: [0, 1, 2] }];
    raw.payload.array = { shape: [2, 3], dimensions: ['index_0', 'index_1'], values: [1, null, 3, 4, 5, 6] };
    Object.assign(raw.metadata, { chunks_touched: 2, decoded_chunk_bytes: 48, nonfinite_values: 1 });
  }
  return raw;
}

test('actual image view preserves row-major numeric values and separate exact axes', async t => {
  const view = mount(t, options => gridFixture(options.kind, options)); await flush(); view.state.viewKind.value = 'image'; await flush();
  await view.state.loadWindow(); await flush(); assert.equal(view.state.error.value, '');
  const trace = view.seen.plots[0].traces[0]; assert.equal(trace.type, 'heatmap'); assert.deepEqual(trace.z, [[1, null, 3], [4, 5, 6]]);
  assert.deepEqual(trace.x, [0, 1, 2]); assert.deepEqual(trace.y, [0, 1]); assert.equal(trace.zsmooth, false);
  assert.deepEqual(view.seen.requests[1].options.selection, [slice(0, 2), slice(0, 3)]);
});

test('empty or unsupported catalogs retain directory without automatic data calls', async t => {
  const view = mount(t, () => { const r = fixture(); r.payload.choices.variables[0].selectable = false; r.payload.choices.variables[0].reason = 'filter'; r.payload.tree[0].attributes.reason = 'filter'; return r; });
  await flush(); assert.equal(view.state.catalog.value.tree.length, 1); assert.equal(view.seen.requests.length, 1); assert.match(view.state.error.value, /没有可安全/);
});

test('actual component only inspects initially then explicit slice pins version and preserves nulls', async t => {
  const view = mount(t, requestFixture); await flush(); assert.equal(view.state.error.value, ''); assert.equal(view.seen.requests.length, 1); assert.deepEqual(view.seen.requests[0].options, { kind: 'tree' }); assert.equal(view.seen.plots.length, 0);
  await view.state.loadWindow(); await flush(); assert.equal(view.state.error.value, ''); assert.equal(view.seen.requests[1].options.version, version);
  assert.deepEqual(view.seen.plots[0].traces[0].y, [1.0000000000000002, null, -9999, 4]); assert.equal(view.seen.plots[0].traces[0].connectgaps, false);
  view.stop(); assert.ok(view.seen.purged.includes(view.seen.plots[0].element)); assert.equal(view.seen.requests[1].signal.aborted, true);
});
test('out of bounds is rejected before a data request', async t => { const view = mount(t, requestFixture); await flush(); view.state.dimensions.value[0].stop = 5; await view.state.loadWindow(); assert.equal(view.seen.requests.length, 1); assert.match(view.state.error.value, /无效/); });
test('same revision catalog clones do not reload but source and effective permission do', async t => {
  const view = mount(t, requestFixture); await flush();
  for (let i = 0; i < 3; i++) { view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); }
  assert.equal(view.seen.requests.length, 1);
  view.props.file.metadata.dataset_file_version = 'b'; await flush(); assert.equal(view.seen.requests.length, 2);
  view.props.plugin.enabled = false; await flush(); assert.equal(view.seen.requests.length, 2); assert.equal(view.state.catalog.value, undefined);
  view.props.plugin.enabled = true; await flush(); assert.equal(view.seen.requests.length, 3);
});
test('late inspection after unmount or disable cannot publish variable choices', async t => {
  for (const action of ['unmount', 'disable']) {
    const task = pending(), view = mount(t, () => task.promise); await flush();
    if (action === 'unmount') view.stop(); else { view.props.plugin.enabled = false; await flush(); }
    task.resolve(fixture()); await flush(); assert.equal(view.state.catalog.value, undefined); assert.equal(view.seen.requests[0].signal.aborted, true); assert.equal(view.seen.plots.length, 0);
  }
});
test('late slice after changed file cannot mount a plot', async t => {
  const task = pending(), view = mount(t, options => options.kind === 'tree' ? fixture() : task.promise); await flush();
  const running = view.state.loadWindow(); await flush(); view.props.file.file_id = 'other'; await flush(); task.resolve(fixture('series')); await running; await flush(); assert.equal(view.state.data.value, undefined); assert.equal(view.seen.plots.length, 0);
});
test('wrong file version or selected variable/window is never drawn', async t => {
  for (const mode of ['version', 'selection', 'dtype']) {
    const view = mount(t, options => { const result = requestFixture(options); if (options.kind !== 'tree') { if (mode === 'version') result.version = 'c'.repeat(64); if (mode === 'selection') result.payload.selected.selection = [slice(1, 4)]; if (mode === 'dtype') result.payload.choices.variables[0].dtype = '>f8'; } return result; });
    await flush(); await view.state.loadWindow(); await flush(); assert.notEqual(view.state.error.value, ''); assert.equal(view.seen.plots.length, 0); view.stop();
  }
});
test('late Plotly render cleans its own element without restoring stale data', async t => {
  const task = pending(), view = mount(t, requestFixture, () => task.promise); await flush(); const running = view.state.loadWindow(); await flush(); assert.equal(view.seen.plots.length, 1); view.props.plugin.enabled = false; await flush(); task.resolve(); await running; await flush(); assert.equal(view.state.data.value, undefined); assert.ok(view.seen.purged.includes(view.seen.plots[0].element));
});
test('template exposes raw semantics and does not accept executable HTML/URLs', () => { const source = readFileSync(new URL('../src/visualizations/extended/ArrayWindowPreview.vue', import.meta.url), 'utf8'); assert.match(source, /原始存储值/); assert.match(source, /未应用 CF/); assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|https?:\/\//); });
