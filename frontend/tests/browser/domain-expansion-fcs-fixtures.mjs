import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-fcs-data.json', import.meta.url), 'utf8'));
const rendered = page => page.waitForFunction(() => { const p = document.querySelector('.js-plotly-plot'); return !!p?.data?.length && !!p._fullLayout; });
export const domainExpansionFcsCases = ['float', 'integer', 'double'].map(format => {
  const fixture = data.cases[format]; let requests = [];
  return {
    name: `domain-expansion-fcs-${format}`, component: 'FcsWindowPreview.vue', filename: `synthetic-${format}.fcs`, reader: 'fcs-window',
    descriptor: { adapter: 'fcs-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } }, file: { size: fixture.tree.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return fixture.tree; }
      assert.equal(request.kind, 'series'); assert.equal(request.version, '1'.repeat(64)); const chosen = request.options.view;
      assert.deepEqual(request.options, fixture[chosen].selected); return fixture[chosen];
    },
    ready: page => page.getByRole('button', { name: '读取事件窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      await page.getByRole('button', { name: '读取事件窗口', exact: true }).click(); await rendered(page);
      const scatter = await page.locator('.js-plotly-plot').evaluate(p => ({ type: p.data[0].type, mode: p.data[0].mode, x: p.data[0].x, y: p.data[0].y, ids: p.data[0].customdata, connectgaps: p.data[0].connectgaps, xTitle: p.layout.xaxis.title.text }));
      assert.equal(scatter.type, 'scatter'); assert.equal(scatter.mode, 'markers'); assert.equal(scatter.connectgaps, false);
      assert.deepEqual(scatter.x, fixture.scatter.series[0].y); assert.deepEqual(scatter.y, fixture.scatter.series[1].y); assert.deepEqual(scatter.ids, [0, 1, 2, 3]); assert.doesNotMatch(scatter.xTitle, /MESF/);
      await page.getByLabel('FCS 视图').selectOption('histogram'); await page.getByLabel('直方图箱数').fill('4'); assert.equal(requests.length, 2); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      await page.getByRole('button', { name: '读取事件窗口', exact: true }).click(); await rendered(page);
      const histogram = await page.locator('.js-plotly-plot').evaluate(p => ({ type: p.data[0].type, counts: p.data[0].y, yTitle: p.layout.yaxis.title.text }));
      assert.equal(histogram.type, 'bar'); assert.equal(histogram.counts.reduce((a, b) => a + b, 0), 4); assert.match(histogram.yTitle, /当前事件窗内计数/);
      assert.equal(requests.length, 3); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []); assert.match(await page.getByRole('note').textContent(), /不作位掩码/);
      if (format === 'float') { assert.match(await page.getByTestId('fcs-channel-metadata').textContent(), /MESF/); assert.match(await page.getByTestId('fcs-channel-metadata').textContent(), /未应用/); }
      const bounds = await page.locator('.js-plotly-plot').boundingBox(); assert.ok(bounds.width > 400 && bounds.height >= 400);
      return { initialMetadataOnly: true, versionPinned: true, rawAxesNotCalibratedUnits: true, scatterNoLines: true, originalEvents: scatter.x, exactHistogramCount: 4,
        readBytes: fixture.scatter.metadata.read_bytes, rowBytes: fixture.scatter.metadata.scanned_event_bytes, selectedDecodeBytes: fixture.scatter.metadata.decoded_bytes, visiblePlot: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldFcsPlot = document.querySelector('.js-plotly-plot'); }),
    verifyCleanup: async page => { const result = await page.evaluate(() => ({ detached: !window.__heldFcsPlot.isConnected, dataPurged: !window.__heldFcsPlot.data })); assert.deepEqual(result, { detached: true, dataPurged: true }); return result; },
  };
});
