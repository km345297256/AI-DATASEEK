import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/diffractionData.ts';
const fixtures = JSON.parse(readFileSync(new URL('./browser/domain-expansion-diffraction-data.json', import.meta.url), 'utf8'));
function result(which = 'sas', kind = 'tree') {
  const { contract_version, type, reader, metadata, warnings, sampled, ...payload } = structuredClone(fixtures[which][kind]); delete payload.kind;
  return { contract_version, kind, plugin_id: 'viz-diffraction', version: 'a'.repeat(64), revision: 'b'.repeat(64), payload: { ...payload, view_kind: kind }, metadata, warnings, sampled };
}
const parseResult = (r, kind = r.kind, options = kind === 'tree' ? {} : { scan: 1 }) => helpers.parseDiffractionData(r, kind, options);
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => resolve = r), resolve: value => resolve(value) }; };
test('real canSAS payload preserves nonuniform q, negative I and declared errors without conversion', () => {
  const value = parseResult(result('sas', 'series'));
  assert.deepEqual(value.trace.x, [.01, .021, .045, .1]); assert.deepEqual(value.trace.y, [10, -2, 5, 1]);
  assert.deepEqual(value.trace.x_error, [.001, .0012, .0015, .002]); assert.deepEqual(value.trace.y_error, [.5, .2, .3, .1]);
  assert.equal(value.scans[1].x_unit, '1/A'); assert.equal(value.scans[1].y_unit, '1/cm'); assert.match(value.scans[1].y_error, /distribution unspecified/);
});
test('XRDML stored values remain counts and explicit axis is not linspaced', () => {
  const value = parseResult(result('explicit', 'series'));
  assert.deepEqual(value.trace.x, [10, 10.25, 11.5, 14]); assert.deepEqual(value.trace.y, [10, 40, 20, 5]); assert.equal(value.trace.y_error, null);
});
const mutations = [
  ['public kind', r => r.kind = 'array'], ['view kind', r => r.payload.view_kind = 'array'], ['version type', r => r.version = []],
  ['revision', r => r.revision = 'bad'], ['sampled', r => r.sampled = true], ['warnings', r => r.warnings = []],
  ['metadata path', r => r.metadata.path = '/private/secret'], ['format array', r => r.metadata.format = ['xml']],
  ['dialect', r => r.metadata.dialect = 'cansas1d-2.0'], ['source bool', r => r.metadata.source_bytes = true],
  ['counter', r => r.metadata.returned_points = 1], ['limit bool', r => r.metadata.limits.max_points = true],
  ['foreign scan', r => r.payload.selected.scan = 0], ['selected bool', r => r.payload.selected.scan = true],
  ['id bool', r => r.payload.choices.scans[0].id = false], ['label URL', r => r.payload.choices.scans[0].label = 'https://example.invalid/'],
  ['unit HTML', r => r.payload.choices.scans[1].x_unit = '<img>'], ['q unit missing', r => r.payload.choices.scans[1].x_unit = ''],
  ['error inference', r => r.payload.choices.scans[1].y_error = 'standard deviation'], ['wrong points', r => r.payload.choices.scans[1].points = 3],
  ['trace foreign scan', r => r.payload.series[0].scan = 0], ['nonfinite', r => r.payload.series[0].y[0] = Infinity],
  ['numeric bool', r => r.payload.series[0].y[0] = true], ['negative error', r => r.payload.series[0].y_error[0] = -.1],
  ['short error', r => r.payload.series[0].x_error.pop()], ['missing error', r => r.payload.series[0].y_error = null],
  ['repeated q', r => r.payload.series[0].x[1] = .01], ['negative q', r => r.payload.series[0].x[0] = -.01],
];
for (const [name, mutate] of mutations) test(`strict result rejects ${name}`, () => { const r = result('sas', 'series'); mutate(r); assert.throws(() => parseResult(r)); });
test('source, suffix, selection and tree array exposure binding', () => {
  const r = result(); assert.throws(() => helpers.parseDiffractionData(r, 'tree', {}, r.metadata.source_bytes + 1, 'x.xml'));
  assert.throws(() => helpers.parseDiffractionData(r, 'tree', {}, r.metadata.source_bytes, 'x.xrdml'));
  assert.throws(() => helpers.validateDiffractionSelection(true, 2)); assert.throws(() => helpers.validateDiffractionSelection(2, 2));
  r.payload.series = [{ x: [1, 2], y: [1, 2] }]; assert.throws(() => parseResult(r));
});
function mount(t, response, library) {
  const requests = [], plots = [], purged = [];
  const element = () => ({ style: {}, children: [], replaceChildren(...children) { this.children = children; }, remove() { this.removed = true; } });
  const plotly = { newPlot: async (el, data, layout, config) => { el.data = data; plots.push({ el, data, layout, config }); }, purge: el => { delete el.data; purged.push(el); }, Plots: { resize() {} } };
  const originalDocument = globalThis.document; globalThis.document = { createElement: element }; t.after(() => globalThis.document = originalDocument);
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './diffractionData': helpers,
    './domains/lifecycle': { displayError: reason => reason.message }, './scientific/browserLibraries': { loadBrowserLibrary: async () => library ? library(plotly) : plotly },
    '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { const request = { file: structuredClone(vue.toRaw(file)), operation, options, signal }; requests.push(request); return response ? response(request) : result('sas', options.kind); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/DiffractionPreview.vue', import.meta.url), 'utf8'), descriptor = parse(source); assert.deepEqual(descriptor.errors, []);
  const compiled = ts.transpileModule(compileScript(descriptor.descriptor, { id: 'diffraction' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: 'sample.xml', size: fixtures.sas.tree.metadata.source_bytes, metadata: { dataset_file_version: 'v1' } },
    plugin: { id: 'viz-diffraction', version: '1', adapter: 'diffraction', reader: 'diffraction', enabled: true, capabilities: { operations: ['preview'], input_mode: 'whole', shared: false }, limits: { max_input_bytes: 16777216, max_output_bytes: 2097152 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.target.value = element(); t.after(() => scope.stop());
  return { state, props, requests, plots, purged, plotly, stop: () => scope.stop() };
}
test('actual Vue opens only tree, explicit scan request pins version and renders real arrays', async t => {
  const v = mount(t); await flush(); assert.equal(v.state.error.value, ''); assert.equal(v.requests.length, 1); assert.equal(v.plots.length, 0);
  v.state.scanId.value = 1; await v.state.loadScan(); await flush();
  assert.deepEqual(v.requests[1].options, { kind: 'series', version: 'a'.repeat(64), scan: 1 }); assert.equal(v.plots.length, 1);
  assert.deepEqual(v.plots[0].data[0].x, [.01, .021, .045, .1]); assert.deepEqual(v.plots[0].data[0].error_y.array, [.5, .2, .3, .1]);
  assert.equal(v.plots[0].layout.xaxis.title.text, 'Q (1/A)'); assert.equal(v.plots[0].layout.yaxis.type, 'linear');
  v.state.target.value = undefined; v.stop(); assert.equal(v.plots[0].el.removed, true); assert.equal(v.plots[0].el.data, undefined); assert.equal(v.state.data.value, undefined);
});
test('catalog clones do not re-read; real identity change resets a selected scan without cancelling fresh load', async t => {
  const v = mount(t); await flush(); v.state.scanId.value = 1;
  for (let i = 0; i < 3; i++) { v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); v.props.file = structuredClone(vue.toRaw(v.props.file)); await flush(); }
  assert.equal(v.requests.length, 1); v.props.file.metadata.dataset_file_version = 'v2'; await flush();
  assert.equal(v.requests.length, 2); assert.equal(v.state.scanId.value, 0); assert.ok(v.state.catalog.value); assert.equal(v.state.error.value, '');
});
for (const action of ['disable', 'unmount', 'selection', 'file']) test(`late scan response does not resurrect after ${action}`, async t => {
  const next = pending(), v = mount(t, request => request.options.kind === 'tree' ? result() : next.promise); await flush(); v.state.scanId.value = 1; const load = v.state.loadScan(); await flush();
  if (action === 'disable') v.props.plugin.enabled = false; else if (action === 'unmount') v.stop(); else if (action === 'selection') v.state.scanId.value = 0; else v.props.file.file_id = 'file-b';
  next.resolve(result('sas', 'series')); await load; await flush(); assert.equal(v.requests[1].signal.aborted, true); assert.equal(v.plots.length, 0); assert.equal(v.state.data.value, undefined);
});
test('late Plotly completion gets purged again after unmount', async t => {
  const next = pending(), v = mount(t); await flush(); v.plotly.newPlot = async (el, data) => { v.plots.push({ el }); await next.promise; el.data = data; };
  v.state.scanId.value = 1; const load = v.state.loadScan(); await flush(); assert.equal(v.plots.length, 1); v.stop(); next.resolve(); await load;
  assert.equal(v.plots[0].el.data, undefined); assert.equal(v.plots[0].el.removed, true); assert.equal(v.state.data.value, undefined);
});
for (const mutation of ['version', 'selection', 'unit']) test(`wrong ${mutation} response rejected before Plotly`, async t => {
  const v = mount(t, request => { const r = result('sas', request.options.kind); if (request.options.kind === 'series') { if (mutation === 'version') r.version = 'c'.repeat(64); else if (mutation === 'selection') r.payload.selected.scan = 0; else r.payload.choices.scans[0].y_unit = 'a.u.'; } return r; });
  await flush(); v.state.scanId.value = 1; await v.state.loadScan(); assert.equal(v.plots.length, 0); assert.ok(v.state.error.value);
});
