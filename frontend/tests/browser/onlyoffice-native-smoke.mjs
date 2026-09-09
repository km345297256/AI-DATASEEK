/** Explicit live integration test. Uses only three synthetic uploads and removes
 * those exact IDs in finally. No user documents, secrets, new listener or mock
 * Office/API responses. The local Vue harness uses the production component. */
import assert from 'node:assert/strict';
import { readFile, readdir, mkdtemp, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import http from 'node:http';
import { dirname, resolve, join } from 'node:path';
import { homedir, tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc';
import { nativeOfficeFixtures } from './onlyoffice-native-fixtures.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const base = new URL(process.env.OFFICE_TEST_ORIGIN || 'http://localhost:7001');
const samples = process.env.OFFICE_TEST_FORMAT ? nativeOfficeFixtures.filter(sample => sample.filename.endsWith(`.${process.env.OFFICE_TEST_FORMAT}`)) : nativeOfficeFixtures;
assert.ok(samples.length, 'At least one actual office fixture must be selected');
assert.ok(['localhost', '127.0.0.1'].includes(base.hostname) && base.protocol === 'http:', 'Live test only targets local HTTP');
const officeOrigin = `http://office.localhost:${base.port || '80'}`;
const output = await mkdtemp(join(tmpdir(), 'dataseek-onlyoffice-native-'));
const require = createRequire(resolve(frontend, 'package.json'));
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) { try { playwrightEntry = require.resolve('playwright'); } catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); } }
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const scrub = value => String(value).replace(/[A-Za-z0-9_-]{43,}/g, '[redacted]');
const hash = value => createHash('sha256').update(value).digest('hex');
const api = async (path, options = {}) => {
  const response = await fetch(`${base.origin}/api/v1${path}`, options);
  if (!response.ok) throw new Error(`API ${path.replace(/\/files\/[^/]+/, '/files/[id]')} returned ${response.status}`);
  const body = await response.json(); assert.equal(body.code, 0); return body.data;
};
const state = enabled => api('/visualizations/viz-onlyoffice/state', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
const officeFetch = path => new Promise((resolve, reject) => {
  const request = http.get({ hostname: base.hostname, port: base.port || 80, path, headers: { Host: new URL(officeOrigin).host } }, response => {
    response.resume(); response.on('end', () => resolve({ status: response.statusCode }));
  });
  request.on('error', reject); request.setTimeout(5000, () => request.destroy(new Error('Office capability probe timed out')));
});
const entry = `import {createApp,h} from 'vue';import Office from ${JSON.stringify(resolve(frontend, 'src/visualizations/extended/OnlyOfficePreview.vue'))};let app;window.mountOffice=(file,plugin)=>{app=createApp({render:()=>h(Office,{file,plugin})});app.mount('#app')};window.unmountOffice=()=>{app?.unmount();app=null};window.nativeReady=true;`;
await build({ stdin: { contents: entry, sourcefile: 'entry.js', resolveDir: frontend }, bundle: true, format: 'esm', platform: 'browser', target: 'es2022', outfile: join(output, 'entry.js'), logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }), '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'trusted-office-sfc', setup(build) { build.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
    const { descriptor, errors } = parse(await readFile(path, 'utf8'), { filename: path }); assert.deepEqual(errors, []);
    const id = hash(path).slice(0, 12), compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' });
    const styles = descriptor.styles.map(style => compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` }).code).join('\n');
    return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
  }); } }] });
const css = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({ headless: true, executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(chrome) ? chrome : undefined),
  args: ['--disable-background-networking', '--disable-component-update', '--host-resolver-rules=MAP office.localhost 127.0.0.1'] });
const uploaded = [], leases = [], results = []; let previous;
try {
  const catalog = await api('/visualizations'); const plugin = catalog.plugins.find(p => p.id === 'viz-onlyoffice');
  assert.ok(plugin, 'Deployed Cordis catalog must register ONLYOFFICE'); previous = plugin.enabled;
  await state(true); plugin.enabled = true;
  for (const sample of samples) {
    const result = { filename: sample.filename, passed: false, failures: [], consoleErrors: [], expectedRejections: [], external: [] };
    const form = new FormData(); form.append('file', new Blob([sample.bytes]), `native-readonly-${Date.now()}-${sample.filename}`);
    form.append('metadata', JSON.stringify({ source: 'onlyoffice_native_regression', synthetic: true }));
    const file = await api('/files', { method: 'POST', body: form }); uploaded.push(file.file_id);
    const before = Buffer.from(await (await fetch(`${base.origin}/api/v1/files/${file.file_id}`)).arrayBuffer());
    assert.equal(hash(before), hash(sample.bytes), 'Upload is byte-for-byte the synthetic fixture');
    const context = await browser.newContext({ viewport: { width: 1200, height: 850 }, serviceWorkers: 'block' });
    const page = await context.newPage(); let issued, stopping = false;
    page.on('pageerror', e => result.failures.push(scrub(e.message)));
    page.on('requestfailed', request => { result.failures.push(`${scrub(new URL(request.url()).pathname)} ${request.failure()?.errorText}`); });
    page.on('console', m => {
      if (m.type() !== 'error') return;
      const url = m.location().url || '';
      const expected = stopping && /404/.test(m.text()) && /\/office-viewer\/(?:status\/|leases\/)/.test(url);
      (expected ? result.expectedRejections : result.consoleErrors).push(scrub(m.text()).slice(0, 500));
    });
    page.on('response', async response => {
      const url = new URL(response.url());
      if (url.origin === base.origin && decodeURIComponent(url.pathname) === `/api/v1/files/${file.file_id}/visualization` && response.ok()) {
        try { const body = await response.json(); if (body.data?.kind === 'resources') { issued = body.data.payload; leases.push(issued.lease); } } catch { /* No unrelated response is an authorization. */ }
      }
      if (url.origin === officeOrigin && response.status() >= 400) {
        const expected = stopping && response.status() === 404 && url.pathname === `/office-viewer/status/${issued?.lease}`;
        (expected ? result.expectedRejections : result.failures).push(`${response.status()} ${scrub(url.pathname)}`);
      }
    });
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (![base.origin, officeOrigin].includes(url.origin)) { result.external.push(`${url.origin}${url.pathname}`); return route.abort(); }
      if (url.origin === base.origin && url.pathname === '/__onlyoffice_native__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: await readFile(join(output, 'entry.js')) });
      if (url.origin === base.origin && url.pathname === '/__onlyoffice_native__/project.css') return route.fulfill({ contentType: 'text/css', body: css ? await readFile(resolve(frontend, 'dist/assets', css)) : '' });
      return route.continue(); // Real backend, gateway and document server.
    });
    try {
      // Establish a genuine loopback document address space before mounting the
      // harness. Fulfilling the document route would manufacture a public/unknown
      // address space and trigger Chrome Local Network Access protection.
      await page.goto(base.origin, { waitUntil: 'commit' });
      await page.setContent('<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__onlyoffice_native__/project.css"><style>html,body{margin:0;height:100%;font-family:Arial}#app{display:flex;height:800px;width:1160px;margin:20px}iframe{flex:1;min-height:0;width:100%}section{display:flex;flex-direction:column;flex:1;min-height:0}</style></head><body><div id="app"></div><script type="module" src="/__onlyoffice_native__/entry.js"></script></body></html>');
      await page.waitForFunction(() => window.nativeReady);
      await page.evaluate(({ file, plugin }) => window.mountOffice(file, plugin), { file, plugin });
      await page.waitForFunction(() => document.querySelector('iframe') && !document.querySelector('[role="status"]') && !document.querySelector('[role="alert"]'), null, { timeout: 120000 });
      assert.ok(issued?.lease, 'Real unified prepare returned an office resource');
      const wrapper = page.frames().find(frame => frame.url().startsWith(`${officeOrigin}/office-viewer/frame/`));
      assert.ok(wrapper, 'Separate-origin trusted wrapper exists');
      const configuration = await wrapper.evaluate(() => ({ mode: config.editorConfig.mode, permissions: config.document.permissions,
        macros: config.editorConfig.customization.macros, plugins: config.editorConfig.customization.plugins, callback: 'callbackUrl' in config.editorConfig,
        isolated: (() => { try { void parent.document; return false; } catch { return true; } })() }));
      assert.equal(configuration.mode, 'view'); assert.equal(configuration.isolated, true); assert.ok(Object.values(configuration.permissions).every(value => value === false));
      assert.equal(configuration.macros, false); assert.equal(configuration.plugins, false); assert.equal(configuration.callback, false);
      const editor = page.frames().find(frame => frame.url().includes('/web-apps/apps/') && frame.url().includes('/index.html'));
      assert.ok(editor, 'Official editor frame loaded');
      const render = await editor.evaluate(extension => {
        const canvases = [...document.querySelectorAll('canvas')].filter(canvas => canvas.width > 100 && canvas.height > 100);
        const builder = window.AscBuilder;
        let text = '', cells;
        // Fixed-version SDK model getters are test-only. Never enable macros or
        // plugins, copy pixels through disabled-copy guards, or mutate the model.
        if (extension === 'docx') text = builder.Word.Api.GetDocument().GetText();
        else if (extension === 'xlsx') {
          const sheet = builder.Cell.Api.GetActiveSheet();
          cells = ['A1', 'A4', 'B4'].map(address => sheet.GetRange(address).GetValue()); text = cells.join('\n');
        } else {
          const slide = builder.Slide.Api.GetPresentation().GetSlideByIndex(0);
          text = slide.GetAllShapes().map(shape => shape.GetDocContent()?.GetText() || '').join('\n');
        }
        return { canvases: canvases.length, text, cells, title: document.title };
      }, sample.filename.split('.').pop());
      assert.ok(render.canvases > 0, 'Native editor created the document rendering surface');
      assert.ok(render.text.includes(sample.expectedText), 'Native SDK model must contain the known fixture text, not only empty grids');
      if (sample.filename.endsWith('.xlsx')) assert.deepEqual(render.cells.map(String), [sample.expectedText, 'Signal', '7']);
      if (sample.filename.endsWith('.docx')) assert.match(render.text, /Signal[\s\S]*7/);
      assert.deepEqual(result.failures, [], 'No native resource failures before deliberate revocation');
      result.render = render; result.configuration = configuration;
      const tip = editor.getByText('知道了', { exact: true }).first();
      await tip.waitFor({ state: 'visible', timeout: 3000 }).then(() => tip.click()).catch(() => {});
      await page.screenshot({ path: join(output, `${sample.filename}.png`) });
      const after = Buffer.from(await (await fetch(`${base.origin}/api/v1/files/${file.file_id}`)).arrayBuffer());
      assert.equal(hash(after), hash(before), 'Viewing never writes back the original'); result.sourceUnchanged = true;
      // Assert the Office-origin simple request is rejected, not merely hidden by CORS.
      assert.equal((await fetch(`${base.origin}/api/v1/visualizations`, { headers: { Origin: officeOrigin } })).status, 403);
      assert.equal((await officeFetch('/api/v1/visualizations')).status, 404);
      assert.equal((await officeFetch('/office-viewer/ConvertService.ashx')).status, 404);
      stopping = true; await state(false);
      assert.equal((await officeFetch(`/office-viewer/status/${issued.lease}`)).status, 404);
      await page.getByRole('alert').waitFor({ timeout: 20000 });
      assert.equal(await page.locator('iframe').count(), 0, 'Stopped plugin terminates normal viewing through lease polling');
      await page.evaluate(() => window.unmountOffice()); await state(true);
      result.stoppedAndUnmounted = true;
      assert.deepEqual(result.external, [], 'No external resource requests'); result.passed = true;
    } catch (e) {
      result.error = scrub(e.stack || e.message);
      await page.screenshot({ path: join(output, `${sample.filename}-failure.png`) }).catch(() => {});
      result.framePaths = page.frames().map(frame => { try { return scrub(new URL(frame.url()).pathname); } catch { return 'blank'; } });
      result.alerts = await page.getByRole('alert').allTextContents().catch(() => []);
    } finally { await page.evaluate(() => window.unmountOffice?.()).catch(() => {}); await context.close(); await state(true); }
    results.push(result); console.log(JSON.stringify(result));
  }
} finally {
  for (const token of leases) await fetch(`${base.origin}/api/v1/office-viewer/leases/${token}/revoke`, { method: 'POST', headers: { 'X-Visualization-Action': 'revoke' } }).catch(() => {});
  for (const id of uploaded) {
    await api(`/files/${encodeURIComponent(id)}`, { method: 'DELETE' });
    assert.equal((await fetch(`${base.origin}/api/v1/files/${encodeURIComponent(id)}/info`)).status, 404, 'Only the test-created upload is removed');
  }
  if (typeof previous === 'boolean') {
    await state(previous);
    assert.equal((await api('/visualizations')).plugins.find(plugin => plugin.id === 'viz-onlyoffice').enabled, previous);
  }
  await browser.close(); await writeFile(join(output, 'results.json'), JSON.stringify(results, null, 2));
  console.log(`Native ONLYOFFICE evidence: ${output}`);
  console.log(JSON.stringify({ cleanup: { syntheticUploadsRemoved: uploaded.length, pluginStateRestored: previous } }));
}
if (results.length !== samples.length || results.some(result => !result.passed)) process.exitCode = 1;
