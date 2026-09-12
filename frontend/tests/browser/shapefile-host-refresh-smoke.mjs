/** Real VisualizationHost/catalog polling with synthetic bytes; no server or public network. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, parse } from '@vue/compiler-sfc';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) {
  try { playwrightEntry = require.resolve('playwright'); }
  catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
}
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const fixtureId = 'synthetic-shapefile-host-refresh';
const points = Array.from({ length: 125 }, (_, index) => [100 + index % 25 * 0.1, 20 + Math.floor(index / 25) * 0.2]);
const label = index => `feature-${String(index + 1).padStart(4, '0')}`;
function shapeBytes() {
  const bytes = Buffer.alloc(100 + points.length * 28);
  bytes.writeInt32BE(9994, 0); bytes.writeInt32BE(bytes.length / 2, 24);
  bytes.writeInt32LE(1000, 28); bytes.writeInt32LE(1, 32);
  [100, 20, 102.4, 20.8].forEach((value, index) => bytes.writeDoubleLE(value, 36 + index * 8));
  points.forEach(([x, y], index) => {
    const start = 100 + index * 28;
    bytes.writeInt32BE(index + 1, start); bytes.writeInt32BE(10, start + 4);
    bytes.writeInt32LE(1, start + 8); bytes.writeDoubleLE(x, start + 12); bytes.writeDoubleLE(y, start + 20);
  });
  return bytes;
}
function dbfBytes() {
  const header = 65, width = 16, length = width + 1;
  const bytes = Buffer.alloc(header + points.length * length + 1, 0);
  bytes[0] = 3; bytes.writeUInt32LE(points.length, 4);
  bytes.writeUInt16LE(header, 8); bytes.writeUInt16LE(length, 10);
  bytes.write('label', 32); bytes[43] = 67; bytes[48] = width; bytes[64] = 13;
  points.forEach((_, index) => {
    const start = header + index * length;
    bytes.fill(32, start, start + length); bytes.write(label(index), start + 1);
  });
  bytes[bytes.length - 1] = 26;
  return bytes;
}
const resources = new Map([
  [fixtureId, shapeBytes()], [`${fixtureId}-attributes`, dbfBytes()],
  [`${fixtureId}-projection`, Buffer.from('LOCAL_CS["Synthetic local coordinates",UNIT["metre",1]]')],
]);
const files = [...resources].map(([file_id, bytes], index) => ({
  file_id, filename: `refresh.${['shp', 'dbf', 'prj'][index]}`, size: bytes.length,
  upload_date: '', metadata: { sha256: 'a'.repeat(64) },
}));
const manifest = JSON.parse(await readFile(resolve(frontend, '../plugin-host/visualizations/shapefile.json'), 'utf8'));
let effectivePlugin = { ...manifest, id: 'test-shapefile-host-refresh', enabled: true };
let revision = '2'.repeat(64);
const hostPath = resolve(frontend, 'src/visualizations/VisualizationHost.vue');
const adaptersPath = resolve(frontend, 'src/visualizations/adapters.ts');
const shapePath = resolve(frontend, 'src/components/filePreviews/ShapefilePreview.vue');
const memoryOutputDir = resolve(frontend, '.shapefile-host-refresh-memory');
const entry = `import {createApp,h,nextTick,ref} from 'vue';
import {createRouter,createMemoryHistory} from 'vue-router';
import Host from ${JSON.stringify(hostPath)};
import {useFilePanel} from ${JSON.stringify(resolve(frontend, 'src/composables/useFilePanel.ts'))};
const clone=value=>JSON.parse(JSON.stringify(value));const initial=${JSON.stringify(files)};
const currentFile=ref(clone(initial[0]));const panel=useFilePanel();panel.relatedFiles.value=clone(initial);
window.publishEquivalentFiles=async()=>{const oldFile=currentFile.value,oldList=panel.relatedFiles.value;
 currentFile.value=clone(oldFile);panel.relatedFiles.value=clone(oldList);await nextTick();
 return{fileReplaced:oldFile!==currentFile.value,listReplaced:oldList!==panel.relatedFiles.value,membersReplaced:oldList.every((item,index)=>item!==panel.relatedFiles.value[index])};};
window.publishFileVersion=async(version)=>{currentFile.value={...clone(currentFile.value),metadata:{...currentFile.value.metadata,sha256:version}};
 panel.relatedFiles.value=panel.relatedFiles.value.map(item=>item.file_id===currentFile.value.file_id?clone(currentFile.value):clone(item));await nextTick();};
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});
await router.push('/chat/synthetic-host-refresh');const app=createApp({render:()=>h(Host,{file:currentFile.value})});
app.use(router);app.mount('#app');window.unmountHarness=()=>app.unmount();window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'shapefile-host-refresh-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  // Native ESM chunks retain module namespace semantics for defineAsyncComponent,
  // just as Vite does. No generated bundle is written to the workspace.
  splitting: true, outdir: memoryOutputDir, entryNames: 'entry', chunkNames: 'chunks/[name]-[hash]',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-host-selected-adapter', setup(builder) {
    // Retain the real registry and its async component. Only unrelated formats
    // are inert, preventing this one-format test from bundling every large SDK.
    builder.onResolve({ filter: /\.vue$/ }, args => {
      if (args.importer === adaptersPath) {
        const path = resolve(dirname(args.importer), args.path);
        if (path !== shapePath) return { path, namespace: 'unselected-adapter' };
      }
    });
    builder.onLoad({ filter: /.*/, namespace: 'unselected-adapter' }, () => ({ contents: 'export default {render:()=>null};', loader: 'js' }));
    builder.onLoad({ filter: /\.vue$/, namespace: 'file' }, async ({ path }) => {
      const source = await readFile(path, 'utf8'), id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' });
      const styles = descriptor.styles.map(style => compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` }).code).join('\n');
      return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
const moduleAssets = new Map(bundle.outputFiles.map(file => [
  `/__shapefile_host_refresh__/${relative(memoryOutputDir, file.path)}`, Buffer.from(file.contents),
]));
assert.ok(moduleAssets.has('/__shapefile_host_refresh__/entry.js'));
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run the ordinary frontend build once before this check');
const css = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'dataseek-shapefile-host-refresh-'));
const report = { name: 'shapefile-host-refresh', passed: false, errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], catalogRequests: [], byteRequests: [], checks: {} };
let holdNextGeometry = false, heldRequest = null;
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({ executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
  headless: true, args: ['--disable-background-networking', '--disable-component-update'] });
const context = await browser.newContext({ viewport: { width: 1160, height: 860 } });
const page = await context.newPage();
page.setDefaultTimeout(10000); page.setDefaultNavigationTimeout(30000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await page.addInitScript(() => {
  // Observation only: forward native fetch unchanged, including its AbortSignal.
  window.__byteFetches = [];
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, options) => {
    if (String(input).includes('/files/') && String(input).endsWith('/visualization')) {
      const entry = { aborted: options?.signal?.aborted === true, finished: false };
      window.__byteFetches.push(entry);
      options?.signal?.addEventListener('abort', () => { entry.aborted = true; }, { once: true });
      return nativeFetch(input, options).finally(() => { entry.finished = true; });
    }
    return nativeFetch(input, options);
  };
});
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__shapefile_host_refresh__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__shapefile_host_refresh__/project.css"><style>html,body{margin:0;height:100%;font-family:Arial,sans-serif}#app{display:flex;flex-direction:column;width:1120px;height:820px;margin:20px}*{box-sizing:border-box}</style></head><body><div id="app"></div><script type="module" src="/__shapefile_host_refresh__/entry.js"></script></body></html>' });
  if (moduleAssets.has(url.pathname)) return route.fulfill({ contentType: 'application/javascript', body: moduleAssets.get(url.pathname) });
  if (url.pathname === '/__shapefile_host_refresh__/project.css') return route.fulfill({ contentType: 'text/css', body: css });
  if (request.method() === 'GET' && url.pathname === '/api/v1/visualizations') {
    report.catalogRequests.push({ time: Date.now(), revision, version: effectivePlugin.version, enabled: effectivePlugin.enabled, maxInputBytes: effectivePlugin.limits.max_input_bytes });
    // Serialize afresh for every real HTTP poll; the app receives new objects.
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { engine: 'cordis', revision, plugins: [effectivePlugin] } }) });
  }
  if (request.method() === 'POST' && url.pathname === `/api/v1/files/${fixtureId}/visualization`) {
    const body = request.postDataJSON(), resourceId = body.options?.resource_id || fixtureId;
    if (body.plugin_id === effectivePlugin.id && body.operation === 'bytes' && resources.has(resourceId)) {
      const info = { resourceId, version: effectivePlugin.version, revision, held: holdNextGeometry && resourceId === fixtureId };
      report.byteRequests.push(info);
      if (info.held) {
        holdNextGeometry = false;
        let release, finished;
        const gate = new Promise(resolve => { release = resolve; }), done = new Promise(resolve => { finished = resolve; });
        heldRequest = { release, done };
        await gate;
        try { await route.fulfill({ contentType: 'application/octet-stream', body: resources.get(resourceId), headers: {
          'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': info.revision, 'X-Visualization-Plugin': effectivePlugin.id,
        } }); } catch { /* The original request was intentionally aborted. */ }
        finally { finished(); }
        return;
      }
      return route.fulfill({ contentType: 'application/octet-stream', body: resources.get(resourceId), headers: {
        'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': revision, 'X-Visualization-Plugin': effectivePlugin.id,
      } });
    }
  }
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`); return route.abort();
});

const map = page.getByLabel('Shapefile 地图', { exact: true });
const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const refresh = async (manual = false) => {
  const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/visualizations');
  if (manual) await page.getByRole('button', { name: '刷新', exact: true }).click();
  else await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await response; await settle();
};
const state = () => page.evaluate(() => {
  const svg = document.querySelector('svg[aria-label="Shapefile 地图"]');
  return { sameMap: svg === window.__preservedMap, viewBox: svg?.getAttribute('viewBox'),
    selectedRecords: [...document.querySelectorAll('tbody tr[aria-selected="true"]')].map(row => Number(row.dataset.recordIndex)),
    rowIndices: [...document.querySelectorAll('tbody tr[data-record-index]')].map(row => Number(row.dataset.recordIndex)),
    filter: document.querySelector('select[aria-label="属性表范围"]')?.value };
});
async function readyAtFirstPage() {
  await map.waitFor(); await page.getByText(label(0), { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('tbody tr[data-record-index]').length === 100);
  assert.equal(await map.locator('circle').count(), points.length);
}
async function assertReload(previousByteCount, check) {
  await readyAtFirstPage(); await settle();
  assert.equal(report.byteRequests.length, previousByteCount + 3, `${check}: exactly one fresh SHP/DBF/PRJ load`);
  assert.equal((await state()).sameMap, false, `${check}: the old preview scope must be replaced`);
  assert.deepEqual((await state()).selectedRecords, []);
  report.checks[check] = { addedByteRequests: 3, resetToFirstPage: true, newPreviewScope: true };
  await page.evaluate(() => { window.__preservedMap = document.querySelector('svg[aria-label="Shapefile 地图"]'); });
}

try {
  await page.goto('http://localhost:7001/__shapefile_host_refresh__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  await readyAtFirstPage(); await settle();
  assert.equal(report.byteRequests.length, 3);
  const target = await map.evaluate((svg, [x, y]) => { const p = new DOMPoint(x, -y).matrixTransform(svg.getScreenCTM()); return { x: p.x, y: p.y }; }, points[124]);
  await page.mouse.click(target.x, target.y);
  await page.locator('tr[data-record-index="124"][aria-selected="true"]').waitFor();
  await page.getByTitle('放大', { exact: true }).click();
  await settle();
  await page.evaluate(() => { window.__preservedMap = document.querySelector('svg[aria-label="Shapefile 地图"]'); });
  const preserved = await state();
  assert.deepEqual(preserved.selectedRecords, [124]);
  assert.deepEqual(preserved.rowIndices, Array.from({ length: 25 }, (_, index) => index + 100));
  const replacements = await page.evaluate(() => window.publishEquivalentFiles());
  assert.deepEqual(replacements, { fileReplaced: true, listReplaced: true, membersReplaced: true });
  await settle(); assert.equal(report.byteRequests.length, 3); assert.deepEqual(await state(), preserved);

  const timedPolls = [];
  for (let index = 0; index < 2; index++) {
    const before = report.catalogRequests.length;
    await page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/visualizations', { timeout: 20000 });
    await settle();
    assert.equal(report.catalogRequests.length, before + 1);
    const newest = report.catalogRequests.at(-1), previous = report.catalogRequests.at(-2);
    assert.ok(newest.time - previous.time >= 14000, 'Exercise the production 15-second interval, not a mocked or disabled poll');
    timedPolls.push(newest.time - previous.time);
    assert.deepEqual(await page.evaluate(() => window.publishEquivalentFiles()), replacements);
    await settle();
    assert.equal(report.byteRequests.length, 3, 'Equal catalog/file/list polling must not fetch preview bytes again');
    assert.deepEqual(await state(), preserved, 'Polling must preserve DOM, selection, page, and zoom');
  }
  report.checks.equivalentPolling = { actualPolls: 2, intervalMilliseconds: timedPolls, totalByteRequests: 3, selection: [124], page: 2, viewBox: preserved.viewBox, samePreviewScope: true, replacementObjectsConfirmed: true };
  await page.screenshot({ path: join(output, 'state-preserved-after-two-polls.png') });

  let previousBytes = report.byteRequests.length;
  await refresh(true); await assertReload(previousBytes, 'explicitManualRefresh');
  previousBytes = report.byteRequests.length;
  revision = '7'.repeat(64);
  await refresh(); await assertReload(previousBytes, 'catalogRevisionChange');
  previousBytes = report.byteRequests.length;
  // Keep revision fixed in these cases, so a revision key or a manual-refresh
  // epoch cannot conceal a missing plugin/file semantic identity dependency.
  effectivePlugin = { ...effectivePlugin, version: '1.0.1' };
  await refresh(); await assertReload(previousBytes, 'pluginVersionChange');
  previousBytes = report.byteRequests.length;
  await page.evaluate(() => window.publishFileVersion('b'.repeat(64)));
  await assertReload(previousBytes, 'fileVersionChange');
  previousBytes = report.byteRequests.length;
  effectivePlugin = { ...effectivePlugin, limits: { ...effectivePlugin.limits, max_input_bytes: effectivePlugin.limits.max_input_bytes - 1024 } };
  await refresh(); await assertReload(previousBytes, 'capabilityLimitChange');

  holdNextGeometry = true;
  effectivePlugin = { ...effectivePlugin, version: '1.0.2' };
  await refresh();
  await page.waitForFunction(() => window.__byteFetches.some(request => !request.finished));
  assert.ok(heldRequest, 'A real fetch must be in flight before revoking its capability');
  const pendingFetchIndex = await page.evaluate(() => window.__byteFetches.length - 1);
  effectivePlugin = { ...effectivePlugin, enabled: false };
  await refresh();
  await page.getByText('此格式没有已启用的可视化插件。', { exact: false }).waitFor();
  await page.waitForFunction(index => window.__byteFetches[index].aborted, pendingFetchIndex);
  assert.equal(await map.count(), 0);
  heldRequest.release(); await heldRequest.done; await settle();
  assert.equal(await map.count(), 0, 'A late response must not revive a disabled preview');
  report.checks.capabilityRevocation = { inFlightSignalAborted: true, previewUnmounted: true, lateResponseIgnored: true };
  assert.deepEqual(report.errors, []); assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.externalAttempts, []); assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.unmountHarness());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message; report.stack = error.stack; process.exitCode = 1;
  await page.screenshot({ path: join(output, 'failure.png') }).catch(() => {});
} finally {
  if (heldRequest) heldRequest.release();
  await context.close(); await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report)); console.log(`BROWSER_REPORT=${output}`);
}
