import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';
import * as vue from 'vue';
import { matchingVisualizations } from '../src/visualizations/contract.ts';
import { useFilePanel } from '../src/composables/useFilePanel.ts';

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const descriptor = (id, extensions, overrides = {}) => ({ id, name: id, extensions, filenames: [], enabled: true, priority: 10, ...overrides });
const ncMap = descriptor('netcdf-map', ['nc'], { priority: 20 });
const ncSeries = descriptor('netcdf-series', ['nc']);
const catalogOf = (...plugins) => ({ engine: 'cordis', revision: 'revision1', plugins });
const file = (path) => ({ name: path.split('/').pop(), path, size: 500, role: 'data' });
const datasetOf = (id = 'dataset-a') => ({ dataset_id: id, files: [file('folder/weather.NC'), file('samples/plain.fastq'), file('unsupported.fastq.gz'), file('POSCAR')] });
const prepared = (id = 'opaque-a') => ({ file: { file_id: id, filename: 'weather.NC', size: 500, upload_date: '' }, related_files: [{ file_id: 'opaque-sidecar', filename: 'weather.prj', upload_date: '' }] });

function mount(t, prepare = async () => prepared()) {
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  const listeners = new Map();
  globalThis.window = { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: (name) => listeners.delete(name) };
  globalThis.document = { visibilityState: 'visible' };
  const catalog = vue.shallowRef(catalogOf(ncMap, ncSeries, descriptor('fastq-quality', ['fastq', 'fq']), descriptor('molecular', [], { filenames: ['POSCAR'] })));
  const error = vue.ref(''), loading = vue.ref(false), dataset = vue.ref(datasetOf());
  const calls = [], errors = [], mounted = [];
  let refreshCount = 0, opened = 0;
  const dependencies = {
    vue: { ...vue, onMounted: (callback) => mounted.push(callback) },
    '../api/dataset': { prepareDatasetFilePreview: (...args) => { calls.push(args); return prepare(...args); } },
    '../visualizations/catalog': { useVisualizationCatalog: () => ({ catalog, error, loading, refresh: async () => { refreshCount++; } }) },
    '../visualizations/contract': { matchingVisualizations },
    './useFilePanel': { useFilePanel },
    '../utils/toast': { showErrorToast: (message) => errors.push(message) },
  };
  const js = ts.transpileModule(source('../src/composables/useDatasetFilePreview.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', js)((id) => { assert.ok(id in dependencies, `Unexpected dependency ${id}`); return dependencies[id]; }, module, module.exports);
  const panel = useFilePanel(); panel.hideFilePanel();
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.useDatasetFilePreview(() => dataset.value, () => opened++));
  mounted.forEach((callback) => callback());
  t.after(() => {
    scope.stop(); panel.hideFilePanel();
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
  });
  return { state, catalog, dataset, error, panel, calls, errors, listeners, stop: () => scope.stop(), opened: () => opened, refreshCount: () => refreshCount };
}

test('dataset preview availability comes from enabled Cordis capabilities, including multiple views and special names', (t) => {
  const view = mount(t);
  assert.deepEqual(view.state.candidates.value.get('folder/weather.NC').map((plugin) => plugin.id), ['netcdf-map', 'netcdf-series']);
  assert.equal(view.state.candidates.value.get('unsupported.fastq.gz').length, 0);
  assert.equal(view.state.candidates.value.get('POSCAR')[0].id, 'molecular');
  view.catalog.value = catalogOf({ ...ncMap, enabled: false }, { ...ncSeries, enabled: false });
  assert.equal(view.state.candidates.value.get('folder/weather.NC').length, 0);
  assert.equal(view.calls.length, 0, 'listing files never reads source bytes or prepares IDs');
});

test('click prepares an exact registered logical path and opens the existing panel with companion files', async (t) => {
  const view = mount(t);
  await view.state.preview('folder/weather.NC');
  assert.deepEqual(view.calls[0].slice(0, 3), ['dataset-a', 'folder/weather.NC', 'netcdf-map']);
  assert.ok(view.calls[0][3] instanceof AbortSignal);
  assert.equal(view.panel.isShow.value, true);
  assert.equal(view.panel.fileInfo.value.file_id, 'opaque-a');
  assert.deepEqual(view.panel.relatedFiles.value, prepared().related_files);
  assert.equal(view.state.pendingPath.value, null);
  assert.equal(view.opened(), 1);
});

test('unsupported, unregistered and disabled files never prepare a file', async (t) => {
  const view = mount(t);
  await view.state.preview('unsupported.fastq.gz');
  await view.state.preview('../private.nc');
  view.catalog.value = catalogOf({ ...ncMap, enabled: false });
  await view.state.preview('folder/weather.NC');
  assert.equal(view.calls.length, 0);
});

test('duplicate pending clicks are coalesced and a newer file cancels and supersedes the previous request', async (t) => {
  const old = deferred();
  const view = mount(t, (_id, path) => path.endsWith('NC') ? old.promise : Promise.resolve(prepared('opaque-new')));
  const first = view.state.preview('folder/weather.NC');
  await view.state.preview('folder/weather.NC');
  assert.equal(view.calls.length, 1);
  await view.state.preview('samples/plain.fastq');
  assert.equal(view.calls[0][3].aborted, true);
  old.resolve(prepared('opaque-old')); await first;
  assert.equal(view.panel.fileInfo.value.file_id, 'opaque-new');
  assert.equal(view.opened(), 1);
});

for (const transition of ['close', 'other-file', 'dataset', 'dispose', 'revision', 'disabled', 'unavailable']) {
  test(`late preview cannot reopen or replace a panel after ${transition}`, async (t) => {
    const pending = deferred();
    const view = mount(t, () => pending.promise);
    const promise = view.state.preview('folder/weather.NC');
    if (transition === 'close') view.panel.hideFilePanel();
    if (transition === 'other-file') view.panel.showFilePanel(prepared('user-selected').file);
    if (transition === 'dataset') view.dataset.value = datasetOf('dataset-b');
    if (transition === 'dispose') view.stop();
    if (transition === 'revision') view.catalog.value = { ...view.catalog.value, revision: 'revision2' };
    if (transition === 'disabled') view.catalog.value = catalogOf({ ...ncMap, enabled: false }, ncSeries);
    if (transition === 'unavailable') view.catalog.value = null;
    if (['dataset', 'dispose', 'revision', 'disabled', 'unavailable'].includes(transition)) assert.equal(view.calls[0][3].aborted, true);
    pending.resolve(prepared()); await promise;
    assert.equal(view.opened(), 0);
    assert.equal(view.panel.fileInfo.value?.file_id, transition === 'other-file' ? 'user-selected' : undefined);
    assert.equal(view.state.pendingPath.value, null);
    if (transition === 'disabled') assert.deepEqual(view.state.candidates.value.get('folder/weather.NC').map((item) => item.id), ['netcdf-series']);
  });
}

test('a same-revision catalog poll does not cancel a valid pending click', async (t) => {
  const pending = deferred(); const view = mount(t, () => pending.promise);
  const promise = view.state.preview('folder/weather.NC');
  view.catalog.value = catalogOf(ncMap, ncSeries);
  assert.equal(view.calls[0][3].aborted, false);
  pending.resolve(prepared()); await promise;
  assert.equal(view.opened(), 1);
});

test('active failures are shown, cleared loading permits retry, stale failures stay silent', async (t) => {
  const pending = deferred(); let attempt = 0;
  const view = mount(t, () => ++attempt === 1 ? Promise.reject(new Error('文件已不可用')) : pending.promise);
  await view.state.preview('folder/weather.NC');
  assert.deepEqual(view.errors, ['文件已不可用']);
  assert.equal(view.state.pendingPath.value, null);
  const promise = view.state.preview('folder/weather.NC');
  view.stop(); pending.reject(new Error('过期请求')); await promise;
  assert.deepEqual(view.errors, ['文件已不可用']);
});

test('catalog refresh is page-scoped, focus-aware, hidden-page-safe and cleaned on disposal', (t) => {
  const view = mount(t);
  assert.equal(view.refreshCount(), 1);
  assert.equal(view.listeners.size, 1);
  view.listeners.get('focus')(); assert.equal(view.refreshCount(), 2);
  globalThis.document.visibilityState = 'hidden';
  view.listeners.get('focus')(); assert.equal(view.refreshCount(), 2);
  view.stop(); assert.equal(view.listeners.size, 0);
});

test('dataset file tree has an accessible always-visible preview control, without changing copy or directory actions', () => {
  const page = source('../src/pages/DatasetSeekPage.vue');
  assert.match(page, /v-if="node\.file && datasetPreview\.candidates/);
  assert.match(page, /:aria-label="`预览文件 \$\{node\.path\}`"/);
  assert.match(page, /@click\.stop="datasetPreview\.preview\(node\.file\.path \|\| node\.file\.name\)"/);
  assert.match(page, /@click\.stop="copyDatasetFilePath\(node\.path\)"/);
  assert.match(page, /@click="toggleDirectory\(node\.path\)"/);
  assert.doesNotMatch(source('../src/composables/useDatasetFilePreview.ts'), /createSession|chatWithSession|localStorage|sessionStorage|storage_directory/);
});
