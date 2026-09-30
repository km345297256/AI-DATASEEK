import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import { matchingVisualizations } from '../src/visualizations/contract.ts';

const source = readFileSync(new URL('../src/components/AnalysisResultImage.vue', import.meta.url), 'utf8');
const descriptor = JSON.parse(readFileSync(new URL('../../plugin-host/visualizations/svg.json', import.meta.url), 'utf8'));
const file = (id = 'first', extension = 'svg') => ({ file_id: id, filename: `${id}.${extension}`, content_type: extension === 'svg' ? 'image/svg+xml' : 'image/png', upload_date: '2026-09-30', file_url: `/protected-download/${id}.${extension}` });
const catalog = () => ({ engine: 'cordis', revision: 'a'.repeat(64), plugins: [{ ...structuredClone(descriptor), enabled: true }] });
const bitmap = () => ({ blob: new Blob(['isolated renderer PNG'], { type: 'image/png' }), width: 100, height: 50, warnings: [] });
const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function mount(t, options = {}) {
  const requests = [], mounted = [];
  const registry = {
    catalog: vue.ref(options.catalog === undefined ? catalog() : options.catalog),
    error: vue.ref(options.error || ''),
    refresh: t.mock.fn(async () => true),
  };
  const modules = {
    vue: { ...vue, onMounted: action => mounted.push(action) },
    '../api/client': { API_CONFIG: { host: 'https://local.invalid' } },
    '../composables/usePreviewLoad': { usePreviewLoad },
    '../visualizations/previewIdentity': identity,
    '../visualizations/catalog': { useVisualizationCatalog: () => registry },
    '../visualizations/contract': { matchingVisualizations },
    '../visualizations/svgPreview': { loadSvgPreview: (value, plugin, signal) => {
      requests.push({ file: structuredClone(vue.toRaw(value)), plugin: structuredClone(vue.toRaw(plugin)), signal });
      return (options.render || (async () => bitmap()))(value, plugin, signal);
    } },
  };
  const script = compileScript(parse(source).descriptor, { id: 'analysis-result-image' }).content;
  const compiled = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(name => {
    assert.ok(name in modules, `Unexpected component dependency ${name}`);
    return modules[name];
  }, module, module.exports);
  const props = vue.reactive({ file: options.file || file() });
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  state.container.value = {};
  mounted.forEach(action => action());
  t.after(() => scope.stop());
  return { props, state, scope, registry, requests };
}

function urls(t) {
  let identity = 0;
  const created = t.mock.method(URL, 'createObjectURL', value => {
    assert.equal(value.type, 'image/png');
    return `blob:result-image-${++identity}`;
  });
  const revoked = t.mock.method(URL, 'revokeObjectURL', () => {});
  return { created, revoked };
}

test('SVG result card uses safe PNG and never its raw signed URL', async t => {
  urls(t);
  const view = mount(t);
  assert.equal(view.state.source.value, '');
  await flush();
  assert.equal(view.state.source.value, 'blob:result-image-1');
  assert.equal(view.requests.length, 1);
  assert.equal(view.requests[0].plugin.id, 'viz-svg');
  assert.equal(view.registry.refresh.mock.callCount(), 1);
  assert.equal(view.state.failed.value, false);
});

for (const cause of ['disabled', 'missing', 'failed-catalog']) {
  test(`SVG ${cause} never falls back to a raw download image`, async t => {
    const resources = urls(t);
    const config = catalog();
    if (cause === 'disabled') config.plugins[0].enabled = false;
    if (cause === 'missing') config.plugins = [];
    const view = mount(t, { catalog: cause === 'failed-catalog' ? null : config, error: cause === 'failed-catalog' ? '目录确认失败，预览暂停' : '' });
    await flush();
    assert.equal(view.requests.length, 0);
    assert.equal(resources.created.mock.callCount(), 0);
    assert.equal(view.state.source.value, '');
    assert.match(view.state.status.value, cause === 'failed-catalog' ? /目录确认失败/ : /未启用/);
  });
}

test('poll-cloned descriptors and rotated SVG download URLs retain the existing bitmap', async t => {
  const resources = urls(t);
  const view = mount(t);
  await flush();
  view.props.file = { ...structuredClone(vue.toRaw(view.props.file)), file_url: '/signed/new-token.svg' };
  view.registry.catalog.value = structuredClone(vue.toRaw(view.registry.catalog.value));
  await flush();
  assert.equal(view.requests.length, 1);
  assert.equal(resources.revoked.mock.callCount(), 0);
  assert.equal(view.state.source.value, 'blob:result-image-1');
});

for (const cause of ['file', 'content', 'disable', 'catalog-failure']) {
  test(`result ${cause} replacement cancels and revokes the owned SVG preview`, async t => {
    const resources = urls(t);
    const pending = deferred();
    let count = 0;
    const view = mount(t, { render: async () => ++count === 1 ? bitmap() : pending.promise });
    await flush();
    const first = view.requests[0];
    if (cause === 'file') view.props.file = file('second');
    if (cause === 'content') view.props.file.metadata = { content_sha256: 'b'.repeat(64) };
    if (cause === 'disable') view.registry.catalog.value.plugins[0].enabled = false;
    if (cause === 'catalog-failure') { view.registry.catalog.value = null; view.registry.error.value = '目录无法确认'; }
    await flush();
    assert.equal(first.signal.aborted, true);
    assert.equal(view.state.source.value, '');
    assert.deepEqual(resources.revoked.mock.calls.map(call => call.arguments[0]), ['blob:result-image-1']);
    if (['file', 'content'].includes(cause)) { pending.resolve(bitmap()); await flush(); assert.equal(view.state.source.value, 'blob:result-image-2'); }
    else assert.equal(view.requests.length, 1);
  });
}

for (const settlement of ['success', 'failure']) {
  test(`late SVG thumbnail ${settlement} cannot alter the next result card`, async t => {
    const resources = urls(t);
    const old = deferred();
    const view = mount(t, { render: value => value.file_id === 'first' ? old.promise : Promise.resolve(bitmap()) });
    view.props.file = file('second');
    await flush();
    if (settlement === 'success') old.resolve(bitmap()); else old.reject(new Error('old failure'));
    await flush();
    assert.equal(view.requests[0].signal.aborted, true);
    assert.equal(view.state.source.value, 'blob:result-image-1');
    assert.equal(view.state.failed.value, false);
    assert.equal(resources.created.mock.callCount(), 1);
  });
}

test('unmount aborts pending SVG rendering and releases a completed bitmap', async t => {
  const resources = urls(t);
  const first = mount(t);
  await flush();
  first.scope.stop();
  assert.equal(first.requests[0].signal.aborted, true);
  assert.deepEqual(resources.revoked.mock.calls.map(call => call.arguments[0]), ['blob:result-image-1']);
  const pending = deferred();
  const second = mount(t, { render: () => pending.promise });
  second.scope.stop();
  pending.resolve(bitmap());
  await flush();
  assert.equal(second.requests[0].signal.aborted, true);
  assert.equal(resources.created.mock.callCount(), 1);
});

test('old image element errors cannot mark a replacement image as failed', async t => {
  urls(t);
  const view = mount(t);
  await flush();
  view.props.file = file('second');
  await flush();
  view.state.imageFailed({ target: { getAttribute: () => 'blob:result-image-1' } });
  assert.equal(view.state.failed.value, false);
  view.state.imageFailed({ target: { getAttribute: () => 'blob:result-image-2' } });
  assert.equal(view.state.failed.value, true);
  assert.match(view.state.status.value, /点击查看或下载/);
});

test('ordinary PNG cards retain the existing URL path without requesting SVG capabilities', async t => {
  const resources = urls(t);
  const view = mount(t, { file: file('pixels', 'png') });
  await flush();
  assert.equal(view.state.source.value, 'https://local.invalid/protected-download/pixels.png');
  assert.equal(view.requests.length, 0);
  assert.equal(view.registry.refresh.mock.callCount(), 0);
  view.props.file.file_url = 'https://download.invalid/pixels.png';
  await flush();
  assert.equal(view.state.source.value, 'https://download.invalid/pixels.png');
  assert.equal(resources.created.mock.callCount(), 0);
});

test('offscreen SVG rendering is deferred until intersection and the observer is disposed', async t => {
  urls(t);
  let observer;
  const old = Object.getOwnPropertyDescriptor(globalThis, 'IntersectionObserver');
  class Observer {
    constructor(callback) { this.callback = callback; this.disconnects = 0; observer = this; }
    observe() {}
    disconnect() { this.disconnects++; }
  }
  Object.defineProperty(globalThis, 'IntersectionObserver', { configurable: true, value: Observer });
  t.after(() => old ? Object.defineProperty(globalThis, 'IntersectionObserver', old) : delete globalThis.IntersectionObserver);
  const view = mount(t);
  await flush();
  assert.equal(view.requests.length, 0);
  assert.equal(view.registry.refresh.mock.callCount(), 0);
  observer.callback([{ isIntersecting: true }]);
  await flush();
  assert.equal(view.requests.length, 1);
  assert.equal(view.state.source.value, 'blob:result-image-1');
  assert.equal(observer.disconnects, 1);
  view.scope.stop();
  assert.equal(observer.disconnects, 2);
});
