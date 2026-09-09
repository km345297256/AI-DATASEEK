import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import { readBoundedBinary, validateTiffDimensions } from '../src/visualizations/boundedBinary.ts';

const file = (id, extension = 'txt') => ({ file_id: id, filename: `${id}.${extension}`, upload_date: '' });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };

// Execute the actual SFC setup with real Vue refs/watch/effect scopes. This
// deliberately allows ignored aborts and late response bodies to settle.
function mountPreview(t, name, options = {}) {
  const source = readFileSync(new URL(`../src/components/filePreviews/${name}.vue`, import.meta.url), 'utf8');
  const script = compileScript(parse(source).descriptor, { id: name });
  const compiled = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const relatedFiles = options.relatedFiles || vue.ref([]);
  const loadBytes = options.loadBytes || (async (value, _plugin, signal, resource) => {
    const url = await (options.getUrl || (async value => value.file_id))(resource || value);
    if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    const response = await fetch(url, { signal });
    return response.arrayBuffer ? response.arrayBuffer() : (await response.blob()).arrayBuffer();
  });
  const dependencies = {
    vue,
    '../../api/file': { getFileDownloadUrl: options.getUrl || (async (value) => value.file_id) },
    '../../visualizations/runtime': { loadPluginBytes: loadBytes, loadPluginText: options.loadText || (async (value, _plugin, signal) => {
      const response = await fetch(value.file_id, { signal }); return response.text();
    }) },
    '../../api/agent': { getSessionFiles: options.getFiles || (async () => []), getSharedSessionFiles: options.getFiles || (async () => []) },
    '../../composables/usePreviewLoad': { usePreviewLoad },
    '../../visualizations/boundedBinary': { readBoundedBinary, validateTiffDimensions },
    '../../composables/useFilePanel': { useFilePanel: () => ({ relatedFiles }) },
    '../../composables/useSessionFileList': { useSessionFileList: () => ({ shared: vue.ref(false) }) },
    '../../utils/relativeFileResources': {
      isRelativeResourceUrl: (value) => !/^(?:https?:|data:|blob:|#)/.test(value),
      splitResourceUrl: () => ({ suffix: '' }),
      findRelatedFile: (_base, files, value) => files.find((candidate) => candidate.filename === value),
    },
    'vue-router': { useRoute: () => ({ params: { sessionId: 'session-a' }, path: '/chat/session-a' }) },
    'vue-i18n': { useI18n: () => ({ t: (value) => value }) },
    marked: { marked: { setOptions() {}, parse: (value) => value } },
    dompurify: { default: { sanitize: (value) => value } },
    '@/components/ui/MonacoEditor.vue': { default: {} },
  };
  const pagesModule = { exports: {} };
  const pagesSource = readFileSync(new URL('../src/composables/useFilePreviewPages.ts', import.meta.url), 'utf8');
  const pagesScript = ts.transpileModule(pagesSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  new Function('require', 'module', 'exports', pagesScript)((id) => {
    if (id === 'vue') return vue;
    if (id === './usePreviewLoad.ts') return { usePreviewLoad };
    if (id === '../visualizations/runtime') return { getPluginPage: (file, _plugin, params, signal) => (options.getPage || (async () => assert.fail('Unexpected preview request')))(file.file_id, { ...params, signal }) };
    assert.fail(`Unexpected page dependency ${id}`);
  }, pagesModule, pagesModule.exports);
  dependencies['../../composables/useFilePreviewPages'] = pagesModule.exports;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)((id) => {
    assert.ok(id in dependencies, `Unexpected setup dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);
  const props = vue.reactive({ file: file('a', options.extension), plugin: { id: 'test-plugin' } });
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  t.after(() => scope.stop());
  return { state, props, relatedFiles, unmount: () => scope.stop(), async select(id) { props.file = file(id, options.extension); await vue.nextTick(); } };
}

test('preview scope aborts replaced/disposed loads and releases only owned resources', () => {
  const scope = vue.effectScope();
  const loads = scope.run(usePreviewLoad);
  const first = loads.begin();
  const disposed = [];
  first.onDispose(() => disposed.push('a'));
  const second = loads.begin();
  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  first.onDispose(() => disposed.push('late-a'));
  second.onDispose(() => disposed.push('b'));
  assert.deepEqual(disposed, ['a', 'late-a']);
  assert.throws(first.assertCurrent, { name: 'AbortError' });
  scope.stop();
  assert.equal(second.signal.aborted, true);
  assert.deepEqual(disposed, ['a', 'late-a', 'b']);
  assert.equal(loads.begin().isCurrent(), false);
});

for (const settle of ['success', 'failure']) {
  test(`code preview ignores obsolete body ${settle} after selecting a newer code file`, async (t) => {
    const oldBody = deferred();
    const requests = [];
    const preview = mountPreview(t, 'CodeFilePreview', { getPage: (id, options) => {
      requests.push({ url: id, signal: options.signal });
      return id === 'a' ? oldBody.promise : Promise.resolve({ text: 'new content', headers: [], next_offset: null, version: 'b' });
    } });
    await flush();
    await preview.select('b');
    await flush();
    assert.equal(preview.state.page.value.text, 'new content');
    assert.equal(requests[0].signal.aborted, true);
    if (settle === 'success') oldBody.resolve({ text: 'obsolete content' });
    else oldBody.reject(new Error('old request failed'));
    await flush();
    assert.equal(preview.state.page.value.text, 'new content');
    assert.equal(preview.state.error.value, '');
  });
}

test('late signed URLs cannot start downloads after replacement or unmount', async (t) => {
  const first = deferred();
  const second = deferred();
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new Error('must not fetch'); });
  const preview = mountPreview(t, 'ImageFilePreview', { getUrl: (value) => value.file_id === 'a' ? first.promise : second.promise });
  await preview.select('b');
  first.resolve('obsolete-url');
  await flush();
  assert.equal(fetch.mock.callCount(), 0);
  assert.equal(preview.state.imageUrl.value, '');
  preview.unmount();
  second.resolve('disposed-url');
  await flush();
  assert.equal(fetch.mock.callCount(), 0);
});

test('image preview clears old image immediately and ignores late protected bytes', async (t) => {
  const first = deferred();
  t.mock.method(URL, 'createObjectURL', () => 'blob:new-image');
  const preview = mountPreview(t, 'ImageFilePreview', { loadBytes: (value) => value.file_id === 'a' ? first.promise : Promise.resolve(new ArrayBuffer(1)) });
  await preview.select('b');
  await flush();
  first.resolve(new ArrayBuffer(2));
  await flush();
  assert.equal(preview.state.imageUrl.value, 'blob:new-image');
});

test('CSV replacement aborts old transfer and retains quoted multiline fields', async (t) => {
  const first = deferred();
  const signals = [];
  const preview = mountPreview(t, 'CsvFilePreview', { getPage: (id, options) => {
    signals.push(options.signal);
    return id === 'a' ? first.promise : Promise.resolve({ headers: ['name', 'note'], rows: [['世界', 'line one\nline two']], next_offset: null, version: 'b' });
  } });
  await flush();
  await preview.select('b');
  await flush();
  first.resolve({ headers: ['wrong', 'headers'], rows: [['old', 'data']], next_offset: null, version: 'a' });
  await flush();
  assert.equal(signals[0].aborted, true);
  assert.deepEqual(preview.state.headers.value, ['name', 'note']);
  assert.deepEqual(preview.state.rows.value, [['世界', 'line one\nline two']]);
});

test('CSV page navigation keeps only one page, forwards revision and resets on file switch', async (t) => {
  const calls = [];
  const preview = mountPreview(t, 'CsvFilePreview', { getPage: async (id, options) => {
    calls.push({ id, ...options });
    return { version: id, offset: options.offset, delimiter: '\t', headers: options.offset ? [] : ['name'], rows: [[`${id}-${options.offset}`]], next_offset: options.offset ? null : 123 };
  } });
  await flush();
  await preview.state.loadPage(1);
  assert.deepEqual(preview.state.rows.value, [['a-123']]);
  assert.deepEqual(preview.state.headers.value, ['name']);
  assert.equal(calls[1].version, 'a');
  assert.equal(calls[1].delimiter, '\t');
  await preview.state.loadPage(0);
  assert.deepEqual(preview.state.rows.value, [['a-0']]);
  await preview.select('b');
  await flush();
  assert.equal(calls.at(-1).version, undefined);
  assert.equal(calls.at(-1).offset, 0);
  assert.equal(preview.state.pageIndex.value, 0);
});

test('changed file revision is visible and does not merge old and new pages', async (t) => {
  const preview = mountPreview(t, 'CodeFilePreview', { getPage: async (_id, options) => {
    if (options.offset) throw { status: 409 };
    return { version: 'a', text: 'page one', headers: [], next_offset: 65536 };
  } });
  await flush();
  await preview.state.loadPage(1);
  assert.match(preview.state.error.value, /文件已更新/);
  assert.equal(preview.state.page.value.text, 'page one');
  assert.equal(preview.state.pageIndex.value, 0);
});

test('CSV blank first page keeps the pending header across forward and backward navigation', async (t) => {
  const calls = [];
  const preview = mountPreview(t, 'CsvFilePreview', { getPage: async (_id, options) => {
    calls.push(options);
    return options.offset === 0
      ? { version: 'a', offset: 0, headers: [], rows: [], header_pending: true, delimiter: ',', next_offset: 131072 }
      : { version: 'a', offset: 131072, headers: ['name', 'value'], rows: [['world', '1']], header_pending: false, delimiter: '\t', next_offset: null };
  } });
  await flush();
  await preview.state.loadPage(1);
  assert.equal(calls.at(-1).header_pending, true);
  assert.equal(calls.at(-1).delimiter, undefined);
  assert.deepEqual(preview.state.headers.value, ['name', 'value']);
  await preview.state.loadPage(0);
  await preview.state.loadPage(1);
  assert.equal(calls.at(-1).header_pending, true);
  assert.equal(calls.at(-1).delimiter, undefined);
});

for (const [name, stateKey] of [['MarkdownFilePreview', 'renderedContent'], ['HtmlFilePreview', 'status'], ['TiffFilePreview', 'status']]) {
  test(`${name} ignores an obsolete error after a new load starts`, async (t) => {
    const first = deferred();
    const second = deferred();
    t.mock.method(globalThis, 'fetch', (url) => url === 'a' ? first.promise : second.promise);
    const preview = mountPreview(t, name);
    await flush();
    await preview.select('b');
    await flush();
    const expected = preview.state[stateKey].value;
    first.reject(new Error('old network failure'));
    await flush();
    assert.equal(preview.state[stateKey].value, expected);
    preview.unmount();
    second.reject(new Error('disposed network failure'));
    await flush();
    assert.equal(preview.state[stateKey].value, expected);
  });
}

for (const name of ['MarkdownFilePreview', 'HtmlFilePreview']) {
  test(`${name} cannot update the next session's shared file list after unmount`, async (t) => {
    const related = deferred();
    t.mock.method(globalThis, 'fetch', async () => ({ ok: true, text: async () => '<p>report</p>' }));
    const preview = mountPreview(t, name, { getFiles: () => related.promise });
    await flush();
    preview.unmount();
    preview.relatedFiles.value = [file('new-session-file')];
    related.resolve([file('old-session-file')]);
    await flush();
    assert.deepEqual(preview.relatedFiles.value.map((item) => item.file_id), ['new-session-file']);
  });
}

test('HTML resource finishing after unmount does not create or leak a blob URL', async (t) => {
  const body = deferred();
  const relatedFiles = vue.ref([file('report', 'html'), file('asset', 'png')]);
  t.mock.method(globalThis, 'fetch', async (url) => ({ ok: true, text: async () => '<img src="asset.png">', blob: () => body.promise }));
  const create = t.mock.method(URL, 'createObjectURL', () => 'blob:test');
  const preview = mountPreview(t, 'HtmlFilePreview', { relatedFiles });
  await flush();
  preview.unmount();
  body.resolve(new Blob(['image']));
  await flush();
  assert.equal(create.mock.callCount(), 0);
});

test('HTML blob resources are released on replacement and on unmount', async (t) => {
  const relatedFiles = vue.ref([file('report', 'html'), file('asset', 'png')]);
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, text: async () => '<img src="asset.png">', blob: async () => new Blob(['image']) }));
  let count = 0;
  t.mock.method(URL, 'createObjectURL', () => `blob:test-${++count}`);
  const revoked = [];
  t.mock.method(URL, 'revokeObjectURL', (url) => revoked.push(url));
  const preview = mountPreview(t, 'HtmlFilePreview', { relatedFiles });
  await flush();
  assert.equal(preview.state.objectUrls.value.length, 1);
  await preview.select('b');
  await flush();
  assert.deepEqual(revoked, ['blob:test-1']);
  assert.deepEqual(preview.state.objectUrls.value, ['blob:test-2']);
  preview.unmount();
  assert.deepEqual(revoked, ['blob:test-1', 'blob:test-2']);
});
