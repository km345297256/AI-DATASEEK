/** NXdata curves and detector planes from the actual h5py bounded reader. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./nexus-window-data.json', import.meta.url), 'utf8'));

export const domainExpansionNexusCases = ['series', 'image'].map(kind => {
  let requests = [], releasePending;
  return {
    name: `domain-expansion-nexus-${kind}`, component: 'NexusWindowPreview.vue', filename: 'synthetic.nxs', reader: 'nexus-window', descriptor: { adapter: 'nexus-window' },
    file: { size: data.tree.metadata.source_bytes }, init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data.tree; }
      assert.equal(request.kind, kind); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[kind].selected); return data[kind];
    },
    ready: page => page.getByRole('button', { name: '读取 NXdata 窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      await page.getByLabel('NXdata 信号', { exact: true }).selectOption(data[kind].selected.nxdata);
      for (const [dimension, part] of data[kind].selected.selection.entries()) {
        await page.getByLabel(`NX 维度 ${dimension} 起点`, { exact: true }).fill(String(part.start));
        await page.getByLabel(`NX 维度 ${dimension} 终点`, { exact: true }).fill(String(part.stop));
        await page.getByLabel(`NX 维度 ${dimension} 步长`, { exact: true }).fill(String(part.step));
      }
      await page.getByRole('button', { name: '读取 NXdata 窗口', exact: true }).click();
      await page.waitForFunction(() => { const plot = document.querySelector('.js-plotly-plot'); return !!plot?.data?.length && !!plot._fullLayout; });
      const output = await page.locator('.js-plotly-plot').evaluate(plot => ({ type: plot.data[0].type, x: plot.data[0].x, y: plot.data[0].y, z: plot.data[0].z,
        errors: plot.data[0].error_y?.array, customdata: plot.data[0].customdata, xLabel: plot.layout.xaxis.title.text, yLabel: plot.layout.yaxis.title.text, connectgaps: plot.data[0].connectgaps }));
      if (kind === 'series') {
        assert.equal(output.type, 'scatter'); assert.deepEqual(output.x, [101, 102, 103, 104]); assert.deepEqual(output.y, [2, 4, 6, 8]); assert.deepEqual(output.errors, [1, 1, 1, 1]);
        assert.equal(output.xLabel, 'energy [eV]'); assert.equal(output.yLabel, 'counts [counts]');
        assert.ok(await page.locator('.errorbar').count() > 0, 'Standard deviations must draw actual error bars');
      } else {
        assert.equal(output.type, 'heatmap'); assert.deepEqual(output.x, [201, 201.5, 202]); assert.deepEqual(output.y, [100.5, 101]);
        assert.deepEqual(output.z, [[8, 9, 10], [14, 15, 16]]); assert.deepEqual(output.customdata, [[1, 1, 1], [1, 1, 1]]);
        assert.equal(output.xLabel, 'x [mm]'); assert.equal(output.yLabel, 'y [mm]');
      }
      assert.equal(output.connectgaps, false); assert.equal(requests.length, 2); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      assert.match(await page.getByRole('note').textContent(), /不应用缩放/);
      const box = await page.locator('.js-plotly-plot').boundingBox(); assert.ok(box.width > 400 && box.height >= 400);
      return { treeFirst: true, versionPinned: true, exactSelection: data[kind].selected, actualCoordinatesAndErrors: output, visiblePlot: box, reads: data[kind].metadata.read_requests };
    },
    beforeUnmount: async page => {
      await page.evaluate(() => { window.__heldNexusPlot = document.querySelector('.js-plotly-plot'); });
      if (kind !== 'image') return;
      const blocked = new Promise(resolve => { releasePending = resolve; });
      const url = '**/api/v1/files/*/visualization';
      await page.route(url, async route => {
        const request = route.request().postDataJSON();
        assert.equal(request.version, '1'.repeat(64)); assert.equal(request.kind, 'image');
        assert.deepEqual(request.options, data.image.selected);
        await blocked;
        await route.abort('aborted').catch(() => {}); // Request may already be cancelled by the component.
      }, { times: 1 });
      await page.evaluate(() => {
        const originalFetch = window.fetch;
        window.fetch = function (input, init) {
          if (String(input).endsWith('/visualization')) window.__nexusPendingSignal = init.signal;
          return originalFetch.call(this, input, init);
        };
      });
      const request = page.waitForRequest(value => value.url().endsWith('/visualization'));
      await page.getByRole('button', { name: '读取 NXdata 窗口', exact: true }).click();
      await request;
      await page.waitForFunction(() => window.__nexusPendingSignal && !window.__nexusPendingSignal.aborted);
      // The normal harness now unmounts with a real version-pinned HTTP read unresolved.
    },
    verifyCleanup: async page => {
      const cleaned = await page.evaluate(() => ({ detached: !window.__heldNexusPlot.isConnected, plotlyDataPurged: !window.__heldNexusPlot.data }));
      assert.deepEqual(cleaned, { detached: true, plotlyDataPurged: true });
      if (kind === 'image') {
        assert.equal(await page.evaluate(() => window.__nexusPendingSignal.aborted), true);
        releasePending();
        cleaned.pendingVersionPinnedReadAborted = true;
      }
      return cleaned;
    },
  };
});
