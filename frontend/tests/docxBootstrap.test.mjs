import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { readBoundedBinary } from '../src/visualizations/boundedBinary.ts';

const source = readFileSync(new URL('../src/visualizations/extended/docxBootstrap.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const module = { exports: {} };
new Function('require', 'module', 'exports', code)(id => {
  assert.equal(id, '../boundedBinary'); return { readBoundedBinary };
}, module, module.exports);
const { loadDocxBootstrap } = module.exports;
const scriptResponse = body => new Response(body, { headers: { 'Content-Type': 'application/javascript; charset=utf-8' } });

function stubFetch(t, implementation) {
  const previous = globalThis.fetch;
  globalThis.fetch = implementation;
  t.after(() => { globalThis.fetch = previous; });
}

test('DOCX bootstrap reads only a fixed same-origin script without credentials or redirects', async t => {
  const signal = new AbortController().signal;
  stubFetch(t, async (path, options) => {
    assert.equal(path, '/visualization-assets/docx/docx.js');
    assert.deepEqual(options, { signal, credentials: 'omit', redirect: 'error' });
    return scriptResponse('const value = "</ScRiPt><script>untrusted()</script>";');
  });
  const script = await loadDocxBootstrap(signal);
  assert.doesNotMatch(script, /<\/script/i);
  assert.equal(script, 'const value = "<\\/script><script>untrusted()<\\/script>";');
});

test('DOCX bootstrap rejects error pages, redirects and non-JavaScript responses', async t => {
  const redirected = scriptResponse('code'); Object.defineProperty(redirected, 'redirected', { value: true });
  const responses = [new Response('no', { status: 404 }), new Response('<html/>', { headers: { 'Content-Type': 'text/html' } }), redirected];
  stubFetch(t, async () => responses.shift());
  for (let index = 0; index < 3; index++) {
    await assert.rejects(loadDocxBootstrap(new AbortController().signal), /本地 DOCX 阅读组件不可用/);
  }
});

test('DOCX bootstrap counts actual bytes instead of trusting Content-Length', async t => {
  stubFetch(t, async () => new Response('x'.repeat(1024 * 1024 + 1), { headers: { 'Content-Type': 'text/javascript', 'Content-Length': '1' } }));
  await assert.rejects(loadDocxBootstrap(new AbortController().signal), /读取上限/);
});

test('DOCX bootstrap cancellation releases a pending body and never returns a stale script', async t => {
  const controller = new AbortController(); let cancelled = false;
  stubFetch(t, async () => scriptResponse(new ReadableStream({ cancel() { cancelled = true; } })));
  const pending = loadDocxBootstrap(controller.signal);
  await Promise.resolve(); await Promise.resolve(); controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(cancelled, true);
});
