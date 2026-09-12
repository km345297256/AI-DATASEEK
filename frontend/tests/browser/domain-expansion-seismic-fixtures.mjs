/** Private results from real libmseed MiniSEED2 and synthetic SAC binaries. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-seismic-data.json', import.meta.url), 'utf8'));
const rendered = page => page.waitForFunction(() => { const p = document.querySelector('.js-plotly-plot'); return !!p?.data?.length && !!p._fullLayout; });
export const domainExpansionSeismicCases = ['mseed', 'sac', 'paged'].map(format => {
  const fixture = data.cases[format]; let requests = [];
  return {
    name: `domain-expansion-seismic-${format}`, component: 'SeismicWindowPreview.vue', filename: `synthetic-seismic.${format === 'paged' ? 'miniseed' : format}`, reader: 'seismic-window',
    descriptor: { adapter: 'seismic-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } }, file: { size: fixture.tree.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') {
        if (!Object.keys(request.options).length) return fixture.tree;
        assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, fixture.page.selected); return fixture.page;
      }
      assert.equal(request.kind, 'series'); assert.equal(request.version, '1'.repeat(64));
      const name = request.options.start_sample === 1 ? 'window' : 'full'; assert.deepEqual(request.options, fixture[name].selected); return fixture[name];
    },
    ready: page => page.getByRole('button', { name: '读取波形窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      if (format === 'paged') {
        await page.getByLabel('地震波形记录').selectOption('2'); assert.match(await page.getByTestId('seismic-continuity').textContent(), /存在间隙/);
        assert.equal(requests.length, 1); await page.getByRole('button', { name: '下一目录页', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('[aria-label="地震波形记录"]')?.value === '16');
        assert.equal(requests.length, 2); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      } else {
        await page.getByRole('button', { name: '读取波形窗口', exact: true }).click(); await rendered(page);
        const y = await page.locator('.js-plotly-plot').evaluate(p => p.data[0].y); assert.deepEqual(y, fixture.full.series[0].y);
      }
      await page.getByLabel('起始样本', { exact: true }).fill('1'); await page.getByLabel('样本数', { exact: true }).fill('2');
      assert.equal(await page.locator('.js-plotly-plot').count(), 0); assert.equal(requests.length, 2);
      await page.getByRole('button', { name: '读取波形窗口', exact: true }).click(); await rendered(page);
      const trace = await page.locator('.js-plotly-plot').evaluate(p => ({ x: p.data[0].x, y: p.data[0].y, connectgaps: p.data[0].connectgaps }));
      assert.deepEqual(trace.x, fixture.window.series[0].x); assert.deepEqual(trace.y, fixture.window.series[0].y); assert.equal(trace.connectgaps, false);
      assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      assert.match(await page.getByRole('note').textContent(), /不拼接记录/); assert.match(await page.getByRole('note').textContent(), /SAC SCALE 不应用/);
      const bounds = await page.locator('.js-plotly-plot').boundingBox(); assert.ok(bounds.width > 400 && bounds.height >= 300);
      return { metadataOnlyInitially: true, versionPinned: true, explicitSingleRecordWindow: true, noGapJoining: true,
        originalValues: trace.y, readBytes: fixture.window.metadata.read_bytes, decodeBytes: fixture.window.metadata.decoded_bytes, visiblePlot: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldSeismicPlot = document.querySelector('.js-plotly-plot'); }),
    verifyCleanup: async page => { const cleanup = await page.evaluate(() => ({ detached: !window.__heldSeismicPlot.isConnected, dataPurged: !window.__heldSeismicPlot.data })); assert.deepEqual(cleanup, { detached: true, dataPurged: true }); return cleanup; },
  };
});
