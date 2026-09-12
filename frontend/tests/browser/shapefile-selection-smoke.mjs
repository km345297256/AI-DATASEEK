/** Real SFC + native pointer regression. Synthetic files only; no listening server. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, parse } from '@vue/compiler-sfc';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) {
  try { playwrightEntry = require.resolve('playwright'); }
  catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
}
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const fixtureId = 'synthetic-shapefile-selection';
const points = Array.from({ length: 125 }, (_, index) => [100 + index % 25 * 0.1, 20 + Math.floor(index / 25) * 0.2]);
const label = index => `feature-${String(index + 1).padStart(4, '0')}`;

function pointShapefile() {
  const data = Buffer.alloc(100 + points.length * 28);
  data.writeInt32BE(9994, 0); data.writeInt32BE(data.length / 2, 24);
  data.writeInt32LE(1000, 28); data.writeInt32LE(1, 32);
  [100, 20, 102.4, 20.8].forEach((value, index) => data.writeDoubleLE(value, 36 + index * 8));
  points.forEach(([x, y], index) => {
    const start = 100 + index * 28;
    data.writeInt32BE(index + 1, start); data.writeInt32BE(10, start + 4);
    data.writeInt32LE(1, start + 8); data.writeDoubleLE(x, start + 12); data.writeDoubleLE(y, start + 20);
  });
  return data;
}

function attributeDbf() {
  const headerLength = 65, fieldWidth = 16, recordLength = fieldWidth + 1;
  const data = Buffer.alloc(headerLength + points.length * recordLength + 1, 0);
  data[0] = 3; data.writeUInt32LE(points.length, 4);
  data.writeUInt16LE(headerLength, 8); data.writeUInt16LE(recordLength, 10);
  data.write('label', 32); data[43] = 67; data[48] = fieldWidth; data[64] = 13;
  points.forEach((_, index) => {
    const start = headerLength + index * recordLength;
    data.fill(32, start, start + recordLength); data.write(label(index), start + 1);
  });
  data[data.length - 1] = 26;
  return data;
}

const resources = new Map([
  [fixtureId, pointShapefile()],
  [`${fixtureId}-attributes`, attributeDbf()],
  [`${fixtureId}-projection`, Buffer.from('LOCAL_CS["Synthetic local coordinates",UNIT["metre",1]]')],
]);
const relatedFiles = [...resources].map(([file_id, bytes], index) => ({
  file_id, filename: `selection.${['shp', 'dbf', 'prj'][index]}`, size: bytes.length, upload_date: '',
}));
const manifest = JSON.parse(await readFile(resolve(frontend, '../plugin-host/visualizations/shapefile.json'), 'utf8'));
const plugin = { ...manifest, id: 'test-shapefile-selection', enabled: true };
const entry = `import {createApp,h} from 'vue';
import Preview from ${JSON.stringify(resolve(frontend, 'src/components/filePreviews/ShapefilePreview.vue'))};
import {useFilePanel} from ${JSON.stringify(resolve(frontend, 'src/composables/useFilePanel.ts'))};
useFilePanel().relatedFiles.value=${JSON.stringify(relatedFiles)};
const app=createApp({render:()=>h(Preview,{file:${JSON.stringify(relatedFiles[0])},plugin:${JSON.stringify(plugin)}})});
app.mount('#app');window.unmountHarness=()=>app.unmount();window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'shapefile-selection-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-shapefile-sfc', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = await readFile(path, 'utf8');
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' });
      const styles = descriptor.styles.map(style => compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` }).code).join('\n');
      return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
assert.equal(bundle.outputFiles.length, 1, 'This isolated preview must bundle into one in-memory module');
const projectCssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(projectCssName, 'Run the normal frontend build once before this browser check');
const projectCss = await readFile(resolve(frontend, 'dist/assets', projectCssName));
const output = await mkdtemp(join(tmpdir(), 'dataseek-shapefile-selection-'));
const report = { name: 'shapefile-selection', passed: false, errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], requestedResources: [], checks: {} };
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({
  executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
  headless: true, args: ['--disable-background-networking', '--disable-component-update'],
});
const context = await browser.newContext({ viewport: { width: 1160, height: 820 } });
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.setDefaultNavigationTimeout(30000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__shapefile_selection__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__shapefile_selection__/project.css"><style>html,body{margin:0;height:100%;font-family:Arial,sans-serif}#app{display:flex;flex-direction:column;width:1120px;height:780px;margin:20px}*{box-sizing:border-box}</style></head><body><div id="app"></div><script type="module" src="/__shapefile_selection__/entry.js"></script></body></html>' });
  if (url.pathname === '/__shapefile_selection__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__shapefile_selection__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  if (request.method() === 'POST' && url.pathname === `/api/v1/files/${fixtureId}/visualization`) {
    const body = request.postDataJSON(), resourceId = body.options?.resource_id || fixtureId;
    if (body.plugin_id === plugin.id && body.operation === 'bytes' && resources.has(resourceId)) {
      report.requestedResources.push(resourceId);
      return route.fulfill({ contentType: 'application/octet-stream', body: resources.get(resourceId), headers: {
        'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': '2'.repeat(64), 'X-Visualization-Plugin': plugin.id,
      } });
    }
  }
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);
  return route.abort();
});

const visibleRecordIndices = () => page.locator('tbody tr[data-record-index]').evaluateAll(rows => rows.map(row => Number(row.dataset.recordIndex)));
const screenPoint = point => page.getByLabel('Shapefile 地图', { exact: true }).evaluate((svg, [x, y]) => {
  const transformed = new DOMPoint(x, -y).matrixTransform(svg.getScreenCTM());
  return { x: transformed.x, y: transformed.y };
}, point);
async function dragBounds([minX, minY, maxX, maxY]) {
  const start = await screenPoint([minX, maxY]), end = await screenPoint([maxX, minY]);
  await page.mouse.move(start.x, start.y); await page.mouse.down();
  await page.mouse.move(end.x, end.y, { steps: 12 }); await page.mouse.up();
}
async function waitForRecords(expected) {
  await page.waitForFunction(expected => JSON.stringify([...document.querySelectorAll('tbody tr[data-record-index]')].map(row => Number(row.dataset.recordIndex))) === JSON.stringify(expected), expected);
  assert.deepEqual(await visibleRecordIndices(), expected);
}

try {
  await page.goto('http://localhost:7001/__shapefile_selection__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  await page.getByText(label(0), { exact: true }).waitFor();
  const map = page.getByLabel('Shapefile 地图', { exact: true });
  assert.equal(await map.locator('circle').count(), points.length);
  assert.equal(await map.locator('image').count(), 0, 'LOCAL_CS must not request public map tiles');
  await waitForRecords(Array.from({ length: 100 }, (_, index) => index));

  await page.getByRole('button', { name: '框选要素', exact: true }).click();
  await dragBounds([101.15, 20.15, 101.35, 20.25]);
  const rectangle = page.getByTestId('selection-rectangle');
  await rectangle.waitFor();
  const stroke = await rectangle.evaluate(rect => {
    const style = getComputedStyle(rect), matrix = rect.getScreenCTM(), box = rect.getBBox();
    return { strokeWidth: parseFloat(style.strokeWidth), vectorEffect: style.vectorEffect,
      fillOpacity: Number(style.fillOpacity), screenScale: matrix.a,
      x: box.x, y: box.y, width: box.width, height: box.height };
  });
  assert.equal(stroke.vectorEffect, 'non-scaling-stroke');
  assert.equal(stroke.strokeWidth, 1);
  assert.ok(stroke.fillOpacity > 0 && stroke.fillOpacity < 0.2);
  assert.ok(stroke.screenScale > 100, 'Small coordinate spans must exercise the former oversized-stroke bug');
  for (const [field, expected] of Object.entries({ x: 101.15, y: -20.25, width: 0.2, height: 0.1 })) {
    assert.ok(Math.abs(stroke[field] - expected) < 0.003, `${field}: pointer must map through the real SVG transform`);
  }
  await waitForRecords([37, 38]);
  assert.equal(await page.locator('tbody tr[aria-selected="true"]').count(), 2);
  assert.equal(await map.locator('circle[fill="#dc2626"]').count(), 2);
  await page.getByText(label(37), { exact: true }).waitFor();
  await page.getByText(label(38), { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => window.getSelection().toString()), '');
  report.checks.selection = { ...stroke, selectedRecordIndices: [37, 38], nativeTextSelection: false };
  await page.screenshot({ path: join(output, 'selected-two-records.png') });

  await page.getByRole('button', { name: '退出框选', exact: true }).click();
  assert.equal(await rectangle.count(), 0);
  await waitForRecords([37, 38]);
  report.checks.cancelMode = { rectangleRemoved: true, confirmedSelectionPreserved: true };

  await page.getByRole('button', { name: '清除选择', exact: true }).click();
  await waitForRecords(Array.from({ length: 100 }, (_, index) => index));
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await waitForRecords(Array.from({ length: 25 }, (_, index) => index + 100));
  assert.equal(await page.getByRole('button', { name: '下一页', exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: '上一页', exact: true }).click();
  await waitForRecords(Array.from({ length: 100 }, (_, index) => index));
  report.checks.pagination = { firstPageRows: 100, secondPageRows: 25, returnedToFirstPage: true };

  const lastPoint = await screenPoint(points[124]);
  await page.mouse.click(lastPoint.x, lastPoint.y);
  const lastRow = page.locator('tr[data-record-index="124"]');
  await lastRow.waitFor();
  await page.getByText(label(124), { exact: true }).waitFor();
  await page.waitForFunction(() => {
    const row = document.querySelector('tr[data-record-index="124"]');
    if (!row) return false;
    const viewport = row.closest('table').parentElement.getBoundingClientRect(), bounds = row.getBoundingClientRect();
    return bounds.top >= viewport.top - 1 && bounds.bottom <= viewport.bottom + 1;
  });
  report.checks.mapAttributeLink = { originalRecordIndex: 124, label: label(124), rowScrolledIntoView: true };
  await page.screenshot({ path: join(output, 'map-to-second-page-attribute.png') });
  await page.locator('tr[data-record-index="100"]').click();
  assert.equal(await map.locator('circle').nth(100).getAttribute('fill'), '#dc2626');
  assert.equal(await map.locator('circle[fill="#dc2626"]').count(), 1);
  report.checks.attributeMapLink = { originalRecordIndex: 100, onlyMatchingGeometrySelected: true };
  assert.deepEqual([...new Set(report.requestedResources)].sort(), [...resources.keys()].sort());
  assert.deepEqual(report.errors, []); assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.externalAttempts, []); assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.unmountHarness());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message; report.stack = error.stack;
  await page.screenshot({ path: join(output, 'failure.png') }).catch(() => {});
  process.exitCode = 1;
} finally {
  await context.close(); await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  console.log(`BROWSER_REPORT=${output}`);
}
