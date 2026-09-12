/** Actual NGFF/Zarr MemoryStore replies; all I/O fulfilled by local fixtures. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const fixture = JSON.parse(readFileSync(new URL('./ome-zarr-memory-store-data.json', import.meta.url), 'utf8'));
const selected = { level: 1, indices: [1, 1, 2], roi: [1, 1, 3, 2] };
const gray = [0, 23, 46, 209, 232, 255];
let requests = [];

export const batchThreeOmeCases = [{
  name: 'batch-three-ome-zarr', component: 'OmeZarrPreview.vue', filename: '.zattrs', reader: 'ome-zarr',
  descriptor: { adapter: 'ome-zarr' }, file: { size: fixture.tree.metadata.source_bytes, metadata: { source: 'dataset_preview', logical_path: 'synthetic.zarr/.zattrs', dataset_file_version: 'fixture' } },
  init: async () => { requests = []; },
  preview: request => {
    requests.push(request);
    if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return fixture.tree; }
    assert.equal(request.kind, 'image'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, selected);
    return fixture.image;
  },
  ready: page => page.getByText('结构已就绪，尚未读取图像像素', { exact: false }).waitFor(),
  verify: async page => {
    assert.equal(requests.length, 1); assert.equal(await page.locator('[data-testid="ome-zarr-canvas"]').count(), 0);
    await page.getByLabel(/^金字塔层级/).selectOption('1');
    for (const [label, content] of [['T 索引', '1'], ['C 索引', '1'], ['Z 索引', '2'], ['ROI X', '1'], ['ROI Y', '1'], ['宽度', '3'], ['高度', '2']]) await page.getByLabel(label, { exact: true }).fill(content);
    await page.getByRole('button', { name: '读取所选分块区域' }).click();
    const canvas = page.locator('[data-testid="ome-zarr-canvas"]');
    await canvas.waitFor();
    const actual = await canvas.evaluate(item => ({ width: item.width, height: item.height, pixels: Array.from(item.getContext('2d').getImageData(0, 0, item.width, item.height).data) }));
    assert.deepEqual(actual, { width: 3, height: 2, pixels: gray.flatMap(v => [v, v, v, 255]) });
    assert.equal(requests.length, 2); assert.deepEqual(await page.locator('[role="alert"]').allTextContents(), []);
    const stats = await page.locator('[data-testid="ome-zarr-stats"]').textContent();
    assert.match(stats, /3555\s*\/\s*6600/); assert.match(stats, /1 个块/); assert.match(stats, /713/); assert.match(stats, /735/);
    const box = await canvas.boundingBox(); assert.ok(box.width >= 300 && box.height >= 100);
    await page.mouse.move(box.x + box.width / 6, box.y + box.height / 4);
    await page.getByText('索引 x=1, y=1；原值=713；坐标 x=-49, y=101', { exact: true }).waitFor();
    return { source: fixture.provenance, treeFirst: true, versionPinned: true, selected,
      rawValues: fixture.image.array.values, nativeCanvasPixels: actual.pixels, physicalCoordinatesVerified: true,
      readBytes: fixture.image.metadata.read_bytes, sourceBytes: fixture.image.metadata.source_bytes,
      loadedChunks: fixture.image.metadata.loaded_chunks, visibleCanvas: box };
  },
  beforeUnmount: page => page.evaluate(() => { window.__heldOmeCanvas = document.querySelector('[data-testid="ome-zarr-canvas"]'); }),
  verifyCleanup: async page => {
    const cleanup = await page.evaluate(() => ({ detached: !window.__heldOmeCanvas.isConnected,
      zeroWidth: window.__heldOmeCanvas.width === 0, zeroHeight: window.__heldOmeCanvas.height === 0,
      pointerHandlerRemoved: window.__heldOmeCanvas.onmousemove === null }));
    assert.deepEqual(cleanup, { detached: true, zeroWidth: true, zeroHeight: true, pointerHandlerRemoved: true });
    return cleanup;
  },
}];
