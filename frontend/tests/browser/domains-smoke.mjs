/** No listening socket, production API access, user file access, or dataset writes.
 * Every browser request is fulfilled from trusted local build bytes / synthetic fixtures.
 */
import { build } from 'esbuild';
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc';
import { readFile, mkdtemp, writeFile, readdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { homedir, tmpdir } from 'node:os';
import { existsSync } from 'node:fs';
import { dirname, resolve, join, extname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { domainCases } from './domain-fixtures.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
const bundledPlaywright = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) { try { playwrightEntry = require.resolve('playwright'); } catch { playwrightEntry = bundledPlaywright; } }
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const output = await mkdtemp(join(tmpdir(), 'dataseek-visualization-browser-'));
let cases = domainCases;
try { const { scientificCases } = await import('./scientific-fixtures.mjs'); cases = [...cases, ...scientificCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { officeCases } = await import('./office-fixtures.mjs'); cases = [...cases, ...officeCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
if (process.env.VISUALIZATION_BROWSER_CASES) cases = cases.filter((item) => process.env.VISUALIZATION_BROWSER_CASES.split(',').includes(item.name));
if (!cases.length) throw new Error('No browser cases selected');
const modules = [...new Set(cases.map((item) => item.component))];
const safeDescriptors = Object.fromEntries(cases.map((item) => [item.name, { file: { file_id: `synthetic-${item.name}`, filename: item.filename, upload_date: '', size: item.bytes?.byteLength || 128, ...item.file }, plugin: { contract_version: 2, id: `test-${item.name}`, version: '2.0.0', enabled: true, data_kind: 'extended', reader: item.reader, limits: { max_input_bytes: 64 * 1024 * 1024, max_output_bytes: 8 * 1024 * 1024 }, ...item.descriptor }, component: item.component }]));
const entry = `import {createApp,h} from 'vue';
const components={${modules.map((path) => `${JSON.stringify(path)}:()=>import(${JSON.stringify(resolve(frontend, 'src/visualizations/extended', path))})`).join(',')}};
const cases=${JSON.stringify(safeDescriptors)}; let app;
window.mountHarness=async(name)=>{if(app)app.unmount();document.getElementById('app').replaceChildren();const item=cases[name],module=await components[item.component]();app=createApp({render:()=>h(module.default,{file:item.file,plugin:item.plugin})});app.mount('#app');};
window.unmountHarness=()=>{if(app){app.unmount();app=null;}document.getElementById('app').replaceChildren();};
window.harnessReady=true;`;

await build({ stdin: { contents: entry, sourcefile: 'entry.js', resolveDir: frontend }, bundle: true, splitting: true, format: 'esm', platform: 'browser', target: 'es2022', outdir: output, publicPath: '/__visualization_test__', entryNames: 'entry', chunkNames: 'chunks/[name]-[hash]', assetNames: 'assets/[name]-[hash]', write: true, minify: false, sourcemap: false, logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }), '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  loader: { '.png': 'file', '.jpg': 'file', '.jpeg': 'file', '.gif': 'file', '.svg': 'file', '.wasm': 'file', '.woff': 'file', '.woff2': 'file', '.ttf': 'file' },
  plugins: [{ name: 'trusted-vue-sfc', setup(build) {
    build.onResolve({ filter: /^xmlbuilder2$/ }, () => ({ path: resolve(frontend, 'src/visualizations/extended/scientific/vtkBrowserXml.ts') }));
    build.onLoad({ filter: /\.vue$/ }, async (args) => {
      const source = await readFile(args.path, 'utf8'); const id = createHash('sha256').update(args.path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: args.path }); if (errors.length) throw errors[0];
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' });
      const styles = descriptor.styles.map((style) => compileStyle({ filename: args.path, source: style.content, scoped: style.scoped, id: `data-v-${id}` }).code).join('\n');
      return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(args.path) };
    });
    build.onResolve({ filter: /\?url$/ }, async (args) => { const path = args.path.replace(/\?url$/, ''); return { path: path.startsWith('.') || path.startsWith('/') ? resolve(args.resolveDir, path) : require.resolve(path), namespace: 'asset-url' }; });
    build.onLoad({ filter: /.*/, namespace: 'asset-url' }, async (args) => ({ contents: await readFile(args.path), loader: 'file' }));
  } }] });

const mime = { '.html': 'text/html', '.js': 'application/javascript', '.mjs': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.wasm': 'application/wasm', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.gif': 'image/gif', '.woff2': 'font/woff2', '.ttf': 'font/ttf' };
const projectCss = (await readdir(resolve(frontend, 'dist/assets'))).find((name) => /^index-.*\.css$/.test(name));
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const executablePath = process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined);
const browser = await chromium.launch({ executablePath, headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disable-background-networking', '--disable-component-update'] });
const results = [];
try {
  for (const scenario of cases) {
    const record = { name: scenario.name, sdk: scenario.component, passed: false, errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], apiRequests: [], webgl: [] };
    const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.on('pageerror', (error) => record.errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') record.consoleErrors.push(message.text().slice(0, 800)); });
    await page.addInitScript(() => {
      window.__webglStats = [];
      window.__webglDraws = 0;
      window.__activeWorkers = new Set(); window.__activeBlobs = new Set(); window.__workerTrace = [];
      const NativeWorker = window.Worker;
      window.Worker = class extends NativeWorker { constructor(...args) { super(...args); window.__activeWorkers.add(this); const trace = { url: String(args[0]), received: 0, errors: [] }; window.__workerTrace.push(trace); this.addEventListener('message', () => trace.received++); this.addEventListener('error', error => trace.errors.push(error.message)); } terminate() { window.__activeWorkers.delete(this); return super.terminate(); } };
      const createBlob = URL.createObjectURL.bind(URL), revokeBlob = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = (blob) => { const url = createBlob(blob); window.__activeBlobs.add(url); return url; };
      URL.revokeObjectURL = (url) => { window.__activeBlobs.delete(url); return revokeBlob(url); };
      const original = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (type, ...args) { const result = original.call(this, type, ...args); if (type.startsWith('webgl') && result && !this.dataset.testContextRecorded) { this.dataset.testContextRecorded = '1'; window.__webglStats.push({ type, renderer: result.getParameter(result.RENDERER), vendor: result.getParameter(result.VENDOR) }); } return result; };
      for (const name of ['WebGLRenderingContext', 'WebGL2RenderingContext']) { const type = window[name]; if (!type) continue; for (const method of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) { const original = type.prototype[method]; if (typeof original === 'function') type.prototype[method] = function (...args) { window.__webglDraws++; return original.apply(this, args); }; } }
    });
    if (scenario.init) await scenario.init(page);
    await context.route('**/*', async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (!['localhost', '127.0.0.1'].includes(url.hostname) || url.port !== '7001') { record.externalAttempts.push(`${request.method()} ${url.origin}${url.pathname}`); return route.abort(); }
      const pathname = decodeURIComponent(url.pathname);
      if (pathname === '/__visualization_test__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__visualization_test__/entry.css"><link rel="stylesheet" href="/__visualization_test__/project.css"><style>html,body{margin:0;height:100%;font-family:Arial,sans-serif}#app{height:700px;width:1060px;margin:20px}*{box-sizing:border-box}button{cursor:pointer}canvas{max-width:100%}</style></head><body><div id="app"></div><script type="module" src="/__visualization_test__/entry.js"></script></body></html>' });
      if (pathname === '/__visualization_test__/project.css' && projectCss) return route.fulfill({ contentType: 'text/css', body: await readFile(resolve(frontend, 'dist/assets', projectCss)) });
      if (pathname.startsWith('/api/v1/files/')) {
        record.apiRequests.push({ method: request.method(), path: pathname });
        if (scenario.api) { const response = await scenario.api(request); if (response) return route.fulfill(response); }
        if (request.method() !== 'POST' || !pathname.includes(`/synthetic-${scenario.name}/`)) { record.unexpectedRequests.push(pathname); return route.abort(); }
        if (pathname.endsWith('/visualization-content')) return route.fulfill({ contentType: 'application/octet-stream', body: Buffer.from(scenario.bytes), headers: { 'X-Preview-Version': '1'.repeat(64) } });
        if (pathname.endsWith('/visualization-v2') && scenario.preview) {
          const options = request.postDataJSON(); const raw = typeof scenario.preview === 'function' ? await scenario.preview(options) : scenario.preview;
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { ...raw, version: '1'.repeat(64), revision: '2'.repeat(64), plugin_id: `test-${scenario.name}` } }) });
        }
        record.unexpectedRequests.push(pathname); return route.abort();
      }
      let file;
      if (pathname.startsWith('/__visualization_test__/')) file = resolve(output, pathname.slice('/__visualization_test__/'.length));
      else if (pathname.startsWith('/visualization-assets/')) file = resolve(frontend, 'dist', pathname.slice(1));
      else { record.unexpectedRequests.push(pathname); return route.abort(); }
      if (!file.startsWith(output + '/') && !file.startsWith(resolve(frontend, 'dist/visualization-assets') + '/')) { record.unexpectedRequests.push(pathname); return route.abort(); }
      try { return await route.fulfill({ contentType: mime[extname(file)] || 'application/octet-stream', body: await readFile(file) }); }
      catch {
        if (pathname === '/__visualization_test__/entry.css') return route.fulfill({ contentType: 'text/css', body: '/* Selected components have no extracted CSS. */' });
        // Fixed trusted SDK assets may not be in a prior production build yet.
        if (/^\/visualization-assets\/maplibre\/maplibre-gl(?:-worker|-shared)?\.(mjs|css)$/.test(pathname)) {
          return route.fulfill({ contentType: mime[extname(pathname)], body: await readFile(resolve(frontend, 'node_modules/maplibre-gl/dist', pathname.split('/').pop())) });
        }
        if (pathname === '/visualization-assets/cesium/Cesium.js') {
          return route.fulfill({ contentType: 'application/javascript', body: await readFile(resolve(frontend, 'node_modules/cesium/Build/Cesium/Cesium.js')) });
        }
        if (pathname === '/visualization-assets/domains/host.html') return route.fulfill({ contentType: 'text/html', body: await readFile(resolve(frontend, 'src/visualizations/extended/domains/assets/host.html')) });
        record.unexpectedRequests.push(pathname); return route.fulfill({ status: 404, body: 'Missing test asset' });
      }
    });
    try {
      await page.goto('http://localhost:7001/__visualization_test__/', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => window.harnessReady, { timeout: 45000 });
      await page.evaluate((name) => window.mountHarness(name), scenario.name);
      if (scenario.setup) await scenario.setup(page);
      if (scenario.ready) await scenario.ready(page);
      else await page.waitForTimeout(1500);
      const alerts = await page.locator('[role="alert"]').allTextContents();
      record.alerts = alerts.filter((value) => value.trim());
      if (record.alerts.length) throw new Error(record.alerts.join('; '));
      if (scenario.verify) record.checks = await scenario.verify(page);
      record.webgl = await page.evaluate(() => window.__webglStats);
      record.drawCalls = await page.evaluate(() => window.__webglDraws);
      record.frameDrawCalls = await Promise.all(page.frames().filter((frame) => frame !== page.mainFrame()).map((frame) => frame.evaluate(() => window.__webglDraws).catch(() => null)));
      record.canvasCount = await page.locator('canvas').count();
      await page.screenshot({ path: join(output, `${scenario.name}.png`) });
      await page.evaluate(() => window.unmountHarness()); await page.waitForTimeout(250);
      record.unmounted = await page.locator('#app').evaluate((element) => element.childElementCount === 0);
      const stoppedDraws = await page.evaluate(() => window.__webglDraws); await page.waitForTimeout(250);
      record.cleanup = await page.evaluate((stoppedDraws) => ({ activeWorkers: window.__activeWorkers.size, activeBlobs: window.__activeBlobs.size, drawingStopped: window.__webglDraws === stoppedDraws, childFramesRemoved: document.querySelectorAll('iframe').length === 0 }), stoppedDraws);
      record.libraryBlobBaseline = scenario.libraryBlobBaseline ?? 0;
      record.passed = record.unmounted && record.cleanup.drawingStopped && !record.cleanup.activeWorkers && record.cleanup.activeBlobs === record.libraryBlobBaseline && record.cleanup.childFramesRemoved && !record.errors.length && !record.unexpectedRequests.length && !record.externalAttempts.length && !record.consoleErrors.length;
    } catch (error) { record.failure = error.message; record.alerts = await page.locator('[role="alert"]').allTextContents().catch(() => []); record.workerTrace = await Promise.all(page.frames().map(frame => frame.evaluate(() => window.__workerTrace).catch(() => []))); await page.screenshot({ path: join(output, `${scenario.name}-failure.png`) }).catch(() => {}); }
    finally { await context.close(); }
    results.push(record); console.log(JSON.stringify(record));
  }
} finally { await browser.close(); }
await writeFile(join(output, 'results.json'), JSON.stringify(results, null, 2));
console.log(`BROWSER_REPORT=${output}`);
if (results.some((result) => !result.passed)) process.exitCode = 1;
