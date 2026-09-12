/** Real bounded-reader generated PNGs, decoded in native browser elements. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./instrument-image-data.json', import.meta.url), 'utf8'));
export const batchThreeInstrumentCases = ['edf', 'spe'].map(format => {
  let requests = [];
  const tree = data[`${format}_tree`], image = data[`${format}_image`];
  return { name: `batch-three-instrument-${format}`, component: 'InstrumentImagePreview.vue', filename: `synthetic-detector.${format}`, reader: 'instrument-window', descriptor: { adapter: 'instrument-image' }, file: { size: tree.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return tree; }
      assert.equal(request.kind, 'image'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, { frame: 1, roi: [1, 1, 2, 2] }); return image;
    },
    ready: page => page.getByText('尚未显示像素；帧从 0 开始', { exact: false }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('img').getAttribute('src'), null);
      for (const [label, value] of [['帧索引', '1'], ['ROI X', '1'], ['ROI Y', '1'], ['宽度', '2'], ['高度', '2']]) await page.getByLabel(label, { exact: true }).fill(value);
      await page.getByRole('button', { name: '读取所选 ROI' }).click();
      await page.waitForFunction(() => { const image = document.querySelector('img'); return image.complete && image.naturalWidth === 2 && image.naturalHeight === 2; });
      const pixels = await page.locator('img').evaluate(image => { const canvas = document.createElement('canvas'); canvas.width = 2; canvas.height = 2; const context = canvas.getContext('2d'); context.drawImage(image, 0, 0); const values = Array.from(context.getImageData(0, 0, 2, 2).data); canvas.width = 0; canvas.height = 0; return values; });
      assert.deepEqual(pixels, [0, 0, 0, 255, 51, 51, 51, 255, 204, 204, 204, 255, 255, 255, 255, 255]);
      assert.equal(requests.length, 2); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      const bounds = await page.locator('img').boundingBox(); assert.ok(bounds.width >= 128 && bounds.height >= 64);
      return { format: tree.metadata.format, exactFrameRoi: true, versionPinned: true, actualPixels: pixels, visibleImage: bounds, nativeSource: data.provenance, readBytes: image.metadata.read_bytes, readRequests: image.metadata.read_requests };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldInstrumentImage = document.querySelector('img'); }),
    verifyCleanup: async page => { const removed = await page.evaluate(() => !window.__heldInstrumentImage.hasAttribute('src')); assert.equal(removed, true); return { imageSourceRemoved: removed }; },
  };
});
