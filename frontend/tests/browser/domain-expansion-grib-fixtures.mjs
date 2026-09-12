/** Actual ECMWF ecCodes output, including directory ranges that never read values. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./grib-window-data.json', import.meta.url), 'utf8'));
const pages = JSON.parse(readFileSync(new URL('./grib-window-pages.json', import.meta.url), 'utf8'));
let imageRequests = [], pageRequests = [], releasePending;
const base = { component: 'GribWindowPreview.vue', filename: 'synthetic.grib2', reader: 'grib-window', descriptor: { adapter: 'grib-window' } };

export const domainExpansionGribCases = [{
  ...base, name: 'domain-expansion-grib-image', file: { size: data.tree.metadata.source_bytes }, init: async () => { imageRequests = []; },
  preview: request => {
    imageRequests.push(request);
    if (request.kind === 'tree') { assert.deepEqual(request.options, { offset: 0 }); return data.tree; }
    assert.equal(request.kind, 'image'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data.image.selected);
    return data.image;
  },
  ready: page => page.getByRole('button', { name: '读取 GRIB 窗口', exact: true }).waitFor(),
  verify: async page => {
    assert.equal(imageRequests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
    await page.getByLabel('GRIB 消息', { exact: true }).selectOption(data.image.selected.message);
    for (const [index, label] of ['列起点', '行起点', '宽度', '高度'].entries()) await page.getByLabel(`GRIB ${label}`, { exact: true }).fill(String(data.image.selected.roi[index]));
    await page.getByRole('button', { name: '读取 GRIB 窗口', exact: true }).click();
    await page.waitForFunction(() => { const p = document.querySelector('.js-plotly-plot'); return !!p?.data?.length && !!p._fullLayout; });
    const output = await page.locator('.js-plotly-plot').evaluate(p => ({ x: p.data[0].x, y: p.data[0].y, z: p.data[0].z,
      unit: p.data[0].colorbar.title.text, connectgaps: p.data[0].connectgaps, zsmooth: p.data[0].zsmooth, xLabel: p.layout.xaxis.title.text, yLabel: p.layout.yaxis.title.text }));
    assert.deepEqual(output.x, [11, 12, 13, 14]); assert.deepEqual(output.y, [50, 49, 48]);
    assert.deepEqual(output.z, [[281, 282, 283, null], [287, 288, 289, 290], [293, 294, 295, 296]]);
    assert.equal(output.unit, 'K'); assert.equal(output.connectgaps, false); assert.equal(output.zsmooth, false);
    assert.match(output.xLabel, /degrees_east/); assert.match(output.yLabel, /degrees_north/);
    assert.equal(imageRequests.length, 2); assert.match(await page.locator('[data-testid=grib-stats]').textContent(), /完整解码 24 点，选区缺失 1 点/);
    assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
    return { treeFirst: true, versionPinned: true, exactSelection: data.image.selected, actualValuesCoordinatesAndMissing: output };
  },
  beforeUnmount: async page => {
    await page.evaluate(() => { window.__heldGribPlot = document.querySelector('.js-plotly-plot'); });
    const gate = new Promise(resolve => { releasePending = resolve; });
    await page.route('**/api/v1/files/*/visualization', async route => {
      const body = route.request().postDataJSON(); assert.equal(body.version, '1'.repeat(64)); assert.deepEqual(body.options, data.image.selected);
      await gate; await route.abort('aborted').catch(() => {});
    }, { times: 1 });
    await page.evaluate(() => { const original = window.fetch; window.fetch = function(input, init) {
      if (String(input).endsWith('/visualization')) window.__pendingGribSignal = init.signal;
      return original.call(this, input, init);
    }; });
    const pending = page.waitForRequest(r => r.url().endsWith('/visualization'));
    await page.getByRole('button', { name: '读取 GRIB 窗口', exact: true }).click(); await pending;
    await page.waitForFunction(() => window.__pendingGribSignal && !window.__pendingGribSignal.aborted);
  },
  verifyCleanup: async page => {
    const result = await page.evaluate(() => ({ detached: !window.__heldGribPlot.isConnected, plotlyDataPurged: !window.__heldGribPlot.data, pendingReadAborted: window.__pendingGribSignal.aborted }));
    assert.deepEqual(result, { detached: true, plotlyDataPurged: true, pendingReadAborted: true }); releasePending(); return result;
  },
}, {
  ...base, name: 'domain-expansion-grib-pagination', file: { size: pages.first.metadata.source_bytes }, init: async () => { pageRequests = []; },
  preview: request => {
    pageRequests.push(request); assert.equal(request.kind, 'tree');
    if (request.options.offset === 0) return pages.first;
    assert.deepEqual(request.options, { offset: pages.first.metadata.next_offset }); assert.equal(request.version, '1'.repeat(64)); return pages.second;
  },
  ready: page => page.getByRole('button', { name: '下一页消息', exact: true }).waitFor(),
  verify: async page => {
    await page.waitForFunction(() => document.querySelector('select[aria-label="GRIB 消息"]')?.options.length === 9);
    assert.equal(pageRequests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
    await page.getByRole('button', { name: '下一页消息', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="GRIB 消息"]')?.options.length === 3);
    assert.equal(pageRequests.length, 2); assert.equal(await page.getByRole('button', { name: '下一页消息', exact: true }).isDisabled(), true);
    assert.equal(await page.getByLabel('GRIB 消息', { exact: true }).inputValue(), '');
    await page.getByRole('button', { name: '上一页消息', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="GRIB 消息"]')?.options.length === 9);
    assert.equal(pageRequests.length, 3); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
    return { pages: [8, 2, 8], nextPageVersionPinned: true, noValueDecodeOrPlot: true, requests: pageRequests.map(r => ({ kind: r.kind, version: r.version, options: r.options })) };
  },
}];
