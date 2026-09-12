/** Real PyArrow-generated private responses with explicit range/precision QA. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-columnar-data.json', import.meta.url), 'utf8'));

export const domainExpansionColumnarCases = ['parquet', 'arrow', 'feather'].map(format => {
  const fixture = data.cases[format]; let requests = [];
  return {
    name: `domain-expansion-columnar-${format}`, component: 'ColumnarWindowPreview.vue', filename: `synthetic-columnar.${format}`, reader: 'columnar-window',
    descriptor: { adapter: 'columnar-window', capabilities: { operations: ['preview'], input_mode: 'window', shared: false } }, file: { size: fixture.tree.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => {
      requests.push(request);
      if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return fixture.tree; }
      assert.equal(request.kind, 'table'); assert.equal(request.version, '1'.repeat(64));
      const name = request.options.columns.length === 1 ? 'column' : request.options.row_offset === 0 ? 'first' : 'next';
      assert.deepEqual(request.options, fixture[name].selected); return fixture[name];
    },
    ready: page => page.getByRole('button', { name: '读取选定窗口', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.getByRole('table').count(), 0);
      await page.getByLabel('本页行数', { exact: true }).fill('2');
      assert.equal(requests.length, 1);
      await page.getByRole('button', { name: '读取选定窗口', exact: true }).click();
      await page.getByRole('table', { name: '列式数据窗口' }).waitFor();
      const first = await page.locator('tbody tr').allTextContents();
      assert.match(first[0], /9223372036854775807/); assert.match(first[1], /-9223372036854775808/);
      assert.match(first[0], /1234567890123456\.1200/); assert.match(first[1], /-0\.0100/);
      assert.match(first[0], /科学 <data>/); assert.equal(await page.locator('data').count(), 0);
      await page.getByRole('button', { name: '下一页', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('tbody tr th')?.textContent.trim() === '2');
      assert.equal(requests.length, 3); assert.match((await page.locator('tbody tr').allTextContents())[1], /0\.0000/);
      await page.getByLabel('列 0 identity', { exact: true }).uncheck(); await page.getByLabel('列 2 label', { exact: true }).uncheck();
      await page.getByLabel('起始行', { exact: true }).fill('0');
      assert.equal(requests.length, 3); assert.equal(await page.getByRole('table').count(), 0);
      await page.getByRole('button', { name: '读取选定窗口', exact: true }).click();
      await page.getByRole('table').waitFor(); assert.equal(requests.length, 4); assert.equal(await page.locator('thead th').count(), 2);
      assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      assert.match(await page.getByRole('note').textContent(), /精确文本/);
      assert.ok(fixture.first.metadata.read_bytes < 20000); assert.ok(fixture.first.metadata.source_bytes > 64 * 1024**2);
      const bounds = await page.getByRole('table').boundingBox(); assert.ok(bounds.width > 500 && bounds.height > 60);
      return { metadataOnlyInitially: true, explicitColumnProjection: true, versionPinned: true, sourceOrderPagination: true, exactInt64Decimal: true,
        readBytes: fixture.first.metadata.read_bytes, readRequests: fixture.first.metadata.read_requests, syntheticVirtualPaddingBytes: data.provenance.virtual_padding_bytes, logicalRows: 4, visibleTable: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldColumnarTable = document.querySelector('table'); }),
    verifyCleanup: async page => { assert.equal(await page.evaluate(() => window.__heldColumnarTable.isConnected), false); return { tableDetached: true }; },
  };
});
