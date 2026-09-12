/** Real isolated ENVI reader outputs, original 4×3×5 BIP cube; no live API. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const all = JSON.parse(readFileSync(new URL('./domain-expansion-envi-data.json', import.meta.url), 'utf8'));
const base = { component: 'EnviWindowPreview.vue', filename: 'synthetic-cube.hdr', reader: 'envi-window', descriptor: { adapter: 'envi-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } } };
async function select(page, kind, options) {
  await page.getByLabel('ENVI 视图', { exact: true }).selectOption(kind);
  for (const [key, value] of Object.entries(options)) await page.getByLabel(`ENVI ${{ band: '波段', x: '列', y: '行', width: '宽', height: '高', band_start: '起始波段', band_count: '波段数' }[key]}`, { exact: true }).fill(String(value));
}
async function plotReady(page, kind) {
  await page.waitForFunction(kind => { const p = document.querySelector('[data-testid=envi-plot]'); return p?._fullLayout && p?.data?.[0]?.type === (kind === 'image' ? 'heatmap' : 'scatter'); }, kind);
}
function scenario(group) {
  const data = all[group]; let requests = [];
  return { ...base, name: `domain-expansion-envi-${group}`, file: { size: data.tree.metadata.header_bytes },
    init: async () => { requests = []; },
    preview: request => { requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data.tree; }
      assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[request.kind].selected); return data[request.kind]; },
    ready: page => page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      assert.equal(data.tree.metadata.read_bytes, data.tree.metadata.header_bytes); assert.equal(data.tree.metadata.read_requests, 1);
      await select(page, 'image', data.image.selected); assert.equal(requests.length, 1);
      await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).click(); await plotReady(page, 'image');
      const image = await page.getByTestId('envi-plot').evaluate(p => {
        const raster = p.querySelector('.heatmaplayer image');
        return { x: p.data[0].x, y: p.data[0].y, z: p.data[0].z, gap: p.data[0].connectgaps, yRange: p._fullLayout.yaxis.range, rowPixelPositions: [p._fullLayout.yaxis.l2p(1), p._fullLayout.yaxis.l2p(2)], rasterBytes: (raster?.getAttribute('href') || raster?.getAttribute('xlink:href') || '').length };
      });
      assert.deepEqual(image.x, [1, 2, 3]); assert.deepEqual(image.y, [1, 2]); assert.deepEqual(image.z, [[211, 212, 213], [221, 222, 223]]); assert.equal(image.gap, false); assert.ok(image.yRange[0] > image.yRange[1], 'Final y-axis range must be reversed'); assert.ok(image.rowPixelPositions[0] < image.rowPixelPositions[1], 'Smaller source row must appear above larger row'); assert.ok(image.rasterBytes > 50, 'Actual Plotly heatmap raster must exist');
      await page.evaluate(() => { window.__oldEnviPlot = document.querySelector('[data-testid=envi-plot]'); });
      await select(page, 'series', data.series.selected); assert.equal(await page.locator('.js-plotly-plot').count(), 0); assert.equal(requests.length, 2);
      assert.equal(await page.evaluate(() => !window.__oldEnviPlot.isConnected && !window.__oldEnviPlot._fullLayout), true, 'Old image must be purged on selection change');
      await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).click(); await plotReady(page, 'series');
      const spectrum = await page.getByTestId('envi-plot').evaluate(p => ({ x: p.data[0].x, y: p.data[0].y, gap: p.data[0].connectgaps, title: p._fullLayout.xaxis.title.text, line: p.querySelector('.scatterlayer .js-line')?.getTotalLength(), points: p.querySelectorAll('.scatterlayer .point').length }));
      assert.deepEqual(spectrum.x, [400, 500, 600, 700, 800]); assert.deepEqual(spectrum.y, [12, 112, 212, 312, 412]); assert.equal(spectrum.gap, false); assert.equal(spectrum.title, `光谱坐标 [${group === 'frequency' ? 'GHz' : 'Nanometers'}]`); assert.ok(spectrum.line > 100); assert.equal(spectrum.points, 5);
      assert.equal(requests.length, 3); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      await page.getByTestId('envi-plot').scrollIntoViewIfNeeded();
      const bounds = await page.getByTestId('envi-plot').boundingBox(); assert.ok(bounds.width > 500 && bounds.height >= 400);
      return { realSandboxReader: true, actualPlotly: true, metadataOnlyInitially: true, headerBytesNotScopeSize: data.tree.metadata.header_bytes, virtualSourceBytes: data.tree.metadata.source_bytes, exactImage: image, exactSpectrum: spectrum, pairVersionPinned: true, requests: requests.length, previousPlotPurged: true, visiblePlot: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__enviPlot = document.querySelector('[data-testid=envi-plot]'); }),
    verifyCleanup: async page => { const clean = await page.evaluate(() => ({ detached: !window.__enviPlot.isConnected, purged: !window.__enviPlot._fullLayout && !window.__enviPlot.data })); assert.deepEqual(clean, { detached: true, purged: true }); return clean; },
  };
}
export const domainExpansionEnviCases = [scenario('wavelength'), scenario('frequency'),
  { ...base, name: 'domain-expansion-envi-wrong-selection', file: { size: all.wavelength.tree.metadata.header_bytes },
    preview: request => { if (request.kind === 'tree') return all.wavelength.tree; const r = structuredClone(all.wavelength.image); r.selected.band = 1; return r; },
    setup: async page => { await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).waitFor(); await select(page, 'image', all.wavelength.image.selected); await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).click(); },
    ready: page => page.locator('[role=alert]').waitFor(), expectedError: /ENVI 响应或窗口选择无效/,
    verify: async page => { assert.equal(await page.locator('.js-plotly-plot').count(), 0); return { wrongSelectedBandRejectedBeforePlotly: true }; },
  },
  { ...base, name: 'domain-expansion-envi-invalid-roi', file: { size: all.wavelength.tree.metadata.header_bytes },
    preview: request => { assert.equal(request.kind, 'tree', 'Invalid ROI must never reach an API window call'); return all.wavelength.tree; },
    setup: async page => { await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).waitFor(); await page.getByLabel('ENVI 列', { exact: true }).fill('4'); await page.getByRole('button', { name: '读取 ENVI 窗口', exact: true }).click(); },
    ready: page => page.locator('[role=alert]').waitFor(), expectedError: /ENVI 响应或窗口选择无效/,
    verify: async page => { assert.equal(await page.locator('.js-plotly-plot').count(), 0); return { invalidRoiRejectedBeforeApi: true }; },
  },
];
