import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';

const loadTS = (path, deps = {}) => {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in deps, id); return deps[id]; }, module, module.exports);
  return module.exports;
};
const common = loadTS('../src/visualizations/extended/cziData.ts');
const czi = loadTS('../src/visualizations/extended/cziWindowData.ts', { './cziData': common });
const flush = async () => { for (let i = 0; i < 70; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function inspect() {
  return { contract_version: 2, type: 'czi-window', reader: 'czi-window', kind: 'tree', media_type: 'application/json', tree: [{ path: '/0', node_type: 'object', attributes: { label: 'Uncompressed CZI directory', children_count: 0 } }], metadata: { format: 'CZI', variant: 'CZI 1.0 DV uncompressed full-resolution', engine: 'dataseek-czi-dv-window-v1', scene: 0, scene_shape: [6, 8], dimension_sizes: { C: 2, Z: 2, T: 1 }, pixel_types: ['Gray16', 'Gray16'], input_mode: 'window', source_bytes: 80002000, read_bytes: 1320, read_requests: 3, directory_bytes: 800, subblocks: 4, metadata_hidden: true, coverage: 'not decoded', limits: { max_source_bytes: 8 * 1024 ** 3, max_read_bytes: 1024 ** 2, max_total_bytes: 32 * 1024 ** 2, max_reads: 4096, max_directory_bytes: 4 * 1024 ** 2, max_entries: 16384, max_roi_size: 1024 } }, choices: { channels: [0, 1], z_count: 2, time_count: 1, max_roi_size: 1024 }, selected: { indices: [0, 0, 0], roi: [0, 0, 8, 6] }, sampled: false, warnings: czi.CZI_WINDOW_WARNINGS, version: 'a'.repeat(64) };
}
function plane() {
  const r = inspect(); r.kind = 'image'; r.media_type = 'image/png'; r.sampled = true; delete r.tree;
  Object.assign(r.metadata, { coverage: 'complete and non-overlapping', read_bytes: 1500, read_requests: 5, display_range: [0, 47], normalization: 'ROI min-max to uint8 grayscale; original data unchanged', output_shape: [6, 8], invalid_pixels: 0 });
  // Unit schema check only. Real native PNG/browser decoding is separate.
  const header = Buffer.alloc(33); Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82]).copy(header); header.writeUInt32BE(8, 16); header.writeUInt32BE(6, 20); header[24] = 8; header[25] = 6; r.data_base64 = header.toString('base64'); return r;
}
function mount(t, request) {
  const seen = { requests: [], urls: [], revoked: [], removed: 0 }, mounted = [];
  const modules = { vue: { ...vue, onMounted: fn => mounted.push(fn) }, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './runtime': { requestPreview: async (file, plugin, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } }, './cziWindowData': czi, './domains/lifecycle': { displayError: reason => reason.message } };
  const previous = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  URL.createObjectURL = () => { const url = `blob:window-${seen.urls.length}`; seen.urls.push(url); return url; }; URL.revokeObjectURL = url => seen.revoked.push(url);
  t.after(() => { URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke; });
  const source = readFileSync(new URL('../src/visualizations/extended/CziWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'czi-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-1', filename: 'data.czi', size: 80002000 }, plugin: { id: 'viz-czi-window', version: '1', enabled: true, reader: 'czi-window', adapter: 'czi-window', capabilities: { input_mode: 'window', operations: ['preview'], shared: false }, limits: { max_input_bytes: 32 * 1024 ** 2, max_output_bytes: 8 * 1024 ** 2 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  state.imageElement.value = { removeAttribute() { seen.removed++; }, naturalWidth: 8, naturalHeight: 6 };
  const stop = () => scope.stop(); t.after(stop);
  return { state, props, seen, stop, start: async () => { for (const fn of mounted) await fn(); await flush(); } };
}

test('window schema accepts actual accounting shape and rejects whole-file metadata', () => {
  assert.equal(czi.parseCziWindowData(inspect()).metadata.read_bytes, 1320);
  for (const mutate of [r => { r.metadata.input_mode = 'whole'; }, r => { r.metadata.engine = 'pylibCZIrw 6.1.0'; }, r => { r.metadata.read_bytes = 32 * 1024 ** 2 + 1; }, r => { r.metadata.read_requests = 4097; }, r => { r.metadata.coverage = 'filled'; }, r => { r.metadata.limits.max_total_bytes = 1; }, r => { r.metadata.path = '/Users/private'; }, r => { r.metadata.pixel_types[0] = ['Gray16']; }, r => { r.kind = ['tree']; }, r => { r.metadata.scene = false; }, r => { r.selected.roi[2] = true; }]) { const r = inspect(); mutate(r); assert.throws(() => czi.parseCziWindowData(r)); }
});
test('window PNG rejects shape/color mismatch, unknown units, huge output and nonfinite range', () => {
  assert.equal(czi.parseCziWindowData(plane()).png.length, 33);
  for (const mutate of [r => { r.metadata.output_shape = [1, 1]; }, r => { r.metadata.pixel_types[0] = 'Bgr24'; }, r => { r.metadata.units = 'mm'; }, r => { r.data_base64 = '<svg>'; }, r => { r.metadata.display_range[1] = Infinity; }, r => { r.metadata.invalid_pixels = 48; }, r => { r.sampled = false; }]) { const r = plane(); mutate(r); assert.throws(() => czi.parseCziWindowData(r)); }
});
test('tree is metadata-only and image action pins exact version and ROI', async t => {
  const v = mount(t, options => options.kind === 'tree' ? inspect() : plane()); await v.start();
  assert.deepEqual(v.seen.requests[0].options, { kind: 'tree' }); assert.equal(v.state.imageUrl.value, '');
  await v.state.load('image'); assert.equal(v.state.error.value, ''); assert.deepEqual(v.seen.requests[1].options, { kind: 'image', version: 'a'.repeat(64), indices: [0, 0, 0], roi: [0, 0, 8, 6] });
  assert.equal(v.state.imageUrl.value, 'blob:window-0'); v.state.imageElement.value = undefined; v.stop(); assert.ok(v.seen.removed > 0); assert.deepEqual(v.seen.revoked, v.seen.urls);
});
test('bad source size, changed version or changed selected image is never published', async t => {
  for (const field of ['source', 'version', 'indices', 'roi']) {
    const v = mount(t, options => { if (options.kind === 'tree') return inspect(); const r = plane(); if (field === 'source') r.metadata.source_bytes++; if (field === 'version') r.version = 'b'.repeat(64); if (field === 'indices') r.selected.indices = [1, 0, 0]; if (field === 'roi') { r.selected.roi = [1, 0, 7, 6]; r.metadata.output_shape = [6, 7]; } return r; });
    await v.start(); await v.state.load('image'); assert.ok(v.state.error.value); assert.equal(v.seen.urls.length, 0); v.stop();
  }
});
test('invalid ROI is rejected locally and pending image prevents duplicate requests', async t => {
  const next = pending(), v = mount(t, options => options.kind === 'tree' ? inspect() : next.promise); await v.start();
  v.state.roi.value = [0, 0, 1025, 1]; await v.state.load('image'); assert.equal(v.seen.requests.length, 1);
  v.state.roi.value = [0, 0, 8, 6]; const call = v.state.load('image'); await flush(); await v.state.load('image'); assert.equal(v.seen.requests.length, 2); next.resolve(plane()); await call;
});
test('clone polling causes no new reads while full descriptor or file identity changes do', async t => {
  const v = mount(t, () => inspect()); await v.start();
  for (let i = 0; i < 3; i++) { v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); v.props.file = { ...v.props.file }; await flush(); }
  assert.equal(v.seen.requests.length, 1); v.props.plugin.limits.max_input_bytes--; await flush(); assert.equal(v.seen.requests.length, 2);
  v.props.file.filename = 'replacement.czi'; await flush(); assert.equal(v.seen.requests.length, 3);
});
test('disable, unmount and late response cannot retain image sources or blobs', async t => {
  const next = pending(), v = mount(t, options => options.kind === 'tree' ? inspect() : next.promise); await v.start();
  const action = v.state.load('image'); await flush(); v.props.plugin.enabled = false; await flush();
  assert.equal(v.seen.requests[1].signal.aborted, true); next.resolve(plane()); await action; assert.equal(v.seen.urls.length, 0); assert.equal(v.state.details.value, undefined);
});
test('PNG decoder error and unexpected decoded size release actual DOM source', async t => {
  for (const mismatch of [false, true]) {
    const v = mount(t, options => options.kind === 'tree' ? inspect() : plane()); await v.start(); await v.state.load('image');
    if (mismatch) { v.state.imageElement.value.naturalWidth = 99; v.state.imageLoaded(); } else v.state.imageError();
    assert.equal(v.state.imageUrl.value, ''); assert.match(v.state.error.value, /解码/); assert.deepEqual(v.seen.revoked, v.seen.urls); v.stop();
  }
});
test('window UI is inert and explicitly communicates bounded unsupported dialects', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/CziWindowPreview.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|https?:\/\//); assert.match(source, /不自动回退为整文件下载/); assert.match(source, /filePreviewIdentity/); assert.match(source, /@error="imageError"/);
});
