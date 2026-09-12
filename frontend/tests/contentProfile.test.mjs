import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as profileTools from '../src/visualizations/contentProfile.ts';
import * as previewIdentity from '../src/visualizations/previewIdentity.ts';
import { matchingVisualizations, viewKindLabel } from '../src/visualizations/contract.ts';
import { VISUALIZATION_ADAPTERS } from '../src/visualizations/adapters.generated.ts';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as plotHelpers from '../src/visualizations/plot.ts';
import { parseArray, plainLabel } from '../src/visualizations/extended/scientific/data.ts';

const { parseContentProfile, profileCandidates, canProfileFilename, profileLabel } = profileTools;
const profile = (overrides = {}) => ({ profile_version: 1, container: 'tiff', dialect: 'tiff', traits: [], evidence: ['tiff-header'], bytes_read: 4096, truncated: false, ...overrides });
const file = (id = 'file', overrides = {}) => ({ file_id: id, filename: `${id}.tif`, size: 8192, upload_date: '2026-09-10T00:00:00Z', metadata: { dataset_file_version: 'source-v1' }, ...overrides });
const descriptor = (id, adapter, overrides = {}) => {
  const spec = VISUALIZATION_ADAPTERS[adapter];
  return { contract_version: 2, id, adapter, reader: spec.readers[0], view_kind: spec.view_kind,
    capabilities: structuredClone(spec.capabilities), version: '1.0.0', enabled: true, default_enabled: true,
    name: id, description: '', extensions: ['tif', 'tiff'], filenames: [], priority: 10, permissions: ['file:read'],
    limits: { max_input_bytes: 64 * 1024 * 1024, max_output_bytes: 8 * 1024 * 1024 }, ...overrides };
};
const tiff = () => descriptor('tiff', 'tiff', { priority: 50 });
const ome = () => descriptor('viz-viv', 'viv', { priority: 30 });
const geo = () => descriptor('viz-openlayers', 'openlayers', { priority: 40 });
const hdf = () => descriptor('viz-h5web', 'h5web', { extensions: ['mat', 'nc', 'nc4', 'h5'] });
const plotly = () => descriptor('viz-plotly', 'plotly', { extensions: ['mat'] });
const ncMap = () => descriptor('netcdf-map', 'scientific-map', { reader: 'netcdf', extensions: ['nc', 'nc4'] });
const ncSeries = () => descriptor('netcdf-series', 'scientific-series', { reader: 'netcdf', extensions: ['nc', 'nc4'] });
const ids = (items) => items.map(item => item.id);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); await vue.nextTick(); };
const response = (content = profile(), revision = 'a'.repeat(64), overrides = {}) => ({
  contract_version: 2, plugin_id: 'tiff', version: 'b'.repeat(64), revision, kind: 'resources',
  payload: { profile: content }, metadata: { purpose: 'content-profile' }, sampled: content.truncated, warnings: [], ...overrides,
});

test('content profile preserves explicit unknown and incomplete evidence without guessing', () => {
  const p = profile({ container: 'unknown', dialect: 'unknown', evidence: [], bytes_read: 0, truncated: true });
  assert.deepEqual(parseContentProfile(p), p);
  assert.match(profileLabel(p), /尚未确证/);
  assert.match(profileLabel(p), /未完整/);
  assert.deepEqual(parseContentProfile(profile({ traits: ['ome', 'geotiff'], bytes_read: 65536 })).traits, ['ome', 'geotiff']);
});

for (const [label, mutation] of [
  ['unknown fields', { path: '/Users/private/source.tif' }], ['contract version', { profile_version: 2 }],
  ['string version', { profile_version: '1' }], ['unknown container', { container: 'video' }],
  ['array container', { container: ['tiff'] }], ['array dialect', { dialect: ['tiff'] }],
  ['null container', { container: null }], ['unknown dialect', { dialect: 'netcdf4-confirmed' }],
  ['string traits', { traits: 'ome' }], ['duplicate traits', { traits: ['ome', 'ome'] }],
  ['unapproved trait', { traits: ['executable'] }], ['oversized traits', { traits: ['ome', 'geotiff', 'numeric-container', 'ome'] }],
  ['nonstring trait', { traits: [1] }], ['string evidence', { evidence: 'tiff-header' }],
  ['nonstring evidence', { evidence: [true] }], ['oversized evidence list', { evidence: Array(33).fill('header') }],
  ['oversized evidence item', { evidence: ['x'.repeat(129)] }], ['negative byte budget', { bytes_read: -1 }],
  ['float byte budget', { bytes_read: 1.5 }], ['NaN byte budget', { bytes_read: NaN }],
  ['string byte budget', { bytes_read: '4096' }], ['excessive byte budget', { bytes_read: 65537 }],
  ['numeric truncated flag', { truncated: 1 }], ['string truncated flag', { truncated: 'false' }],
]) test(`content profile rejects ${label}`, () => assert.throws(() => parseContentProfile(profile(mutation))));

for (const field of ['profile_version', 'container', 'dialect', 'traits', 'evidence', 'bytes_read', 'truncated']) {
  test(`content profile rejects missing ${field}`, () => {
    const p = profile(); delete p[field]; assert.throws(() => parseContentProfile(p));
  });
}

test('filename prefilter is bounded to existing high-frequency probe families', () => {
  for (const name of ['a.tif', 'a.TIFF', 'a.ome.tif', 'a.mat', 'a.H5', 'a.hdf5', 'a.nc', 'a.nc4', 'a.netcdf', 'a.nxs', 'a.nx']) assert.equal(canProfileFilename(name), true, name);
  for (const name of ['a.tif.js', 'a.mat.gz', 'a.zip', 'a.h5.exe', 'a.fastq', 'a.txt', 'no-extension']) assert.equal(canProfileFilename(name), false, name);
});

test('classic NetCDF promotes existing NetCDF candidates and leaves every candidate selectable', () => {
  const candidates = [hdf(), ncMap(), ncSeries()];
  const result = profileCandidates(candidates, profile({ container: 'netcdf', dialect: 'netcdf-classic' }));
  assert.deepEqual(ids(result), ['netcdf-map', 'netcdf-series', 'viz-h5web']);
  assert.deepEqual(ids(candidates), ['viz-h5web', 'netcdf-map', 'netcdf-series'], 'sorting must not mutate catalog');
});

test('HDF5 magic alone preserves the established NetCDF order, including a numeric-series default', () => {
  for (const candidates of [[ncMap(), ncSeries(), hdf()], [ncSeries(), ncMap(), hdf()]]) {
    assert.deepEqual(ids(profileCandidates(candidates, profile({ container: 'hdf5', dialect: 'hdf5' }))), ids(candidates));
  }
});

test('MAT v7.3 recommends H5Web and level-5 recommends Plotly without adding or removing adapters', () => {
  assert.deepEqual(ids(profileCandidates([plotly(), hdf()], profile({ container: 'hdf5', dialect: 'matlab-v7.3' }))), ['viz-h5web', 'viz-plotly']);
  assert.deepEqual(ids(profileCandidates([hdf(), plotly()], profile({ container: 'mat', dialect: 'matlab-level5' }))), ['viz-plotly', 'viz-h5web']);
  assert.deepEqual(ids(profileCandidates([plotly()], profile({ container: 'hdf5', dialect: 'matlab-v7.3' }))), ['viz-plotly']);
});

test('OME and GeoTIFF recommend only members of the existing enabled filename candidate set', () => {
  const all = [tiff(), geo(), ome(), descriptor('disabled-viv', 'viv', { enabled: false }), plotly()];
  const candidates = matchingVisualizations(all, 'sample.tif');
  const result = profileCandidates(candidates, profile({ traits: ['ome', 'geotiff'] }));
  assert.deepEqual(ids(result), ['viz-viv', 'viz-openlayers', 'tiff']);
  assert.equal(result.some(p => !p.enabled || p.id === 'viz-plotly'), false);
  assert.deepEqual(ids(profileCandidates(candidates, profile({ traits: ['geotiff'] }))), ['viz-openlayers', 'tiff', 'viz-viv']);
  assert.deepEqual(ids(profileCandidates(candidates, profile())), ['tiff', 'viz-openlayers', 'viz-viv']);
});

test('unknown/incomplete content never suppresses the original fallback candidates', () => {
  const candidates = [tiff(), geo(), ome()];
  assert.deepEqual(ids(profileCandidates(candidates, null)), ids(candidates));
  assert.deepEqual(ids(profileCandidates(candidates, profile({ container: 'unknown', dialect: 'unknown', truncated: true }))), ids(candidates));
});

function compileSfc(path, dependencies, inlineTemplate = true) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const descriptor = parse(source).descriptor;
  const code = compileScript(descriptor, { id: 'profile-host-test', inlineTemplate }).content;
  const js = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', js)((id) => {
    if (id === 'vue') return vue;
    assert.ok(Object.hasOwn(dependencies, id), `Unexpected host dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);
  return module.exports.default;
}

function find(node, type) {
  if (node.type === type) return node;
  for (const child of node.children ?? []) { const found = find(child, type); if (found) return found; }
}
function texts(node) { return `${node.text || ''} ${(node.children ?? []).map(texts).join(' ')}`; }

function mountHost(t, { plugins = [tiff(), geo(), ome()], current = file(), request = async () => response(), path = '/chat/1' } = {}) {
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  globalThis.document = { visibilityState: 'visible' };
  const catalog = vue.ref({ engine: 'cordis', revision: 'a'.repeat(64), plugins });
  const error = vue.ref(''), loading = vue.ref(false), route = vue.reactive({ path });
  const instances = [], requests = [];
  const Adapter = vue.defineComponent({ props: ['file', 'plugin'], setup(props) {
    const instance = { file: props.file.file_id, plugin: props.plugin.id, disposed: false };
    instances.push(instance); vue.onScopeDispose(() => { instance.disposed = true; });
    return () => vue.h('canvas');
  } });
  const Host = compileSfc('../src/visualizations/VisualizationHost.vue', {
    'vue-router': { useRoute: () => route, RouterLink: { render: () => vue.h('a') } },
    './catalog': { useVisualizationCatalog: () => ({ catalog, error, loading, refresh: async () => {} }) },
    './contract': { matchingVisualizations, viewKindLabel },
    './adapters': { getVisualizationAdapter: () => Adapter },
    './contentProfile': profileTools,
    './previewIdentity': previewIdentity,
    './runtime': { requestVisualization: (f, p, operation, options, signal) => {
      const item = { file: f, plugin: p, operation, options, signal }; requests.push(item);
      return request(item, requests.length);
    } },
  });
  const renderer = vue.createRenderer({
    createElement: (type) => ({ type, children: [], props: {} }), createText: (text) => ({ text }), createComment: (text) => ({ text }),
    insert: (node, parent, anchor) => {
      if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node);
      parent.children ??= []; const index = anchor ? parent.children.indexOf(anchor) : -1;
      if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
      node.parent = parent;
    },
    remove: (node) => { if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node); },
    setElementText: (node, text) => { node.text = text; node.children = []; }, setText: (node, text) => { node.text = text; },
    parentNode: (node) => node.parent, nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: (node, key, _old, value) => { node.props[key] = value; },
  });
  const shown = vue.ref(true), currentFile = vue.ref(current), container = { children: [] };
  const Root = { setup: () => () => shown.value ? vue.h(Host, { file: currentFile.value }) : null };
  const app = renderer.createApp(Root); app.mount(container);
  t.after(() => {
    app.unmount();
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
  });
  return { catalog, error, route, instances, requests, currentFile, shown, container,
    select: (id) => find(container, 'select').props.onChange({ target: { value: id } }),
    active: () => instances.filter(item => !item.disposed), text: () => texts(container),
  };
}

test('host waits for bounded content evidence and mounts only the recommended adapter', async (t) => {
  const pending = deferred();
  const host = mountHost(t, { request: () => pending.promise });
  assert.equal(host.requests.length, 1); assert.equal(host.instances.length, 0);
  assert.equal(host.requests[0].operation, 'prepare'); assert.deepEqual(host.requests[0].options, {});
  pending.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.deepEqual(host.active().map(item => item.plugin), ['viz-viv']);
  assert.match(host.text(), /OME 显微元数据/);
});

test('a same-revision polling snapshot never repeats probe or remounts its adapter', async (t) => {
  const host = mountHost(t); await flush();
  assert.equal(host.requests.length, 1); assert.equal(host.instances.length, 1);
  for (let index = 0; index < 3; index++) { host.catalog.value = JSON.parse(JSON.stringify(host.catalog.value)); await flush(); }
  assert.equal(host.requests.length, 1); assert.equal(host.instances.length, 1); assert.equal(host.instances[0].disposed, false);
});

test('manual view selection survives the first profile recommendation and same-revision polling', async (t) => {
  const pending = deferred(); const host = mountHost(t, { request: () => pending.promise });
  host.select('tiff'); await flush();
  pending.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.active()[0].plugin, 'tiff');
  host.select('viz-openlayers'); await flush();
  host.catalog.value = JSON.parse(JSON.stringify(host.catalog.value)); await flush();
  assert.equal(host.active()[0].plugin, 'viz-openlayers'); assert.equal(host.requests.length, 1);
});

for (const outcome of ['success', 'failure']) test(`late ${outcome} after file replacement cannot attach or clear the new profile`, async (t) => {
  const old = deferred();
  const host = mountHost(t, { request: (_, count) => count === 1 ? old.promise : Promise.resolve(response(profile({ traits: ['geotiff'] }))) });
  host.currentFile.value = file('new'); await flush();
  assert.equal(host.requests[0].signal.aborted, true); assert.equal(host.requests.length, 2);
  assert.equal(host.active()[0].plugin, 'viz-openlayers'); assert.equal(host.active()[0].file, 'new');
  if (outcome === 'success') old.resolve(response(profile({ traits: ['ome'] }))); else old.reject(new Error('old result'));
  await flush(); assert.equal(host.active()[0].plugin, 'viz-openlayers'); assert.match(host.text(), /地理标签/); assert.doesNotMatch(host.text(), /OME 显微|识别未完成/);
});

for (const action of ['close', 'disable-all', 'catalog-unavailable', 'share-route']) test(`late profile cannot mount after ${action}`, async (t) => {
  const pending = deferred(); const host = mountHost(t, { request: () => pending.promise });
  if (action === 'close') host.shown.value = false;
  if (action === 'disable-all') host.catalog.value.plugins.forEach(item => { item.enabled = false; });
  if (action === 'catalog-unavailable') { host.catalog.value = null; host.error.value = 'runtime unavailable'; }
  if (action === 'share-route') host.route.path = '/share/token';
  await flush(); assert.equal(host.requests[0].signal.aborted, true);
  const before = host.instances.length;
  pending.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.instances.length, before); assert.equal(host.active().some(item => item.plugin === 'viz-viv'), false);
});

test('disabling the probe anchor starts only an enabled alternative and ignores old results', async (t) => {
  const old = deferred(); const host = mountHost(t, { request: (_, count) => count === 1 ? old.promise : Promise.resolve(response(profile({ traits: ['geotiff'] }))) });
  host.catalog.value.plugins.find(p => p.id === 'tiff').enabled = false; await flush();
  assert.equal(host.requests[0].signal.aborted, true); assert.equal(host.requests.length, 2);
  assert.equal(host.requests[1].plugin.enabled, true); assert.equal(host.requests[1].plugin.id, 'viz-openlayers');
  old.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.active()[0].plugin, 'viz-openlayers');
});

test('changed catalog revision disposes old adapter and reprobes using the new revision', async (t) => {
  const next = deferred(); const host = mountHost(t, { request: (_, count) => count === 1 ? Promise.resolve(response()) : next.promise });
  await flush(); const first = host.instances[0];
  host.catalog.value = { ...host.catalog.value, revision: 'c'.repeat(64) }; await flush();
  assert.equal(first.disposed, true); assert.equal(host.requests.length, 2); assert.equal(host.active().length, 0);
  next.resolve(response(profile({ traits: ['ome'] }), 'c'.repeat(64))); await flush();
  assert.equal(host.active()[0].plugin, 'viz-viv');
});

test('same-version approved limit change cancels the old content probe and ignores its late result', async (t) => {
  const old = deferred();
  const host = mountHost(t, { request: (_, count) => count === 1 ? old.promise : Promise.resolve(response(profile({ traits: ['geotiff'] }))) });
  host.catalog.value.plugins.find(p => p.id === 'tiff').limits.max_input_bytes /= 2;
  await flush();
  assert.equal(host.requests[0].signal.aborted, true);
  assert.equal(host.requests.length, 2);
  assert.equal(host.active()[0].plugin, 'viz-openlayers');
  old.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.active()[0].plugin, 'viz-openlayers');
});

for (const [label, change] of [
  ['revision', { revision: 'c'.repeat(64) }], ['kind', { kind: 'tree' }],
  ['purpose', { metadata: { purpose: 'unrelated' } }], ['malformed profile', { payload: { profile: profile({ bytes_read: 65537 }) } }],
]) test(`invalid profile ${label} falls back to the existing filename-selected reader`, async (t) => {
  const host = mountHost(t, { request: async () => response(profile({ traits: ['ome'] }), 'a'.repeat(64), change) });
  await flush(); assert.equal(host.active()[0].plugin, 'tiff'); assert.match(host.text(), /内容识别未完成/);
});

test('shared pages never issue content prepare even when the image plugin allows shared bytes', async (t) => {
  const host = mountHost(t, { path: '/share/token' }); await flush();
  assert.equal(host.requests.length, 0); assert.equal(host.active()[0].plugin, 'tiff');
});

test('unsupported filenames never issue a probe', async (t) => {
  const plain = descriptor('text', 'text', { extensions: ['txt'] });
  const host = mountHost(t, { plugins: [plain], current: file('readme', { filename: 'readme.txt' }) });
  await flush(); assert.equal(host.requests.length, 0); assert.equal(host.active()[0].plugin, 'text');
});

test('a matching filename without declared prepare capability never issues a probe', async (t) => {
  const plugin = tiff(); plugin.capabilities.operations = ['bytes'];
  const host = mountHost(t, { plugins: [plugin] }); await flush();
  assert.equal(host.requests.length, 0); assert.equal(host.active()[0].plugin, 'tiff');
});

for (const size of [-1, NaN, Infinity, 65 * 1024 * 1024]) test(`invalid or excessive source size ${size} never issues probe or mounts a decoder`, async (t) => {
  const host = mountHost(t, { current: file('invalid', { size }) }); await flush();
  assert.equal(host.requests.length, 0); assert.equal(host.active().length, 0);
  assert.match(host.text(), /安全预览上限/);
});

for (const marker of ['dataset_file_version', 'sha256', 'content_sha256']) test(`same-ID same-size ${marker} replacement cancels old probe and reprobes`, async (t) => {
  const old = deferred(); const host = mountHost(t, { request: (_, count) => count === 1 ? old.promise : Promise.resolve(response(profile({ traits: ['geotiff'] }))) });
  host.currentFile.value = { ...host.currentFile.value, metadata: { ...host.currentFile.value.metadata, [marker]: 'source-v2' } };
  await flush(); assert.equal(host.requests[0].signal.aborted, true); assert.equal(host.requests.length, 2);
  old.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.active()[0].plugin, 'viz-openlayers'); assert.doesNotMatch(host.text(), /OME 显微/);
});

test('same-ID same-size changed upload date invalidates the old content profile', async (t) => {
  const old = deferred(); const host = mountHost(t, { request: (_, count) => count === 1 ? old.promise : Promise.resolve(response()) });
  host.currentFile.value = { ...host.currentFile.value, upload_date: '2026-09-10T01:00:00Z' };
  await flush(); assert.equal(host.requests[0].signal.aborted, true); assert.equal(host.requests.length, 2);
  old.resolve(response(profile({ traits: ['ome'] }))); await flush(); assert.equal(host.active()[0].plugin, 'tiff');
});

test('source-version change also disposes an already mounted decoder despite unchanged file ID and size', async (t) => {
  const next = deferred(); const host = mountHost(t, { request: (_, count) => count === 1 ? Promise.resolve(response()) : next.promise });
  await flush(); const first = host.instances[0];
  host.currentFile.value = { ...host.currentFile.value, metadata: { dataset_file_version: 'source-v2' } }; await flush();
  assert.equal(first.disposed, true); assert.equal(host.active().length, 0); assert.equal(host.requests.length, 2);
  next.resolve(response(profile({ traits: ['ome'] }))); await flush();
  assert.equal(host.active()[0].plugin, 'viz-viv'); assert.equal(host.instances.length, 2);
});

test('actual NetCDF adapter does not re-read or reset selection for an equivalent polled plugin object', async (t) => {
  let reads = 0;
  const Component = compileSfc('../src/visualizations/ScientificFilePreview.vue', {
    '../api/visualization': { getScientificVisualization: async () => {
      reads++;
      return { kind: 'series', version: 'b'.repeat(64), variables: [], selected_variable: '', metadata: {}, x: [], y: [], warnings: [] };
    } },
    '../composables/usePreviewLoad': { usePreviewLoad }, './plot': plotHelpers,
    './assets/world-outline.json': { default: { geometry: { coordinates: [] } } },
  }, false);
  const props = vue.reactive({ file: file('source', { filename: 'source.nc' }), plugin: ncSeries() });
  const scope = vue.effectScope(); const state = scope.run(() => Component.setup(props, { expose() {} }));
  t.after(() => scope.stop()); await flush(); assert.equal(reads, 1);
  state.variable.value = 'kept-user-selection';
  for (let index = 0; index < 3; index++) { props.plugin = structuredClone(ncSeries()); await flush(); }
  assert.equal(reads, 1); assert.equal(state.variable.value, 'kept-user-selection');
});

test('actual H5Web adapter does not re-read its tree or reset selection for an equivalent polled plugin object', async (t) => {
  let reads = 0;
  const Component = compileSfc('../src/visualizations/extended/scientific/H5WebPreview.vue', {
    '../../../composables/usePreviewLoad': { usePreviewLoad },
    '../runtime': { requestPreview: async () => { reads++; return { tree: [], choices: { variables: [] }, version: 'b'.repeat(64), warnings: [], sampled: false }; } },
    './data': { parseArray: () => { throw new Error('No array should load for an empty tree'); }, plainLabel: String },
    './PreviewFrame.vue': { default: {} },
  }, false);
  const props = vue.reactive({ file: file('source', { filename: 'source.h5' }), plugin: hdf() });
  const scope = vue.effectScope(); const state = scope.run(() => Component.setup(props, { expose() {} }));
  t.after(() => scope.stop()); await flush(); assert.equal(reads, 1);
  state.path.value = '/kept-user-selection';
  for (let index = 0; index < 3; index++) { props.plugin = structuredClone(hdf()); await flush(); }
  assert.equal(reads, 1); assert.equal(state.path.value, '/kept-user-selection');
});

test('actual H5Web uses the approved tree version for automatic and user slices while retaining Float64 precision', async (t) => {
  const version = 'd'.repeat(64), requests = [], renders = [], roots = [];
  const values = [16777217, 1.0000000000000002, null, 1e40];
  const tree = [
    { path: '/science', node_type: 'group' },
    { path: '/science/data', node_type: 'dataset', shape: [3, 2, 4], dtype: 'float64' },
    { path: '/science/second', node_type: 'dataset', shape: [4, 2, 2], dtype: 'float64' },
  ];
  const Component = compileSfc('../src/visualizations/extended/scientific/H5WebPreview.vue', {
    '../../../composables/usePreviewLoad': { usePreviewLoad },
    '../runtime': { requestPreview: async (_file, _plugin, options, signal) => {
      requests.push({ options: structuredClone(options), signal });
      if (options.kind === 'tree') return { version, tree, choices: { variables: ['/science/data', '/science/second'] }, warnings: [], sampled: false };
      return { version, array: { shape: options.kind === 'heatmap' ? [2, 2] : [4], values }, metadata: { strides: options.kind === 'heatmap' ? [2, 3] : [3] }, warnings: ['Bounded slice, original indices retained.'], sampled: true };
    } },
    './data': { parseArray, plainLabel }, './PreviewFrame.vue': { default: {} },
    react: { default: { createElement: (component, props) => ({ component, props }) } },
    'react-dom/client': { default: { createRoot: target => {
      assert.ok(target, 'the actual adapter receives a render target');
      const root = { unmounted: false, render(value) { renders.push(value); }, unmount() { this.unmounted = true; } };
      roots.push(root); return root;
    } } },
    '@h5web/lib': { LineVis: 'line', HeatmapVis: 'heatmap' },
    ndarray: { default: (data, shape) => ({ data, shape }) }, '@h5web/lib/styles.css': {},
  }, false);
  const props = vue.reactive({ file: file('source', { filename: 'source.h5' }), plugin: hdf() });
  const scope = vue.effectScope(); const state = scope.run(() => Component.setup(props, { expose() {} }));
  state.target.value = { clientWidth: 800, clientHeight: 480 };
  t.after(() => scope.stop()); await flush();
  assert.equal(state.error.value, ''); assert.equal(state.loading.value, false);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].options, { kind: 'tree' });
  assert.deepEqual(requests[1].options, { version, path: '/science/data', kind: 'series', indices: [0, 0] });
  assert.equal(state.version.value, version); assert.equal(renders.length, 1); assert.equal(renders[0].component, 'line');
  const first = renders[0].props.dataArray.data;
  assert.ok(first instanceof Float64Array);
  assert.equal(first[0], values[0]); assert.equal(first[1], values[1]); assert.ok(Number.isNaN(first[2])); assert.equal(first[3], values[3]);
  assert.notEqual(first[0], Math.fround(values[0]), 'scientific values must not silently narrow to Float32');
  assert.deepEqual([...renders[0].props.abscissaParams.value], [0, 3, 6, 9]);
  state.path.value = '/science/second'; state.kind.value = 'heatmap'; state.indices.value = [2];
  await state.loadData(); await flush();
  assert.equal(state.error.value, ''); assert.equal(requests.length, 3);
  assert.deepEqual(requests[2].options, { version, path: '/science/second', kind: 'heatmap', indices: [2] });
  assert.equal(requests[1].signal.aborted, true); assert.equal(roots[0].unmounted, true);
  assert.equal(renders.length, 2); assert.equal(renders[1].component, 'heatmap');
  assert.ok(renders[1].props.dataArray.data instanceof Float64Array);
  assert.equal(renders[1].props.dataArray.data[0], values[0]); assert.equal(renders[1].props.dataArray.data[3], values[3]);
  assert.deepEqual([...renders[1].props.abscissaParams.value], [0, 3]);
  assert.deepEqual([...renders[1].props.ordinateParams.value], [0, 2]);
  assert.deepEqual(state.warnings.value, ['Bounded slice, original indices retained.']); assert.equal(state.sampled.value, true);
  scope.stop(); assert.equal(requests[2].signal.aborted, true); assert.equal(roots[1].unmounted, true);
});

test('actual H5Web preserves a group and string-only directory with warnings without requesting a numeric slice', async (t) => {
  const requests = [], version = 'e'.repeat(64);
  const tree = [
    { path: '/experiment', node_type: 'group' },
    { path: '/experiment/description', node_type: 'dataset', shape: [2], dtype: '|S32' },
    { path: '/empty', node_type: 'group' },
  ];
  const warnings = ['No plottable numeric datasets within this budget.', 'Directory scan stopped at the configured depth.'];
  const Component = compileSfc('../src/visualizations/extended/scientific/H5WebPreview.vue', {
    '../../../composables/usePreviewLoad': { usePreviewLoad },
    '../runtime': { requestPreview: async (_file, _plugin, options) => {
      requests.push(structuredClone(options));
      assert.equal(options.kind, 'tree', 'a string dataset must not be selected as an implicit numeric fallback');
      return { version, tree, choices: { variables: [] }, warnings, sampled: true };
    } },
    './data': { parseArray: () => assert.fail('No numeric decoder should run'), plainLabel }, './PreviewFrame.vue': { default: {} },
  }, false);
  const props = vue.reactive({ file: file('source', { filename: 'metadata-only.h5' }), plugin: hdf() });
  const scope = vue.effectScope(); const state = scope.run(() => Component.setup(props, { expose() {} }));
  t.after(() => scope.stop()); await flush();
  assert.deepEqual(requests, [{ kind: 'tree' }]); assert.equal(state.error.value, ''); assert.equal(state.loading.value, false);
  assert.deepEqual(state.nodes.value, tree); assert.deepEqual(state.warnings.value, warnings);
  assert.deepEqual(state.datasets.value, []); assert.equal(state.path.value, ''); assert.equal(state.version.value, version);
  assert.equal(state.sampled.value, true);
  props.plugin = structuredClone(hdf()); await flush();
  assert.equal(requests.length, 1); assert.deepEqual(state.nodes.value, tree); assert.deepEqual(state.warnings.value, warnings);
});

test('actual Plotly adapter does not re-read an array or reset its variable and slice after equivalent catalog polling', async (t) => {
  let reads = 0, libraryLoads = 0;
  const Component = compileSfc('../src/visualizations/extended/scientific/PlotlyPreview.vue', {
    '../../../composables/usePreviewLoad': { usePreviewLoad },
    '../runtime': { requestPreview: async () => {
      reads++;
      return { array: { shape: [2], values: [1, 2] }, choices: { variables: ['initial'] }, selected: { variable: 'initial' }, metadata: { source_shape: [3, 2], strides: [1] }, sampled: false };
    } },
    './data': { parseArray: value => value, parseTable: value => value, numericColumns: () => [], numericCell: value => value, plainLabel: String },
    './PreviewFrame.vue': { default: {} },
    './browserLibraries': { loadBrowserLibrary: async () => {
      libraryLoads++;
      return { purge() {}, react: async () => {}, Plots: { resize() {} } };
    } },
  }, false);
  const props = vue.reactive({ file: file('source', { filename: 'source.mat' }), plugin: plotly() });
  const scope = vue.effectScope(); const state = scope.run(() => Component.setup(props, { expose() {} }));
  t.after(() => scope.stop()); await flush();
  assert.equal(reads, 1); assert.equal(libraryLoads, 1); assert.equal(state.error.value, '');
  state.variable.value = 'kept-user-selection'; state.indices.value = [2];
  for (let index = 0; index < 3; index++) { props.plugin = structuredClone(plotly()); await flush(); }
  assert.equal(reads, 1); assert.equal(libraryLoads, 1);
  assert.equal(state.variable.value, 'kept-user-selection'); assert.deepEqual(state.indices.value, [2]);
  assert.deepEqual(state.sourceShape.value, [3, 2]); assert.equal(state.error.value, '');
});
