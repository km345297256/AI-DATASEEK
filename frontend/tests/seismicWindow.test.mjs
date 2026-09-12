import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/seismicWindowData.ts';

const generated = JSON.parse(readFileSync(new URL('./browser/domain-expansion-seismic-data.json', import.meta.url), 'utf8'));
const version = '1'.repeat(64), copy = value => structuredClone(value);
function publicResult(privateResult) {
  const { contract_version, type, reader, kind, metadata, warnings, sampled, ...payload } = copy(privateResult);
  return { contract_version, plugin_id: 'viz-seismic-window', kind, version, revision: '2'.repeat(64), payload: { ...payload, view_kind: kind }, metadata, warnings, sampled };
}
const fixture = (name = 'full', format = 'mseed') => publicResult(generated.cases[format][name]);
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function requestFixture(options, format = 'mseed') {
  if (options.kind === 'tree') return fixture(options.record_offset ? 'page' : 'tree', format);
  if (format === 'paged') return fixture('window', format);
  const r = fixture('full', format), original = copy(r.payload.series[0]), start = options.start_sample, count = options.sample_count;
  r.payload.selected = { record: options.record, start_sample: start, sample_count: count };
  r.payload.series[0].x = original.x.slice(start, start + count); r.payload.series[0].y = original.y.slice(start, start + count);
  if (!r.payload.choices.records[0].encoding.startsWith('STEIM')) { r.metadata.decoded_samples = count; r.metadata.decoded_bytes = count * 4; }
  r.sampled = start !== 0 || count !== original.y.length;
  return r;
}

for (const format of ['mseed', 'sac', 'paged']) test(`real ${format} metadata and exact sample window`, () => {
  const raw = fixture('tree', format), parsed = data.parseSeismicWindow('tree', raw.payload, raw.metadata);
  assert.equal(parsed.y, null); assert.equal(parsed.decodedSamples, 0);
  const r = fixture('window', format), output = data.parseSeismicWindow('series', r.payload, r.metadata, r.payload.selected);
  assert.deepEqual(output.x, r.payload.series[0].x); assert.deepEqual(output.y, r.payload.series[0].y);
  if (format === 'sac') { assert.equal(output.records[0].declared_scale, 2); assert.deepEqual(output.y, [-2.5, 0]); assert.equal(output.records[0].unit, 'nm/s'); }
  if (format === 'paged') { assert.equal(parsed.records[2].relation, 'gap'); assert.equal(parsed.records[2].gap_seconds, 1); assert.equal(parsed.catalogComplete, false); }
});
test('actual 1.2 GiB logical SAC source only reads 648 bytes', () => {
  const r = fixture('window', 'large_sac'), parsed = data.parseSeismicWindow('series', r.payload, r.metadata);
  assert.equal(parsed.sourceBytes, 1200000632); assert.equal(parsed.readBytes, 648); assert.equal(parsed.reads, 2); assert.equal(parsed.decodedBytes, 16);
});
test('page and selection identity ignores object property order', () => {
  const page = fixture('page', 'paged'); assert.doesNotThrow(() => data.parseSeismicWindow('tree', page.payload, page.metadata, { record_limit: 16, record_offset: 16 }));
  const r = fixture(); assert.doesNotThrow(() => data.parseSeismicWindow('series', r.payload, r.metadata, { sample_count: 8, start_sample: 0, record: 0 }));
});
const mutations = [
  ['path leak', r => { r.metadata.source_path = '/private/secret'; }], ['wrong kind', r => { r.payload.view_kind = 'tree'; }],
  ['unsafe label', r => { r.payload.choices.records[0].label = '<script>'; }], ['boolean record', r => { r.payload.choices.records[0].id = true; }],
  ['bool source', r => { r.metadata.source_bytes = true; }], ['oversize source', r => { r.metadata.source_bytes = 8589934593; }],
  ['read byte limit', r => { r.metadata.read_bytes = 8388609; }], ['read count limit', r => { r.metadata.read_requests = 129; }],
  ['decode sample limit', r => { r.metadata.decoded_samples = 65536; }], ['decode byte limit', r => { r.metadata.decoded_bytes = 524281; }],
  ['record capacity', r => { r.metadata.record_bytes = 511; }], ['record slot count', r => { r.metadata.record_slots = 2; }],
  ['NaN sampling', r => { r.payload.choices.records[0].sample_interval = NaN; }], ['too fine sampling', r => { r.payload.choices.records[0].sample_interval = 1e-8; }],
  ['unit guessed', r => { r.payload.choices.records[0].unit = 'nm'; }], ['calibrated values claim', r => { r.metadata.value_semantics = 'physical'; }],
  ['scale applied', r => { r.payload.choices.records[0].declared_scale = 2; }], ['invalid date', r => { r.payload.choices.records[0].start_time = '2024-02-30T00:00:00.000000Z'; }],
  ['time zone unknown', r => { r.payload.choices.records[0].start_time = '2024-02-29T00:00:00.000000+08:00'; }], ['undefined MiniSEED start', r => { r.payload.choices.records[0].start_time = null; }],
  ['timing quality', r => { r.payload.choices.records[0].timing_quality = 101; }], ['quality flags', r => { r.payload.choices.records[0].quality_flags = 256; }],
  ['merged relation', r => { r.payload.choices.records[0].relation = 'merged'; }], ['false continuity', r => { r.payload.choices.records[0].relation = 'continuous'; }],
  ['series other channel', r => { r.payload.series[0].record = 1; }], ['series other interval', r => { r.payload.series[0].x[1] = .25; }],
  ['integer overflow', r => { r.payload.series[0].y[0] = 2147483648; }], ['lossy integer', r => { r.payload.series[0].y[0] = .5; }],
  ['nonfinite', r => { r.payload.series[0].y[0] = Infinity; }], ['integer missing', r => { r.payload.series[0].y[0] = null; }],
  ['points mismatch', r => { r.payload.series[0].y.pop(); }], ['hidden read counter', r => { r.metadata.nonfinite_values = 1; }],
  ['expanded budget', r => { r.metadata.limits.max_samples = 16385; }], ['unknown option', r => { r.payload.selected.merge = true; }],
];
for (const [name, mutate] of mutations) test(`strict seismic schema rejects ${name}`, () => { const r = fixture(); mutate(r); assert.throws(() => data.parseSeismicWindow('series', r.payload, r.metadata)); });
for (const options of [{}, { record: true, start_sample: 0, sample_count: 1 }, { record: 0, start_sample: -1, sample_count: 1 },
  { record: 0, start_sample: 0, sample_count: 16385 }, { record: 0, start_sample: 0, sample_count: 1, filter: 'lowpass' }, { record: 33554432, start_sample: 0, sample_count: 1 }]) {
  test(`invalid waveform selection ${JSON.stringify(options)}`, () => assert.throws(() => data.validateSeismicSelection(options)));
}
for (const options of [null, { record_offset: 0 }, { record_offset: 0, record_limit: 17 }, { record_offset: -1, record_limit: 1 }, { record_offset: true, record_limit: 1 }]) {
  test(`invalid directory selection ${JSON.stringify(options)}`, () => assert.throws(() => data.validateSeismicPage(options)));
}

function mount(t, request, format = 'mseed') {
  const requests = []; request ??= options => requestFixture(options, format);
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './seismicWindowData': data,
    './NumericSeriesPlot.vue': { default: {} }, '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => {
      requests.push({ file: copy(JSON.parse(JSON.stringify(file))), options: copy(options), signal }); return request(options, signal);
    } } };
  const source = readFileSync(new URL('../src/visualizations/extended/SeismicWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'seismic-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: `data.${format === 'paged' ? 'miniseed' : format}`, size: fixture('tree', format).metadata.source_bytes, metadata: { dataset_file_version: 'a' } },
    plugin: { id: 'viz-seismic-window', version: '1', enabled: true, capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  const stop = () => scope.stop(); t.after(stop); return { state, props, requests, stop };
}
test('actual component metadata only and explicit selection pins version', async t => {
  const view = mount(t); await flush(); assert.equal(view.state.error.value, ''); assert.equal(view.requests.length, 1); assert.equal(view.state.data.value, undefined);
  view.state.startSample.value = 1; view.state.sampleCount.value = 2; assert.equal(view.requests.length, 1);
  await view.state.loadWindow(); assert.equal(view.state.error.value, ''); assert.equal(view.requests[1].options.version, version); assert.deepEqual(view.state.data.value.y, [-2, 3]);
  assert.deepEqual(view.state.traces.value[0].x, [.05, .1]);
});
test('actual component paged directory version and per-record window', async t => {
  const view = mount(t, undefined, 'paged'); await flush(); assert.equal(view.state.catalog.value.records[2].relation, 'gap');
  await view.state.page(1); assert.equal(view.state.error.value, ''); assert.equal(view.requests.length, 2); assert.equal(view.requests[1].options.version, version);
  assert.equal(view.requests[1].options.record_offset, 16); assert.equal(view.state.recordId.value, 16); assert.equal(view.state.data.value, undefined);
  view.state.startSample.value = 1; view.state.sampleCount.value = 2; await view.state.loadWindow(); assert.deepEqual(view.state.data.value.y, [-2, 3]);
});
test('SAC does not apply SCALE and missing values remain gaps at exact positions', async t => {
  const view = mount(t, options => { const r = requestFixture(options, 'sac'); if (options.kind === 'series') { r.payload.series[0].y[1] = null; r.metadata.nonfinite_values = 1; } return r; }, 'sac');
  await flush(); await view.state.loadWindow(); assert.equal(view.state.error.value, '');
  assert.equal(view.state.traces.value[0].y[0], 1.25); assert.equal(Number.isNaN(view.state.traces.value[0].y[1]), true); assert.equal(view.state.traces.value[0].x[1], .25);
});
test('same-revision clones never reload; disabling removes catalog synchronously', async t => {
  const view = mount(t); await flush(); view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); assert.equal(view.requests.length, 1);
  view.props.file.metadata.dataset_file_version = 'b'; await flush(); assert.equal(view.requests.length, 2);
  view.props.plugin.enabled = false; assert.equal(view.state.catalog.value, undefined); assert.equal(view.requests.length, 2);
});
for (const action of ['disable', 'unmount', 'file', 'selection', 'page']) test(`late series cannot publish after ${action}, even before next tick`, async t => {
  const task = pending(), view = mount(t, options => options.kind === 'tree' ? fixture('tree') : task.promise); await flush();
  const running = view.state.loadWindow(); await flush(); const signal = view.requests[1].signal;
  if (action === 'disable') view.props.plugin.enabled = false; else if (action === 'unmount') view.stop(); else if (action === 'file') view.props.file.file_id = 'file-b';
  else if (action === 'page') view.state.pageLimit.value = 4; else view.state.startSample.value = 1;
  task.resolve(fixture()); await running; assert.equal(signal.aborted, true); assert.equal(view.state.data.value, undefined);
});
test('late inspection cannot publish after disable', async t => {
  const task = pending(), view = mount(t, () => task.promise); await flush(); view.props.plugin.enabled = false; task.resolve(fixture('tree')); await flush();
  assert.equal(view.state.catalog.value, undefined); assert.equal(view.requests[0].signal.aborted, true);
});
test('late directory page cannot publish after page selection changes', async t => {
  const task = pending(), view = mount(t, options => options.record_offset ? task.promise : fixture('tree', 'paged'), 'paged'); await flush();
  const running = view.state.page(1); await flush(); view.state.pageLimit.value = 4; task.resolve(fixture('page', 'paged')); await running;
  assert.equal(view.requests[1].signal.aborted, true); assert.equal(view.state.catalog.value.catalogOffset, 0);
});
for (const mode of ['version', 'selection', 'record', 'source', 'format']) test(`actual component rejects unbound ${mode}`, async t => {
  const view = mount(t, options => { const r = requestFixture(options);
    if (options.kind === 'tree') { if (mode === 'source') { r.metadata.source_bytes *= 2; r.metadata.record_bytes *= 2; } if (mode === 'format') r.metadata.format = 'miniseed'; }
    else { if (mode === 'version') r.version = '3'.repeat(64); if (mode === 'selection') r.payload.selected.start_sample = 1; if (mode === 'record') { r.payload.choices.records[0].label = 'XX.OTHER.00.BHZ'; r.payload.series[0].label = 'XX.OTHER.00.BHZ'; } }
    return r;
  });
  await flush(); await view.state.loadWindow(); assert.equal(view.state.data.value, undefined); assert.notEqual(view.state.error.value, '');
});
test('invalid selection rejected without API', async t => { const view = mount(t); await flush(); view.state.sampleCount.value = 16385; await view.state.loadWindow(); assert.equal(view.requests.length, 1); });
