import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';

const source = readFileSync(new URL('../src/components/filePreviews/SvgFilePreview.vue', import.meta.url), 'utf8');
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const pending = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const file = (id = 'first') => ({ file_id: id, filename: `${id}.svg`, content_type: 'image/svg+xml', size: 300, upload_date: '2026-09-30' });
const plugin = () => ({
  contract_version: 2, id: 'viz-svg', version: '1.0.0', name: 'SVG 图表', description: '安全静态预览',
  extensions: ['svg'], filenames: [], view_kind: 'image', adapter: 'svg', reader: 'svg',
  capabilities: { operations: ['preview'], input_mode: 'whole', shared: false },
  default_enabled: true, enabled: true, priority: 100, permissions: ['file:read'],
  limits: { max_input_bytes: 8 * 1024 * 1024, max_output_bytes: 8 * 1024 * 1024 },
});
const result = () => ({ blob: new Blob(['validated PNG pixels'], { type: 'image/png' }), width: 800, height: 400, warnings: [] });

function mount(t, implementation = async () => result()) {
  const requests = [];
  const modules = {
    vue,
    '../../composables/usePreviewLoad': { usePreviewLoad },
    '../../visualizations/previewIdentity': identity,
    '../../visualizations/svgPreview': { loadSvgPreview: async (file, plugin, signal) => {
      requests.push({ file: structuredClone(vue.toRaw(file)), plugin: structuredClone(vue.toRaw(plugin)), signal });
      if (!plugin.enabled) throw new Error('可视化插件未启用。');
      return implementation(file, plugin, signal);
    } },
  };
  const compiled = compileScript(parse(source).descriptor, { id: 'svg-file-preview' });
  const script = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', script)(name => {
    assert.ok(name in modules, `Unexpected component dependency ${name}`);
    return modules[name];
  }, module, module.exports);
  const props = vue.reactive({ file: file(), plugin: plugin() });
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  t.after(() => scope.stop());
  return { scope, props, state, requests };
}

function urls(t) {
  let id = 0;
  const created = t.mock.method(URL, 'createObjectURL', blob => {
    assert.equal(blob.type, 'image/png');
    return `blob:svg-static-${++id}`;
  });
  const revoked = t.mock.method(URL, 'revokeObjectURL', () => {});
  return { created, revoked };
}

test('SVG preview uses the static PNG capability and preserves state across poll clones', async t => {
  const resources = urls(t);
  const view = mount(t);
  await flush();
  assert.equal(view.requests.length, 1);
  assert.equal(view.state.imageUrl.value, 'blob:svg-static-1');
  assert.equal(view.state.width.value, 800);
  view.state.changeZoom(1);
  view.state.background.value = 'checkerboard';
  view.props.file = { ...structuredClone(vue.toRaw(view.props.file)), file_url: 'https://example.invalid/rotated-signature' };
  view.props.plugin = structuredClone(vue.toRaw(view.props.plugin));
  await flush();
  assert.equal(view.requests.length, 1);
  assert.equal(view.state.zoom.value, 1.25);
  assert.equal(view.state.fitWidth.value, false);
  assert.equal(view.state.background.value, 'checkerboard');
  assert.equal(resources.revoked.mock.callCount(), 0);
});

for (const cause of ['file', 'content', 'plugin', 'disable']) {
  test(`SVG ${cause} change aborts the owned request and drops its image`, async t => {
    const resources = urls(t);
    const next = pending();
    let count = 0;
    const view = mount(t, async () => ++count === 1 ? result() : next.promise);
    await flush();
    const originalRequest = view.requests[0];
    if (cause === 'file') view.props.file = file('second');
    if (cause === 'content') view.props.file.metadata = { sha256: 'b'.repeat(64) };
    if (cause === 'plugin') view.props.plugin.version = '1.1.0';
    if (cause === 'disable') view.props.plugin.enabled = false;
    await flush();
    assert.equal(originalRequest.signal.aborted, true);
    assert.equal(view.state.imageUrl.value, '');
    assert.deepEqual(resources.revoked.mock.calls.map(item => item.arguments[0]), ['blob:svg-static-1']);
    if (cause === 'disable') assert.match(view.state.error.value, /未启用/);
    else { next.resolve(result()); await flush(); assert.equal(view.state.imageUrl.value, 'blob:svg-static-2'); }
  });
}

for (const settlement of ['success', 'failure']) {
  test(`late SVG ${settlement} cannot replace a newer file`, async t => {
    const resources = urls(t);
    const old = pending();
    const view = mount(t, value => value.file_id === 'first' ? old.promise : Promise.resolve(result()));
    view.props.file = file('second');
    await flush();
    assert.equal(view.state.imageUrl.value, 'blob:svg-static-1');
    if (settlement === 'success') old.resolve(result()); else old.reject(new Error('stale error'));
    await flush();
    assert.equal(view.state.imageUrl.value, 'blob:svg-static-1');
    assert.equal(view.state.error.value, '');
    assert.equal(resources.created.mock.callCount(), 1);
    assert.equal(view.requests[0].signal.aborted, true);
  });
}

test('unmount releases SVG bitmap resources and ignored cancellation cannot leak a URL', async t => {
  const resources = urls(t);
  const first = mount(t);
  await flush();
  first.scope.stop();
  assert.equal(first.requests[0].signal.aborted, true);
  assert.deepEqual(resources.revoked.mock.calls.map(item => item.arguments[0]), ['blob:svg-static-1']);
  const late = pending();
  const second = mount(t, () => late.promise);
  second.scope.stop();
  late.resolve(result());
  await flush();
  assert.equal(resources.created.mock.callCount(), 1);
  assert.equal(second.state.imageUrl.value, '');
});

test('zoom, fit, reset and background are local controls and do not re-render SVG', async t => {
  urls(t);
  const view = mount(t);
  await flush();
  view.state.viewport.value = { clientWidth: 432, scrollLeft: 25, scrollTop: 25 };
  view.state.changeZoom(1);
  assert.equal(view.state.zoom.value, 0.63); // Fit width was 400/800 before zooming.
  for (let i = 0; i < 30; i++) view.state.changeZoom(1);
  assert.equal(view.state.zoom.value, 4);
  for (let i = 0; i < 30; i++) view.state.changeZoom(-1);
  assert.equal(view.state.zoom.value, 0.1);
  view.state.fitToWidth();
  assert.equal(view.state.fitWidth.value, true);
  assert.equal(view.state.viewport.value.scrollLeft, 0);
  view.state.resetView();
  assert.equal(view.state.zoom.value, 1);
  assert.equal(view.state.fitWidth.value, false);
  view.state.background.value = 'checkerboard';
  await flush();
  assert.equal(view.requests.length, 1);
});

test('image failure is current-URL scoped and explicit retry owns a fresh bitmap', async t => {
  const resources = urls(t);
  const view = mount(t);
  await flush();
  view.state.onImageError({ target: { getAttribute: () => 'blob:obsolete' } });
  assert.equal(view.state.error.value, '');
  view.state.onImageError({ target: { getAttribute: () => 'blob:svg-static-1' } });
  assert.match(view.state.error.value, /未能显示/);
  assert.equal(view.state.imageUrl.value, '');
  await view.state.loadPreview();
  assert.equal(view.state.error.value, '');
  assert.equal(view.state.imageUrl.value, 'blob:svg-static-2');
  assert.equal(view.requests.length, 2);
  assert.deepEqual(resources.revoked.mock.calls.map(item => item.arguments[0]), ['blob:svg-static-1']);
});

test('SVG preview does not embed source markup or replace the original vector download', () => {
  assert.doesNotMatch(source, /v-html|innerHTML|<iframe|<object|<embed|image\/svg\+xml|file_url|getFileDownloadUrl/);
  assert.match(source, /原始 SVG 矢量文件/);
  assert.match(source, /安全静态预览/);
  assert.match(source, /透明棋盘/);
  const adapters = readFileSync(new URL('../src/visualizations/adapters.ts', import.meta.url), 'utf8');
  assert.match(adapters, /svg:.*SvgFilePreview\.vue/);
});
