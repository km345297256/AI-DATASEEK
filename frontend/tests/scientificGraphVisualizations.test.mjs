import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import cytoscape from 'cytoscape';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';

const helperSource = readFileSync(new URL('../src/visualizations/extended/scientificGraphData.ts', import.meta.url), 'utf8');
const helperModule = { exports: {} };
new Function('module', 'exports', ts.transpileModule(helperSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(helperModule, helperModule.exports);
const helpers = helperModule.exports;
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
function result() {
  return { contract_version: 2, type: 'scientific-graph', reader: 'scientific-graph', kind: 'graph', media_type: 'application/json', graph: { directed: true, nodes: [{ id: 'n0', key: 'TP53', label: 'TP53', group: 'regulator' }, { id: 'n1', key: 'MDM2', label: 'MDM2', group: null }], edges: [{ id: 'e0', key: null, source: 'n0', target: 'n1', label: 'binding', weight: 0.5 }] }, metadata: { format: 'json', dialect: 'dataseek-graph-json-v1', input_mode: 'whole', source_bytes: 128, node_count: 2, edge_count: 1, simple: true }, warnings: helpers.GRAPH_WARNINGS, sampled: false, version: 'a'.repeat(64) };
}
function mount(t, respond = () => result()) {
  const seen = { requests: [], instances: [], layouts: [], observers: [] }, mounted = [];
  const FakeResizeObserver = class { constructor(fn) { this.fn = fn; this.disconnected = false; seen.observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  const factory = options => {
    const instance = { options, destroyedValue: false, listeners: {}, highlight: [], elements: () => ({ removeClass() {} }), getElementById: id => ({ addClass: () => instance.highlight.push(id) }), destroyed: () => instance.destroyedValue,
      layout: options => { const layout = { options, stopped: false, run() {}, stop() { this.stopped = true; } }; seen.layouts.push(layout); return layout; },
      on: (event, fn) => { instance.listeners[event] = fn; }, removeAllListeners: () => { instance.listeners = {}; }, destroy: () => { instance.destroyedValue = true; }, resize() {}, fit() {} };
    seen.instances.push(instance); return instance;
  };
  const modules = { vue: { ...vue, onMounted: fn => mounted.push(fn) }, cytoscape: { default: factory }, '../../composables/usePreviewLoad': { usePreviewLoad }, '../previewIdentity': identity,
    '../runtime': { requestVisualization: async (file, plugin, operation, options, signal) => { assert.equal(operation, 'preview'); seen.requests.push({ options, signal }); const raw = await respond(); const { contract_version, type, reader, kind, version, metadata, warnings, sampled, publicKind, ...payload } = raw; return { contract_version, kind: publicKind ?? kind, payload: { ...payload, view_kind: kind }, version, revision: 'b'.repeat(64), metadata, warnings, sampled }; } }, './scientificGraphData': helpers, './domains/lifecycle': { displayError: reason => reason.message } };
  const source = readFileSync(new URL('../src/visualizations/extended/ScientificGraphPreview.vue', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'scientific-graph' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', 'ResizeObserver', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports, FakeResizeObserver);
  const props = vue.reactive({ file: { file_id: 'file-1', filename: 'network.json', size: 128 }, plugin: { id: 'viz-scientific-graph', version: '1', enabled: true, reader: 'scientific-graph', adapter: 'scientific-graph', limits: { max_input_bytes: 4 * 1024 ** 2, max_output_bytes: 1024 ** 2 }, capabilities: { input_mode: 'whole', operations: ['preview'], shared: false } } });
  const scope = vue.effectScope(), state = scope.run(() => module.exports.default.setup(props, { expose() {} })); state.container.value = {};
  t.after(() => scope.stop());
  return { state, seen, props, stop: () => scope.stop(), start: async () => { for (const fn of mounted) await fn(); await flush(); } };
}

test('strict graph schema and source/format binding accept only supported static topology', () => {
  assert.equal(helpers.parseScientificGraphData(result(), 128, 'NETWORK.JSON').nodes.length, 2);
  assert.throws(() => helpers.parseScientificGraphData(result(), 129, 'network.json'));
  assert.throws(() => helpers.parseScientificGraphData(result(), 128, 'network.graphml'));
  for (const mutate of [r => r.metadata.node_count = true, r => r.graph.directed = 'true', r => r.sampled = true, r => r.version = 'bad', r => r.metadata.dialect = ['dataseek-graph-json-v1'], r => r.type = 'tree', r => r.graph.nodes[0].label = '<img>', r => r.graph.nodes[0].key = '/Users/private', r => r.graph.nodes[0].label = 'https://example.com', r => r.graph.edges[0].weight = Infinity, r => r.graph.edges[0].source = 'missing', r => r.graph.nodes[0].style = {}, r => r.graph.edges[0].target = 'n0', r => r.graph.edges.push({ ...r.graph.edges[0], id: 'e1' })]) {
    const value = result(); mutate(value); assert.throws(() => helpers.parseScientificGraphData(value));
  }
});
test('Cytoscape receives only fixed inert fields and real deterministic layouts', () => {
  const data = helpers.parseScientificGraphData(result()); const elements = helpers.graphElements(data);
  assert.deepEqual(Object.keys(elements[0].data), ['id', 'label']); assert.ok(!JSON.stringify(elements).includes('regulator'));
  const core = cytoscape({ elements, headless: true });
  try { for (const name of ['circle', 'grid']) { core.layout({ name, animate: false, boundingBox: { x1: 0, y1: 0, w: 600, h: 400 } }).run(); assert.equal(core.nodes().length, 2); for (const node of core.nodes()) assert.ok(Number.isFinite(node.position('x')) && Number.isFinite(node.position('y'))); } }
  finally { core.destroy(); } assert.equal(core.destroyed(), true);
});
test('load renders graph; layout and search reuse the same authorized data', async t => {
  const v = mount(t); await v.start(); assert.equal(v.state.error.value, ''); assert.deepEqual(v.seen.requests[0].options, { kind: 'graph' });
  assert.equal(v.seen.layouts[0].options.name, 'circle'); v.state.layoutName.value = 'grid'; v.state.applyLayout();
  assert.equal(v.seen.requests.length, 1); assert.equal(v.seen.layouts[1].options.name, 'grid'); assert.equal(v.seen.layouts[0].stopped, true);
  v.state.query.value = 'regulator'; await flush(); assert.equal(v.state.matchCount.value, 1); assert.deepEqual(v.seen.instances[0].highlight, ['n0']);
  v.seen.instances[0].listeners.tap({ target: { id: () => 'n0' } }); assert.equal(v.state.selectedNode.value.key, 'TP53');
});
test('clone polling causes no repeated read; changed full descriptors trigger replacement', async t => {
  const v = mount(t); await v.start(); const first = v.seen.instances[0];
  for (let i = 0; i < 3; i++) { v.props.file = { ...v.props.file }; v.props.plugin = structuredClone(vue.toRaw(v.props.plugin)); await flush(); }
  assert.equal(v.seen.requests.length, 1); v.props.plugin.limits.max_input_bytes--; await flush();
  assert.equal(v.seen.requests.length, 2); assert.equal(first.destroyedValue, true); assert.equal(v.seen.observers[0].disconnected, true);
});
test('disable and unmount destroy actual captured instance after DOM ref disappears', async t => {
  const v = mount(t); await v.start(); v.state.container.value = undefined; v.stop();
  assert.equal(v.seen.instances[0].destroyedValue, true); assert.equal(v.seen.observers[0].disconnected, true); assert.equal(v.seen.layouts[0].stopped, true);
  const second = mount(t); await second.start(); second.props.plugin.enabled = false; await flush();
  assert.equal(second.seen.instances[0].destroyedValue, true); assert.equal(second.state.data.value, undefined); assert.equal(second.seen.requests.length, 1);
});
test('cancellation and disabled late responses never instantiate Cytoscape', async t => {
  for (const mode of ['cancel', 'disable', 'unmount']) {
    const next = pending(), v = mount(t, () => next.promise); const started = v.start(); await flush();
    if (mode === 'cancel') v.state.cancel(); else if (mode === 'disable') v.props.plugin.enabled = false; else v.stop();
    next.resolve(result()); await started; assert.equal(v.seen.instances.length, 0); assert.equal(v.seen.requests[0].signal.aborted, true);
  }
});
test('malicious valid-envelope data produces error before SDK initialization', async t => {
  const value = result(); value.graph.nodes[0].label = 'javascript:alert(1)'; const v = mount(t, () => value); await v.start();
  assert.match(v.state.error.value, /检查/); assert.equal(v.seen.instances.length, 0);
});
test('wrong public result kind cannot be overridden by a valid graph view_kind payload', async t => {
  const value = result(); value.publicKind = 'table'; const v = mount(t, () => value); await v.start(); assert.match(v.state.error.value, /类型不一致/); assert.equal(v.seen.instances.length, 0);
});
test('controls never request layouts or executable configuration from the data', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/ScientificGraphPreview.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|eval\(|https?:\/\//); assert.match(source, /animate: false/); assert.match(source, /autoungrabify: true/); assert.match(source, /filePreviewIdentity/);
});
