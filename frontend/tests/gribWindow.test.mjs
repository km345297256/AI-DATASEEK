import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/gribWindowData.ts';

const fixture = JSON.parse(readFileSync(new URL('./browser/grib-window-data.json', import.meta.url), 'utf8'));
const version = 'a'.repeat(64);
function envelope(kind = 'tree') {
  const { contract_version, type: _type, reader: _reader, metadata, warnings, sampled, kind: view_kind, ...payload } = structuredClone(fixture[kind]);
  return { contract_version, kind: kind === 'tree' ? 'tree' : 'array', version, revision: 'b'.repeat(64), plugin_id: 'viz-grib-window', payload: { ...payload, view_kind }, metadata, warnings, sampled };
}
const selection = () => structuredClone(fixture.image.selected);
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; };

test('actual ecCodes payload keeps units, missing bitmap and independently ordered geographic axes', () => {
  const r = envelope('image'), value = data.parseGribWindow('image', r.payload, r.metadata, selection());
  assert.deepEqual(value.array.values, [281, 282, 283, null, 287, 288, 289, 290, 293, 294, 295, 296]);
  assert.deepEqual(value.axes.map(v => v.values), [[50, 49, 48], [11, 12, 13, 14]]);
  assert.equal(value.messages[0].unit, 'K'); assert.equal(value.decoded, 24); assert.equal(value.missing, 1);
});
for (const format of ['grib', 'grb', 'grib2', 'grb2']) test(`registered extension ${format}`, () => {
  const r = envelope(); r.metadata.format = format; assert.equal(data.parseGribWindow('tree', r.payload, r.metadata).messages.length, 2);
});
const changes = {
  'unknown payload': r => { r.payload.url = 'https://invalid'; }, 'html': r => { r.payload.media_type = 'text/html'; },
  'wrong view': r => { r.payload.view_kind = 'map'; }, 'extra meta': r => { r.metadata.path = '/private'; },
  'source overflow': r => { r.metadata.source_bytes = 2 ** 33 + 1; }, 'read overflow': r => { r.metadata.read_bytes = 8388609; },
  'reads overflow': r => { r.metadata.read_requests = 129; }, 'decoder': r => { r.metadata.decoder = 'other'; },
  'packing': r => { r.metadata.packing = 'grid_complex'; }, 'edition bool': r => { r.metadata.edition = true; },
  'grid': r => { r.metadata.grid_type = 'rotated_ll'; }, 'metadata array': r => { r.metadata = []; },
  'decoded lie': r => { r.metadata.decoded_points = 12; }, 'output lie': r => { r.metadata.output_values = 0; },
  'missing lie': r => { r.metadata.missing_values = 0; }, 'next offset lie': r => { r.metadata.next_offset = null; },
  'page lie': r => { r.metadata.page_offset = 1; }, 'message count': r => { r.metadata.scanned_messages = 9; },
  'id path': r => { r.payload.choices.messages[0].id = '/private'; }, 'huge id': r => { r.payload.choices.messages[0].id = 'g-ffffffffffffffff'; },
  'duplicate': r => { r.payload.choices.messages.push(r.payload.choices.messages[0]); }, 'label': r => { r.payload.choices.messages[0].label = '<script>'; },
  'unit': r => { r.payload.choices.messages[0].unit = '/Users/private'; }, 'date': r => { r.payload.choices.messages[0].reference_time = '2026-02-30T00:00:00Z'; },
  'shape': r => { r.payload.choices.messages[0].shape = [16384, 16384]; }, 'shape bool': r => { r.payload.choices.messages[0].shape[0] = true; },
  'scan': r => { r.payload.choices.messages[0].scanning_mode = 16; }, 'wrong step': r => { r.payload.choices.messages[0].step[0] = -1; },
  'zero step': r => { r.payload.choices.messages[0].step[0] = 0; }, 'span': r => { r.payload.choices.messages[0].step[0] = 100; },
  'latitude': r => { r.payload.choices.messages[0].first[1] = 99; }, 'longitude': r => { r.payload.choices.messages[0].first[0] = 360; },
  'bits': r => { r.payload.choices.messages[0].packing_bits = 33; }, 'bitmap bool': r => { r.payload.choices.messages[0].bitmap = 'true'; },
  'missing count': r => { r.payload.choices.messages[0].missing_count = 0; }, 'source length': r => { r.payload.choices.messages[0].byte_length = 2 ** 20 + 1; },
  'axis swap': r => { r.payload.axes.reverse(); }, 'axis units': r => { r.payload.axes[0].unit = 'radians'; },
  'axis order': r => { r.payload.axes[0].values.sort(); }, 'axis bool': r => { r.payload.axes[0].values[0] = true; },
  'shape transposed': r => { r.payload.array.shape = [4, 3]; }, 'dimensions': r => { r.payload.array.dimensions = ['x', 'y']; },
  'values short': r => { r.payload.array.values.pop(); }, 'values bool': r => { r.payload.array.values[0] = true; },
  'values NaN': r => { r.payload.array.values[0] = NaN; }, 'selected wrong': r => { r.payload.selected.roi[0] = 0; },
};
for (const [name, change] of Object.entries(changes)) test(`strict helper rejects ${name}`, () => {
  const r = envelope('image'); change(r); assert.throws(() => data.parseGribWindow('image', r.payload, r.metadata, selection()));
});
test('selection copied and property ordering does not grant a different ROI', () => {
  const input = { roi: [1, 0, 4, 3], message: fixture.image.selected.message };
  const output = data.validateGribSelection(input); input.roi[0] = 0; assert.deepEqual(output, selection());
});

function mount(t, request = options => envelope(options.kind), render) {
  const seen = { requests: [], plots: [], purged: [], removed: [] }, previous = globalThis.document;
  globalThis.document = { createElement() { const element = { style: {}, dataset: {}, remove() { seen.removed.push(element); } }; return element; } };
  t.after(() => { globalThis.document = previous; });
  const library = { newPlot: async (element, traces, layout) => { seen.plots.push({ element, traces, layout }); await render?.(); }, purge: element => seen.purged.push(element), Plots: { resize() {} } };
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity,
    '../runtime': { requestVisualization: async (_file, _plugin, _op, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } },
    './scientific/browserLibraries': { loadBrowserLibrary: async () => library }, './gribWindowData': data };
  const source = readFileSync(new URL('../src/visualizations/extended/GribWindowPreview.vue', import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'grib' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'opaque-grib', filename: 'synthetic.grib2', size: fixture.tree.metadata.source_bytes, metadata: { dataset_file_version: 'a' } },
    plugin: { id: 'viz-grib-window', version: '1.0.0', enabled: true, capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = { replaceChildren() {} };
  const stop = () => scope.stop(); t.after(stop); return { state, props, seen, stop };
}
async function choose(v) { v.state.selectedId.value = fixture.image.selected.message; await flush(); v.state.roi.value = selection().roi; await flush(); }
test('component opens metadata only then explicitly reads version-pinned field with geographic axes and missing gap', async t => {
  const v = mount(t); await flush(); assert.deepEqual(v.seen.requests[0].options, { kind: 'tree', offset: 0 }); assert.equal(v.seen.plots.length, 0);
  await choose(v); await v.state.loadWindow(); assert.equal(v.state.error.value, '');
  assert.deepEqual(v.seen.requests[1].options, { kind: 'image', version, ...selection() });
  const trace = v.seen.plots[0].traces[0]; assert.deepEqual(trace.x, [11, 12, 13, 14]); assert.deepEqual(trace.y, [50, 49, 48]);
  assert.deepEqual(trace.z[0], [281, 282, 283, null]); assert.equal(trace.colorbar.title.text, 'K'); assert.equal(trace.connectgaps, false); assert.equal(trace.zsmooth, false);
  v.stop(); assert.equal(v.seen.purged.length, 1); assert.equal(v.seen.removed.length, 1);
});
test('same file/plugin polling clones retain the current selection and plot', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadWindow();
  v.props.file = structuredClone(vue.toRaw(v.props.file)); v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(v.seen.plots.length, 1); assert.equal(v.state.selectedId.value, selection().message);
});
for (const change of ['version', 'selected', 'size', 'message', 'sampled']) test(`component rejects mismatched ${change}`, async t => {
  const v = mount(t, options => { const r = envelope(options.kind); if (options.kind === 'image') {
    if (change === 'version') r.version = 'c'.repeat(64); if (change === 'selected') r.payload.selected.roi[0] = 0;
    if (change === 'size') r.metadata.source_bytes++; if (change === 'message') r.payload.choices.messages[0].unit = 'degC'; if (change === 'sampled') r.sampled = true;
  } return r; });
  await flush(); await choose(v); await v.state.loadWindow(); assert.ok(v.state.error.value); assert.equal(v.seen.plots.length, 0);
});
for (const action of ['unmount', 'disable', 'file-change']) test(`late window result cannot remount after ${action}`, async t => {
  const blocked = pending(), v = mount(t, options => options.kind === 'image' ? blocked.promise : envelope()); await flush(); await choose(v);
  const loading = v.state.loadWindow(); await flush(); const request = v.seen.requests.at(-1);
  if (action === 'unmount') v.stop(); else if (action === 'disable') v.props.plugin.enabled = false; else v.props.file.file_id = 'different';
  await flush(); assert.equal(request.signal.aborted, true); blocked.resolve(envelope('image')); await loading;
  assert.equal(v.seen.plots.length, 0); assert.equal(v.state.displayed.value, undefined);
});
test('ROI changes invalidate the plot without starting a new request', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadWindow(); v.state.roi.value[0] = 0; await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(v.seen.purged.length, 1); assert.equal(v.state.displayed.value, undefined);
});
test('invalid ROI and disabled plugin never request a field', async t => {
  const v = mount(t); await flush(); await choose(v); v.state.roi.value = [0, 0, 16384, 2]; await flush(); await v.state.loadWindow(); assert.equal(v.seen.requests.length, 1);
  v.props.plugin.enabled = false; await flush(); assert.equal(v.seen.requests.length, 1);
});
test('explicit reload finishes after clearing existing selected-message watchers', async t => {
  const v = mount(t); await flush(); await choose(v); await v.state.loadWindow(); await v.state.inspect();
  assert.equal(v.seen.requests.length, 3); assert.equal(v.state.error.value, ''); assert.ok(v.state.catalog.value); assert.equal(v.state.selectedId.value, '');
});

test('late asynchronous Plotly completion cannot retain a canvas after unmount', async t => {
  const render = pending(), v = mount(t, options => envelope(options.kind), () => render.promise); await flush(); await choose(v);
  const loading = v.state.loadWindow(); await flush(); assert.equal(v.seen.plots.length, 1);
  v.stop(); render.resolve(); await loading;
  assert.ok(v.seen.purged.length >= 1); assert.ok(v.seen.removed.length >= 1); assert.equal(v.state.displayed.value, undefined);
});

test('actual paginated tree binds offset and version and resets per-page selection', async t => {
  const pages = JSON.parse(readFileSync(new URL('./browser/grib-window-pages.json', import.meta.url), 'utf8'));
  const v = mount(t, options => {
    const r = envelope(), raw = structuredClone(options.offset ? pages.second : pages.first);
    r.metadata = raw.metadata; r.payload.choices = raw.choices; r.payload.selected = raw.selected; r.payload.tree = raw.tree;
    return r;
  });
  v.props.file.size = pages.first.metadata.source_bytes; await flush();
  assert.equal(v.state.catalog.value.messages.length, 8); const before = v.seen.requests.length;
  await v.state.inspect(pages.first.metadata.next_offset);
  assert.deepEqual(v.seen.requests.at(-1).options, { kind: 'tree', version, offset: 1824 });
  assert.equal(v.seen.requests.length, before + 1); assert.equal(v.state.catalog.value.messages.length, 2);
  assert.deepEqual(v.state.history.value, [0, 1824]); assert.equal(v.state.catalog.value.nextOffset, null); assert.equal(v.seen.plots.length, 0);
});

test('a valid empty supported-message list explains a rejected format page without guessing a fallback', async t => {
  const v = mount(t, () => { const r = envelope(); r.payload.choices.messages = []; r.payload.tree = []; r.metadata.skipped_messages = r.metadata.scanned_messages; return r; });
  await flush(); assert.deepEqual(v.state.catalog.value.messages, []); assert.equal(v.state.error.value, ''); assert.equal(v.seen.requests.length, 1);
});
