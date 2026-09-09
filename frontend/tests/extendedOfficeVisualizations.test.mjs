import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import { readBoundedBinary } from '../src/visualizations/boundedBinary.ts';

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); await vue.nextTick(); };
function mount(name, dependencies) {
  const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: name }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }, mounted = [];
  const modules = { vue: { ...vue, onMounted: callback => mounted.push(callback) },
    '../../composables/usePreviewLoad': { usePreviewLoad }, '../boundedBinary': { readBoundedBinary },
    '../runtime': { visualizationJobsPath: file => `/files/${file.file_id}/visualization/jobs`, readVisualizationResult: async response => (await response.json()).data }, ...dependencies };
  new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup({ file: { file_id: 'synthetic-file' }, plugin: { id: 'viz-fastqc' } }, { expose() {} }));
  return { state, start: async () => { for (const callback of mounted) await callback(); await flush(); }, stop: () => scope.stop() };
}
const excel = (sheet = 'Measurements') => ({ selected: { sheet }, choices: { sheets: ['Measurements', 'Second'] },
  table: { columns: ['A'], rows: [[1]], total_rows: 400, total_columns: 200 } });

test('production serves local SDK module workers with JavaScript MIME and no SPA fallback', () => {
  const config = readFileSync(new URL('../nginx.conf', import.meta.url), 'utf8');
  assert.match(config, /location ~ \^\/\(assets\|visualization-assets\)\/\.\*\\\.mjs\$/);
  assert.match(config, /types \{ application\/javascript mjs; \}/);
  assert.match(config, /location \/visualization-assets\/ \{[^}]*try_files \$uri =404;/);
});

test('Excel initial selected sheet does not repeat isolated reading; sheet and offsets coalesce', async t => {
  const calls = [];
  const adapter = mount('ExcelPreview', { './runtime': { requestPreview: async (_file, _plugin, options) => { calls.push(options); return excel(options.sheet); } } });
  t.after(adapter.stop); await adapter.start();
  assert.equal(calls.length, 1);
  adapter.state.rowOffset.value = 200; adapter.state.columnOffset.value = 100; await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].row_offset, 200); assert.equal(calls[1].column_offset, 100);
  adapter.state.sheet.value = 'Second'; await flush();
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[2], { sheet: 'Second', row_offset: 0, column_offset: 0 });
});

test('Excel ignores a late reader response after the plugin is stopped', async () => {
  let resolve, signal;
  const pending = new Promise(done => { resolve = done; });
  const adapter = mount('ExcelPreview', { './runtime': { requestPreview: (_file, _plugin, _options, inputSignal) => { signal = inputSignal; return pending; } } });
  const start = adapter.start(); adapter.stop(); assert.equal(signal.aborted, true);
  resolve(excel()); await start; assert.equal(adapter.state.table.value, undefined);
});

test('QC failed cancellation is shown without an unhandled rejection', async t => {
  const originalFetch = globalThis.fetch; globalThis.fetch = async () => new Response('{}');
  t.after(() => { globalThis.fetch = originalFetch; });
  const adapter = mount('FastQcPreview', { '../../api/client': { BASE_URL: '/api/v1', apiClient: {
    get: async () => ({ data: { data: [] } }), post: async () => { throw new Error('Temporary transport failure'); },
  } } });
  t.after(adapter.stop); adapter.state.job.value = { job_id: 'test-job', status: 'running' };
  await assert.doesNotReject(adapter.state.cancel());
  assert.match(adapter.state.error.value, /Temporary transport failure/);
});

test('QC cancelled view never starts a new status request after its pending cancel returns', async t => {
  const originalFetch = globalThis.fetch; globalThis.fetch = async () => new Response('{}');
  t.after(() => { globalThis.fetch = originalFetch; });
  let resolve, signal, reads = 0;
  const pending = new Promise(done => { resolve = done; });
  const adapter = mount('FastQcPreview', { '../../api/client': { BASE_URL: '/api/v1', apiClient: {
    get: async () => { reads++; return { data: { data: [] } }; },
    post: (_path, _data, options) => { signal = options.signal; return pending; },
  } } });
  adapter.state.job.value = { job_id: 'test-job', status: 'running' };
  const cancelling = adapter.state.cancel(); adapter.stop(); assert.equal(signal.aborted, true);
  resolve({}); await cancelling; assert.equal(reads, 0);
});
