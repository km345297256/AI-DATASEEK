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
import assert from 'node:assert/strict';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
const bundledPlaywright = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) { try { playwrightEntry = require.resolve('playwright'); } catch { playwrightEntry = bundledPlaywright; } }
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const output = await mkdtemp(join(tmpdir(), 'dataseek-visualization-browser-'));
// These fault cases still use the real Cesium SDK and local fixture bytes.
// Only bundled imagery requests fail; data loading and scene creation remain real.
let cases = domainCases.flatMap(item => item.component === 'domains/CesiumPreview.vue'
  ? [item, { ...item, name: `${item.name}-basemap-fallback`, cesiumBasemapFailure: true }]
  : [item]);
// Cross-continent synthetic points force a globe overview so the bundled
// land/ocean imagery can be visually inspected at its intended coarse scale.
cases.push({ ...domainCases.find(item => item.name === 'cesium-geojson'), name: 'cesium-global-geojson',
  filename: 'synthetic-global-points.geojson', bytes: new TextEncoder().encode(JSON.stringify({
    type: 'FeatureCollection', features: [[-122,37],[-74,41],[-58,-34],[2,49],[25,-25],[120,35],[135,-25]].map(
      (coordinates,index) => ({type:'Feature',properties:{name:`Synthetic point ${index+1}`},geometry:{type:'Point',coordinates}})),
  })) });
try { const { mainMatrixCases } = await import('./main-matrix-fixtures.mjs'); cases = [...cases, ...mainMatrixCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { mainAstronomyCases } = await import('./main-astronomy-fixtures.mjs'); cases = [...cases, ...mainAstronomyCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { mainBioCases } = await import('./main-bio-fixtures.mjs'); cases = [...cases, ...mainBioCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { alignmentBrowserCases } = await import('./alignment-browser-fixtures.mjs'); cases = [...cases, ...alignmentBrowserCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { scientificCases } = await import('./scientific-fixtures.mjs'); cases = [...cases, ...scientificCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { officeCases } = await import('./office-fixtures.mjs'); cases = [...cases, ...officeCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { docxCases } = await import('./docx-fixtures.mjs'); cases = [...cases, ...docxCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { unifiedCases } = await import('./unified-fixtures.mjs'); cases = [...cases, ...unifiedCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchOneCases } = await import('./batch-one-fixtures.mjs'); cases = [...cases, ...batchOneCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchTwoCases } = await import('./batch-two-fixtures.mjs'); cases = [...cases, ...batchTwoCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchTwoGeoCases } = await import('./batch-two-geo-fixtures.mjs'); cases = [...cases, ...batchTwoGeoCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchThreeCziWindowCases } = await import('./batch-three-czi-window-fixtures.mjs'); cases = [...cases, ...batchThreeCziWindowCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchThreeInstrumentCases } = await import('./batch-three-instrument-fixtures.mjs'); cases = [...cases, ...batchThreeInstrumentCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchThreeArrayCases } = await import('./batch-three-array-fixtures.mjs'); cases = [...cases, ...batchThreeArrayCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { batchThreeOmeCases } = await import('./batch-three-ome-fixtures.mjs'); cases = [...cases, ...batchThreeOmeCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionGraphCases } = await import('./domain-expansion-graph-fixtures.mjs'); cases = [...cases, ...domainExpansionGraphCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionNexusCases } = await import('./domain-expansion-nexus-fixtures.mjs'); cases = [...cases, ...domainExpansionNexusCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionColumnarCases } = await import('./domain-expansion-columnar-fixtures.mjs'); cases = [...cases, ...domainExpansionColumnarCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionPhylogenyCases } = await import('./domain-expansion-phylogeny-fixtures.mjs'); cases = [...cases, ...domainExpansionPhylogenyCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionGribCases } = await import('./domain-expansion-grib-fixtures.mjs'); cases = [...cases, ...domainExpansionGribCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionSeismicCases } = await import('./domain-expansion-seismic-fixtures.mjs'); cases = [...cases, ...domainExpansionSeismicCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionEnviCases } = await import('./domain-expansion-envi-fixtures.mjs'); cases = [...cases, ...domainExpansionEnviCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { massSpectrumCases } = await import('./mass-spectrum-fixtures.mjs'); cases = [...cases, ...massSpectrumCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionDiffractionCases } = await import('./domain-expansion-diffraction-fixtures.mjs'); cases = [...cases, ...domainExpansionDiffractionCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionRippleCases } = await import('./domain-expansion-ripple-fixtures.mjs'); cases = [...cases, ...domainExpansionRippleCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionFcsCases } = await import('./domain-expansion-fcs-fixtures.mjs'); cases = [...cases, ...domainExpansionFcsCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionDicomCases } = await import('./domain-expansion-dicom-fixtures.mjs'); cases = [...cases, ...domainExpansionDicomCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionSpatialCases } = await import('./domain-expansion-spatial-fixtures.mjs'); cases = [...cases, ...domainExpansionSpatialCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { pointCloudWindowCases } = await import('./pointcloud-window-fixtures.mjs'); cases = [...cases, ...pointCloudWindowCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { geometryCases } = await import('./geometry-fixtures.mjs'); cases = [...cases, ...geometryCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { domainExpansionRadarCases } = await import('./domain-expansion-radar-fixtures.mjs'); cases = [...cases, ...domainExpansionRadarCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { ugridWindowCases } = await import('./ugrid-window-fixtures.mjs'); cases = [...cases, ...ugridWindowCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
try { const { sqliteTableCases } = await import('./sqlite-table-fixtures.mjs'); cases = [...cases, ...sqliteTableCases]; } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
const { databaseTableCases } = await import('./database-table-fixtures.mjs'); cases = [...cases, ...databaseTableCases];
const { databaseRecordsCases } = await import('./database-records-fixtures.mjs'); cases = [...cases, ...databaseRecordsCases];
const { pgDumpCases } = await import('./pg-dump-fixtures.mjs'); cases = [...cases, ...pgDumpCases];
const { sqlDumpCases } = await import('./sql-dump-fixtures.mjs'); cases = [...cases, ...sqlDumpCases];
const { physicalDatabaseCases } = await import('./physical-database-fixtures.mjs'); cases = [...cases, ...physicalDatabaseCases];
if (process.env.VISUALIZATION_BROWSER_CASES) cases = cases.filter((item) => process.env.VISUALIZATION_BROWSER_CASES.split(',').includes(item.name));
if (!cases.length) throw new Error('No browser cases selected');
const modules = [...new Set(cases.map((item) => item.component))];
const manifestDirectory = resolve(frontend, '../plugin-host/visualizations');
const manifests = await Promise.all((await readdir(manifestDirectory)).filter(name => name.endsWith('.json')).map(async name => JSON.parse(await readFile(resolve(manifestDirectory, name), 'utf8'))));
const safeDescriptors = Object.fromEntries(cases.map(item => {
  const adapter = item.descriptor?.adapter || item.name.replace(/^unified-/, '').split('-')[0];
  const manifest = manifests.find(plugin => plugin.adapter === adapter && plugin.reader === item.reader);
  if (!manifest) throw new Error(`Missing approved manifest for browser fixture ${item.name}`);
  return [item.name, { file: { file_id: `synthetic-${item.name}`, filename: item.filename, upload_date: '', size: item.bytes?.byteLength || 128, ...item.file }, plugin: { ...manifest, id: `test-${item.name}`, enabled: true }, component: item.component }];
}));
const entry = `import {createApp,h} from 'vue';
import {useFilePanel} from ${JSON.stringify(resolve(frontend, 'src/composables/useFilePanel.ts'))};
const related=${JSON.stringify(Object.fromEntries(cases.filter(item => item.relatedFiles).map(item => [item.name, item.relatedFiles])))};
const components={${modules.map((path) => `${JSON.stringify(path)}:()=>import(${JSON.stringify(resolve(frontend, 'src/visualizations/extended', path))})`).join(',')}};
const cases=${JSON.stringify(safeDescriptors)}; let app;
window.mountHarness=async(name)=>{if(app)app.unmount();document.getElementById('app').replaceChildren();useFilePanel().relatedFiles.value=related[name]||[];const item=cases[name],module=await components[item.component]();app=createApp({render:()=>h(module.default,{file:item.file,plugin:item.plugin})});app.mount('#app');};
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
const cesiumNaturalAsset = /^\/visualization-assets\/cesium\/Assets\/Textures\/NaturalEarthII\//;
const cesiumQaHooks = `;(() => {
 const qa=window.__cesiumQA={viewers:[],parseCalls:{geojson:0,czml:0},zoomCalls:0};
 const zoom=Cesium.Viewer.prototype.zoomTo;
 Cesium.Viewer.prototype.zoomTo=function(...args){if(!qa.viewers.includes(this))qa.viewers.push(this);qa.zoomCalls++;return zoom.apply(this,args);};
 for(const [name,key] of [['GeoJsonDataSource','geojson'],['CzmlDataSource','czml']]){
  const load=Cesium[name].load;Cesium[name].load=function(...args){qa.parseCalls[key]++;return load.apply(this,args);};
 }
})();`;

async function verifyCesiumBasemap(page, scenario, record) {
  const selector = page.getByTestId('cesium-basemap-select');
  const frame = page.frames().find(item => item !== page.mainFrame());
  assert.ok(frame, 'Cesium must own an isolated real SDK realm');
  await page.locator('[data-basemap][data-basemap-status="ready"]').waitFor({ timeout: 30000 });
  const expected = scenario.cesiumBasemapFailure ? 'grid' : 'natural-earth';
  assert.equal(await selector.inputValue(), expected);
  const csp = await frame.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content');
  assert.match(csp, /connect-src 'self' data: blob:/);
  assert.match(csp, /img-src 'self' data: blob:/);
  assert.doesNotMatch(csp, /(?:https?:|\*)/);
  if (scenario.cesiumBasemapFailure) await page.getByTestId('cesium-basemap-warning').waitFor();
  else {
    // Real fetched JPEG textures must reach the globe, not just an empty canvas.
    await frame.waitForFunction(() => window.__cesiumQA?.viewers[0]?.scene.globe._surface._tilesToRender.some(
      tile => tile.data?.imagery?.some(item => item.readyImagery?.texture)), undefined, { timeout: 30000 });
    assert.ok(record.cesiumAssets.some(item => /\/\d+\/\d+\/\d+\.jpg$/.test(item.path) && item.status === 200),
      'Natural Earth must load actual local image tiles');
  }
  await page.screenshot({ path: join(output, `${scenario.name}-initial-basemap.png`) });
  const before = await frame.evaluate(() => {
    const qa = window.__cesiumQA, viewer = qa.viewers[0];
    viewer.clock.shouldAnimate = false;
    viewer.camera.moveRight(100000); viewer.camera.moveUp(50000); viewer.scene.requestRender();
    qa.originalViewer = viewer; qa.originalSource = viewer.dataSources.get(0);
    const pose = () => ({position: [viewer.camera.position.x,viewer.camera.position.y,viewer.camera.position.z],
      direction: [viewer.camera.direction.x,viewer.camera.direction.y,viewer.camera.direction.z],
      up: [viewer.camera.up.x,viewer.camera.up.y,viewer.camera.up.z]});
    qa.pose = pose;
    return {pose:pose(), parseCalls:{...qa.parseCalls}, zoomCalls:qa.zoomCalls,
      entities:qa.originalSource.entities.values.length, time:Cesium.JulianDate.toIso8601(viewer.clock.currentTime)};
  });
  assert.equal(before.parseCalls.geojson + before.parseCalls.czml, 1);
  assert.equal(before.zoomCalls, 1);
  const apiBefore = record.apiRequests.length;
  const states = [];
  for (const mode of ['none', 'grid', 'natural-earth']) {
    await selector.selectOption(mode);
    const targetMode = scenario.cesiumBasemapFailure && mode === 'natural-earth' ? 'grid' : mode;
    await page.locator(`[data-basemap="${targetMode}"][data-basemap-status="ready"]`).waitFor({ timeout: 30000 });
    const state = await frame.evaluate(() => {
      const qa=window.__cesiumQA, viewer=qa.viewers[0];
      return {sameViewer:viewer===qa.originalViewer, sameSource:viewer.dataSources.get(0)===qa.originalSource,
        viewerCount:qa.viewers.length, parseCalls:{...qa.parseCalls}, zoomCalls:qa.zoomCalls, pose:qa.pose(),
        entities:viewer.dataSources.get(0).entities.values.length,
        time:Cesium.JulianDate.toIso8601(viewer.clock.currentTime), layers:viewer.imageryLayers.length};
    });
    assert.equal(state.sameViewer, true); assert.equal(state.sameSource, true); assert.equal(state.viewerCount, 1);
    assert.deepEqual(state.parseCalls, before.parseCalls); assert.equal(state.zoomCalls, before.zoomCalls);
    for (const key of ['position','direction','up']) state.pose[key].forEach((value,index) => {
      assert.ok(Math.abs(value-before.pose[key][index]) <= (key === 'position' ? 0.0001 : 1e-10),
        `Basemap ${mode} must not reset the user camera ${key}`);
    });
    assert.equal(state.time, before.time); assert.equal(state.entities, before.entities);
    assert.equal(state.layers, targetMode === 'none' ? 0 : 1);
    assert.equal(record.apiRequests.length, apiBefore, 'Basemap switching must not refetch or parse user input');
    states.push({ requested: mode, displayed: targetMode, layers: state.layers });
  }
  assert.ok(record.cesiumAssets.length > 0, 'The local imagery path must actually be exercised');
  if (scenario.cesiumBasemapFailure) assert.ok(record.cesiumAssets.every(item => item.status === 404));
  return {localNaturalEarthTextures:!scenario.cesiumBasemapFailure, imageryFailureFallback:!!scenario.cesiumBasemapFailure,
    parseCalls:before.parseCalls, cameraPreserved:true, clockPreserved:true, viewerAndDataSourcePreserved:true,
    originalEntities:before.entities, cspRemainsLocalOnly:true, states};
}
const projectCss = (await readdir(resolve(frontend, 'dist/assets'))).find((name) => /^index-.*\.css$/.test(name));
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const executablePath = process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined);
const browser = await chromium.launch({ executablePath, headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disable-background-networking', '--disable-component-update'] });
const results = [];
try {
  for (const scenario of cases) {
    const record = { name: scenario.name, sdk: scenario.component, passed: false, errors: [], consoleErrors: [], expectedConsoleErrors: [], externalAttempts: [], unexpectedRequests: [], apiRequests: [], cesiumAssets: [], webgl: [] };
    const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: scenario.deviceScaleFactor ?? 1 });
    const page = await context.newPage();
    page.on('pageerror', (error) => record.errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') {
      const localMissingImagery = scenario.cesiumBasemapFailure && cesiumNaturalAsset.test(new URL(message.location().url || 'http://localhost:7001/').pathname)
        && /Failed to load resource.*404/.test(message.text());
      (localMissingImagery ? record.expectedConsoleErrors : record.consoleErrors).push(message.text().slice(0, 800));
    } });
    await page.addInitScript(() => {
      window.__webglStats = [];
      window.__webglDraws = 0;
      window.__canvasReferences = new Set();
      window.__activeWorkers = new Set(); window.__activeBlobs = new Set(); window.__workerTrace = [];
      const NativeWorker = window.Worker;
      window.Worker = class extends NativeWorker { constructor(...args) { super(...args); window.__activeWorkers.add(this); const trace = { url: String(args[0]), received: 0, errors: [] }; window.__workerTrace.push(trace); this.addEventListener('message', () => trace.received++); this.addEventListener('error', error => trace.errors.push(error.message)); } terminate() { window.__activeWorkers.delete(this); return super.terminate(); } };
      const createBlob = URL.createObjectURL.bind(URL), revokeBlob = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = (blob) => { const url = createBlob(blob); window.__activeBlobs.add(url); return url; };
      URL.revokeObjectURL = (url) => { window.__activeBlobs.delete(url); return revokeBlob(url); };
      const original = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (type, ...args) { window.__canvasReferences.add(this); const result = original.call(this, type, ...args); if (type.startsWith('webgl') && result && !this.dataset.testContextRecorded) { this.dataset.testContextRecorded = '1'; window.__webglStats.push({ type, renderer: result.getParameter(result.RENDERER), vendor: result.getParameter(result.VENDOR) }); } return result; };
      for (const name of ['WebGLRenderingContext', 'WebGL2RenderingContext']) { const type = window[name]; if (!type) continue; for (const method of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) { const original = type.prototype[method]; if (typeof original === 'function') type.prototype[method] = function (...args) { window.__webglDraws++; return original.apply(this, args); }; } }
    });
    if (scenario.init) await scenario.init(page);
    await context.route('**/*', async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (!['localhost', '127.0.0.1'].includes(url.hostname) || url.port !== '7001') { record.externalAttempts.push(`${request.method()} ${url.origin}${url.pathname}`); return route.abort(); }
      const pathname = decodeURIComponent(url.pathname);
      if (cesiumNaturalAsset.test(pathname)) {
        record.cesiumAssets.push({ path: pathname, status: scenario.cesiumBasemapFailure ? 404 : 200 });
        if (scenario.cesiumBasemapFailure) return route.fulfill({ status: 404, body: 'Synthetic local imagery unavailable' });
      }
      if (pathname === '/visualization-assets/cesium/Cesium.js' && scenario.component === 'domains/CesiumPreview.vue') {
        const sdk = await readFile(resolve(frontend, 'node_modules/cesium/Build/Cesium/Cesium.js'), 'utf8');
        return route.fulfill({ contentType:'application/javascript', body:sdk + cesiumQaHooks });
      }
      if (pathname === '/__visualization_test__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__visualization_test__/entry.css"><link rel="stylesheet" href="/__visualization_test__/project.css"><style>html,body{margin:0;height:100%;font-family:Arial,sans-serif}#app{display:flex;flex-direction:column;height:700px;width:1060px;margin:20px}*{box-sizing:border-box}button{cursor:pointer}canvas{max-width:100%}</style></head><body><div id="app"></div><script type="module" src="/__visualization_test__/entry.js"></script></body></html>' });
      if (pathname === '/__visualization_test__/project.css' && projectCss) return route.fulfill({ contentType: 'text/css', body: await readFile(resolve(frontend, 'dist/assets', projectCss)) });
      if (pathname === '/vendor/o3dv.min.js') return route.fulfill({ contentType: 'application/javascript', body: await readFile(resolve(frontend, 'public/vendor/o3dv.min.js')) });
      if (pathname.startsWith('/api/v1/files/')) {
        record.apiRequests.push({ method: request.method(), path: pathname });
        if (scenario.api) { const response = await scenario.api(request); if (response) return route.fulfill(response); }
        if (request.method() !== 'POST' || !pathname.includes(`/synthetic-${scenario.name}/`)) { record.unexpectedRequests.push(pathname); return route.abort(); }
        if (pathname.endsWith('/visualization') && request.postDataJSON().operation === 'bytes') return route.fulfill({ contentType: 'application/octet-stream', body: Buffer.from(scenario.bytes), headers: { 'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': '2'.repeat(64), 'X-Visualization-Plugin': `test-${scenario.name}` } });
        if (pathname.endsWith('/visualization') && request.postDataJSON().operation === 'preview' && scenario.preview) {
          const options = request.postDataJSON(); const raw = typeof scenario.preview === 'function' ? await scenario.preview(options) : scenario.preview;
          const { contract_version: _contract, type: _type, reader: _reader, metadata = {}, warnings = [], sampled = false, kind: view_kind, ...payload } = raw;
          const mainKinds = { 'matrix-workbench': {tree:'tree',image:'array',series:'series'}, 'astronomy-workbench': {tree:'tree',image:'raster',table:'table',series:'series'}, 'alignment-browser': {tree:'tree',table:'table'}, 'sequence-browser': {tree:'tree',table:'table'}, 'genome-tracks': {tree:'tree',map:'features'}, 'blast-hits': {tree:'tree',table:'table'} };
          const kind = mainKinds[_reader]?.[view_kind] ?? (view_kind === 'geometry' && ['spatial-window','pointcloud-window','gro-trajectory','simulation-mesh','ugrid-window'].includes(_reader) ? 'geometry' : payload.data_base64 ? 'media' : payload.sections ? 'report' : payload.series ? 'series' : payload.geojson ? 'features' : payload.graph ? 'graph' : payload.array ? 'array' : payload.table ? 'table' : 'tree');
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { contract_version: 2, version: '1'.repeat(64), revision: '2'.repeat(64), plugin_id: `test-${scenario.name}`, kind, payload: { ...payload, view_kind }, metadata, warnings, sampled } }) });
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
      if (scenario.expectedError) {
        record.expectedError = String(scenario.expectedError);
        const matches = value => typeof scenario.expectedError === 'string' ? value.includes(scenario.expectedError) : new RegExp(scenario.expectedError.source, scenario.expectedError.flags.replace('g', '')).test(value);
        record.expectedErrorMatched = record.alerts.some(matches);
        if (!record.expectedErrorMatched) throw new Error(`Expected alert ${record.expectedError}, received: ${record.alerts.join('; ') || '(none)'}`);
      } else if (record.alerts.length) throw new Error(record.alerts.join('; '));
      if (scenario.verify) record.checks = await scenario.verify(page);
      if (scenario.component === 'domains/CesiumPreview.vue') record.cesiumChecks = await verifyCesiumBasemap(page, scenario, record);
      record.webgl = await page.evaluate(() => window.__webglStats);
      record.drawCalls = await page.evaluate(() => window.__webglDraws);
      record.frameDrawCalls = await Promise.all(page.frames().filter((frame) => frame !== page.mainFrame()).map((frame) => frame.evaluate(() => window.__webglDraws).catch(() => null)));
      record.canvasCount = await page.locator('canvas').count();
      await page.screenshot({ path: join(output, `${scenario.name}.png`) });
      if (scenario.beforeUnmount) await scenario.beforeUnmount(page);
      const requestsBeforeUnmount = record.apiRequests.length + record.cesiumAssets.length;
      await page.evaluate(() => window.unmountHarness()); await page.waitForTimeout(250);
      record.unmounted = await page.locator('#app').evaluate((element) => element.childElementCount === 0);
      const stoppedDraws = await page.evaluate(() => window.__webglDraws); await page.waitForTimeout(250);
      record.cleanup = await page.evaluate((stoppedDraws) => ({ activeWorkers: window.__activeWorkers.size, activeBlobs: window.__activeBlobs.size, drawingStopped: window.__webglDraws === stoppedDraws, childFramesRemoved: document.querySelectorAll('iframe').length === 0 }), stoppedDraws);
      if (scenario.verifyCleanup) record.resourceChecks = await scenario.verifyCleanup(page);
      if (scenario.component === 'domains/CesiumPreview.vue') {
        assert.equal(record.apiRequests.length + record.cesiumAssets.length, requestsBeforeUnmount,
          'Detached Cesium realm must not continue input or imagery requests');
        record.cleanup.cesiumRequestsStopped = true;
      }
      record.libraryBlobBaseline = scenario.libraryBlobBaseline ?? 0;
      record.passed = record.unmounted && record.cleanup.drawingStopped && !record.cleanup.activeWorkers && record.cleanup.activeBlobs === record.libraryBlobBaseline && record.cleanup.childFramesRemoved && !record.errors.length && !record.unexpectedRequests.length && !record.externalAttempts.length && !record.consoleErrors.length;
    } catch (error) { record.failure = error.message; record.failureStack = error.stack; record.alerts = await page.locator('[role="alert"]').allTextContents().catch(() => []); record.workerTrace = await Promise.all(page.frames().map(frame => frame.evaluate(() => window.__workerTrace).catch(() => []))); await page.screenshot({ path: join(output, `${scenario.name}-failure.png`) }).catch(() => {}); }
    finally { await context.close(); }
    results.push(record); console.log(JSON.stringify(record));
  }
} finally { await browser.close(); }
await writeFile(join(output, 'results.json'), JSON.stringify(results, null, 2));
console.log(`BROWSER_REPORT=${output}`);
if (results.some((result) => !result.passed)) process.exitCode = 1;
