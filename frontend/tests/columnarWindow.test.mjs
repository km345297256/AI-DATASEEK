import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as data from '../src/visualizations/extended/columnarWindowData.ts';

const generated = JSON.parse(readFileSync(new URL('./browser/domain-expansion-columnar-data.json', import.meta.url), 'utf8'));
const version = '1'.repeat(64), copy = value => structuredClone(value);
function publicResult(privateResult) {
  const { contract_version, type, reader, kind, metadata, warnings, sampled, ...payload } = copy(privateResult);
  return { contract_version, plugin_id: 'viz-columnar-window', kind, version, revision: '2'.repeat(64), payload: { ...payload, view_kind: kind }, metadata, warnings, sampled };
}
const fixture = (name = 'first', format = 'parquet') => publicResult(generated.cases[format][name]);
const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function requestFixture(options) {
  if (options.kind === 'tree') return fixture('tree');
  const result = fixture(), all = [...fixture().payload.table.rows, ...fixture('next').payload.table.rows];
  const selected = { columns: options.columns, row_offset: options.row_offset, row_limit: options.row_limit };
  result.payload.selected = selected;
  result.payload.table.rows = all.slice(options.row_offset, options.row_offset + options.row_limit).map(row => options.columns.map(c => row[c]));
  result.payload.table.columns = options.columns.map(c => result.payload.choices.columns[c].label);
  result.payload.table.row_offset = options.row_offset;
  result.metadata.scan_rows = 4; result.metadata.groups_read = 2;
  return result;
}

for (const format of ['parquet', 'arrow', 'feather']) test(`real ${format} tree/window preserves precision and range counters`, () => {
  const raw = fixture('tree', format), parsed = data.parseColumnarWindow('tree', raw.payload, raw.metadata);
  assert.equal(parsed.rows, null); assert.equal(parsed.scanRows, 0); assert.ok(parsed.sourceBytes > 64 * 1024 ** 2); assert.ok(parsed.readBytes < 20000);
  for (const name of ['first', 'next', 'column']) {
    const r = fixture(name, format), value = data.parseColumnarWindow('table', r.payload, r.metadata, r.payload.selected);
    assert.deepEqual(value.rows, r.payload.table.rows);
  }
  const r = fixture('first', format); assert.equal(r.payload.table.rows[0][0], '9223372036854775807'); assert.equal(r.payload.table.rows[0][1], '1234567890123456.1200');
});
test('selection equality ignores property order, never projection order', () => {
  assert.equal(data.columnarSelectionsEqual({ columns: [0, 1], row_limit: 2, row_offset: 0 }, { row_offset: 0, columns: [0, 1], row_limit: 2 }), true);
  assert.equal(data.columnarSelectionsEqual({ columns: [0, 1], row_limit: 2, row_offset: 0 }, { row_offset: 0, columns: [1, 0], row_limit: 2 }), false);
});
const mutations = [
  ['extra source path', r => { r.metadata.path = '/private/secret'; }], ['wrong kind', r => { r.payload.view_kind = 'tree'; }],
  ['unsafe label', r => { r.payload.choices.columns[0].label = '<script>'; }], ['duplicate column index', r => { r.payload.choices.columns[1].id = 0; }],
  ['nested type', r => { r.payload.choices.columns[0].type = 'list'; }], ['invalid nullable', r => { r.payload.choices.columns[0].nullable = 1; }],
  ['precision loss number', r => { r.payload.table.rows[0][0] = 9223372036854775807; }], ['int64 overflow', r => { r.payload.table.rows[0][0] = '9223372036854775808'; }],
  ['noncanonical integer', r => { r.payload.table.rows[0][0] = '01'; }], ['decimal scientific notation', r => { r.payload.table.rows[0][1] = '1e2'; }],
  ['decimal wrong scale', r => { r.payload.table.rows[0][1] = '1234567890123456.12'; }], ['decimal overflow', r => { r.payload.choices.columns[1].precision = 19; }],
  ['read bytes', r => { r.metadata.read_bytes = 8388609; }], ['read count', r => { r.metadata.read_requests = 129; }],
  ['decode bytes', r => { r.metadata.decoded_bytes = 16777217; }], ['scan rows', r => { r.metadata.scan_rows = 262145; }],
  ['boolean source size', r => { r.metadata.source_bytes = true; }], ['source oversize', r => { r.metadata.source_bytes = 8589934593; }],
  ['metadata bytes', r => { r.metadata.metadata_bytes = 1048577; }], ['row groups', r => { r.metadata.groups_read = 3; }],
  ['nonfinite counter', r => { r.metadata.nonfinite_values = 1; }], ['output rows', r => { r.payload.table.rows.push(r.payload.table.rows[0]); }],
  ['projection mismatch', r => { r.payload.table.columns.reverse(); }], ['row offset mismatch', r => { r.payload.table.row_offset = 1; }],
  ['column extra key', r => { r.payload.choices.columns[0].url = 'bad'; }], ['large string', r => { r.payload.table.rows[0][2] = 'x'.repeat(2049); }],
  ['unknown options', r => { r.payload.selected.sql = 'SELECT 1'; }], ['wrong semantics', r => { r.metadata.value_semantics = 'sorted'; }],
  ['expanded budget', r => { r.metadata.limits.max_rows = 201; }], ['media URL', r => { r.payload.url = 'bad'; }],
];
for (const [name, mutate] of mutations) test(`parser rejects ${name}`, () => { const r = fixture(); mutate(r); assert.throws(() => data.parseColumnarWindow('table', r.payload, r.metadata)); });
for (const options of [{}, { columns: [], row_offset: 0, row_limit: 1 }, { columns: [true], row_offset: 0, row_limit: 1 },
  { columns: [0, 0], row_offset: 0, row_limit: 1 }, { columns: [128], row_offset: 0, row_limit: 1 }, { columns: [0], row_offset: -1, row_limit: 1 },
  { columns: [0], row_offset: 0, row_limit: 201 }, { columns: Array.from({ length: 33 }, (_, i) => i), row_offset: 0, row_limit: 1 }]) {
  test(`invalid columnar request ${JSON.stringify(options)}`, () => assert.throws(() => data.validateColumnarSelection(options)));
}
function mount(t, request = requestFixture) {
  const requests = [];
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './columnarWindowData': data,
    '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { requests.push({ file: JSON.parse(JSON.stringify(file)), options: JSON.parse(JSON.stringify(options)), signal }); return request(options, signal); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/ColumnarWindowPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'columnar-window' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: 'data.parquet', size: fixture('tree').metadata.source_bytes, metadata: { dataset_file_version: 'a' } },
    plugin: { id: 'viz-columnar-window', version: '1', enabled: true, capabilities: { operations: ['preview'], shared: false, input_mode: 'window' }, limits: { max_input_bytes: 8388608, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  const stop = () => scope.stop(); t.after(stop); return { state, props, requests, stop };
}
test('actual component metadata only; explicit projection and pagination pin version', async t => {
  const view = mount(t); await flush(); assert.equal(view.state.error.value, ''); assert.equal(view.requests.length, 1); assert.equal(view.state.data.value, undefined);
  view.state.rowLimit.value = 2; await flush(); assert.equal(view.requests.length, 1);
  await view.state.loadWindow(); assert.equal(view.state.error.value, ''); assert.equal(view.requests[1].options.version, version); assert.equal(view.state.data.value.rows[0][0], '9223372036854775807');
  await view.state.page(1); assert.equal(view.requests.length, 3); assert.equal(view.requests[2].options.row_offset, 2); assert.equal(view.state.data.value.rows[1][0], '42');
  view.state.selectedColumns.value = [1]; view.state.rowOffset.value = 0; await flush(); assert.equal(view.state.data.value, undefined); assert.equal(view.requests.length, 3);
  await view.state.loadWindow(); assert.deepEqual(view.state.data.value.rows, [['1234567890123456.1200'], ['-0.0100']]);
});
test('same-revision clones never reload; source changes and permission disable do', async t => {
  const view = mount(t); await flush();
  view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); assert.equal(view.requests.length, 1);
  view.props.file.metadata.dataset_file_version = 'new'; await flush(); assert.equal(view.requests.length, 2);
  view.props.plugin.enabled = false; assert.equal(view.state.catalog.value, undefined); assert.equal(view.requests.length, 2);
});
for (const action of ['disable', 'unmount', 'file', 'selection']) test(`late table cannot publish after ${action}, even before next tick`, async t => {
  const task = pending(), view = mount(t, options => options.kind === 'tree' ? fixture('tree') : task.promise); await flush(); view.state.rowLimit.value = 2;
  const running = view.state.loadWindow(); await flush(); const signal = view.requests[1].signal;
  if (action === 'disable') view.props.plugin.enabled = false;
  else if (action === 'unmount') view.stop(); else if (action === 'file') view.props.file.file_id = 'file-b'; else view.state.rowOffset.value = 2;
  task.resolve(fixture()); await running; assert.equal(signal.aborted, true); assert.equal(view.state.data.value, undefined);
});
test('late initial inspection after disable cannot publish catalog', async t => {
  const task = pending(), view = mount(t, () => task.promise); await flush(); view.props.plugin.enabled = false; task.resolve(fixture('tree')); await flush();
  assert.equal(view.state.catalog.value, undefined); assert.equal(view.requests[0].signal.aborted, true);
});
for (const mode of ['version', 'selection', 'columns', 'source', 'format']) test(`actual component rejects unbound ${mode}`, async t => {
  const view = mount(t, options => { const r = requestFixture(options);
    if (options.kind === 'tree') { if (mode === 'source') r.metadata.source_bytes++; if (mode === 'format') { r.metadata.format = 'arrow'; r.metadata.container = 'Arrow IPC file'; } }
    else { if (mode === 'version') r.version = '3'.repeat(64); if (mode === 'selection') r.payload.selected.columns.reverse(); if (mode === 'columns') r.payload.choices.columns[0].type = 'uint64'; }
    return r;
  });
  await flush(); view.state.rowLimit.value = 2; await view.state.loadWindow(); assert.equal(view.state.data.value, undefined); assert.notEqual(view.state.error.value, '');
});
test('invalid window rejected before API and final page does not wrap', async t => {
  const view = mount(t); await flush(); view.state.rowOffset.value = 4; await view.state.loadWindow(); assert.equal(view.requests.length, 1);
  view.state.rowOffset.value = 2; view.state.rowLimit.value = 200; await view.state.loadWindow(); assert.equal(view.state.data.value.rows.length, 2); await view.state.page(1); assert.equal(view.requests.length, 2);
});
