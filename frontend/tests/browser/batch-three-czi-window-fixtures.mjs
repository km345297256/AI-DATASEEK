/** Actual native-writer CZI range outputs; no mock media element or user data. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./batch-three-czi-window-data.json', import.meta.url), 'utf8'));
let requests = [];
export const batchThreeCziWindowCases = [
  { name: 'batch-three-czi-window', component: 'CziWindowPreview.vue', filename: 'synthetic-uncompressed.czi', reader: 'czi-window', descriptor: { adapter: 'czi-window' }, file: { size: data.provenance.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return data.tree; }
      assert.equal(request.kind, 'image'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, { indices: [0, 0, 0], roi: [1234, 50, 8, 4] }); return data.image;
    },
    ready: page => page.getByText('目录已读取，尚未显示像素。', { exact: false }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('img').getAttribute('src'), null);
      for (const [label, value] of [['ROI X', '1234'], ['ROI Y', '50'], ['宽度', '8'], ['高度', '4']]) await page.getByLabel(label, { exact: true }).fill(value);
      await page.getByRole('button', { name: '读取所选区域' }).click();
      await page.waitForFunction(() => { const image = document.querySelector('img'); return image.complete && image.naturalWidth === 8 && image.naturalHeight === 4; });
      const pixels = await page.locator('img').evaluate(image => {
        const canvas = document.createElement('canvas'); canvas.width = 8; canvas.height = 4; const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
        const result = { first: Array.from(context.getImageData(0, 0, 1, 1).data), all: Array.from(context.getImageData(0, 0, 8, 4).data) }; canvas.width = 0; canvas.height = 0; return result;
      });
      const values = Array.from({ length: 32 }, (_, i) => ((50 + Math.floor(i / 8)) * 40000 + 1234 + i % 8) % 65536);
      const minimum = Math.min(...values), maximum = Math.max(...values);
      for (let i = 0; i < values.length; i++) assert.deepEqual(pixels.all.slice(i * 4, i * 4 + 4), [Math.round((values[i] - minimum) / (maximum - minimum) * 255), Math.round((values[i] - minimum) / (maximum - minimum) * 255), Math.round((values[i] - minimum) / (maximum - minimum) * 255), 255]);
      const text = await page.getByTestId('czi-window-stats').textContent(); assert.ok(text.includes(String(data.image.metadata.read_bytes))); assert.ok(text.includes(String(data.provenance.source_bytes)));
      assert.ok(data.provenance.source_bytes > 64 * 1024 ** 2); assert.ok(data.image.metadata.read_bytes < 4096); assert.equal(requests.length, 2);
      assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      const bounds = await page.locator('img').boundingBox(); assert.ok(bounds.width >= 128 && bounds.height >= 64);
      return { source: data.provenance, actualBytes: data.image.metadata.read_bytes, requestCount: data.image.metadata.read_requests, exact32NativePixels: true, versionPinned: true, realPngDecode: true, visibleImage: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldCziWindowImage = document.querySelector('img'); }),
    verifyCleanup: async page => { const removed = await page.evaluate(() => !window.__heldCziWindowImage.hasAttribute('src')); assert.equal(removed, true); return { imageSourceRemoved: removed }; },
  },
];
