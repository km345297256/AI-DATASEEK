import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import * as three from 'three';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/pointcloudWindowData.ts';

const fixture = JSON.parse(readFileSync(new URL('./browser/pointcloud-window-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64), selected = { point_offset: 8, point_count: 32 };
function envelope(kind = 'tree', prefix = 'modern') {
  const { contract_version, type, reader, metadata, warnings, sampled, kind: view_kind, ...payload } = structuredClone(fixture[prefix + '_' + kind]);
  return { contract_version, kind, version, revision: 'b'.repeat(64), plugin_id: 'viz-pointcloud-window', payload: { ...payload, view_kind }, metadata, warnings, sampled };
}
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; };

for (const prefix of ['modern', 'legacy', 'precision']) test(`${prefix}: actual raw coordinates, local origin, units and flags`, () => {
  const r = envelope('geometry', prefix), parsed = data.parsePointCloudWindow('geometry', r.payload, r.metadata, r.payload.selected);
  const local = data.localPointGeometry(parsed);
  assert.equal(parsed.metadata.units, 'unknown'); assert.deepEqual(local.positions.slice(0, 3), new Float32Array(3));
  assert.deepEqual(data.pointCoordinates(parsed, 0).raw, r.payload.array.values.slice(0, 3));
  if (prefix === 'precision') {
    assert.equal(parsed.raw[3] - parsed.raw[0], 1);
    assert.ok(Math.abs(local.positions[3] * local.displayScale - .001) < 1e-9);
    assert.ok(Math.abs(local.positions[4] * local.displayScale - .002) < 1e-9);
    assert.deepEqual(local.originRaw, [2147483000, -2147483000, 0]);
  }
});

const changes = {
  'unknown payload': r => r.payload.url = 'https://invalid', 'media': r => r.payload.media_type = 'text/html',
  'view': r => r.payload.view_kind = 'series', 'selected': r => r.payload.selected.point_offset = 0,
  'count': r => r.payload.selected.point_count = 16385, 'metadata extra': r => r.metadata.path = '/private',
  'source': r => r.metadata.source_bytes = 8589934593, 'read bytes': r => r.metadata.read_bytes++, 'reads': r => r.metadata.read_requests = 0,
  'format': r => r.metadata.format = 'laz', 'version': r => r.metadata.las_version = '1.5', 'point format': r => r.metadata.point_format = 9,
  'record': r => r.metadata.record_bytes = 20, 'extent': r => r.metadata.point_data_offset = 10, 'total': r => r.metadata.total_points = 2 ** 32,
  'extra stride': r => r.metadata.extra_bytes_per_point = 1, 'vlrs': r => r.metadata.vlr_count = 97,
  'scale': r => r.metadata.scales[0] = 0, 'offset': r => r.metadata.offsets[0] = Infinity,
  'CRS guess': r => r.metadata.crs_declarations = ['EPSG:4326'], 'unit guess': r => r.metadata.units = 'm',
  'window bounds': r => r.metadata.window_bounds[0][0] = 0, 'header bounds': r => r.metadata.declared_bounds[0] = [2, 1],
  'output': r => r.metadata.output_points = 31, 'point bytes': r => r.metadata.point_bytes = 0,
  'shape': r => r.payload.array.shape = [16, 6], 'dims': r => r.payload.array.dimensions = ['x', 'y', 'z'],
  'float': r => r.payload.array.values[0] = .1, 'int32': r => r.payload.array.values[0] = 2 ** 31, 'null': r => r.payload.array.values[0] = null,
  'bool': r => r.payload.array.values[0] = true, 'length': r => r.payload.array.values.pop(),
  'attribute extra': r => r.payload.point_attributes.url = 'http://invalid', 'intensity': r => r.payload.point_attributes.intensity[0] = 65536,
  'classification': r => r.payload.point_attributes.classification[0] = 256, 'flags': r => r.payload.point_attributes.classification_flags[0] = 16,
  'RGB': r => r.payload.point_attributes.rgb[0] = -1, 'RGB missing': r => r.payload.point_attributes.rgb = null,
};
for (const [name, change] of Object.entries(changes)) test(`strict LAS schema rejects ${name}`, () => {
  const r = envelope('geometry'); change(r); assert.throws(() => data.parsePointCloudWindow('geometry', r.payload, r.metadata, selected));
});

function mount(t, request = options => envelope(options.kind), libraryGate) {
  const seen = { requests: [], renderers: [], renders: [], removed: [], observers: [], disposed: [], controls: [] };
  const saved = { document: globalThis.document, window: globalThis.window, ResizeObserver: globalThis.ResizeObserver };
  function element() { return { style: {}, dataset: {}, clientWidth: 640, appendChild() {}, replaceChildren() {}, addEventListener() {}, removeEventListener() {}, getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 440 }; }, remove() { seen.removed.push(this); } }; }
  globalThis.document = { createElement: element }; globalThis.window = { devicePixelRatio: 1 };
  globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; this.disconnected = false; seen.observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  t.after(() => Object.assign(globalThis, saved));
  class Renderer {
    domElement = element(); disposed = false; lost = false;
    constructor() { seen.renderers.push(this); }
    setPixelRatio() {} setClearColor() {} setSize() {}
    render(scene, camera) { const point = scene.children[0]; seen.renders.push({ scene, camera, position: point.geometry.getAttribute('position').array, color: point.geometry.getAttribute('color').array });
      if (!point.userData.observed) { point.userData.observed = true; point.geometry.addEventListener('dispose', () => seen.disposed.push('geometry')); point.material.addEventListener('dispose', () => seen.disposed.push('material')); }
    }
    dispose() { this.disposed = true; } forceContextLoss() { this.lost = true; }
  }
  class Controls { target = new three.Vector3(); disposed = false; constructor() { seen.controls.push(this); } update() {} addEventListener() {} dispose() { this.disposed = true; } }
  const library = { ...three, WebGLRenderer: Renderer };
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity,
    '../runtime': { requestVisualization: async (_f, _p, _o, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } },
    './pointcloudWindowData': data, three: libraryGate ? libraryGate.promise.then(() => library) : library,
    'three/addons/controls/OrbitControls.js': { OrbitControls: Controls } };
  const source = readFileSync(new URL('../src/visualizations/extended/PointCloudWindowPreview.vue', import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'pointcloud' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'opaque-las', filename: 'synthetic.las', size: fixture.modern_tree.metadata.source_bytes, metadata: { dataset_file_version: 'a' } }, plugin: { id: 'viz-pointcloud-window', version: '1.0.0', enabled: true, reader: 'pointcloud-window', capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = element(); const stop = () => scope.stop(); t.after(stop);
  return { state, props, seen, stop };
}
async function choose(v) { v.state.pointOffset.value = 8; v.state.pointCount.value = 32; await flush(); }

test('tree first, explicit version, raw values translated before Float32, resources disposed', async t => {
  const v = mount(t); await flush(); assert.deepEqual(v.seen.requests[0].options, { kind: 'tree' }); assert.equal(v.seen.renders.length, 0);
  await choose(v); await v.state.loadWindow(); assert.equal(v.state.error.value, ''); assert.deepEqual(v.seen.requests[1].options, { kind: 'geometry', version, ...selected });
  assert.equal(v.seen.renders.length, 1); assert.equal(v.seen.renders[0].position.length, 96); assert.deepEqual([...v.seen.renders[0].position.slice(0, 3)], [0, 0, 0]);
  assert.deepEqual(v.state.detail.value.xyz, [600000, 4500005, 100.25]);
  v.stop(); assert.ok(v.seen.renderers.every(r => r.disposed && r.lost)); assert.ok(v.seen.observers.every(o => o.disconnected));
  assert.ok(v.seen.controls.every(c => c.disposed)); assert.deepEqual(v.seen.disposed, ['geometry', 'material']); assert.equal(v.seen.removed.length, 1);
});
test('same file and Cordis polling clones do not reread or rebuild the selected scene', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadWindow();
  v.props.file = structuredClone(vue.toRaw(v.props.file)); v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(v.seen.renderers.length, 1); assert.equal(v.state.pointOffset.value, 8);
});
test('classification and RGB recoloring are local, dispose previous scene and never reread points', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadWindow(); v.state.colorMode.value = 'rgb'; await flush();
  assert.equal(v.state.error.value, ''); assert.equal(v.seen.requests.length, 2); assert.equal(v.seen.renderers.length, 2); assert.ok(v.seen.renderers[0].disposed);
  const expected = fixture.modern_geometry.point_attributes.rgb.slice(0, 3).map(n => n / 65535);
  expected.forEach((n, i) => assert.ok(Math.abs(v.seen.renders.at(-1).color[i] - n) < 1e-7));
});
for (const change of ['version', 'source', 'scale', 'sampled', 'selection']) test(`component binds ${change} to inspected directory`, async t => {
  const v = mount(t, options => { const r = envelope(options.kind); if (options.kind === 'geometry') {
    if (change === 'version') r.version = 'c'.repeat(64); if (change === 'source') r.metadata.source_bytes++;
    if (change === 'scale') { r.metadata.scales[0] *= 2; r.metadata.window_bounds[0][1] = 600035; }
    if (change === 'sampled') r.sampled = false; if (change === 'selection') r.payload.selected.point_offset = 0;
  } return r; });
  await flush(); await choose(v); await v.state.loadWindow(); assert.ok(v.state.error.value); assert.equal(v.seen.renderers.length, 0);
});
for (const action of ['unmount', 'disable', 'file-change']) test(`pending range request aborted on ${action}`, async t => {
  const blocked = pending(), v = mount(t, options => options.kind === 'geometry' ? blocked.promise : envelope()); await flush(); await choose(v);
  const running = v.state.loadWindow(); await flush(); const request = v.seen.requests.at(-1);
  if (action === 'unmount') v.stop(); else if (action === 'disable') v.props.plugin.enabled = false; else v.props.file.file_id = 'other';
  await flush(); assert.equal(request.signal.aborted, true); blocked.resolve(envelope('geometry')); await running;
  assert.equal(v.seen.renderers.length, 0); assert.equal(v.state.displayed.value, undefined);
});
test('late Three library resolution after unmount cannot create a WebGL context', async t => {
  const blocked = pending(), v = mount(t, undefined, blocked); await flush(); await choose(v); const running = v.state.loadWindow(); await flush(); v.stop(); blocked.resolve(); await running;
  assert.equal(v.seen.renderers.length, 0);
});
