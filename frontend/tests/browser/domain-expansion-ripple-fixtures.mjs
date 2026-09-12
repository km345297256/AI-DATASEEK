/** Real sandbox outputs from an independent NumPy vector cube, not user data. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const fixtures = JSON.parse(readFileSync(new URL('./domain-expansion-ripple-data.json', import.meta.url), 'utf8'));
const base = { component: 'RippleWindowPreview.vue', filename: 'synthetic-cube.rpl', reader: 'ripple-window', descriptor: { adapter: 'ripple-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } } };
async function select(page, kind, options) {
  await page.getByLabel('Ripple 视图', { exact: true }).selectOption(kind);
  for (const [key, value] of Object.entries(options)) await page.getByLabel(`Ripple ${{ channel: '通道', x: '列', y: '行', width: '宽', height: '高', channel_start: '起始通道', channel_count: '通道数' }[key]}`, { exact: true }).fill(String(value));
}
const rendered = (page, kind) => page.waitForFunction(kind => { const p = document.querySelector('[data-testid=ripple-plot]'); return p?._fullLayout && p?.data?.[0]?.type === (kind === 'image' ? 'heatmap' : 'scatter'); }, kind);
function scenario(calibrated) {
  const data = calibrated ? fixtures : fixtures.index; let requests = [];
  return { ...base, name: `domain-expansion-ripple-${calibrated ? 'physical' : 'index'}`, file: { size: data.tree.metadata.header_bytes },
    init: async () => { requests = []; },
    preview: request => { requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data.tree; }
      assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[request.kind].selected); return data[request.kind]; },
    ready: page => page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      assert.equal(data.tree.metadata.read_bytes, data.tree.metadata.header_bytes); assert.equal(data.tree.metadata.read_requests, 1);
      await select(page, 'image', data.image.selected); assert.equal(requests.length, 1);
      await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).click(); await rendered(page, 'image');
      const image = await page.getByTestId('ripple-plot').evaluate(p => {
        const raster = p.querySelector('.heatmaplayer image'), axis = p._fullLayout.yaxis;
        return { x: p.data[0].x, y: p.data[0].y, z: p.data[0].z, gap: p.data[0].connectgaps, yRange: axis.range, rowPixels: p.data[0].y.map(y => axis.l2p(y)), xTitle: p._fullLayout.xaxis.title.text, yTitle: axis.title.text,
          rasterBytes: (raster?.getAttribute('href') || raster?.getAttribute('xlink:href') || '').length };
      });
      assert.deepEqual(image.x, data.image.axes[1].values); assert.deepEqual(image.y, data.image.axes[0].values); assert.deepEqual(image.z, [[113,123,133],[213,223,233]]); assert.equal(image.gap, false);
      assert.ok(image.rowPixels[0] < image.rowPixels[1], 'Source row one must appear above row two for both declared decreasing height and index mode');
      assert.ok(image.rasterBytes > 50, 'Real heatmap raster exists');
      if (calibrated) { assert.ok(image.yRange[0] < image.yRange[1], 'Physical coordinates use ordinary Cartesian y; never reverse a negative physical scale again'); assert.equal(image.yTitle, 'height [nm]'); }
      else { assert.ok(image.yRange[0] > image.yRange[1], 'Raw source row indices run downward'); assert.equal(image.yTitle, 'height'); assert.match(await page.locator('section').innerText(), /索引（无标定声明）/); }
      await page.evaluate(() => { window.__oldRipplePlot = document.querySelector('[data-testid=ripple-plot]'); });
      await select(page, 'series', data.series.selected); assert.equal(await page.locator('.js-plotly-plot').count(), 0); assert.equal(requests.length, 2);
      assert.equal(await page.evaluate(() => !window.__oldRipplePlot.isConnected && !window.__oldRipplePlot._fullLayout), true);
      await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).click(); await rendered(page, 'series');
      const spectrum = await page.getByTestId('ripple-plot').evaluate(p => ({ x: p.data[0].x, y: p.data[0].y, gap: p.data[0].connectgaps, title: p._fullLayout.xaxis.title.text, length: p.querySelector('.js-line')?.getTotalLength(), points: p.querySelectorAll('.point').length }));
      assert.deepEqual(spectrum.x, data.series.axes[0].values); assert.deepEqual(spectrum.y, [121,122,123,124]); assert.equal(spectrum.gap, false); assert.equal(spectrum.title, calibrated ? 'depth [eV]' : 'depth'); assert.ok(spectrum.length > 100); assert.equal(spectrum.points, 4);
      assert.equal(requests.length, 3); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      await page.getByTestId('ripple-plot').scrollIntoViewIfNeeded(); const bounds = await page.getByTestId('ripple-plot').boundingBox(); assert.ok(bounds.width > 500 && bounds.height >= 400);
      return { actualPlotly: true, realNumPyReader: true, metadataOnlyInitially: true, headerBytes: data.tree.metadata.header_bytes, pairBytes: data.tree.metadata.source_bytes, exactImage: image, exactSpectrum: spectrum, versionPinned: true, oldPlotPurged: true, visible: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__ripplePlot = document.querySelector('[data-testid=ripple-plot]'); }),
    verifyCleanup: async page => { const clean = await page.evaluate(() => ({ detached: !window.__ripplePlot.isConnected, purged: !window.__ripplePlot.data && !window.__ripplePlot._fullLayout })); assert.deepEqual(clean, { detached: true, purged: true }); return clean; },
  };
}
export const domainExpansionRippleCases = [scenario(true), scenario(false),
  { ...base, name: 'domain-expansion-ripple-wrong-selection', file: { size: fixtures.tree.metadata.header_bytes },
    preview: request => { if (request.kind === 'tree') return fixtures.tree; const r = structuredClone(fixtures.image); r.selected.channel = 2; return r; },
    setup: async page => { await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).waitFor(); await select(page, 'image', fixtures.image.selected); await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).click(); },
    ready: page => page.locator('[role=alert]').waitFor(), expectedError: /Ripple 响应或窗口选择无效/,
    verify: async page => { assert.equal(await page.locator('.js-plotly-plot').count(), 0); return { wrongChannelRejected: true }; },
  },
  { ...base, name: 'domain-expansion-ripple-invalid-roi', file: { size: fixtures.tree.metadata.header_bytes },
    preview: request => { assert.equal(request.kind, 'tree', 'Invalid ROI must not reach window API'); return fixtures.tree; },
    setup: async page => { await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).waitFor(); await page.getByLabel('Ripple 列', { exact: true }).fill('4'); await page.getByRole('button', { name: '读取 Ripple 窗口', exact: true }).click(); },
    ready: page => page.locator('[role=alert]').waitFor(), expectedError: /Ripple 响应或窗口选择无效/,
    verify: async page => { assert.equal(await page.locator('.js-plotly-plot').count(), 0); return { invalidRoiBeforeApi: true }; },
  },
];
