import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';
import { readBoundedBinary } from '../src/visualizations/boundedBinary.ts';

const file = () => ({ file_id: 'safe-file', filename: 'result.csv', size: 10 ** 10, upload_date: '' });
const plugin = (extra = {}) => ({ contract_version: 2, id: 'csv', version: '1.0.0', enabled: true, reader: 'csv', capabilities: { operations: ['page'], input_mode: 'page', shared: true }, limits: { max_input_bytes: 131072, max_output_bytes: 524288 }, ...extra });
const pagePayload = { offset: 0, next_offset: 131072, total_bytes: 10 ** 10, text: '', headers: ['value'], rows: [['one\ntwo']], columns_truncated: false, delimiter: ',', header_pending: false, bytes_read: 131072 };
const result = (extra = {}) => ({ contract_version: 2, plugin_id: 'csv', version: 'a'.repeat(64), revision: 'b'.repeat(64), kind: 'page', payload: pagePayload, metadata: {}, warnings: [], sampled: false, ...extra });
const response = data => new Response(JSON.stringify({ code: 0, data }), { headers: { 'content-type': 'application/json' } });
function sdk(stubs = {}) {
  const source = readFileSync(new URL('../src/visualizations/runtime.ts', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const modules = { '../api/client': { BASE_URL: '/api/v1' }, '../api/file': { getFileDownloadUrl: async () => assert.fail('Owner preview may not create signed links') }, './boundedBinary': { readBoundedBinary }, ...stubs };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  return module.exports;
}

test('all owner previews share one SDK endpoint and preserve large-file bounded pages', async t => {
  const requests = []; t.mock.method(globalThis, 'fetch', async (url, options) => { requests.push({ url, ...JSON.parse(options.body), signal: options.signal }); return response(result()); });
  const signal = new AbortController().signal;
  const value = await sdk().getPluginPage(file(), plugin(), { offset: 0, header_pending: true }, signal);
  assert.deepEqual(value.rows, [['one\ntwo']]); assert.equal(value.total_bytes, 10 ** 10);
  assert.equal(requests[0].url, '/api/v1/files/safe-file/visualization'); assert.equal(requests[0].operation, 'page'); assert.equal(requests[0].signal, signal);
  assert.deepEqual(requests[0].options, { offset: 0, header_pending: true });
});

test('shared page and molecule prepare keep server authority without a blanket shared-page rejection', async t => {
  const previous = globalThis.window; globalThis.window = { location: { pathname: '/share/existing-session' } };
  t.after(() => { if (previous === undefined) delete globalThis.window; else globalThis.window = previous; });
  let reads = 0; t.mock.method(globalThis, 'fetch', async () => { reads++; return response(result()); });
  const runtime = sdk(); await runtime.getPluginPage(file(), plugin(), {}, new AbortController().signal);
  await assert.rejects(runtime.requestVisualization(file(), plugin({ capabilities: { operations: ['preview'], input_mode: 'whole', shared: false } }), 'preview', {}, new AbortController().signal), /共享/);
  assert.equal(reads, 1);
});

test('disabled plugins and undeclared operations cannot start a transfer', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', () => assert.fail('not authorized'));
  const runtime = sdk(), signal = new AbortController().signal;
  await assert.rejects(runtime.requestVisualization(file(), plugin({ enabled: false }), 'page', {}, signal), /未启用/);
  await assert.rejects(runtime.loadPluginBytes(file(), plugin(), signal), /不支持/);
  assert.equal(fetch.mock.callCount(), 0);
});

test('the unified result envelope rejects unknown fields, malformed revisions and wrong plugins', () => {
  const runtime = sdk(); assert.equal(runtime.parseVisualizationResult(result(), plugin()).kind, 'page');
  for (const change of [{ extra: true }, { revision: 'rev' }, { version: 'v1' }, { plugin_id: 'other' }, { contract_version: 1 }, { payload: [] }, { kind: 'untrusted' }]) {
    assert.throws(() => runtime.parseVisualizationResult(result(change), plugin()), /统一/);
  }
});

for (const reader of ['spatial-window','pointcloud-window','gro-trajectory','simulation-mesh','ugrid-window']) test(`additive geometry result approved only for ${reader}`, () => {
  const value=result({kind:'geometry',payload:{}});
  assert.equal(sdk().parseVisualizationResult(value,plugin({reader})).kind,'geometry');
});
for (const reader of ['csv','hdf5','netcdf','dicom-window','binary','scientific-graph']) test(`geometry does not widen ${reader} result capability`, () => {
  assert.throws(()=>sdk().parseVisualizationResult(result({kind:'geometry',payload:{}}),plugin({reader})),/几何/);
});

test('page parser rejects unbounded shapes and non-progressing cursors', async t => {
  let payload; t.mock.method(globalThis, 'fetch', async () => response(result({ payload })));
  const runtime = sdk(), signal = new AbortController().signal;
  for (const change of [{ rows: Array(101).fill(['x']) }, { headers: Array(51).fill('x') }, { rows: [[{ bad: true }]] }, { next_offset: 0 }, { bytes_read: 131073 }]) {
    payload = { ...pagePayload, ...change }; await assert.rejects(runtime.getPluginPage(file(), plugin(), {}, signal), /分页/);
  }
});

test('version pins do not cross plugin scopes and explicit resets clear the next request pin', async t => {
  const requests = []; t.mock.method(globalThis, 'fetch', async (_url, options) => { const request = JSON.parse(options.body); requests.push(request); return response(result({ plugin_id: request.plugin_id })); });
  const runtime = sdk(), input = file(), signal = new AbortController().signal;
  await runtime.requestVisualization(input, plugin(), 'page', {}, signal);
  await runtime.requestVisualization(input, plugin(), 'page', {}, signal);
  await runtime.requestVisualization(input, plugin({ id: 'other' }), 'page', {}, signal);
  await runtime.requestVisualization(input, plugin(), 'page', { version: undefined }, signal);
  await runtime.requestVisualization(input, plugin(), 'page', {}, new AbortController().signal);
  assert.equal(requests[1].version, 'a'.repeat(64)); assert.equal(requests[2].version, undefined); assert.equal(requests[3].version, undefined); assert.equal(requests[4].version, undefined, 'Reopening the same file object starts a fresh snapshot');
});

test('binary results validate all protocol headers and use the declared reader budget', async t => {
  const binary = plugin({ reader: 'binary', capabilities: { operations: ['bytes'], input_mode: 'whole', shared: false }, limits: { max_input_bytes: 256 * 1024 * 1024, max_output_bytes: 524288 } });
  const input = { ...file(), size: 128 * 1024 * 1024 };
  const headers = { 'X-Preview-Version': 'a'.repeat(64), 'X-Visualization-Revision': 'b'.repeat(64), 'X-Visualization-Plugin': 'csv' };
  t.mock.method(globalThis, 'fetch', async () => new Response(new Uint8Array([1, 2]), { headers }));
  const runtime = sdk(), signal = new AbortController().signal;
  assert.deepEqual([...new Uint8Array(await runtime.loadPluginBytes(input, binary, signal))], [1, 2]);
  delete headers['X-Visualization-Revision']; await assert.rejects(runtime.loadPluginBytes(input, binary, signal), /标识/);
});

test('resource version is pinned to the resource object, never the parent preview', async t => {
  const requests = []; t.mock.method(globalThis, 'fetch', async (_url, options) => { const request = JSON.parse(options.body); requests.push(request); return new Response(new Uint8Array([1]), { headers: { 'X-Preview-Version': (request.options.resource_id ? 'c' : 'a').repeat(64), 'X-Visualization-Revision': 'b'.repeat(64), 'X-Visualization-Plugin': 'csv' } }); });
  const runtime = sdk(), input = { ...file(), size: 1 }, resource = { ...file(), file_id: 'related-file', size: 1 }, signal = new AbortController().signal;
  const binary = plugin({ capabilities: { operations: ['bytes'], input_mode: 'whole', shared: false } });
  await runtime.loadPluginBytes(input, binary, signal); await runtime.loadPluginBytes(input, binary, signal, resource); await runtime.loadPluginBytes(input, binary, signal);
  assert.deepEqual(requests[1].options, { resource_id: 'related-file' }); assert.equal(requests[1].version, undefined); assert.equal(requests[2].version, 'a'.repeat(64));
});

test('all registered file adapters use the common SDK; ordinary download and archive import remain separate', () => {
  for (const name of readdirSync(new URL('../src/components/filePreviews/', import.meta.url)).filter(name => name.endsWith('.vue') && name !== 'UnknownFilePreview.vue')) {
    const source = readFileSync(new URL(`../src/components/filePreviews/${name}`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /getFileDownloadUrl|prepareMolecularPreview|fetch\(/, name);
    assert.match(source, /plugin: VisualizationPlugin/, name);
  }
  const api = readFileSync(new URL('../src/api/file.ts', import.meta.url), 'utf8');
  assert.match(api, /function downloadFile/); assert.match(api, /function prepareShapefilePreview/);
  assert.doesNotMatch(api, /getFilePreviewPage|molecular-preview\/prepare/);
});
