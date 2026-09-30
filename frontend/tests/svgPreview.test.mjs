import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

const plugin = { adapter: 'svg', reader: 'svg', enabled: true, limits: { max_input_bytes: 8 * 1024 * 1024, max_output_bytes: 8 * 1024 * 1024 } };
const file = { file_id: 'opaque', filename: 'chart.svg', size: 1024, file_url: '/never-inline-this-svg' };
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xkAAAAASUVORK5CYII=';
const result = () => ({ kind: 'media', payload: { view_kind: 'image', media_type: 'image/png', data_base64: png }, metadata: { width: 1, height: 1, source_format: 'SVG', rasterized: true }, warnings: [] });
function sdk(requestVisualization = async () => result()) {
  const source = readFileSync(new URL('../src/visualizations/svgPreview.ts', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(id => { assert.equal(id, './runtime'); return { requestVisualization }; }, module, module.exports);
  return module.exports;
}
test('SVG uses owner-authorized preview operation and returns only bounded PNG', async () => {
  const requests = [];
  const api = sdk(async (...args) => { requests.push(args); return result(); });
  const signal = new AbortController().signal;
  const value = await api.loadSvgPreview(file, plugin, signal);
  assert.deepEqual(requests, [[file, plugin, 'preview', { kind: 'image' }, signal]]);
  assert.equal(value.blob.type, 'image/png');
  assert.deepEqual(Buffer.from(await value.blob.arrayBuffer()), Buffer.from(png, 'base64'));
  assert.equal(value.width, 1); assert.equal(value.height, 1);
});
for (const [label, mutate] of [
  ['raw SVG', value => { value.payload.data_base64 = Buffer.from('<svg onload="alert(1)"/>').toString('base64'); }],
  ['wrong mime', value => { value.payload.media_type = 'image/svg+xml'; }],
  ['wrong kind', value => { value.kind = 'report'; }],
  ['wrong view', value => { value.payload.view_kind = 'map'; }],
  ['missing raster marker', value => { delete value.metadata.rasterized; }],
  ['wrong source', value => { value.metadata.source_format = 'HTML'; }],
  ['forged dimensions', value => { value.metadata.width = 2; }],
  ['URL instead of base64', value => { value.payload.data_base64 = 'https://evil.example/thing.png'; }],
  ['whitespace encoding', value => { value.payload.data_base64 += '\n'; }],
  ['unbounded dimensions', value => { const bytes = Buffer.from(png, 'base64'); bytes.writeUInt32BE(2049, 16); value.payload.data_base64 = bytes.toString('base64'); value.metadata.width = 2049; }],
]) test(`SVG loader rejects ${label}`, () => {
  const value = result(); mutate(value);
  assert.throws(() => sdk().parseSvgPreview(value, plugin.limits.max_output_bytes), /预览响应无效/);
});
test('SVG response byte cap applies before and after base64 decode', () => {
  for (const limit of [0, -1, 16 * 1024 * 1024 + 1, 2, Buffer.from(png, 'base64').length - 1]) {
    assert.throws(() => sdk().parseSvgPreview(result(), limit), /预览响应无效/);
  }
});
test('disabled SVG, mismatched plugin and oversized files do not start rendering', async () => {
  const api = sdk(() => assert.fail('must not request'));
  const signal = new AbortController().signal;
  for (const override of [{ enabled: false }, { reader: 'binary' }, { adapter: 'image' }]) {
    await assert.rejects(api.loadSvgPreview(file, { ...plugin, ...override }, signal), /未启用/);
  }
  for (const size of [plugin.limits.max_input_bytes + 1, -1, Infinity]) {
    await assert.rejects(api.loadSvgPreview({ ...file, size }, plugin, signal), /上限/);
  }
});
test('cancelled request cannot publish a response even when the transport ignores abort', async () => {
  const controller = new AbortController();
  const api = sdk(async () => { controller.abort(); return result(); });
  await assert.rejects(api.loadSvgPreview(file, plugin, controller.signal), { name: 'AbortError' });
  await assert.rejects(sdk(() => assert.fail('already aborted')).loadSvgPreview(file, plugin, controller.signal), { name: 'AbortError' });
});
