import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/enviWindowData.ts';
const fixture = JSON.parse(readFileSync(new URL('./browser/domain-expansion-envi-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64), imageOptions = fixture.wavelength.image.selected, seriesOptions = fixture.wavelength.series.selected;
const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function result(kind = 'tree', group = 'wavelength') {
  const { contract_version, type, reader, metadata, warnings, sampled, kind: privateKind, ...payload } = structuredClone(fixture[group][kind]);
  return { contract_version, kind: kind === 'tree' ? 'tree' : 'array', plugin_id: 'viz-envi-window', version, revision: 'b'.repeat(64), metadata, warnings, sampled, payload: { ...payload, view_kind: kind } };
}
for (const group of ['wavelength', 'frequency']) test(`real ${group} reader fixtures preserve exact image/series axes and read counts`, () => {
  for (const kind of ['tree', 'image', 'series']) {
    const r = result(kind, group), data = helpers.parseEnviWindow(kind, r.payload, r.metadata, r.payload.selected);
    assert.equal(data.sourceBytes, data.headerBytes + 248); assert.equal(data.cube.interleave, 'bip');
    if (kind === 'tree') { assert.equal(data.array, null); assert.equal(data.readBytes, data.headerBytes); assert.equal(data.reads, 1); }
    else { assert.deepEqual(data.array.values, fixture[group][kind].array.values); assert.equal(data.reads, 2); }
    if (kind === 'series') { assert.equal(data.axes[0].label, '光谱坐标'); assert.equal(data.axes[0].unit, group === 'frequency' ? 'GHz' : 'Nanometers'); assert.deepEqual(data.axes[0].values, [400, 500, 600, 700, 800]); }
  }
});
const mutations = [
  ['wrong view kind', r => r.payload.view_kind = 'series'], ['unknown root resource', r => r.payload.url = 'file:///etc/passwd'],
  ['host path metadata', r => r.metadata.path = '/Users/private'], ['wrong format', r => r.metadata.format = 'tiff'],
  ['wrong input mode', r => r.metadata.input_mode = 'whole'], ['boolean source bytes', r => r.metadata.source_bytes = true],
  ['source over budget', r => r.metadata.source_bytes = 8589934593], ['read over budget', r => r.metadata.read_bytes = 8388609],
  ['read requests over budget', r => r.metadata.read_requests = 257], ['unaccounted pair bytes', r => r.metadata.data_bytes++],
  ['oversize header', r => r.metadata.header_bytes = 65537], ['null count mismatch', r => r.metadata.null_values = 1],
  ['calibrated data', r => r.metadata.no_calibration = false], ['georeferenced data', r => r.metadata.no_georeferencing = false],
  ['limits expansion', r => r.metadata.limits.max_values++], ['boolean limit', r => r.metadata.limits.max_values = true],
  ['unknown choices resource', r => r.payload.choices.resource_id = 'other'], ['source dimensions mismatch', r => r.payload.choices.cube.samples++],
  ['complex dtype', r => r.payload.choices.cube.data_type = 6], ['64 bit integer dtype', r => r.payload.choices.cube.data_type = 14],
  ['boolean dtype', r => r.payload.choices.cube.data_type = true], ['unknown interleave', r => r.payload.choices.cube.interleave = 'gzip'],
  ['infinite wavelength', r => r.payload.choices.cube.wavelengths[0] = Infinity], ['boolean wavelength', r => r.payload.choices.cube.wavelengths[0] = true],
  ['unsafe wavelength unit', r => r.payload.choices.cube.wavelength_unit = '<img src=x>'], ['float32 nonquantized ignore', r => r.payload.choices.cube.ignore_value = -9999.1],
  ['array dtype mismatch', r => r.payload.array.dtype = 'float64'], ['array count mismatch', r => r.payload.array.values.pop()],
  ['boolean array value', r => r.payload.array.values[0] = true], ['infinite array value', r => r.payload.array.values[0] = Infinity],
  ['wrong array shape', r => r.payload.array.shape = [3, 2]], ['wrong dimensions', r => r.payload.array.dimensions = ['band', 'pixel']],
  ['boolean axis', r => r.payload.axes[0].values[0] = true], ['injected axis URL', r => r.payload.axes[0].url = 'file:///etc/passwd'],
  ['axis location mismatch', r => r.payload.axes[0].values[0] = 0], ['projection in options', r => r.payload.selected.crs = 'EPSG:4326'],
];
for (const [name, mutate] of mutations) test(`ENVI parser rejects ${name}`, () => { const r = result('image'); mutate(r); assert.throws(() => helpers.parseEnviWindow('image', r.payload, r.metadata)); });
test('tree shape and flags reject bool-as-number; header alone is not whole pair', () => {
  const r = result(); r.payload.tree[0].shape[0] = true; assert.throws(() => helpers.parseEnviWindow('tree', r.payload, r.metadata));
  assert.ok(result().metadata.source_bytes !== result().metadata.header_bytes);
});
test('exact selection binds band/pixel/ROI without relying on object key order', () => {
  const r = result('image');
  assert.deepEqual(helpers.parseEnviWindow('image', r.payload, r.metadata, { height: 2, width: 3, y: 1, x: 1, band: 2 }).array.values, [211, 212, 213, 221, 222, 223]);
  for (const [key, value] of [['band', 1], ['x', 0], ['y', 0], ['width', 2], ['height', 1]]) assert.throws(() => helpers.parseEnviWindow('image', r.payload, r.metadata, { ...imageOptions, [key]: value }));
});
test('absent spectral coordinates use raw band indices, never guessed units', () => {
  const r = result('series'); r.payload.choices.cube.wavelengths = null; r.payload.choices.cube.wavelength_unit = null; r.payload.axes = [{ label: '波段索引', unit: null, values: [0, 1, 2, 3, 4] }];
  assert.deepEqual(helpers.parseEnviWindow('series', r.payload, r.metadata).axes, r.payload.axes);
});
for (const [kind, options] of [['tree', { x: 0 }], ['tree', []], ['image', {}], ['image', { ...imageOptions, band: true }], ['image', { ...imageOptions, width: 129 }], ['image', { ...imageOptions, x: 4 }], ['image', { ...imageOptions, band: 5 }], ['series', { ...seriesOptions, band_count: 129 }], ['series', { ...seriesOptions, band_start: 5 }]]) {
  test(`invalid request ${kind} ${JSON.stringify(options)}`, () => assert.throws(() => helpers.validateEnviOptions(kind, options, fixture.wavelength.tree.choices.cube)));
}
function mount(t, { response = options => result(options.kind), libraryWait, plotWait } = {}) {
  const seen = { requests: [], libraries: [], plots: [], purged: [], observers: [], elements: [] };
  const lib = { async newPlot(element, traces, layout, config) { seen.plots.push({ element, traces, layout, config }); if (plotWait) await plotWait; element.rendered = true; }, purge(element) { seen.purged.push(element); element.rendered = false; }, Plots: { resize() {} } };
  const FakeResizeObserver = class { constructor() { this.disconnected = false; seen.observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  const document = { createElement() { const el = { dataset: {}, style: {}, rendered: false, removed: false, remove() { this.removed = true; } }; seen.elements.push(el); return el; } };
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './enviWindowData': helpers,
    './scientific/browserLibraries': { loadBrowserLibrary: async (name, signal) => { seen.libraries.push({ name, signal }); if (libraryWait) await libraryWait; return lib; } },
    '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { seen.requests.push({ file: structuredClone(vue.toRaw(file)), operation, options, signal }); return response(options); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/EnviWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'envi-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', 'document', 'ResizeObserver', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports, document, FakeResizeObserver);
  const props = vue.reactive({ file: { file_id: 'dataset-preview:envi-a', filename: 'cube.hdr', size: result().metadata.header_bytes, metadata: { dataset_file_version: 'a' } }, plugin: { id: 'viz-envi-window', version: '1', adapter: 'envi-window', reader: 'envi-window', enabled: true, capabilities: { operations: ['preview'], input_mode: 'window', shared: false }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const scope = vue.effectScope(), state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = { replaceChildren() {} };
  t.after(() => scope.stop()); return { state, props, seen, stop: () => scope.stop() };
}
function select(view, kind = 'image') {
  view.state.view.value = kind;
  for (const [key, value] of Object.entries(kind === 'image' ? imageOptions : seriesOptions)) view.state[{ band_start: 'bandStart', band_count: 'bandCount' }[key] ?? key].value = value;
}
test('component metadata only then explicit image and spectrum pin pair version', async t => {
  const v = mount(t); await flush(); assert.equal(v.state.error.value, ''); assert.equal(v.seen.requests.length, 1); assert.equal(v.seen.libraries.length, 0); assert.equal(v.state.displayed.value, undefined);
  select(v); await v.state.loadWindow(); assert.equal(v.state.error.value, ''); assert.equal(v.seen.requests[1].options.version, version); assert.deepEqual(v.seen.plots[0].traces[0].z, [[211, 212, 213], [221, 222, 223]]);
  assert.equal(v.seen.plots[0].layout.yaxis.autorange, 'reversed'); assert.equal(v.seen.plots[0].traces[0].connectgaps, false);
  select(v, 'series'); assert.equal(v.state.displayed.value, undefined); await v.state.loadWindow(); assert.equal(v.state.error.value, ''); assert.deepEqual(v.seen.plots[1].traces[0].y, [12, 112, 212, 312, 412]);
  assert.equal(v.seen.plots[1].layout.xaxis.title.text, '光谱坐标 [Nanometers]'); assert.equal(v.seen.elements[0].removed, true); assert.equal(v.seen.observers[0].disconnected, true);
});
test('clone polling never re-reads; disable synchronously clears prior catalog and plot', async t => {
  const v = mount(t); await flush(); select(v); await v.state.loadWindow();
  for (let i = 0; i < 3; i++) { v.props.file = structuredClone(vue.toRaw(v.props.file)); v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); await flush(); }
  assert.equal(v.seen.requests.length, 2); v.props.plugin.enabled = false; assert.equal(v.state.catalog.value, undefined); assert.equal(v.state.displayed.value, undefined); assert.equal(v.seen.elements[0].removed, true);
});
for (const action of ['disable', 'unmount', 'selection', 'file', 'limits']) test(`late image after ${action} cannot render or revive`, async t => {
  const task = pending(), v = mount(t, { response: options => options.kind === 'tree' ? result() : task.promise }); await flush(); select(v); const running = v.state.loadWindow(); await flush();
  if (action === 'disable') v.props.plugin.enabled = false; else if (action === 'unmount') v.stop(); else if (action === 'selection') v.state.band.value = 1; else if (action === 'file') v.props.file.file_id = 'dataset-preview:envi-b'; else v.props.plugin.limits.max_input_bytes--;
  task.resolve(result('image')); await running; assert.equal(v.seen.requests[1].signal.aborted, true); assert.equal(v.state.displayed.value, undefined); assert.equal(v.seen.plots.length, 0);
});
test('late initial tree after disable cannot revive catalog', async t => {
  const task = pending(), v = mount(t, { response: () => task.promise }); await flush(); v.props.plugin.enabled = false; task.resolve(result()); await flush(); assert.equal(v.state.catalog.value, undefined); assert.equal(v.seen.requests[0].signal.aborted, true);
});
for (const phase of ['library', 'newPlot']) test(`late ${phase} completion after unmount is cleaned, including cleared DOM ref`, async t => {
  const task = pending(), v = mount(t, phase === 'library' ? { libraryWait: task.promise } : { plotWait: task.promise }); await flush(); select(v); const running = v.state.loadWindow(); await flush(); v.state.target.value = undefined; v.stop(); task.resolve(); await running;
  assert.equal(v.state.displayed.value, undefined); assert.equal(v.seen.libraries[0].signal.aborted, true);
  for (const element of v.seen.elements) { assert.equal(element.removed, true); assert.equal(element.rendered, false); assert.ok(v.seen.purged.includes(element)); }
});
for (const change of ['version', 'public-kind', 'band', 'cube', 'source', 'header']) test(`component rejects valid-envelope wrong ${change}`, async t => {
  const v = mount(t, { response: options => { const r = result(options.kind);
    if (options.kind === 'tree' && change === 'header') { r.metadata.header_bytes++; r.metadata.source_bytes++; r.metadata.read_bytes++; }
    if (options.kind !== 'tree') { if (change === 'version') r.version = 'c'.repeat(64); if (change === 'public-kind') r.kind = 'series'; if (change === 'band') r.payload.selected.band = 1;
      if (change === 'cube') r.payload.choices.cube.wavelength_unit = 'GHz'; if (change === 'source') { r.payload.choices.cube.header_offset++; r.metadata.data_bytes++; r.metadata.source_bytes++; } }
    return r; } });
  await flush(); select(v); await v.state.loadWindow(); assert.equal(v.state.displayed.value, undefined); assert.notEqual(v.state.error.value, ''); assert.equal(v.seen.plots.length, 0);
});
test('invalid ROI stops before an API window request', async t => {
  const v = mount(t); await flush(); v.state.x.value = 4; await v.state.loadWindow(); assert.equal(v.seen.requests.length, 1); assert.equal(v.seen.plots.length, 0); assert.notEqual(v.state.error.value, '');
});
