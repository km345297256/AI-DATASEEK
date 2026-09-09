import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { parseVisualizationCatalog, matchingVisualizations, selectVisualization } from '../src/visualizations/contract.ts';
import { coordinateEdges, finiteExtent, seriesSegments } from '../src/visualizations/plot.ts';
import { readBoundedBinary, validateTiffDimensions } from '../src/visualizations/boundedBinary.ts';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import { VISUALIZATION_ADAPTERS } from '../src/visualizations/adapters.generated.ts';

const descriptor = (overrides = {}) => { const spec = VISUALIZATION_ADAPTERS[overrides.adapter || 'tiff'] || VISUALIZATION_ADAPTERS.tiff; return ({ contract_version: 2, id: 'tiff', version: '1.0.0', name: 'TIFF', description: 'image', extensions: ['tif', 'tiff'], filenames: [], view_kind: spec.view_kind, adapter: 'tiff', reader: spec.readers[0], capabilities: spec.capabilities, default_enabled: true, enabled: true, priority: 20, permissions: ['file:read'], limits: { max_input_bytes: 64 * 1024 * 1024, max_output_bytes: 512 * 1024 }, ...overrides }); };
const catalogOf = (...plugins) => ({ engine: 'cordis', revision: 'rev1', plugins });
const ncMap = () => descriptor({ id: 'netcdf-map', extensions: ['nc'], adapter: 'scientific-map', reader: 'netcdf', view_kind: 'map' });
const ncSeries = () => descriptor({ id: 'netcdf-series', extensions: ['nc'], adapter: 'scientific-series', reader: 'netcdf', view_kind: 'series', priority: 10 });
const file = (id, extension = 'nc') => ({ file_id: id, filename: `${id}.${extension}`, upload_date: '', size: 200 });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); await vue.nextTick(); };
const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('Cordis catalog supports several independently selectable views for the same format', () => {
  const catalog = parseVisualizationCatalog(catalogOf(ncSeries(), ncMap()));
  assert.deepEqual(matchingVisualizations(catalog.plugins, 'DATA.NC').map((item) => item.id), ['netcdf-map', 'netcdf-series']);
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', null).id, 'netcdf-map');
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', 'netcdf-series').id, 'netcdf-series');
  catalog.plugins[1].enabled = false;
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', 'netcdf-map').id, 'netcdf-series');
});

test('stopped TIFF has no fallback and special filenames obey exactly the same enabled policy', () => {
  const molecular = descriptor({ id: 'molecular', adapter: 'molecular', view_kind: 'structure', extensions: ['cif'], filenames: ['POSCAR', 'CONTCAR'] });
  assert.equal(selectVisualization([molecular], 'POSCAR', null).id, 'molecular');
  assert.equal(selectVisualization([{ ...molecular, enabled: false }], 'POSCAR', null), null);
  assert.equal(selectVisualization([descriptor({ enabled: false })], 'a.TIF', null), null);
  assert.equal(selectVisualization([], 'a.png', null), null);
});

test('matching is deterministic, basename-scoped and never falls through to unrelated data', () => {
  const one = descriptor({ id: 'a', priority: 20 }), two = descriptor({ id: 'b', priority: 20 });
  assert.equal(selectVisualization([two, one], 'result.tif', null).id, 'a');
  assert.equal(selectVisualization([one], 'result.tif.js', null), null);
  assert.equal(selectVisualization([one], 'tif', null), null);
  assert.equal(selectVisualization([descriptor({ extensions: ['fastq.gz'] })], 'sample.fastq.gz', null).id, 'tiff');
});

test('every checked-in Cordis manifest is accepted by the frontend contract without reinterpretation', () => {
  const directory = new URL('../../plugin-host/visualizations/', import.meta.url);
  const plugins = readdirSync(directory).filter((name) => name.endsWith('.json')).map((name) => {
    const plugin = JSON.parse(readFileSync(new URL(name, directory), 'utf8'));
    return { ...plugin, enabled: plugin.default_enabled };
  });
  assert.equal(plugins.length, 36);
  assert.equal(plugins.filter(plugin => plugin.contract_version === 2).length, 36);
  assert.ok(plugins.every(plugin => !Object.hasOwn(plugin, 'data_kind') && !plugin.adapter.startsWith('v2-')));
  assert.deepEqual(parseVisualizationCatalog(catalogOf(...plugins)).plugins, plugins);
});

for (const [label, change] of [
  ['unknown contract', { contract_version: 3 }], ['obsolete contract', { contract_version: 1 }], ['remote adapter', { adapter: 'https://evil.example/script.js' }],
  ['write permission', { permissions: ['file:write'] }], ['wrong reader kind', { data_kind: 'scientific' }],
  ['wrong view', { view_kind: 'map' }], ['unbounded bytes', { limits: { max_input_bytes: -1, max_output_bytes: 1 } }],
]) test(`catalog fails closed for ${label}`, () => assert.throws(() => parseVisualizationCatalog(catalogOf(descriptor(change)))));

test('catalog rejects duplicate capability identities and untrusted engine', () => {
  assert.throws(() => parseVisualizationCatalog(catalogOf(descriptor(), descriptor())));
  assert.throws(() => parseVisualizationCatalog({ ...catalogOf(descriptor()), engine: 'legacy' }));
});

test('legacy renderer configs cannot execute arbitrary API/component entries or masquerade as images', () => {
  const registry = source('../src/renderers/registry.ts');
  assert.doesNotMatch(registry, /ImageFilePreview|preview:|apiClient|fetch\(/);
  const fileType = source('../src/utils/fileType.ts');
  assert.doesNotMatch(fileType, /FilePreview|findRendererByFilename|preview:/);
  const adapters = source('../src/visualizations/adapters.ts');
  assert.doesNotMatch(adapters.replace(/^\s*\/\/.*$/gm, ''), /import\((?:plugin|descriptor|manifest)|eval\(|new Function|api_url|iframe/);
});

test('plot preserves missing-value gaps, singleton points, and nonuniform map cell widths', () => {
  assert.deepEqual(seriesSegments([0, 1, 2, 3, 4], [2, null, 6, 7, null]), [[[0, 2]], [[2, 6], [3, 7]]]);
  assert.deepEqual(finiteExtent([null, 0, 10, NaN]), [0, 10]);
  assert.equal(finiteExtent([null, NaN]), null);
  assert.deepEqual(coordinateEdges([0, 10, 11]), [-5, 5, 10.5, 11.5]);
  assert.deepEqual(coordinateEdges([85, 75], -90, 90), [90, 80, 70]);
});

test('binary preview transfer enforces observed bytes even with a false Content-Length', async () => {
  let cancelled = false;
  const body = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(10)); controller.enqueue(new Uint8Array(10)); }, cancel() { cancelled = true; } });
  await assert.rejects(readBoundedBinary(new Response(body, { headers: { 'content-length': '1' } }), 12, new AbortController().signal), /上限/);
  assert.equal(cancelled, true);
});

test('binary preview transfer cancellation releases a blocked stream and never returns old data', async () => {
  const controller = new AbortController();
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const pending = readBoundedBinary(new Response(body), 12, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(cancelled, true);
});

test('TIFF dimensions and unpacked bytes are checked before any decompression', () => {
  assert.deepEqual(validateTiffDimensions({ t256: [100], t257: [100], t258: [8, 8, 8] }), { width: 100, height: 100 });
  assert.throws(() => validateTiffDimensions({ t256: [100000], t257: [100000] }), /安全/);
  assert.throws(() => validateTiffDimensions({ t256: [4096], t257: [4096], t258: [64, 64, 64] }), /安全/);
  assert.throws(() => validateTiffDimensions({ t256: [10], t257: [10], t258: Array(100).fill(8) }), /安全/);
  const component = source('../src/components/filePreviews/TiffFilePreview.vue');
  assert.ok(component.indexOf('validateTiffDimensions(ifds[0])') < component.indexOf('UTIF.decodeImage(buffer'));
});

function compileSfc(path, dependencies, inlineTemplate = false) {
  const descriptor = parse(source(path)).descriptor;
  const code = compileScript(descriptor, { id: 'visualization-test', inlineTemplate }).content;
  const js = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', js)((id) => {
    if (id === 'vue') return vue;
    assert.ok(id in dependencies, `Unexpected dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);
  return module.exports.default;
}

function mountScientific(t, getData, plugin = ncSeries()) {
  const component = compileSfc('../src/visualizations/ScientificFilePreview.vue', {
    '../api/visualization': { getScientificVisualization: (file, _plugin, options, signal) => getData(file.file_id, options, signal) },
    '../composables/usePreviewLoad': { usePreviewLoad },
    './plot': { coordinateEdges, finiteExtent, seriesSegments, heatColor: () => '', tickLabel: String },
    './assets/world-outline.json': { default: { geometry: { coordinates: [] } } },
  });
  const props = vue.reactive({ file: file('a'), plugin });
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup(props, { expose() {} }));
  t.after(() => scope.stop());
  return { props, state, unmount: () => scope.stop() };
}
const seriesResponse = (overrides = {}) => ({ kind: 'series', plugin_id: 'netcdf-series', revision: 'r', version: 'v1', reader: 'netcdf', variables: [{ name: 'temperature', dimensions: [{ name: 'time', size: 5 }, { name: 'station', size: 3 }], shape: [5, 3], units: 'K' }], selected_variable: 'temperature', x_label: 'time', y_label: 'temperature [K]', x: [0, 1], y: [1, 2], width: 0, height: 0, values: [], extent: null, metadata: { x_dimension: 'time', indices: { station: 0 } }, warnings: [], sampled: false, ...overrides });

for (const outcome of ['success', 'error']) test(`scientific plugin ignores late ${outcome} from a replaced file`, async (t) => {
  const old = deferred(), calls = [];
  const { props, state } = mountScientific(t, (id, options, signal) => { calls.push({ id, options, signal }); return id === 'a' ? old.promise : Promise.resolve(seriesResponse({ version: 'b' })); });
  props.file = file('b'); await flush();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(state.data.value.version, 'b');
  if (outcome === 'success') old.resolve(seriesResponse({ version: 'old' })); else old.reject(new Error('old'));
  await flush();
  assert.equal(state.data.value.version, 'b'); assert.equal(state.error.value, '');
});

test('scientific request cancellation on plugin disposal suppresses ignored-abort callbacks', async (t) => {
  const result = deferred(); let signal;
  const preview = mountScientific(t, (_id, _options, value) => { signal = value; return result.promise; });
  preview.unmount(); assert.equal(signal.aborted, true);
  result.resolve(seriesResponse()); await flush(); assert.equal(preview.state.data.value, null);
});

test('scientific axis and slice controls exclude the x axis and pin the file version', async (t) => {
  const calls = [];
  const { state } = mountScientific(t, async (_id, options) => { calls.push(options); return seriesResponse(); });
  await flush(); state.indices.value.station = 2; state.indices.value.time = 4;
  await state.load(false);
  assert.deepEqual(calls.at(-1), { plugin_id: 'netcdf-series', variable: 'temperature', x_dimension: 'time', indices: { station: 2 }, version: 'v1' });
  await state.load(true); assert.equal(calls.at(-1).version, undefined);
});

test('FITS HDU selection never sends NetCDF variable options or stale slice indices', async (t) => {
  const calls = [];
  const { state } = mountScientific(t, async (_id, options) => { calls.push(options); return seriesResponse({ plugin_id: 'fits-series', reader: 'fits', selected_variable: 'HDU 0', variables: [{ name: 'HDU 0', dimensions: [{ name: 'axis0', size: 3 }], shape: [3] }], metadata: { hdu: 0, hdus: [{ index: 0, shape: [3] }, { index: 1, shape: [5] }], x_dimension: 'axis0' } }); }, descriptor({ id: 'fits-series', adapter: 'scientific-series', view_kind: 'series', reader: 'fits' }));
  await flush(); state.hdu.value = 1; state.changeHdu(); await state.load(false);
  assert.deepEqual(calls.at(-1), { plugin_id: 'fits-series', hdu: 1, version: 'v1' });
});

test('plugin host actually unmounts old adapters on view switch, disable, file switch and close', async (t) => {
  const oldWindow = globalThis.window;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  t.after(() => { if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow; });
  const catalog = vue.ref(catalogOf(ncMap(), ncSeries()));
  const error = vue.ref('');
  const instances = [];
  const Adapter = vue.defineComponent({ props: ['file', 'plugin'], setup(props) {
    const item = { file: props.file.file_id, plugin: props.plugin.id, disposed: false };
    instances.push(item); vue.onScopeDispose(() => { item.disposed = true; }); return () => vue.h('canvas');
  } });
  const Host = compileSfc('../src/visualizations/VisualizationHost.vue', {
    'vue-router': { useRoute: () => ({ path: '/chat/1' }), RouterLink: { render: () => vue.h('a') } },
    './catalog': { useVisualizationCatalog: () => ({ catalog, error, loading: vue.ref(false), refresh: async () => {} }) },
    './contract': { matchingVisualizations, selectVisualization, viewKindLabel: (value) => value },
    './adapters': { getVisualizationAdapter: () => Adapter },
  }, true);
  const renderer = vue.createRenderer({
    createElement: (type) => ({ type, children: [], props: {} }), createText: (text) => ({ text }), createComment: (text) => ({ text }),
    insert: (node, parent) => { parent.children ??= []; parent.children.push(node); node.parent = parent; },
    remove: (node) => { if (node.parent) node.parent.children = node.parent.children.filter((value) => value !== node); },
    setElementText: (node, text) => { node.text = text; }, setText: (node, text) => { node.text = text; },
    parentNode: (node) => node.parent, nextSibling: () => null, patchProp: (node, key, _old, value) => { node.props[key] = value; },
  });
  const shown = vue.ref(true), currentFile = vue.ref(file('a'));
  const Root = { setup: () => () => shown.value ? vue.h(Host, { file: currentFile.value }) : null };
  const container = { children: [] }; const app = renderer.createApp(Root); app.mount(container); t.after(() => app.unmount());
  await flush(); assert.equal(instances[0].plugin, 'netcdf-map');
  function find(node, type) { if (node.type === type) return node; for (const child of node.children ?? []) { const found = find(child, type); if (found) return found; } }
  find(container, 'select').props.onChange({ target: { value: 'netcdf-series' } }); await flush();
  assert.equal(instances[0].disposed, true); assert.equal(instances.at(-1).plugin, 'netcdf-series');
  catalog.value.plugins[1].enabled = false; await flush(); assert.equal(instances[1].disposed, true); assert.equal(instances.at(-1).plugin, 'netcdf-map');
  currentFile.value = file('b'); await flush(); assert.equal(instances[2].disposed, true); assert.equal(instances.at(-1).file, 'b');
  catalog.value.plugins[0].enabled = false; await flush(); assert.equal(instances.at(-1).disposed, true);
  catalog.value.plugins[0].enabled = true; await flush();
  error.value = 'runtime unavailable'; catalog.value = null; await flush(); assert.equal(instances.at(-1).disposed, true);
  error.value = ''; catalog.value = catalogOf(ncMap()); await flush();
  shown.value = false; await flush(); assert.equal(instances.at(-1).disposed, true);
});

function catalogStore(api) {
  const code = ts.transpileModule(source('../src/visualizations/catalog.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)((id) => id === 'vue' ? vue : api, module, module.exports);
  return module.exports.useVisualizationCatalog();
}

test('authoritative plugin state survives a late pre-toggle poll and network failure fails closed', async () => {
  const old = deferred(), stateResponse = deferred(); let readCount = 0;
  const store = catalogStore({ getVisualizationCatalog: () => { readCount++; return old.promise; }, setVisualizationEnabled: () => stateResponse.promise });
  const pendingPoll = store.refresh();
  const pendingStop = store.toggle('tiff', false);
  await store.refresh(); assert.equal(readCount, 1, 'polling cannot race an in-flight mutation');
  stateResponse.resolve(catalogOf(descriptor({ enabled: false }))); await pendingStop;
  old.resolve(catalogOf(descriptor({ enabled: true }))); await pendingPoll;
  assert.equal(store.catalog.value.plugins[0].enabled, false);
  const failure = catalogStore({ getVisualizationCatalog: async () => { throw new Error('network'); } });
  await failure.refresh(); assert.equal(failure.catalog.value, null); assert.match(failure.error.value, /暂停/);
});

test('a failed state confirmation never leaves a previously active renderer running', async () => {
  const store = catalogStore({ getVisualizationCatalog: async () => catalogOf(descriptor()), setVisualizationEnabled: async () => { throw new Error('unknown'); } });
  await store.refresh(); assert.equal(store.catalog.value.plugins[0].enabled, true);
  await store.toggle('tiff', false);
  assert.equal(store.catalog.value, null); assert.match(store.error.value, /未能确认/);
});

test('file preview host is destroyed when hidden, and signed share scientific readers stay closed', () => {
  const panel = source('../src/components/FilePanel.vue');
  assert.match(panel, /v-if="isShow && visible && fileInfo && fileType"/);
  assert.match(panel, /<VisualizationHost :key="fileInfo.file_id"/);
  const host = source('../src/visualizations/VisualizationHost.vue');
  assert.match(host, /shared.value && !plugin.capabilities.shared/);
  assert.match(host, /props.file.size > plugin.limits.max_input_bytes/);
  assert.match(host, /plugin.capabilities.input_mode === 'whole'/);
  assert.doesNotMatch(host, /data_kind|startsWith\('v2-'/);
  assert.match(host, /catalog.value\?\.revision/);
  assert.doesNotMatch(host, /localStorage|sessionStorage|window\.open\(.*plugin|fileType.preview/);
});
