import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/fcsWindowData.ts';
const generated = JSON.parse(readFileSync(new URL('./browser/domain-expansion-fcs-data.json', import.meta.url), 'utf8'));
const version = '1'.repeat(64), copy = value => structuredClone(value);
function publicResult(raw) { const { contract_version, type, reader, kind, metadata, warnings, sampled, ...payload } = copy(raw); return { contract_version, plugin_id: 'viz-fcs-window', kind, version, revision: '2'.repeat(64), payload: { ...payload, view_kind: kind }, metadata, warnings, sampled }; }
const fixture = (name = 'scatter', format = 'float') => publicResult(generated.cases[format][name]);
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function requestFixture(options, format = 'float') {
  if (options.kind === 'tree') return fixture('tree', format);
  const r = fixture(options.view, format);
  r.payload.selected = { view: options.view, channels: options.channels, event_offset: options.event_offset, event_count: options.event_count, ...(options.view === 'histogram' ? { bins: options.bins } : {}) };
  const all = fixture('scatter', format).payload.series;
  r.payload.series = options.channels.map(id => { const t = copy(all[id]); t.x = t.x.slice(options.event_offset, options.event_offset + options.event_count); t.y = t.y.slice(options.event_offset, options.event_offset + options.event_count); return t; });
  r.metadata.scanned_event_bytes = options.event_count * r.metadata.event_bytes; r.metadata.decoded_values = options.event_count * options.channels.length;
  r.metadata.decoded_bytes = r.metadata.decoded_values * r.payload.choices.channels[0].bits / 8;
  r.metadata.nonfinite_values = r.payload.series.flatMap(t => t.y).filter(v => v === null).length;
  r.metadata.plottable_events = r.payload.series[0].y.filter((_, i) => r.payload.series.every(t => t.y[i] !== null)).length;
  r.sampled = options.event_count < r.metadata.total_events;
  return r;
}
for (const format of ['float', 'integer', 'double']) test(`real ${format} inert metadata and raw event values`, () => {
  const t = fixture('tree', format), tree = data.parseFcsWindow('tree', t.payload, t.metadata); assert.equal(tree.traces.length, 0); assert.equal(tree.scannedBytes, 0);
  for (const name of ['scatter', 'histogram', 'window']) { const r = fixture(name, format), parsed = data.parseFcsWindow('series', r.payload, r.metadata, r.payload.selected); assert.deepEqual(parsed.traces.map(t => t.y), r.payload.series.map(t => t.y)); }
});
test('actual >1 GiB logical FCS source uses bounded row IO', () => { const r = fixture('window', 'large'), p = data.parseFcsWindow('series', r.payload, r.metadata); assert.ok(p.sourceBytes > 1024 ** 3); assert.ok(p.readBytes < 1024); assert.equal(p.scannedBytes, 16); assert.equal(p.reads, 4); });
test('int32 unsigned max preserved exactly, calibration gain and spillover not applied', () => {
  const r = fixture('scatter', 'integer'); assert.equal(data.parseFcsWindow('series', r.payload, r.metadata).traces[0].y[1], 4294967295);
  const f = fixture(); const parsed = data.parseFcsWindow('series', f.payload, f.metadata); assert.deepEqual(parsed.traces[0].y, [1.25, -2.5, 0, 4.75]); assert.equal(parsed.channels[0].calibration.unit, 'MESF');
});
const mutations = [
  ['path leak', r => { r.metadata.path = '/private/source'; }], ['wrong view', r => { r.payload.view_kind = 'tree'; }], ['new format', r => { r.metadata.fcs_version = '3.2'; }],
  ['native byteorder', r => { r.metadata.byte_order = 'native'; }], ['ASCII datatype', r => { r.metadata.datatype = 'A'; }], ['bool source', r => { r.metadata.source_bytes = true; }],
  ['oversize source', r => { r.metadata.source_bytes = 8589934593; }], ['read budget', r => { r.metadata.read_bytes = 8388609; }], ['read count', r => { r.metadata.read_requests = 129; }],
  ['text budget', r => { r.metadata.text_bytes = 262145; }], ['event stride', r => { r.metadata.event_bytes = 4; }], ['source length', r => { r.metadata.total_events = 9000000; }],
  ['decode values', r => { r.metadata.decoded_values = 16385; }], ['decode bytes', r => { r.metadata.decoded_bytes = 131073; }], ['nonfinite count', r => { r.metadata.nonfinite_values = 1; }],
  ['plottable count', r => { r.metadata.plottable_events = 1; }], ['timestep', r => { r.metadata.timestep = -1; }], ['raw semantic', r => { r.metadata.value_semantics = 'scaled'; }],
  ['budget expansion', r => { r.metadata.limits.max_events = 9999; }], ['unsafe name', r => { r.payload.choices.channels[0].name = '<img>'; }],
  ['source-name', r => { r.payload.choices.channels[0].name = '/Users/private'; }], ['bidi name', r => { r.payload.choices.channels[0].name = 'A\u202eB'; }],
  ['missing channel', r => { r.payload.choices.channels.pop(); }], ['bool channel', r => { r.payload.choices.channels[0].id = true; }], ['mixed bits', r => { r.payload.choices.channels[0].bits = 64; }],
  ['invalid exponent', r => { r.payload.choices.channels[0].exponent = [4, 0]; }], ['float logarithmic data', r => { r.payload.choices.channels[0].exponent = [4, 1]; }],
  ['invalid gain', r => { r.payload.choices.channels[0].gain = 0; }], ['invalid calibration unit', r => { r.payload.choices.channels[0].calibration.unit = '<img>'; }],
  ['unknown display', r => { r.payload.choices.channels[1].display.scale = 'Logicle'; }], ['missing compensation', r => { r.metadata.compensation.spillover = null; }],
  ['bad matrix', r => { r.metadata.compensation.spillover.matrix[0].pop(); }], ['other matrix channel', r => { r.metadata.compensation.spillover.channels[0] = 2; }],
  ['wrong trace channel', r => { r.payload.series[0].channel = 1; }], ['wrong event index', r => { r.payload.series[0].x[0] = 1; }], ['extra trace', r => { r.payload.series.push(r.payload.series[0]); }],
  ['NaN not null', r => { r.payload.series[0].y[0] = NaN; }], ['float32 overflow', r => { r.payload.series[0].y[0] = 1e100; }], ['boolean value', r => { r.payload.series[0].y[0] = true; }],
  ['option extra', r => { r.payload.selected.compensate = true; }], ['duplicate selected channel', r => { r.payload.selected.channels = [0, 0]; }],
];
for (const [name, mutate] of mutations) test(`strict FCS parser rejects ${name}`, () => { const r = fixture(); mutate(r); assert.throws(() => data.parseFcsWindow('series', r.payload, r.metadata)); });
test('integer no-mask subset and exact integer bounds', () => {
  for (const mutate of [r => { r.payload.choices.channels[0].range = 1024; }, r => { r.payload.series[0].y[0] = -1; }, r => { r.payload.series[0].y[0] = 4294967296; }, r => { r.payload.series[0].y[0] = null; }]) { const r = fixture('scatter', 'integer'); mutate(r); assert.throws(() => data.parseFcsWindow('series', r.payload, r.metadata)); }
});
test('histogram exact bins preserve negatives, count boundary max once, ignore null', () => {
  const h = data.fcsHistogram([-2, -1, 0, 1, 2, null], 4); assert.deepEqual(h.edges, [-2, -1, 0, 1, 2]); assert.deepEqual(h.counts, [1, 1, 1, 2]); assert.equal(h.missing, 1);
  assert.deepEqual(data.fcsHistogram([7, 7, null], 32), { centers: [7], counts: [2], edges: [7, 7], missing: 1, constant: true });
  assert.deepEqual(data.fcsHistogram([null], 4).counts, []);
});
test('histogram extreme finite values cannot overflow intermediate edges or counts', () => {
  const h = data.fcsHistogram([-1e308, 0, 1e308], 2); assert.ok(h.edges.every(Number.isFinite)); assert.deepEqual(h.counts, [1, 2]);
  assert.throws(() => data.fcsHistogram([1, 1 + Number.EPSILON], 128));
});
for (const [values, bins] of [[[1], 0], [[1], 129], [[Infinity], 4], [[], 4], [Array(8193).fill(1), 4]]) test(`invalid histogram ${values.length}/${bins}`, () => assert.throws(() => data.fcsHistogram(values, bins)));
test('catalog comparison ignores key order, but not semantics, compensation or datatype', () => {
  const r = fixture('tree'), a = data.parseFcsWindow('tree', r.payload, r.metadata), b = copy(a); b.channels[0] = Object.fromEntries(Object.entries(b.channels[0]).reverse()); assert.equal(data.fcsCatalogMatches(a, b), true);
  b.channels[0].gain = 10; assert.equal(data.fcsCatalogMatches(a, b), false);
});
function mount(t, request = options => requestFixture(options), library = async () => ({})) {
  const requests = [], modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './fcsWindowData': data,
    './scientific/browserLibraries': { loadBrowserLibrary: library }, '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { requests.push({ file: copy(JSON.parse(JSON.stringify(file))), options: copy(options), signal }); return request(options, signal); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/FcsWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'fcs-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: 'data.fcs', size: fixture('tree').metadata.source_bytes, metadata: { dataset_file_version: 'a' } },
    plugin: { id: 'viz-fcs-window', version: '1', enabled: true, capabilities: { operations: ['preview'], input_mode: 'window', shared: false }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); const stop = () => scope.stop(); t.after(stop); return { state, props, requests, stop };
}
test('actual component initially metadata only; explicit scatter then histogram pins version', async t => {
  const view = mount(t); await flush(); assert.equal(view.state.error.value, ''); assert.equal(view.requests.length, 1); assert.equal(view.state.data.value, undefined);
  await view.state.loadWindow(); assert.equal(view.state.error.value, ''); assert.equal(view.requests[1].options.version, version); assert.deepEqual(view.state.data.value.traces[0].y, [1.25, -2.5, 0, 4.75]);
  view.state.view.value = 'histogram'; view.state.bins.value = 4; assert.equal(view.requests.length, 2); assert.equal(view.state.data.value, undefined);
  await view.state.loadWindow(); assert.equal(view.state.error.value, ''); assert.deepEqual(view.state.histogram.value.counts, [1, 1, 1, 1]); assert.equal(view.requests[2].options.version, version);
});
test('event offset and reversed channel order preserve request semantics', async t => {
  const view = mount(t); await flush(); view.state.xChannel.value = 1; view.state.yChannel.value = 0; view.state.eventOffset.value = 1; view.state.eventCount.value = 2;
  await view.state.loadWindow(); assert.equal(view.state.error.value, ''); assert.deepEqual(view.state.data.value.traces[0].y, [20, 30]); assert.deepEqual(view.state.data.value.traces[1].y, [-2.5, 0]);
});
test('same identity clones never reread; disabling clears catalog immediately', async t => {
  const view = mount(t); await flush(); view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); assert.equal(view.requests.length, 1);
  view.props.plugin.enabled = false; assert.equal(view.state.catalog.value, undefined); assert.equal(view.requests.length, 1);
});
for (const action of ['disable', 'unmount', 'file', 'channel', 'offset', 'view', 'bins']) test(`late events cannot publish after ${action} before nextTick`, async t => {
  const task = pending(), view = mount(t, options => options.kind === 'tree' ? fixture('tree') : task.promise); await flush(); const running = view.state.loadWindow(); await flush();
  if (action === 'disable') view.props.plugin.enabled = false; else if (action === 'unmount') view.stop(); else if (action === 'file') view.props.file.file_id = 'file-b';
  else if (action === 'channel') view.state.xChannel.value = 1; else if (action === 'offset') view.state.eventOffset.value = 1; else if (action === 'view') view.state.view.value = 'histogram'; else view.state.bins.value = 8;
  task.resolve(fixture()); await running; assert.equal(view.requests[1].signal.aborted, true); assert.equal(view.state.data.value, undefined);
});
test('late directory cannot publish after disable', async t => { const task = pending(), view = mount(t, () => task.promise); await flush(); view.props.plugin.enabled = false; task.resolve(fixture('tree')); await flush(); assert.equal(view.state.catalog.value, undefined); });
test('late Plotly library cannot revive closed plot', async t => {
  const task = pending(), view = mount(t, options => requestFixture(options), () => task.promise); await flush(); const running = view.state.loadWindow(); await flush(); view.props.plugin.enabled = false; task.resolve({}); await running; assert.equal(view.state.data.value, undefined); assert.equal(view.state.error.value, '');
});
for (const mode of ['version', 'selection', 'source', 'datatype', 'endian', 'calibration', 'matrix']) test(`actual component rejects unbound ${mode}`, async t => {
  const view = mount(t, options => { const r = requestFixture(options); if (options.kind === 'tree') { if (mode === 'source') r.metadata.source_bytes++; }
    else { if (mode === 'version') r.version = '3'.repeat(64); if (mode === 'selection') r.payload.selected.channels.reverse(); if (mode === 'datatype') r.metadata.datatype = 'I'; if (mode === 'endian') r.metadata.byte_order = 'big'; if (mode === 'calibration') r.payload.choices.channels[0].calibration.factor = 10; if (mode === 'matrix') r.metadata.compensation.spillover.matrix[0][1] = .9; } return r; });
  await flush(); await view.state.loadWindow(); assert.equal(view.state.data.value, undefined); assert.notEqual(view.state.error.value, '');
});
test('invalid window or duplicate scatter axis rejected before API', async t => { const view = mount(t); await flush(); view.state.eventCount.value = 8193; await view.state.loadWindow(); assert.equal(view.requests.length, 1); view.state.eventCount.value = 4; view.state.yChannel.value = 0; await view.state.loadWindow(); assert.equal(view.requests.length, 1); });
