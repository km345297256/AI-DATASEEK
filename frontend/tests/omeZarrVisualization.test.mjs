import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as ome from '../src/visualizations/extended/omeZarrData.ts';

const fixture = JSON.parse(readFileSync(new URL('./browser/ome-zarr-memory-store-data.json', import.meta.url), 'utf8'));
const value = (kind = 'tree') => ({ ...structuredClone(fixture[kind]), version: 'a'.repeat(64), revision: 'b'.repeat(64) });
const selection = { level: 1, indices: [1, 1, 2], roi: [1, 1, 3, 2] };
const gray = [0, 23, 46, 209, 232, 255];
const rgba = gray.flatMap(v => [v, v, v, 255]);
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function mount(t, request) {
  const seen = { requests: [], canvases: [], draws: [], children: [], replaced: 0 };
  const modules = {
    vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './omeZarrData': ome,
    './runtime': { requestPreview: async (_file, _plugin, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } },
  };
  const before = globalThis.document;
  globalThis.document = { createElement(name) {
    assert.equal(name, 'canvas');
    const canvas = { width: 0, height: 0, dataset: {}, style: {}, attributes: {}, removed: false, onmousemove: null,
      setAttribute(key, item) { this.attributes[key] = item; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 300, height: 200 }; },
      getContext() { return { createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
        putImageData(image) { canvas.pixels = [...image.data]; seen.draws.push(canvas.pixels); } }; },
      remove() { this.removed = true; seen.children = seen.children.filter(child => child !== this); },
    };
    seen.canvases.push(canvas); return canvas;
  } };
  t.after(() => { globalThis.document = before; });
  const source = readFileSync(new URL('../src/visualizations/extended/OmeZarrPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'ome-zarr-test' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const props = vue.reactive({ file: { file_id: 'opaque-dataset-reference', filename: '.zattrs', size: 6600,
    metadata: { source: 'dataset_preview', logical_path: 'synthetic.zarr/.zattrs', dataset_file_version: 'initial' } },
    plugin: { contract_version: 2, id: 'viz-ome-zarr', version: '1.0.0', name: 'OME-Zarr', description: '',
      extensions: [], filenames: ['.zattrs'], view_kind: 'image', adapter: 'ome-zarr', reader: 'ome-zarr',
      capabilities: { operations: ['preview'], input_mode: 'window', shared: false }, enabled: true, default_enabled: true,
      priority: 10, permissions: ['file:read'], limits: { max_input_bytes: 32 * 1024 ** 2, max_output_bytes: 2 * 1024 ** 2 } } });
  const scope = vue.effectScope(), state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  state.target.value = { replaceChildren(...children) { seen.children = children; seen.replaced++; } };
  const stop = () => scope.stop(); t.after(stop);
  return { props, state, seen, stop, start: flush };
}
async function choose(view) {
  view.state.level.value = 1; await flush();
  view.state.indices.value = [1, 1, 2]; view.state.roi.value = [1, 1, 3, 2];
}

test('actual NGFF/Zarr MemoryStore tree and image retain dimensions units values and selection', () => {
  const tree = ome.parseOmeZarr(value(), 'tree');
  assert.deepEqual(tree.axes.map(a => a.name), ['t', 'c', 'z', 'y', 'x']);
  assert.deepEqual(tree.axes.map(a => a.unit), ['second', null, 'micrometer', 'micrometer', 'micrometer']);
  const data = ome.parseOmeZarr(value('image'), 'image', selection);
  assert.deepEqual(data.values, [713, 715, 717, 731, 733, 735]);
  assert.deepEqual(data.levels[1].scale, [2, 1, 3, 1, 1]);
  assert.deepEqual(data.levels[1].translation, [10, 0, -2, 100, -50]);
  assert.equal(data.metadata.loaded_chunks, 1); assert.equal(data.metadata.decoded_chunk_bytes, 48);
});
test('display grayscale is correct without modifying original values', () => {
  const raw = value('image'), data = ome.parseOmeZarr(raw, 'image');
  assert.deepEqual(data.values.map(v => ome.omePixelGray(v, data.metadata.value_range)), gray);
  assert.deepEqual(data.values, [713, 715, 717, 731, 733, 735]);
  assert.equal(ome.omePixelGray(null, [0, 1]), null); assert.equal(ome.omePixelGray(0, null), null);
  assert.equal(ome.omePixelGray(7, [7, 7]), 0);
  assert.deepEqual([-1e308, 0, 1e308].map(v => ome.omePixelGray(v, [-1e308, 1e308])), [0, 128, 255]);
});
test('valid selection is copied rather than retaining mutable caller arrays', () => {
  const chosen = structuredClone(selection), data = ome.parseOmeZarr(value(), 'tree');
  const result = ome.validateOmeSelection(chosen, data); chosen.indices[0] = 0; chosen.roi[0] = 0;
  assert.deepEqual(result, selection);
});
const mutations = {
  'wrong contract': r => { r.contract_version = true; }, 'wrong reader type': r => { r.type = 'array-window'; },
  'wrong media': r => { r.media_type = 'text/html'; }, 'sampling false': r => { r.sampled = false; },
  'axis duplicate': r => { r.choices.axes[4].name = 'z'; }, 'axis order': r => { [r.choices.axes[0], r.choices.axes[1]] = [r.choices.axes[1], r.choices.axes[0]]; },
  'space seconds': r => { r.choices.axes[4].unit = 'second'; }, 'time micrometers': r => { r.choices.axes[0].unit = 'micrometer'; },
  'channel unit': r => { r.choices.axes[1].unit = 'meter'; }, 'unknown space unit': r => { r.choices.axes[4].unit = 'fakeunit'; },
  'unknown time unit': r => { r.choices.axes[0].unit = 'fakesecond'; }, 'path in axis': r => { r.choices.axes[4].path = '/private'; },
  'codec plugin': r => { r.choices.levels[0].compression = 'plugin'; }, 'unsafe dtype': r => { r.choices.levels[0].dtype = '|O'; },
  'fake fill array': r => { r.choices.levels[0].fill_value = ['NaN']; }, 'huge chunk': r => { r.choices.levels[0].chunks = [1000, 1000, 1000, 1000, 1000]; },
  'cross-level size increase': r => { r.choices.levels[1].shape[4] = 10; }, 'cross-level spatial scale decrease': r => { r.choices.levels[1].scale[4] = .25; },
  'cross-level time shape': r => { r.choices.levels[1].shape[0] = 3; }, 'cross-level time scale': r => { r.choices.levels[1].scale[0] = 3; },
  'nonfinite scale': r => { r.choices.levels[0].scale[0] = Infinity; }, 'nonfinite translation': r => { r.choices.levels[1].translation[4] = NaN; },
  'metadata path': r => { r.metadata.path = '/private'; }, 'wrong scope': r => { r.metadata.scope = 'public URL'; },
  'whole-file claim': r => { r.metadata.input_mode = 'whole'; }, 'fill-zero claim': r => { r.metadata.missing_chunks = 'filled'; },
  'normalized values claim': r => { r.metadata.value_semantics = 'normalized'; }, 'oversized source': r => { r.metadata.source_bytes = 8 * 1024 ** 3 + 1; },
  'oversized reads': r => { r.metadata.read_bytes = 32 * 1024 ** 2 + 1; }, 'boolean read count': r => { r.metadata.read_requests = true; },
  'wrong chunk count': r => { r.metadata.loaded_chunks = 2; }, 'wrong decoded byte count': r => { r.metadata.decoded_chunk_bytes = 0; },
  'wrong range': r => { r.metadata.value_range = [0, 735]; }, 'unknown array field': r => { r.array.url = 'https://invalid.example'; },
  'wrong array shape': r => { r.array.shape = [3, 2]; }, 'wrong array dtype': r => { r.array.dtype = '<f4'; },
  'integer fractional value': r => { r.array.values[0] = .5; r.metadata.value_range[0] = .5; },
  'integer negative value': r => { r.array.values[0] = -1; r.metadata.value_range[0] = -1; },
  'integer out of range': r => { r.array.values[0] = 65536; r.metadata.value_range = [715, 65536]; },
  'integer null': r => { r.array.values[0] = null; r.metadata.invalid_values = 1; r.metadata.value_range = [715, 735]; },
  'boolean pixel': r => { r.array.values[0] = true; }, 'out of frame roi': r => { r.selected.roi = [4, 3, 3, 2]; },
  'oversized roi': r => { r.selected.roi = [0, 0, 129, 1]; }, 'out of range plane': r => { r.selected.indices = [2, 1, 2]; },
  'boolean level': r => { r.selected.level = true; }, 'extra tree in image': r => { r.tree = []; },
};
for (const [name, mutate] of Object.entries(mutations)) test(`OME strict helper rejects ${name}`, () => {
  const raw = value('image'); mutate(raw); assert.throws(() => ome.parseOmeZarr(raw, 'image'));
});
test('tree dtype cannot be impersonated by an array and tree cannot carry image pixels', () => {
  const raw = value(); raw.choices.levels[0].dtype = ['<u2']; raw.tree[0].attributes.dtype = ['<u2'];
  assert.throws(() => ome.parseOmeZarr(raw, 'tree'));
  const pixels = value(); pixels.array = value('image').array; assert.throws(() => ome.parseOmeZarr(pixels, 'tree'));
});
test('a structurally valid response cannot be rebound to another level or plane', () => {
  assert.throws(() => ome.parseOmeZarr(value('image'), 'image', { ...selection, level: 0 }));
  assert.throws(() => ome.parseOmeZarr(value('image'), 'image', { ...selection, indices: [0, 1, 2] }));
  assert.throws(() => ome.parseOmeZarr(value('image'), 'image', { ...selection, roi: [0, 0, 3, 2] }));
});
test('component starts tree-only, sends explicit version pin and draws exact original-value grayscale', async t => {
  const view = mount(t, options => value(options.kind)); await view.start();
  assert.equal(view.state.error.value, ''); assert.equal(view.seen.canvases.length, 0);
  assert.deepEqual(view.seen.requests[0].options, { kind: 'tree' });
  await choose(view); await view.state.load('image');
  assert.equal(view.state.error.value, ''); assert.deepEqual(view.seen.requests[1].options, { kind: 'image', version: 'a'.repeat(64), ...selection });
  assert.deepEqual(view.seen.draws[0], rgba); assert.equal(view.state.displayed.value.metadata.value_range[0], 713);
  const canvas = view.seen.canvases[0]; canvas.onmousemove({ clientX: 1, clientY: 1 });
  assert.match(view.state.hover.value, /索引 x=1, y=1；原值=713；坐标 x=-49, y=101/);
  view.stop(); assert.equal(canvas.width, 0); assert.equal(canvas.height, 0); assert.equal(canvas.onmousemove, null); assert.equal(canvas.removed, true);
});
test('invalid selection never reaches a chunk request', async t => {
  const view = mount(t, () => value()); await view.start(); view.state.roi.value = [0, 0, 129, 1]; await view.state.load('image');
  assert.equal(view.seen.requests.length, 1); assert.notEqual(view.state.error.value, ''); assert.equal(view.seen.canvases.length, 0);
});
for (const mismatch of ['version', 'selection', 'layout']) test(`component refuses ${mismatch} mismatch without drawing`, async t => {
  const view = mount(t, options => {
    const raw = value(options.kind);
    if (options.kind === 'image') { if (mismatch === 'version') raw.version = 'c'.repeat(64); if (mismatch === 'selection') raw.selected.indices = [0, 1, 2]; if (mismatch === 'layout') raw.choices.levels[1].translation[4] = -49; }
    return raw;
  });
  await view.start(); await choose(view); await view.state.load('image');
  assert.notEqual(view.state.error.value, ''); assert.equal(view.seen.canvases.length, 0); assert.equal(view.state.displayed.value, undefined);
});
test('same-object catalog polling does not read again but full identities do', async t => {
  const view = mount(t, options => value(options.kind)); await view.start();
  for (let i = 0; i < 3; i++) { view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); }
  assert.equal(view.seen.requests.length, 1);
  view.props.file.metadata.dataset_file_version = 'new-content'; await flush(); assert.equal(view.seen.requests.length, 2);
  view.props.plugin.limits.max_input_bytes--; await flush(); assert.equal(view.seen.requests.length, 3);
  view.props.plugin.capabilities.shared = true; await flush(); assert.equal(view.seen.requests.length, 4);
});
test('plugin disable aborts requests and clears displayed canvas controls and hover', async t => {
  const view = mount(t, options => value(options.kind)); await view.start(); await choose(view); await view.state.load('image');
  const canvas = view.seen.canvases[0]; canvas.onmousemove({ clientX: 1, clientY: 1 }); view.props.plugin.enabled = false; await flush();
  assert.equal(view.seen.requests.length, 2); assert.equal(view.seen.requests[1].signal.aborted, true);
  assert.equal(view.state.displayed.value, undefined); assert.equal(view.state.details.value, undefined); assert.equal(view.state.hover.value, '');
  assert.equal(canvas.width, 0); assert.equal(canvas.removed, true); assert.equal(canvas.onmousemove, null);
});
test('switching level removes stale canvas while waiting for explicit new pixels', async t => {
  const view = mount(t, options => value(options.kind)); await view.start(); await choose(view); await view.state.load('image');
  const canvas = view.seen.canvases[0]; view.state.level.value = 0; await flush();
  assert.equal(view.seen.requests.length, 2); assert.equal(view.state.displayed.value, undefined);
  assert.equal(canvas.width, 0); assert.equal(canvas.removed, true); assert.deepEqual(view.state.indices.value, [0, 0, 0]);
});
test('unmounting while image waits aborts and discards a late response', async t => {
  const late = pending(), view = mount(t, options => options.kind === 'tree' ? value() : late.promise); await view.start(); await choose(view);
  const loading = view.state.load('image'); await flush(); view.stop(); late.resolve(value('image')); await loading;
  assert.equal(view.seen.requests[1].signal.aborted, true); assert.equal(view.seen.canvases.length, 0); assert.equal(view.state.displayed.value, undefined);
});
test('file switch ignores a late old tree and retains newly inspected structure', async t => {
  const late = pending(); let calls = 0;
  const view = mount(t, () => ++calls === 1 ? late.promise : value()); await view.start();
  view.props.file.file_id = 'new-file'; await flush(); assert.equal(view.seen.requests[0].signal.aborted, true);
  const old = value(); old.choices.levels[0].translation[4] = -500; late.resolve(old); await flush();
  assert.equal(view.state.details.value.levels[0].translation[4], -50); assert.equal(view.state.error.value, '');
});
test('explicit structure reload releases previous canvas and pixel labels', async t => {
  const view = mount(t, options => value(options.kind)); await view.start(); await choose(view); await view.state.load('image');
  const canvas = view.seen.canvases[0]; await view.state.load('tree');
  assert.equal(view.seen.requests.length, 3); assert.equal(view.state.displayed.value, undefined); assert.equal(canvas.width, 0); assert.equal(canvas.removed, true);
});
test('OME template remains inert and uses no public URL or browser storage', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/OmeZarrPreview.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|https?:\/\//);
  assert.match(source, /filePreviewIdentity/); assert.match(source, /pluginPreviewIdentity/); assert.match(source, /onDispose/);
});
