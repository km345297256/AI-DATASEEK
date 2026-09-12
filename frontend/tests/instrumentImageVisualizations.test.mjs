import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as instrument from '../src/visualizations/extended/instrumentImageData.ts';

const fixtures = JSON.parse(readFileSync(new URL('./browser/instrument-image-data.json', import.meta.url), 'utf8'));
const value = (kind = 'tree', format = 'edf') => ({ ...structuredClone(fixtures[`${format}_${kind}`]), version: 'a'.repeat(64), revision: 'b'.repeat(64) });
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const choose = view => { view.state.frame.value = 1; view.state.roi.value = [1, 1, 2, 2]; };

function mount(t, request, format = 'edf') {
  const seen = { requests: [], urls: [], revoked: [], removed: 0 }, mounted = [];
  const modules = {
    vue: { ...vue, onMounted: callback => mounted.push(callback) },
    '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity,
    './instrumentImageData': instrument, './domains/lifecycle': { displayError: reason => reason.message },
    './runtime': { requestPreview: async (_file, _plugin, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } },
  };
  const previous = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  URL.createObjectURL = blob => { assert.equal(blob.type, 'image/png'); const url = `blob:instrument-${seen.urls.length}`; seen.urls.push(url); return url; };
  URL.revokeObjectURL = url => seen.revoked.push(url);
  t.after(() => { URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke; });
  const source = readFileSync(new URL('../src/visualizations/extended/InstrumentImagePreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'instrument' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const props = vue.reactive({ file: { file_id: 'opaque-file', filename: `synthetic.${format}`, size: fixtures[`${format}_tree`].metadata.source_bytes, metadata: { dataset_file_version: 'initial' } },
    plugin: { contract_version: 2, id: 'viz-instrument-images', version: '1.0.0', name: 'instrument', description: '',
      extensions: ['edf', 'spe'], filenames: [], view_kind: 'image', adapter: 'instrument-image', reader: 'instrument-window',
      capabilities: { operations: ['preview'], input_mode: 'window', shared: false }, default_enabled: true,
      enabled: true, priority: 10, permissions: ['file:read'], limits: { max_input_bytes: 32 * 1024 ** 2, max_output_bytes: 8 * 1024 ** 2 } } });
  const scope = vue.effectScope(), state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  state.imageElement.value = { removeAttribute: () => seen.removed++ };
  const stop = () => scope.stop(); t.after(stop);
  return { props, state, seen, stop, start: async () => { for (const callback of mounted) await callback(); await flush(); } };
}

for (const format of ['edf', 'spe']) for (const kind of ['tree', 'image']) test(`actual ${format} ${kind} reader fixture obeys frontend contract`, () => {
  const data = instrument.parseInstrumentImageData(value(kind, format));
  assert.equal(data.frames, 2); assert.deepEqual(data.shape, [3, 4]); assert.equal(data.kind, kind);
  assert.equal(!!data.png, kind === 'image');
  if (data.png) assert.deepEqual(data.range, [105, 110]);
});

const mutations = {
  'unknown root field': r => { r.path = '/private/secret'; }, 'wrong version': r => { r.version = 'not-pinned'; },
  'wrong reader': r => { r.reader = 'edf'; }, 'wrong envelope type': r => { r.type = ['instrument']; },
  'identity leaked': r => { r.metadata.comment = 'PRIVATE'; }, 'implied calibration': r => { r.metadata.calibration = 'energy'; },
  'whole input': r => { r.metadata.input_mode = 'whole'; }, 'unknown metadata': r => { r.metadata.extra = true; },
  'invalid source': r => { r.metadata.source_bytes = 8 * 1024 ** 3 + 1; }, 'false read counter': r => { r.metadata.read_bytes++; },
  'boolean count': r => { r.metadata.frame_count = true; }, 'read count overflow': r => { r.metadata.read_requests = 2049; },
  'invalid shape': r => { r.metadata.frame_shape = [3, 100001]; }, 'unknown dtype': r => { r.metadata.dtype = '__proto__'; },
  'dtype array': r => { r.metadata.dtype = ['uint16']; }, 'format array': r => { r.metadata.format = ['esrf-edf']; },
  'different pixel bytes': r => { r.metadata.dtype = 'int32'; }, 'header inconsistency': r => { r.metadata.header_bytes = 1023; },
  'bad choices': r => { r.choices.frame_count = 3; }, 'boolean frame': r => { r.selected.frame = true; },
  'out of range frame': r => { r.selected.frame = 2; }, 'fractional ROI': r => { r.selected.roi[0] = .5; },
  'oversized ROI': r => { r.selected.roi[2] = 1025; }, 'out of frame ROI': r => { r.selected.roi[0] = 3; },
  'unsafe warnings': r => { r.warnings = ['<script>']; }, 'sampling mismatch': r => { r.sampled = false; },
  'extra tree': r => { r.tree = []; }, 'range infinity': r => { r.metadata.display_range = [0, Infinity]; },
  'range beyond uint16': r => { r.metadata.display_range = [-1, 65536]; }, 'fractional integer range': r => { r.metadata.display_range = [105.5, 110]; },
  'integer invalid pixels': r => { r.metadata.invalid_pixels = 1; }, 'oversized PNG': r => { r.data_base64 = 'A'.repeat(2 * 1024 ** 2 + 4); },
  'SVG data': r => { r.data_base64 = '<svg onload="alert(1)"/>'; }, 'truncated PNG': r => { r.data_base64 = Buffer.from('PNG').toString('base64'); },
  'PNG CRC': r => { const bytes = Buffer.from(r.data_base64, 'base64'); bytes[bytes.length - 1] ^= 1; r.data_base64 = bytes.toString('base64'); },
  'PNG trailing': r => { r.data_base64 = Buffer.concat([Buffer.from(r.data_base64, 'base64'), Buffer.from('tail')]).toString('base64'); },
};
for (const [name, mutate] of Object.entries(mutations)) test(`strict instrument payload rejects ${name}`, () => { const raw = value('image'); mutate(raw); assert.throws(() => instrument.parseInstrumentImageData(raw)); });

test('tree cannot carry pixels, arbitrary header text or false read accounting', () => {
  for (const mutate of [r => { r.data_base64 = 'AAAA'; }, r => { r.tree[0].attributes.value = 'PRIVATE'; }, r => { r.tree[0].attributes.children_count = false; }, r => { r.metadata.read_requests = 3; }, r => { r.selected.frame = 1; }]) {
    const raw = value(); mutate(raw); assert.throws(() => instrument.parseInstrumentImageData(raw));
  }
});
test('SPE rejects 3.x version, unsupported endian and ambiguous datatype codes', () => {
  for (const mutate of [r => { r.metadata.format_version = 3; }, r => { r.metadata.byte_order = 'big'; }, r => { r.metadata.dtype = 'uint32'; }]) { const raw = value('image', 'spe'); mutate(raw); assert.throws(() => instrument.parseInstrumentImageData(raw)); }
});
test('component starts with metadata only and uses explicit version-pinned frame/ROI action', async t => {
  const view = mount(t, options => value(options.kind)); await view.start();
  assert.equal(view.state.error.value, ''); assert.equal(view.state.imageUrl.value, ''); assert.deepEqual(view.seen.requests[0].options, { kind: 'tree' });
  choose(view); await view.state.load('image');
  assert.equal(view.state.error.value, ''); assert.deepEqual(view.seen.requests[1].options, { kind: 'image', version: 'a'.repeat(64), frame: 1, roi: [1, 1, 2, 2] });
  assert.equal(view.state.imageUrl.value, 'blob:instrument-0'); assert.deepEqual(view.state.displayed.value.range, [105, 110]);
  view.stop(); assert.deepEqual(view.seen.revoked, view.seen.urls); assert.ok(view.seen.removed >= 1);
});
test('invalid ROI is rejected before any pixel request', async t => {
  const view = mount(t, () => value()); await view.start(); view.state.roi.value = [3, 2, 2, 2]; await view.state.load('image');
  assert.equal(view.seen.requests.length, 1); assert.match(view.state.error.value, /越界/);
});
for (const mismatch of ['version', 'frame', 'roi', 'format']) test(`component rejects valid-shaped wrong ${mismatch} result`, async t => {
  const view = mount(t, options => {
    const raw = value(options.kind, mismatch === 'format' && options.kind === 'image' ? 'spe' : 'edf');
    if (options.kind === 'image') { if (mismatch === 'version') raw.version = 'c'.repeat(64); if (mismatch === 'frame') raw.selected.frame = 0; if (mismatch === 'roi') raw.selected.roi = [0, 0, 2, 2]; }
    return raw;
  });
  await view.start(); choose(view); await view.state.load('image'); assert.notEqual(view.state.error.value, ''); assert.equal(view.seen.urls.length, 0); assert.equal(view.state.displayed.value, undefined);
});
test('cancelled late inspection cannot populate controls or create an object URL', async t => {
  const late = pending(), view = mount(t, () => late.promise); await view.start(); view.state.cancel();
  assert.equal(view.seen.requests[0].signal.aborted, true); late.resolve(value()); await flush();
  assert.equal(view.state.details.value, undefined); assert.equal(view.state.busy.value, false); assert.equal(view.seen.urls.length, 0);
});
test('unmount cancels a late image and never publishes stale pixels', async t => {
  const late = pending(), view = mount(t, options => options.kind === 'tree' ? value() : late.promise); await view.start(); choose(view);
  const loading = view.state.load('image'); await flush(); view.stop(); late.resolve(value('image')); await loading;
  assert.equal(view.seen.requests[1].signal.aborted, true); assert.equal(view.seen.urls.length, 0); assert.equal(view.state.displayed.value, undefined);
});
test('stopping plugin cancels pixels, revokes URL and does not read again', async t => {
  const view = mount(t, options => value(options.kind)); await view.start(); choose(view); await view.state.load('image'); view.props.plugin.enabled = false; await flush();
  assert.equal(view.seen.requests.length, 2); assert.equal(view.seen.requests[1].signal.aborted, true);
  assert.equal(view.state.details.value, undefined); assert.equal(view.state.imageUrl.value, ''); assert.deepEqual(view.seen.revoked, view.seen.urls);
});
test('clone refresh is cheap; content and complete plugin descriptor changes reread metadata', async t => {
  const view = mount(t, options => value(options.kind)); await view.start();
  for (let i = 0; i < 3; i++) { view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); }
  assert.equal(view.seen.requests.length, 1);
  view.props.file.metadata.dataset_file_version = 'changed'; await flush(); assert.equal(view.seen.requests.length, 2);
  view.props.plugin.limits.max_input_bytes -= 1; await flush(); assert.equal(view.seen.requests.length, 3);
  view.props.plugin.capabilities.shared = true; await flush(); assert.equal(view.seen.requests.length, 4);
});
test('file switch cancels late old response and preserves the new inspection', async t => {
  const late = pending(); let calls = 0;
  const view = mount(t, () => ++calls === 1 ? late.promise : value()); await view.start(); view.props.file.file_id = 'new-file'; await flush();
  assert.equal(view.seen.requests[0].signal.aborted, true); assert.equal(view.state.details.value.format, 'esrf-edf');
  late.resolve(value('tree', 'spe')); await flush(); assert.equal(view.state.details.value.format, 'esrf-edf'); assert.equal(view.state.error.value, '');
});
test('template uses inert text and Blob-only image URLs without browser storage', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/InstrumentImagePreview.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|https?:\/\//);
  assert.match(source, /filePreviewIdentity/); assert.match(source, /pluginPreviewIdentity/); assert.match(source, /flush: 'sync'/); assert.match(source, /revokeObjectURL/);
});
