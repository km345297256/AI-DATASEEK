import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/phylogenyData.ts';
const generated = JSON.parse(readFileSync(new URL('./browser/domain-expansion-phylogeny-data.json', import.meta.url), 'utf8'));
function result(which = 'complete') {
  const { contract_version, type, reader, kind, metadata, warnings, sampled, ...payload } = structuredClone(generated[which]);
  return { contract_version, kind, plugin_id: 'viz-phylogeny', version: 'a'.repeat(64), revision: 'b'.repeat(64), payload: { ...payload, view_kind: kind }, metadata, warnings, sampled };
}
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
test('real parser payload preserves internal labels and raw lengths without support inference', () => {
  const r = result(), p = helpers.parsePhylogenyData(r, r.metadata.source_bytes, 'human.nwk');
  assert.equal(p.nodes[1].label, '95'); assert.equal(p.nodes[1].length, '0.3'); assert.equal(p.nodes[0].length, '0.01');
  assert.deepEqual(p.depths, [0, 1, 2, 2, 1]); assert.deepEqual(p.distances, [0, 0.3, 0.4, 0.5, 0.8]);
  assert.equal(p.leaves, 3); assert.deepEqual(p.descendants, [4, 2, 0, 0, 0]); assert.equal(p.branchLengthMode, true);
});
test('deterministic topology and branch scale differ with correct edge distances', () => {
  const p = helpers.parsePhylogenyData(result()), a = helpers.layoutPhylogeny(p, 'topology', new Set()), b = helpers.layoutPhylogeny(p, 'length', new Set());
  assert.equal(a.paths.length, 4); assert.equal(b.paths.length, 4); assert.equal(a.points[2].x, 632); assert.equal(b.points[2].x, 332); assert.equal(b.points[4].x, 632);
  assert.equal(b.scale, 0.8); assert.equal(b.points[0].x, 32, 'root incoming length is never added');
  for (const layout of [a, b]) for (const point of layout.points) assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y));
});
test('collapse changes topology display without inventing lengths or mutating source', () => {
  const p = helpers.parsePhylogenyData(result()), before = JSON.stringify(p);
  const folded = helpers.layoutPhylogeny(p, 'length', new Set([1]));
  assert.deepEqual(folded.points.map(p => p.index), [0, 1, 4]); assert.equal(folded.paths.length, 2); assert.equal(folded.points[1].collapsed, true); assert.equal(folded.points[1].x, 257);
  assert.equal(JSON.stringify(p), before); assert.equal(helpers.layoutPhylogeny(p, 'topology', new Set([0])).points.length, 1);
});
for (const which of ['missing', 'zero']) test(`${which} lengths allow topology only; missing is not zero`, () => {
  const p = helpers.parsePhylogenyData(result(which)); assert.equal(p.branchLengthMode, false);
  assert.throws(() => helpers.layoutPhylogeny(p, 'length', new Set())); assert.ok(helpers.layoutPhylogeny(p, 'topology', new Set()).points.length > 1);
  if (which === 'missing') { assert.equal(p.nodes[2].length, null); assert.equal(p.distances[2], null); }
});
for (const value of ['1e-999', '1e-13', '1e1000', '1e+999', '-0', '-1', 'NaN', 'Infinity', '1e', '1000000001', '1000000000.0000000000001', '9.999999999999999999e-13', '1'.repeat(33), 1, true, [], {}]) {
  test(`invalid raw branch length ${JSON.stringify(value)}`, () => assert.throws(() => helpers.branchNumber(value)));
}
for (const value of ['0', '0e999', '0e-999', '+.5', '1e-12', '1E+9']) test(`bounded exact branch lexeme ${value}`, () => assert.equal(helpers.branchNumber(value), Number(value)));
const mutations = [
  ['wrong kind', r => r.kind = 'graph'], ['wrong media view', r => r.payload.view_kind = 'graph'], ['wrong version', r => r.version = 'bad'], ['sampled tree', r => r.sampled = true],
  ['invented support', r => r.payload.phylogeny.nodes[1].support = 95], ['unsafe label', r => r.payload.phylogeny.nodes[1].label = '<script>'],
  ['external resource', r => r.payload.phylogeny.nodes[1].label = 'https://evil.invalid/track'], ['host path', r => r.payload.phylogeny.nodes[1].label = '/Users/private'],
  ['control label', r => r.payload.phylogeny.nodes[1].label = '\u202eabc'], ['oversize label', r => r.payload.phylogeny.nodes[1].label = '科'.repeat(257)],
  ['unknown root semantics', r => r.payload.phylogeny.rootedness = 'rooted'], ['parent cycle', r => r.payload.phylogeny.nodes[1].parent = 'n1'],
  ['unknown parent', r => r.payload.phylogeny.nodes[1].parent = 'n999'], ['duplicate ID', r => r.payload.phylogeny.nodes[1].id = 'n0'],
  ['numeric branch length', r => r.payload.phylogeny.nodes[1].length = .3], ['underflow', r => r.payload.phylogeny.nodes[1].length = '1e-999'],
  ['boolean depth', r => r.metadata.max_depth = true], ['fake branch mode', r => r.metadata.branch_length_mode = 1],
  ['missing counter', r => r.metadata.missing_lengths = 1], ['fake root length', r => r.metadata.root_length_present = false],
  ['extra metadata path', r => r.metadata.path = '/Users/private'], ['array format', r => r.metadata.format = ['nwk']],
  ['warnings altered', r => r.warnings = []], ['new public field', r => r.payload.url = 'file:///etc/passwd'],
];
for (const [name, mutate] of mutations) test(`strict response rejects ${name}`, () => { const r = result(); mutate(r); assert.throws(() => helpers.parsePhylogenyData(r)); });
test('source size and format binding reject structurally valid wrong file', () => {
  const r = result(); assert.throws(() => helpers.parsePhylogenyData(r, r.metadata.source_bytes + 1, 'tree.nwk'));
  assert.throws(() => helpers.parsePhylogenyData(r, r.metadata.source_bytes, 'tree.tre'));
  assert.throws(() => helpers.parsePhylogenyData(r, true));
});
function mount(t, response = () => result()) {
  const requests = [];
  const modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity, './phylogenyData': helpers,
    './domains/lifecycle': { displayError: reason => reason.message }, '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { requests.push({ file: structuredClone(vue.toRaw(file)), operation, options, signal }); return response(); } } };
  const source = readFileSync(new URL('../src/visualizations/extended/PhylogenyPreview.vue', import.meta.url), 'utf8');
  const parsed = parse(source); assert.deepEqual(parsed.errors, []);
  const compiled = ts.transpileModule(compileScript(parsed.descriptor, { id: 'phylogeny' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-a', filename: 'tree.nwk', size: result().metadata.source_bytes, metadata: { dataset_file_version: 'source-a' } }, plugin: { id: 'viz-phylogeny', version: '1', adapter: 'phylogeny', reader: 'phylogeny', enabled: true, capabilities: { operations: ['preview'], input_mode: 'whole', shared: false }, limits: { max_input_bytes: 4194304, max_output_bytes: 1048576 } } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); t.after(() => scope.stop());
  return { state, props, requests, stop: () => scope.stop() };
}
test('actual component local mode, search and collapse reuse one authorized read', async t => {
  const v = mount(t); await flush(); assert.equal(v.state.error.value, ''); assert.equal(v.requests.length, 1); assert.deepEqual(v.requests[0].options, { kind: 'tree' });
  v.state.mode.value = 'length'; assert.equal(v.state.layout.value.points[2].x, 332);
  v.state.selected.value = 1; v.state.toggle(); assert.equal(v.state.layout.value.points.length, 3);
  v.state.query.value = 'human'; await v.state.findNext(); assert.equal(v.state.selected.value, 2); assert.equal(v.state.collapsed.value.has(1), false);
  assert.equal(v.state.layout.value.points.length, 5); assert.equal(v.requests.length, 1);
});
test('clone catalog polling never downloads again; real file revision changes do', async t => {
  const v = mount(t); await flush();
  for (let i = 0; i < 4; i++) { v.props.file = structuredClone(vue.toRaw(v.props.file)); v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); await flush(); }
  assert.equal(v.requests.length, 1); v.props.file.metadata.dataset_file_version = 'source-b'; await flush(); assert.equal(v.requests.length, 2); assert.equal(v.requests[0].signal.aborted, true);
});
for (const action of ['disable', 'unmount', 'cancel', 'file', 'limits']) test(`late response cannot publish after ${action} before a tick`, async t => {
  const next = pending(); let calls = 0;
  const v = mount(t, () => ++calls === 1 ? next.promise : new Promise(() => {})); await flush();
  if (action === 'disable') v.props.plugin.enabled = false;
  else if (action === 'unmount') v.stop(); else if (action === 'cancel') v.state.cancel();
  else if (action === 'file') v.props.file.file_id = 'file-b'; else v.props.plugin.limits.max_input_bytes--;
  next.resolve(result()); await flush(); assert.equal(v.requests[0].signal.aborted, true);
  if (['file', 'limits'].includes(action)) assert.equal(v.requests.length, 2);
  assert.equal(v.state.data.value, undefined);
});
test('unmount clears captured result even after viewport ref disappears', async t => {
  const v = mount(t); await flush(); assert.ok(v.state.data.value); v.state.viewport.value = undefined; v.stop(); assert.equal(v.state.data.value, undefined); assert.equal(v.requests[0].signal.aborted, true);
});
test('wrong public type and schema rejected before SVG receives nodes', async t => {
  for (const mutation of [r => r.kind = 'graph', r => r.metadata.source_bytes++, r => r.payload.phylogeny.nodes[1].label = '<img>']) {
    const v = mount(t, () => { const r = result(); mutation(r); return r; }); await flush(); assert.equal(v.state.data.value, undefined); assert.notEqual(v.state.error.value, '');
  }
});
test('no HTML/URL rendering, storage, async force layout or external dependency', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/PhylogenyPreview.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /v-html|innerHTML|localStorage|sessionStorage|new Worker|new Image|https?:\/\/|eval\(/);
});
