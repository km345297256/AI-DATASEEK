import assert from 'node:assert/strict';

function syntheticPdf() {
  const content = 'BT /F1 20 Tf 25 120 Td (DataSeek scientific preview) Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
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
  const canvas = document.querySelector('canvas'); if (!canvas || canvas.width <= 100) return false;
  const values = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  let marks = 0; for (let i = 0; i < values.length; i += 4) if (values[i + 3] && values[i] < 180) marks++;
  return marks > 50;
}, { timeout: 20000 }); }
async function verifyPdf(page) {
  const pixels = await page.locator('canvas').evaluate(canvas => { const values = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; let marks = 0; for (let i = 0; i < values.length; i += 4) if (values[i + 3] && values[i] < 180) marks++; return { width: canvas.width, height: canvas.height, marks }; });
  assert.ok(pixels.marks > 50, 'Real PDF.js must paint glyph pixels, not just a blank canvas');
  await page.getByLabel('缩放').selectOption('1.5');
  await page.waitForFunction(width => document.querySelector('canvas')?.width > width, pixels.width);
  return { ...pixels, zoomRendered: true };
}
async function imageReady(page) { await page.waitForFunction(() => { const img = document.querySelector('#app img'); return img?.complete && img.naturalWidth > 0; }); }
const qc = base('fastqc', 'report', { sections: [{ name: 'Basic Statistics', status: 'pass', columns: ['Measure', 'Value'], rows: [['Total Sequences', '500']] }], table: { columns: ['sample', 'total_sequences'], rows: [['synthetic', 500]], row_offset: 0, column_offset: 0, total_rows: 1, total_columns: 2 } });
const job = { job_id: 'a'.repeat(32), status: 'succeeded' };
const envelope = data => ({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data }) });

export const officeCases = [
  { name: 'pdfjs', component: 'DocumentPreview.vue', filename: 'synthetic.pdf', reader: 'binary', bytes: pdf, ready: pdfReady, verify: verifyPdf },
  ...[['word', 'docx'], ['powerpoint', 'pptx']].map(([name, extension]) => ({ name, component: 'DocumentPreview.vue', filename: `synthetic.${extension}`, reader: 'office', preview: base('office', 'pdf', { media_type: 'application/pdf', data_base64: pdf.toString('base64') }), ready: pdfReady, verify: verifyPdf })),
  { name: 'excel', component: 'ExcelPreview.vue', filename: 'synthetic.xlsx', reader: 'excel',
    preview: base('excel', 'table', { table: { columns: ['A', 'B'], rows: [[1, 3.5], [2, null]], formulas: [[null, null], [null, '=SUM(A1:A2)']], row_offset: 0, column_offset: 0, total_rows: 2, total_columns: 2 }, choices: { sheets: ['Measurements'] }, selected: { sheet: 'Measurements' } }),
    ready: page => page.getByText('3.5', { exact: true }).waitFor(),
    verify: async page => { await page.getByLabel('显示公式文本').check(); await page.getByText('=SUM(A1:A2)', { exact: true }).waitFor(); return { cachedValues: true, literalFormula: true, noRecalculation: true }; } },
  { name: 'rdkit', component: 'ScientificImagePreview.vue', filename: 'synthetic.smi', reader: 'rdkit', preview: base('rdkit', 'image', { media_type: 'image/png', data_base64: png, metadata: { formula: 'C2H6O' } }), ready: imageReady, verify: async page => ({ imageDecoded: await page.locator('img').evaluate(img => img.naturalWidth > 0) }) },
  { name: 'metpy', component: 'ScientificImagePreview.vue', filename: 'synthetic.csv', reader: 'metpy', preview: base('metpy', 'image', { media_type: 'image/png', data_base64: png }), setup: async page => { for (const [i, value] of ['p', 't', 'td'].entries()) await page.locator('form input').nth(i).fill(value); await page.getByRole('button', { name: '生成探空图' }).click(); }, ready: imageReady, verify: async page => ({ explicitColumns: true, imageDecoded: await page.locator('img').evaluate(img => img.naturalWidth > 0) }) },
  { name: 'fastqc', component: 'FastQcPreview.vue', filename: 'synthetic.fastq', reader: 'fastqc',
    api: async request => { const pathname = new URL(request.url()).pathname; if (!pathname.includes('/visualization-jobs')) return null;
      if (pathname.endsWith('/result')) return envelope({ ...qc, plugin_id: 'test-fastqc', version: '1'.repeat(64), revision: '2'.repeat(64) });
      if (pathname.endsWith('/cancel')) return envelope({ ...job, status: 'cancelled' });
      if (pathname.endsWith('/visualization-jobs') && request.method() === 'GET') return envelope([]);
      if (request.method() === 'POST') { assert.deepEqual(request.postDataJSON(), { plugin_id: 'test-fastqc', options: { confirm: true } }); return envelope({ ...job, status: 'running' }); }
      return envelope(job);
    }, setup: async page => { await page.getByRole('button', { name: '启动质控' }).click(); },
    ready: page => page.getByText('MultiQC 汇总').waitFor({ timeout: 12000 }),
    verify: async page => { assert.equal(await page.getByText('Basic Statistics · pass').count(), 1); return { explicitStart: true, jobCompletion: true, qcModuleRendered: true, artifactSummaryRendered: true }; } },
];
