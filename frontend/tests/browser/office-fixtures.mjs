import assert from 'node:assert/strict';

function syntheticPdf() {
  const content = 'BT /F1 20 Tf 25 120 Td (DataSeek scientific preview) Tj ET\n0.25 w 25 80.625 m 320 80.625 l S\n25.3125 25 m 25.3125 65 l S\n26.0625 25 m 26.0625 65 l S';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
  ];
  let source = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(Buffer.byteLength(source)); source += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = Buffer.byteLength(source);
  source += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(source);
}
const pdf = syntheticPdf();
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6D9sAAAAASUVORK5CYII=';
const base = (reader, kind, extra = {}) => ({ contract_version: 2, type: reader, reader, kind, media_type: 'application/json', metadata: {}, warnings: [], sampled: false, ...extra });
async function pdfReady(page) { await page.waitForFunction(() => {
  const canvas = document.querySelector('canvas'); if (!canvas || canvas.width <= 100 || !canvas.style.width || document.querySelector('[aria-busy="true"]')) return false;
  const values = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  let marks = 0; for (let i = 0; i < values.length; i += 4) if (values[i + 3] && values[i] < 180) marks++;
  return marks > 50;
}, { timeout: 20000 }); }
async function verifyPdf(page) {
  if (await page.getByText('转换说明', { exact: true }).count()) {
    await page.getByText('转换说明', { exact: true }).click();
    await page.getByText('图片无损导出，不降采样。', { exact: true }).waitFor();
    await page.getByText('转换说明', { exact: true }).click();
  }
  const pixels = await page.locator('canvas').evaluate(canvas => {
    const values = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let marks = 0; for (let i = 0; i < values.length; i += 4) if (values[i + 3] && values[i] < 180) marks++;
    const dpr = window.devicePixelRatio, lineY = 212.5 * dpr, lineX = Math.floor(100 * dpr);
    let thinLineInk = 0; for (let y = Math.floor(lineY) - 2; y <= Math.ceil(lineY) + 2; y++) thinLineInk += (255 - values[(y * canvas.width + lineX) * 4]) / 255;
    const row = Math.floor((240 - 40) * 4 / 3 * dpr), columns = [];
    for (let x = Math.floor(32 * dpr); x < Math.ceil(36 * dpr); x++) columns.push(255 - values[(row * canvas.width + x) * 4]);
    let separateLines = 0, insideLine = false;
    for (const ink of columns) { if (ink >= 60 && !insideLine) separateLines++; insideLine = ink >= 60; }
    return { width: canvas.width, height: canvas.height, cssWidth: canvas.getBoundingClientRect().width, cssHeight: canvas.getBoundingClientRect().height, dpr, marks, thinLineInk, separateLines };
  });
  assert.ok(pixels.marks > 50, 'Real PDF.js must paint glyph pixels, not just a blank canvas');
  assert.equal(pixels.width, 480 * pixels.dpr); assert.equal(pixels.height, 320 * pixels.dpr);
  assert.equal(pixels.cssWidth, 480); assert.equal(pixels.cssHeight, 320);
  assert.ok(pixels.thinLineInk > pixels.dpr * 0.25 && pixels.thinLineInk < Math.max(1.1, pixels.dpr * 0.45), `Quarter-point scientific rules must retain their raster ink at device density: ${JSON.stringify(pixels)}`);
  if (pixels.dpr >= 2) assert.equal(pixels.separateLines, 2, 'Two fine rules separated by one CSS pixel remain visually distinct on HiDPI');
  await page.getByLabel('缩放').selectOption('1.5');
  await page.waitForFunction(width => document.querySelector('canvas')?.width === width * 1.5 && !document.querySelector('[aria-busy="true"]'), pixels.width);
  const enlarged = await page.locator('canvas').evaluate(canvas => ({ width: canvas.width, cssWidth: canvas.getBoundingClientRect().width }));
  assert.equal(enlarged.cssWidth, 720); assert.equal(enlarged.width, 720 * pixels.dpr);
  await page.getByLabel('缩放').selectOption('4'); await pdfReady(page);
  const maximum = await page.locator('canvas').evaluate(canvas => ({ width: canvas.width, height: canvas.height, cssWidth: canvas.getBoundingClientRect().width }));
  assert.equal(maximum.cssWidth, 1920); assert.ok(maximum.width * maximum.height <= 8 * 1024 * 1024);
  if (pixels.dpr === 3) await page.getByText('当前页已达到高清画布预算', { exact: false }).waitFor();
  await page.getByLabel('缩放').selectOption('fit-width'); await pdfReady(page);
  const fitWidth = await page.locator('canvas').evaluate(canvas => canvas.getBoundingClientRect().width);
  await page.locator('#app').evaluate(element => { element.style.width = '700px'; });
  await page.waitForFunction(width => document.querySelector('canvas').getBoundingClientRect().width < width && !document.querySelector('[aria-busy="true"]'), fitWidth);
  await page.getByLabel('缩放').selectOption('fit-page'); await pdfReady(page);
  assert.equal(await page.locator('.document-page-container').evaluate(element => element.scrollHeight <= element.clientHeight + 1 && element.scrollWidth <= element.clientWidth + 1), true);
  await page.getByRole('button', { name: '全屏阅读' }).click();
  await page.waitForFunction(() => document.querySelector('.document-preview').getBoundingClientRect().width === window.innerWidth);
  await pdfReady(page); await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '全屏阅读' }).waitFor();
  await page.getByRole('button', { name: '下一页' }).click(); await pdfReady(page);
  assert.equal(await page.getByText('2 / 2', { exact: true }).count(), 1);
  await page.getByRole('button', { name: '上一页' }).click(); await pdfReady(page);
  await page.getByLabel('缩放').selectOption('1'); await pdfReady(page);
  const updatedDpr = pixels.dpr === 2 ? 3 : 2;
  const emulation = await page.context().newCDPSession(page);
  await emulation.send('Emulation.setDeviceMetricsOverride', { width: 1101, height: 760, deviceScaleFactor: updatedDpr, mobile: false });
  await page.waitForFunction(dpr => window.devicePixelRatio === dpr && document.querySelector('canvas').width === 480 * dpr && !document.querySelector('[aria-busy="true"]'), updatedDpr);
  assert.equal(await page.locator('canvas').evaluate(canvas => canvas.getBoundingClientRect().width), 480);
  await emulation.detach();
  const retainedPixels = await page.evaluate(() => [...window.__canvasReferences].reduce((sum, canvas) => sum + canvas.width * canvas.height, 0));
  assert.ok(retainedPixels <= 8 * 1024 * 1024, 'Finished renders immediately release scratch canvases instead of retaining prior pages');
  return { ...pixels, maximum, zoomRendered: true, resizeRedrawn: true, fitPage: true, fullscreen: true, dprChangeRedrawn: true, retainedPixels };
}
async function pdfCleanup(page) {
  const retainedPixels = await page.evaluate(() => [...window.__canvasReferences].reduce((sum, canvas) => sum + canvas.width * canvas.height, 0));
  assert.equal(retainedPixels, 0, 'Unmount releases displayed and scratch canvas backing stores');
  return { retainedPixels };
}
async function cancelPdfRender(page) {
  await page.evaluate(async () => {
    const select = document.querySelector('select'); select.value = '4'; select.dispatchEvent(new Event('change', { bubbles: true }));
    // Flush Vue's watcher and PDF's already-resolved getPage, then dispose before the next paint.
    await Promise.resolve(); await Promise.resolve();
    window.unmountHarness();
  });
}
async function imageReady(page) { await page.waitForFunction(() => { const img = document.querySelector('#app img'); return img?.complete && img.naturalWidth > 0; }); }
const qc = base('fastqc', 'report', { sections: [{ name: 'Basic Statistics', status: 'pass', columns: ['Measure', 'Value'], rows: [['Total Sequences', '500']] }], table: { columns: ['sample', 'total_sequences'], rows: [['synthetic', 500]], row_offset: 0, column_offset: 0, total_rows: 1, total_columns: 2 } });
const job = { job_id: 'a'.repeat(32), status: 'succeeded' };
const envelope = data => ({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data }) });

export const officeCases = [
  ...[1, 2, 3].map(deviceScaleFactor => ({ name: deviceScaleFactor === 1 ? 'pdfjs' : `pdfjs-dpr${deviceScaleFactor}`, descriptor: { adapter: 'pdfjs' }, component: 'DocumentPreview.vue', filename: 'synthetic.pdf', reader: 'binary', bytes: pdf, deviceScaleFactor, ready: pdfReady, verify: verifyPdf, beforeUnmount: cancelPdfRender, verifyCleanup: pdfCleanup })),
  ...[['word', 'docx'], ['powerpoint', 'pptx']].flatMap(([name, extension]) => [1, 2, 3].map(deviceScaleFactor => ({ name: deviceScaleFactor === 1 ? name : `${name}-dpr${deviceScaleFactor}`, descriptor: { adapter: name }, component: 'DocumentPreview.vue', filename: `synthetic.${extension}`, reader: 'office', deviceScaleFactor, preview: base('office', 'pdf', { media_type: 'application/pdf', data_base64: pdf.toString('base64'), warnings: ['图片无损导出，不降采样。', '缺失字体使用隔离环境中的本地替代字体。'] }), ready: pdfReady, verify: verifyPdf, beforeUnmount: cancelPdfRender, verifyCleanup: pdfCleanup }))),
  { name: 'excel', component: 'ExcelPreview.vue', filename: 'synthetic.xlsx', reader: 'excel',
    preview: base('excel', 'table', { table: { columns: ['A', 'B'], rows: [[1, 3.5], [2, null]], formulas: [[null, null], [null, '=SUM(A1:A2)']], row_offset: 0, column_offset: 0, total_rows: 2, total_columns: 2 }, choices: { sheets: ['Measurements'] }, selected: { sheet: 'Measurements' } }),
    ready: page => page.getByText('3.5', { exact: true }).waitFor(),
    verify: async page => { await page.getByLabel('显示公式文本').check(); await page.getByText('=SUM(A1:A2)', { exact: true }).waitFor(); return { cachedValues: true, literalFormula: true, noRecalculation: true }; } },
  { name: 'rdkit', component: 'ScientificImagePreview.vue', filename: 'synthetic.smi', reader: 'rdkit', preview: base('rdkit', 'image', { media_type: 'image/png', data_base64: png, metadata: { formula: 'C2H6O' } }), ready: imageReady, verify: async page => ({ imageDecoded: await page.locator('img').evaluate(img => img.naturalWidth > 0) }) },
  { name: 'metpy', component: 'ScientificImagePreview.vue', filename: 'synthetic.csv', reader: 'metpy', preview: base('metpy', 'image', { media_type: 'image/png', data_base64: png }), setup: async page => { for (const [i, value] of ['p', 't', 'td'].entries()) await page.locator('form input').nth(i).fill(value); await page.getByRole('button', { name: '生成探空图' }).click(); }, ready: imageReady, verify: async page => ({ explicitColumns: true, imageDecoded: await page.locator('img').evaluate(img => img.naturalWidth > 0) }) },
  { name: 'fastqc', component: 'FastQcPreview.vue', filename: 'synthetic.fastq', reader: 'fastqc',
    api: async request => { const pathname = new URL(request.url()).pathname; if (!pathname.includes('/visualization/jobs')) return null;
      if (pathname.endsWith('/result')) return envelope({ contract_version: 2, plugin_id: 'test-fastqc', version: '1'.repeat(64), revision: '2'.repeat(64), kind: 'report', payload: { sections: qc.sections, table: qc.table, view_kind: 'report' }, metadata: {}, warnings: [], sampled: false });
      if (pathname.endsWith('/cancel')) return envelope({ ...job, status: 'cancelled' });
      if (pathname.endsWith('/visualization/jobs') && request.method() === 'GET') return envelope([]);
      if (request.method() === 'POST') { assert.deepEqual(request.postDataJSON(), { plugin_id: 'test-fastqc', options: { confirm: true } }); return envelope({ ...job, status: 'running' }); }
      return envelope(job);
    }, setup: async page => { await page.getByRole('button', { name: '启动质控' }).click(); },
    ready: page => page.getByText('MultiQC 汇总').waitFor({ timeout: 12000 }),
    verify: async page => { assert.equal(await page.getByText('Basic Statistics · pass').count(), 1); return { explicitStart: true, jobCompletion: true, qcModuleRendered: true, artifactSummaryRendered: true }; } },
];
