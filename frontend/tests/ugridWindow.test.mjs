import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';
import { parse, compileScript } from '@vue/compiler-sfc';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/ugridWindowData.ts';
const fixture = JSON.parse(readFileSync(new URL('./browser/ugrid-window-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64), selected = fixture.node_geometry.selected;
function envelope(kind = 'tree', prefix = 'node') {
  const { contract_version, type, reader, kind: view_kind, metadata, warnings, sampled, ...payload } = structuredClone(fixture[prefix + '_' + kind]);
  return { contract_version, kind, version, revision: 'b'.repeat(64), plugin_id: 'viz-ugrid-window', payload: { ...payload, view_kind }, metadata, warnings, sampled };
}
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: v => resolve(v) }; };
for (const prefix of ['node', 'face']) test(`${prefix}: actual NetCDF4 geometry and explicit raw values`, () => {
  const result = envelope('geometry', prefix), parsed = data.parseUgridWindow('geometry', result.payload, result.metadata, result.payload.selected);
  assert.deepEqual(parsed.geometry.faces, [[0, 1, 2, 3], [1, 4, 2]]);
  assert.deepEqual(parsed.geometry.values, prefix === 'node' ? [17, 20, null, 26, 29] : [2, 4]);
  const pixels = data.ugridCanvasPoints(parsed.geometry, 640, 420);
  assert.equal(pixels.length, 5); assert.equal(pixels[1][0] - pixels[0][0], pixels[0][1] - pixels[3][1]);
});
const mutations = {
  unknown: r => r.payload.url = 'https://example.org', media: r => r.payload.media_type = 'text/html', view: r => r.payload.view_kind = 'array',
  source: r => r.metadata.source_bytes = 8 * 1024 ** 3 + 1, bytes: r => r.metadata.read_bytes = 8388609, reads: r => r.metadata.read_requests = 129,
  metadata: r => r.metadata.path = '/private/data', limits: r => r.metadata.limits.max_faces++, bounds: r => r.metadata.bounds[0][0]++, missing: r => r.metadata.missing_values++,
  conventions: r => r.metadata.conventions = 'CF-1.8', topology: r => r.metadata.topology_complete = false,
  dtype: r => r.payload.choices.meshes[0].fields[1].dtype = 'object', dimensions: r => r.payload.choices.meshes[0].fields[1].dimensions[0].size = 3,
  location: r => r.payload.ugrid.location = 'edge', fieldLocation: r => r.payload.choices.meshes[0].fields[1].location = 'edge',
  selection: r => r.payload.selected.indices = [0, 0], floatIndex: r => r.payload.selected.indices = [.5, 0], boolIndex: r => r.payload.selected.indices = [true, 0],
  invalidNode: r => r.payload.ugrid.faces[0][0] = 100, duplicate: r => r.payload.ugrid.faces[0][0] = 1,
  intersection: r => r.payload.ugrid.faces[0] = [0, 2, 1, 3], short: r => r.payload.ugrid.faces[0] = [0, 1],
  nullCoordinate: r => r.payload.ugrid.coordinates[0] = null, boolCoordinate: r => r.payload.ugrid.coordinates[0] = true,
  emptyCoordinate: r => r.payload.ugrid.coordinates.pop(), dateline: r => { r.payload.ugrid.coordinates[0] = -179; r.metadata.bounds[0][0] = -179; },
  infiniteValue: r => r.payload.ugrid.values[0] = Infinity, float32Rounding: r => r.payload.ugrid.values[0] = .1,
  unknownUnit: r => r.payload.choices.meshes[0].fields[1].unit = 'https://evil/',
};
for (const [name, mutate] of Object.entries(mutations)) test(`strict boundary rejects ${name}`, () => { const r = envelope('geometry'); mutate(r); assert.throws(() => data.parseUgridWindow('geometry', r.payload, r.metadata, selected)); });

function mount(t, request = options => envelope(options.kind)) {
  const saved = { document: globalThis.document, window: globalThis.window, ResizeObserver: globalThis.ResizeObserver };
  const seen = { requests: [], canvases: [], observers: [], fills: 0, arcs: 0, strokes: 0 };
  const context = { setTransform() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() { seen.fills++; }, stroke() { seen.strokes++; }, arc() { seen.arcs++; } };
  const host = { clientWidth: 640, clientHeight: 420, replaceChildren() {} };
  globalThis.document = { createElement() { const canvas = { style: {}, dataset: {}, width: 0, height: 0, removed: false, getContext() { return context; }, remove() { this.removed = true; } }; seen.canvases.push(canvas); return canvas; } };
  globalThis.window = { devicePixelRatio: 1 };
  globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; this.disconnected = false; seen.observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  t.after(() => Object.assign(globalThis, saved));
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './ugridWindowData': data,
    '../runtime': { requestVisualization: async (_f, _p, _o, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/UgridWindowPreview.vue', import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'ugrid' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'opaque-ugrid', filename: 'synthetic.nc', size: fixture.node_tree.metadata.source_bytes, metadata: { dataset_file_version: 'a' } }, plugin: { id: 'viz-ugrid-window', version: '1.0.0', enabled: true, reader: 'ugrid-window', capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = host; const stop = () => scope.stop(); t.after(stop);
  return { state, props, seen, stop };
}
async function choose(value) { value.state.fieldId.value = selected.field; await flush(); value.state.indices.value = [1, 2]; await flush(); }
test('directory first, explicit version+all dimensions, correct node semantics and release', async t => {
  const value = mount(t); await flush(); assert.equal(value.seen.requests.length, 1); assert.equal(value.seen.canvases.length, 0);
  await choose(value); await value.state.loadGeometry(); assert.equal(value.state.error.value, '');
  assert.deepEqual(value.seen.requests[1].options, { kind: 'geometry', version, ...selected });
  assert.equal(value.seen.arcs, 5); assert.equal(value.seen.strokes, 2); assert.equal(value.seen.fills, 5);
  assert.deepEqual(value.state.displayed.value.geometry.values, [17, 20, null, 26, 29]);
  value.state.inspectIndex.value = 4; await flush(); assert.match(value.state.detail.value.text, /节点 4；原始坐标：102, 30；原始值：29/); assert.equal(value.seen.requests.length, 2);
  value.stop(); assert.ok(value.seen.canvases.every(c => c.removed && !c.width && !c.height)); assert.ok(value.seen.observers.every(o => o.disconnected));
});
test('unchanged file/plugin polling clones retain canvas and selection without reread', async t => {
  const value = mount(t); await flush(); await choose(value); await value.state.loadGeometry();
  value.props.file = structuredClone(vue.toRaw(value.props.file)); value.props.plugin = structuredClone(vue.toRaw(value.props.plugin)); await flush();
  assert.equal(value.seen.requests.length, 2); assert.equal(value.seen.canvases.length, 1); assert.deepEqual(value.state.indices.value, [1, 2]);
});
for (const change of ['version', 'source', 'catalog', 'sampled']) test(`bind ${change} to inspected source`, async t => {
  const value = mount(t, options => { const r = envelope(options.kind); if (options.kind === 'geometry') { if (change === 'version') r.version = 'c'.repeat(64); if (change === 'source') r.metadata.source_bytes++; if (change === 'catalog') r.payload.choices.meshes[0].fields[0].unit = 'km'; if (change === 'sampled') r.sampled = true; } return r; });
  await flush(); await choose(value); await value.state.loadGeometry(); assert.ok(value.state.error.value); assert.equal(value.seen.canvases.length, 0);
});
for (const action of ['unmount', 'disable', 'file-change', 'selection-change']) test(`cancel pending response on ${action}`, async t => {
  const gate = pending(), value = mount(t, options => options.kind === 'geometry' ? gate.promise : envelope()); await flush(); await choose(value);
  const running = value.state.loadGeometry(); await flush(); const request = value.seen.requests[1];
  if (action === 'unmount') value.stop(); if (action === 'disable') value.props.plugin.enabled = false; if (action === 'file-change') value.props.file.file_id = 'other'; if (action === 'selection-change') value.state.indices.value = [0, 0];
  await flush(); assert.equal(request.signal.aborted, true); gate.resolve(envelope('geometry')); await running; assert.equal(value.seen.canvases.length, 0);
});
