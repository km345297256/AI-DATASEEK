/** Real h5py fileobj-VFD replies; browser renders the actual Plotly component. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./batch-three-array-data.json', import.meta.url), 'utf8'));

export const batchThreeArrayCases = ['series', 'image'].map(kind => {
  let requests = [];
  return {
    name: `batch-three-array-${kind}`, component: 'ArrayWindowPreview.vue', filename: 'synthetic-sparse.h5', reader: 'array-window',
    descriptor: { adapter: 'array-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } }, file: { size: data.provenance.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data.tree; }
      assert.equal(request.kind, kind); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[kind].selected);
      return data[kind];
    },
    ready: page => page.getByRole('button', { name: '读取显式切片' }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      await page.getByLabel('数组视图', { exact: true }).selectOption(kind);
      if (kind === 'image') await page.getByLabel('维度 0 终点', { exact: true }).fill('2');
      await page.getByLabel('维度 1 终点', { exact: true }).fill(kind === 'series' ? '4' : '3');
      await page.getByRole('button', { name: '读取显式切片' }).click();
      await page.waitForFunction(() => { const plot = document.querySelector('.js-plotly-plot'); return !!plot?.data?.length && !!plot._fullLayout; });
      const trace = await page.locator('.js-plotly-plot').evaluate(plot => ({ type: plot.data[0].type, x: plot.data[0].x, y: plot.data[0].y, z: plot.data[0].z, gap: plot.data[0].connectgaps }));
      if (kind === 'series') { assert.equal(trace.type, 'scatter'); assert.deepEqual(trace.x, [0, 1, 2, 3]); assert.deepEqual(trace.y, [1.0000000000000002, -9999, 3, 4]); }
      else { assert.equal(trace.type, 'heatmap'); assert.deepEqual(trace.x, [0, 1, 2]); assert.deepEqual(trace.y, [0, 1]); assert.deepEqual(trace.z, [[1.0000000000000002, -9999, 3], [5, null, 7]]); }
      assert.equal(trace.gap, false); assert.equal(requests.length, 2);
      assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      assert.match(await page.getByRole('note').textContent(), /未应用 CF/);
      assert.ok(data.provenance.source_bytes > 1024 ** 3); assert.ok(data[kind].metadata.read_bytes <= 32768); assert.ok(data[kind].metadata.read_requests <= 2);
      const bounds = await page.locator('.js-plotly-plot').boundingBox(); assert.ok(bounds.width > 400 && bounds.height >= 400);
      return { source: data.provenance, readBytes: data[kind].metadata.read_bytes, readRequests: data[kind].metadata.read_requests,
        versionPinned: true, originalFloat64AndFillRetained: true, indexAxes: true, actualPlotly: trace.type, visiblePlot: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldArrayPlot = document.querySelector('.js-plotly-plot'); }),
    verifyCleanup: async page => { const detached = await page.evaluate(() => !window.__heldArrayPlot.isConnected); assert.equal(detached, true); return { plotDetached: detached }; },
  };
});
