import assert from 'node:assert/strict';
const encode = text => Buffer.from(text, 'utf8');
const reply = (name, kind, payload, rest = {}) => ({ contentType: 'application/json', body: JSON.stringify({ code: 0, data: { contract_version: 2, plugin_id: `test-${name}`, version: '1'.repeat(64), revision: '2'.repeat(64), kind, payload, metadata: {}, warnings: [], sampled: false, ...rest } }) });
const bytesReply = (name, bytes) => ({ contentType: 'application/octet-stream', body: Buffer.from(bytes), headers: { 'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': '2'.repeat(64), 'X-Visualization-Plugin': `test-${name}` } });
const science = kind => ({ view_kind: kind, variables: [{ name: 'temperature', dimensions: [{ name: 'time', size: 4 }], shape: [4], units: 'K' }], selected_variable: 'temperature', x_label: 'time', y_label: 'temperature [K]', x: [0, 1, 2, 3], y: [10, null, 12, 14], width: 0, height: 0, values: [], extent: null });
async function canvasReady(page) { await page.locator('canvas').waitFor(); await page.waitForFunction(() => { const canvas = document.querySelector('canvas'); if (!canvas?.width) return false; const values = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; return values.some(value => value !== 0); }); }
function shapeBytes() { const bytes = Buffer.alloc(128); bytes.writeInt32BE(9994, 0); bytes.writeInt32BE(64, 24); bytes.writeInt32LE(1000, 28); bytes.writeInt32LE(1, 32); [1200, 1300, 1200, 1300].forEach((value, i) => bytes.writeDoubleLE(value, 36 + i * 8)); bytes.writeInt32BE(1, 100); bytes.writeInt32BE(10, 104); bytes.writeInt32LE(1, 108); bytes.writeDoubleLE(1200, 112); bytes.writeDoubleLE(1300, 120); return bytes; }
function dbfBytes() { const bytes = Buffer.alloc(72, 0); bytes[0] = 3; bytes.writeInt32LE(1, 4); bytes.writeInt16LE(65, 8); bytes.writeInt16LE(7, 10); bytes.write('label', 32); bytes[43] = 67; bytes[48] = 6; bytes[64] = 13; bytes[65] = 32; bytes.write('point1', 66); return bytes; }
const localProjection = 'LOCAL_CS["Synthetic local coordinates",UNIT["metre",1]]';
function shapefileSidecarCase({ name, entryExtension = 'shp', failDbf = false }) {
  const entryId = `synthetic-${name}`;
  const geometryId = entryExtension === 'shp' ? entryId : `${entryId}-geometry`;
  const attributesId = `${entryId}-attributes`;
  const projectionId = entryExtension === 'prj' ? entryId : `${entryId}-projection`;
  const requested = new Set();
  return {
    name, component: '../../components/filePreviews/ShapefilePreview.vue', filename: `sample.${entryExtension}`, reader: 'shapefile',
    descriptor: { adapter: 'shapefile' }, bytes: entryExtension === 'prj' ? encode(localProjection) : shapeBytes(),
    relatedFiles: [
      { file_id: geometryId, filename: 'sample.shp', size: 128, upload_date: '' },
      { file_id: attributesId, filename: 'sample.dbf', size: 72, upload_date: '' },
      { file_id: projectionId, filename: 'sample.prj', size: encode(localProjection).length, upload_date: '' },
    ],
    init: async () => requested.clear(),
    api: async request => {
      assert.equal(request.method(), 'POST');
      // A .prj entry must resolve to its real geometry anchor, never parse the
      // projection bytes as SHP or request siblings through an unrelated file.
      assert.equal(new URL(request.url()).pathname, `/api/v1/files/${geometryId}/visualization`);
      const data = request.postDataJSON();
      assert.equal(data.plugin_id, `test-${name}`);
      assert.equal(data.operation, 'bytes');
      const resource = data.options.resource_id || geometryId;
      requested.add(resource);
      if (resource === geometryId) return bytesReply(name, shapeBytes());
      if (resource === attributesId) {
        // A successful HTTP transport with an invalid byte-stream contract
        // exercises loadPluginBytes' real rejection without introducing the
        // browser's unrelated HTTP-status console error into this UI test.
        return failDbf ? { contentType: 'application/octet-stream', body: dbfBytes() } : bytesReply(name, dbfBytes());
      }
      assert.equal(resource, projectionId);
      return bytesReply(name, encode(localProjection));
    },
    ready: async page => {
      await page.locator('svg circle').waitFor();
      await page.locator('[title^="LOCAL_CS["]').waitFor();
      if (failDbf) await page.getByText(/DBF.*(?:失败|无法)/i).waitFor();
      else await page.getByText('point1', { exact: true }).waitFor();
    },
    verify: async page => {
      assert.equal(await page.locator('svg circle').count(), 1);
      assert.equal(await page.locator('svg circle').getAttribute('cx'), '1200');
      assert.equal(await page.locator('svg circle').getAttribute('cy'), '-1300');
      assert.equal(await page.locator('svg image').count(), 0, 'Local coordinates must not request a public basemap');
      assert.deepEqual([...requested].sort(), [geometryId, attributesId, projectionId].sort());
      if (failDbf) await page.getByText('未找到 DBF 属性数据', { exact: true }).waitFor();
      await page.locator('svg circle').click();
      // Selection is now expressed as source records and complete geometries;
      // missing sidecar attributes must not prevent selecting the geometry.
      const selectedStatus = `已选择 1 条记录 · 1 个完整要素${failDbf ? ' · 1 条无可用属性' : ''}`;
      await page.getByText(selectedStatus, { exact: true }).waitFor();
      assert.equal(await page.locator('svg circle').getAttribute('fill'), '#dc2626');
      if (!failDbf) assert.equal(await page.locator('tbody tr[data-record-index="0"]').getAttribute('aria-selected'), 'true');
      return { geometryPreserved: true, featureSelection: true, localProjectionNoPublicTiles: true,
        dbfFailureIsNonBlocking: failDbf, prjReadAfterDbfFailure: failDbf, prjEntryResolvedToShp: entryExtension === 'prj' };
    },
  };
}
export const unifiedCases = [
  { name: 'unified-csv', component: '../../components/filePreviews/CsvFilePreview.vue', filename: 'sample.csv', reader: 'csv', file: { size: 10 ** 10 },
    descriptor: { capabilities: { operations: ['page'], input_mode: 'page', shared: true }, limits: { max_input_bytes: 131072, max_output_bytes: 524288 } },
    api: async request => { const data = request.postDataJSON(); assert.equal(data.operation, 'page'); const next = data.options.offset > 0; return reply('unified-csv', 'page', { offset: next ? 131072 : 0, next_offset: next ? null : 131072, total_bytes: 10 ** 10, text: '', headers: next ? [] : ['label', 'value'], rows: [[next ? 'second-page' : 'quoted\nmultiline', '42']], columns_truncated: false, delimiter: ',', header_pending: false, bytes_read: 131072 }); },
    ready: page => page.getByText('quoted\nmultiline', { exact: true }).waitFor(),
    verify: async page => { await page.getByRole('button', { name: '下一页' }).click(); await page.getByText('second-page', { exact: true }).waitFor(); assert.equal(await page.locator('th').first().textContent(), 'label'); await page.getByRole('button', { name: '上一页' }).click(); await page.getByText('quoted\nmultiline', { exact: true }).waitFor(); return { sourceBytes: 10 ** 10, boundedPage: true, headerRetained: true, multilinePreserved: true }; } },
  ...['series', 'map', 'quality'].map(kind => ({ name: `unified-${kind}`, component: '../ScientificFilePreview.vue', filename: kind === 'quality' ? 'sample.fastq' : 'sample.nc', reader: kind === 'quality' ? 'fastq' : 'netcdf', file: { size: kind === 'quality' ? 10 ** 10 : 100 },
    descriptor: { adapter: `scientific-${kind}`, view_kind: kind === 'quality' ? 'series' : kind, capabilities: { operations: ['preview'], input_mode: kind === 'quality' ? 'prefix' : 'whole', shared: false } },
    api: async request => { assert.equal(request.postDataJSON().operation, 'preview'); return reply(`unified-${kind}`, kind === 'map' ? 'raster' : 'series', kind === 'map' ? { ...science(kind), x: [10, 20], y: [30, 40], width: 2, height: 2, values: [1, null, 3, 4], extent: [5, 25, 25, 45] } : science(kind), { sampled: kind === 'quality' }); }, ready: canvasReady,
    verify: async page => { assert.equal(await page.locator('[role="alert"]').count(), 0); if (kind === 'quality') await page.getByText('抽样 / 降采样预览，非完整数据').waitFor(); await page.getByRole('button', { name: '应用 / 重新读取' }).click(); await canvasReady(page); return { typedPayloadRendered: true, reread: true, prefixNotWholeFile: kind === 'quality' }; } })),
  { name: 'unified-shapefile', component: '../../components/filePreviews/ShapefilePreview.vue', filename: 'sample.shp', reader: 'shapefile', bytes: shapeBytes(),
    relatedFiles: [{ file_id: 'synthetic-unified-shapefile', filename: 'sample.shp', size: 128, upload_date: '' }, { file_id: 'synthetic-shape-attributes', filename: 'sample.dbf', size: 72, upload_date: '' }],
    api: async request => { const data = request.postDataJSON(); assert.equal(data.operation, 'bytes'); if (data.options.resource_id) { assert.equal(data.options.resource_id, 'synthetic-shape-attributes'); return bytesReply('unified-shapefile', dbfBytes()); } return bytesReply('unified-shapefile', shapeBytes()); },
    ready: page => page.getByText('point1', { exact: true }).waitFor(), verify: async page => { assert.equal(await page.locator('svg circle').count(), 1); return { geometry: true, samePluginSiblingAttributes: true }; } },
  shapefileSidecarCase({ name: 'unified-shapefile-dbf-failure', failDbf: true }),
  shapefileSidecarCase({ name: 'unified-shapefile-prj-entry', entryExtension: 'prj' }),
  { name: 'unified-image', component: '../../components/filePreviews/ImageFilePreview.vue', filename: 'sample.png', reader: 'binary', file: { content_type: 'image/png' }, bytes: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6D9sAAAAASUVORK5CYII=', 'base64'),
    ready: page => page.waitForFunction(() => { const img = document.querySelector('img'); return img?.complete && img.naturalWidth > 0; }), verify: async page => ({ protectedImageDecoded: await page.locator('img').evaluate(img => img.naturalWidth > 0) }) },
  { name: 'unified-obj', component: '../../components/filePreviews/ObjFilePreview.vue', filename: 'sample.obj', reader: 'binary', bytes: encode('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'),
    ready: page => page.waitForFunction(() => window.__webglDraws > 0), verify: async page => { assert.equal(await page.getByText('Failed', { exact: false }).count(), 0); return { nativeFileListPreservesExtension: true, actualGpuDraw: await page.evaluate(() => window.__webglDraws > 0) }; } },
];
