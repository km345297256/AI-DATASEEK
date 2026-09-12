import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./ugrid-window-data.json', import.meta.url), 'utf8'));
let requests = [], release;
export const ugridWindowCases = ['node', 'face'].map(prefix => ({
  name: 'domain-expansion-ugrid-' + prefix, component: 'UgridWindowPreview.vue', reader: 'ugrid-window', filename: 'synthetic.nc', descriptor: { adapter: 'ugrid-window' }, file: { size: data[prefix + '_tree'].metadata.source_bytes },
  init: async () => { requests = []; },
  preview: request => { requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data[prefix + '_tree']; }
    assert.equal(request.kind, 'geometry'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, data[prefix + '_geometry'].selected); return data[prefix + '_geometry']; },
  ready: page => page.getByRole('button', { name: '读取 UGRID 网格', exact: true }).waitFor(),
  verify: async page => {
    assert.equal(requests.length, 1); assert.equal(await page.locator('[data-testid=ugrid-scene] canvas').count(), 0);
    const selection = data[prefix + '_geometry'].selected; await page.getByLabel('UGRID 场', { exact: true }).selectOption(selection.field);
    await page.getByLabel('UGRID time 索引', { exact: true }).fill('1'); if (prefix === 'node') await page.getByLabel('UGRID layer 索引', { exact: true }).fill('2');
    await page.getByRole('button', { name: '读取 UGRID 网格', exact: true }).click(); await page.locator('[data-testid=ugrid-values]').waitFor();
    assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []); assert.equal(requests.length, 2);
    const warning = await page.locator('[data-testid=ugrid-storage-warning]').textContent(); assert.match(warning, /原始存储值，未应用 CF 标定/); assert.match(warning, /当前未应用/);
    assert.match(await page.locator('[data-testid=ugrid-values]').textContent(), prefix === 'node' ? /17, 20, 缺失, 26, 29/ : /2, 4/);
    assert.match(await page.locator('[data-testid=ugrid-color-range]').textContent(), prefix === 'node' ? /17 — 29/ : /2 — 4/);
    await page.getByLabel('UGRID 检查索引', { exact: true }).fill('1'); assert.match(await page.locator('[data-testid=ugrid-detail]').textContent(), prefix === 'node' ? /原始值：20/ : /原始值：4/); assert.equal(requests.length, 2);
    const pixels = await page.locator('[data-testid=ugrid-scene] canvas').evaluate(canvas => { const context = canvas.getContext('2d'), bytes = context.getImageData(0, 0, canvas.width, canvas.height).data; const bg = [...bytes.slice(0, 3)]; let count = 0; for (let i = 0; i < bytes.length; i += 4) if (bytes[i] !== bg[0] || bytes[i + 1] !== bg[1] || bytes[i + 2] !== bg[2]) count++; return { foreground: count, association: canvas.dataset.ugridLocation, faces: canvas.dataset.ugridFaces }; });
    assert.ok(pixels.foreground > 50); assert.equal(pixels.association, prefix); assert.equal(pixels.faces, '2');
    return { treeFirst: true, explicitIndices: selection.indices, rawValues: true, noInterpolation: true, pixels };
  },
  beforeUnmount: async page => {
    await page.evaluate(() => { window.__ugridCanvas = document.querySelector('[data-testid=ugrid-scene] canvas'); const original = window.fetch; window.fetch = function(input, init) { if (String(input).endsWith('/visualization')) window.__ugridSignal = init.signal; return original.call(this, input, init); }; });
    const gate = new Promise(resolve => { release = resolve; }); await page.route('**/api/v1/files/*/visualization', async route => { await gate; await route.abort('aborted').catch(() => {}); }, { times: 1 });
    const next = page.waitForRequest(r => r.url().endsWith('/visualization')); await page.getByRole('button', { name: '读取 UGRID 网格', exact: true }).click(); await next;
  },
  verifyCleanup: async page => { const value = await page.evaluate(() => ({ detached: !window.__ugridCanvas.isConnected, width: window.__ugridCanvas.width, height: window.__ugridCanvas.height, aborted: window.__ugridSignal.aborted })); assert.deepEqual(value, { detached: true, width: 0, height: 0, aborted: true }); release(); return value; },
}));
