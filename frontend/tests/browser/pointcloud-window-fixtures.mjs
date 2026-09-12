/** Actual fixed-record LAS outputs, cross-checked with official laspy 2.6.1. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./pointcloud-window-data.json', import.meta.url), 'utf8'));
let requests = [], releasePending;
const base = { component: 'PointCloudWindowPreview.vue', reader: 'pointcloud-window', filename: 'synthetic.las', descriptor: { adapter: 'pointcloud-window' } };
export const pointCloudWindowCases = ['modern', 'legacy', 'precision'].map(prefix => ({
  ...base, name: 'domain-expansion-pointcloud-' + prefix, file: { size: data[prefix + '_tree'].metadata.source_bytes }, init: async () => { requests = []; },
  preview: request => { requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data[prefix + '_tree']; }
    assert.equal(request.kind, 'geometry'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[prefix + '_geometry'].selected); return data[prefix + '_geometry']; },
  ready: page => page.getByRole('button', { name: '读取 LAS 点窗口', exact: true }).waitFor(),
  verify: async page => {
    assert.equal(requests.length, 1); assert.equal(await page.locator('[data-testid=pointcloud-scene] canvas').count(), 0);
    const selection = data[prefix + '_geometry'].selected;
    await page.getByLabel('LAS 点记录起点', { exact: true }).fill(String(selection.point_offset));
    await page.getByLabel('LAS 连续点数', { exact: true }).fill(String(selection.point_count));
    await page.getByRole('button', { name: '读取 LAS 点窗口', exact: true }).click();
    await page.locator('[data-testid=pointcloud-point-detail]').waitFor();
    assert.equal(await page.locator('[data-testid=pointcloud-scene] canvas').count(), 1);
    assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []); assert.equal(requests.length, 2);
    const stats = await page.locator('[data-testid=pointcloud-stats]').textContent(), detail = await page.locator('[data-testid=pointcloud-point-detail]').textContent();
    assert.match(stats, /单位未知/); assert.match(stats, new RegExp(`仅本窗 ${selection.point_count} 点`));
    assert.match(detail, new RegExp(data[prefix + '_geometry'].array.values.slice(0, 3).join(', ')));
    if (prefix === 'precision') { await page.getByLabel('LAS 本窗点索引', { exact: true }).fill('1'); assert.match(await page.locator('[data-testid=pointcloud-point-detail]').textContent(), /2147483001, -2147482999, 1/); }
    else { await page.getByLabel('LAS 着色', { exact: true }).selectOption('rgb'); await page.waitForTimeout(100); assert.equal(requests.length, 2); }
    const pixels = await page.locator('[data-testid=pointcloud-scene] canvas').evaluate(canvas => {
      const gl = canvas.getContext('webgl2'); if (!gl) throw new Error('Actual Three WebGL2 context required');
      const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4); gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      const bg = [...pixels.slice(0, 3)]; let foreground = 0; for (let i = 0; i < pixels.length; i += 4) if (pixels[i] !== bg[0] || pixels[i + 1] !== bg[1] || pixels[i + 2] !== bg[2]) foreground++;
      return { width: gl.drawingBufferWidth, height: gl.drawingBufferHeight, foreground };
    });
    assert.ok(pixels.foreground > 10, 'The real GPU frame must contain visible point pixels');
    return { treeFirst: true, exactVersionAndWindow: true, unitsUnknown: true, rawXYZ: data[prefix + '_geometry'].array.values.slice(0, 3), actualGPU: pixels };
  },
  beforeUnmount: async page => {
    await page.evaluate(() => { window.__lasCanvas = document.querySelector('[data-testid=pointcloud-scene] canvas'); window.__lasGL = window.__lasCanvas.getContext('webgl2'); });
    const gate = new Promise(resolve => { releasePending = resolve; });
    await page.route('**/api/v1/files/*/visualization', async route => { assert.equal(route.request().postDataJSON().version, '1'.repeat(64)); await gate; await route.abort('aborted').catch(() => {}); }, { times: 1 });
    await page.evaluate(() => { const original = window.fetch; window.fetch = function(input, init) { if (String(input).endsWith('/visualization')) window.__lasPending = init.signal; return original.call(this, input, init); }; });
    const next = page.waitForRequest(r => r.url().endsWith('/visualization')); await page.getByRole('button', { name: '读取 LAS 点窗口', exact: true }).click(); await next;
  },
  verifyCleanup: async page => { const result = await page.evaluate(() => ({ detached: !window.__lasCanvas.isConnected, contextReleased: window.__lasGL.isContextLost(), pendingAborted: window.__lasPending.aborted }));
    assert.deepEqual(result, { detached: true, contextReleased: true, pendingAborted: true }); releasePending(); return result; },
}));
