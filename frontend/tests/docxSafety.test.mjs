import assert from 'node:assert/strict';
import { test } from 'node:test';
import JSZip from 'jszip';
import { inspectDocxZip, imagePixels, prepareSafeDocx } from '../src/visualizations/extended/docxSafety.ts';
import { readFileSync } from 'node:fs';

const minimal = async (change = () => {}) => {
  const zip = new JSZip(); zip.file('[Content_Types].xml', '<Types/>'); zip.file('word/document.xml', '<document/>'); change(zip);
  return zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE' });
};
test('DOCX preflight accepts bounded ordinary stored/deflated packages', async () => {
  const bytes = await minimal(); assert.equal(inspectDocxZip(bytes).filter(entry => entry.name.endsWith('.xml')).length, 2);
});
test('DOCX preflight rejects unsupported or mismatched ZIP structures before decompression', async () => {
  assert.throws(() => inspectDocxZip(new Uint8Array(10)));
  const bytes = await minimal(); const bad = bytes.slice(); bad[0] = 0; assert.throws(() => inspectDocxZip(bad));
  const split = bytes.slice(); new DataView(split.buffer).setUint16(split.length - 18, 1, true); assert.throws(() => inspectDocxZip(split));
  const corruptSize = bytes.slice(); new DataView(corruptSize.buffer).setUint32(22, 123456, true); assert.throws(() => inspectDocxZip(corruptSize));
});
test('DOCX preflight rejects traversal, duplicate case names, too many entries and expansion bombs', async () => {
  for (const callback of [
    zip => zip.file('../outside.xml', '<x/>'), zip => zip.file('WORD/document.xml', '<x/>'),
    zip => zip.file('word/oversized.xml', 'a'.repeat(3 * 1024 * 1024)),
    zip => { for (let i = 0; i < 520; i++) zip.file(`part${i}.xml`, '<x/>'); },
  ]) { const bytes = await minimal(callback); assert.throws(() => inspectDocxZip(bytes)); }
});
test('DOCX streaming decompression rejects forged small declared sizes before XML parsing', async () => {
  const original = await minimal(zip => zip.file('[Content_Types].xml', '<Types>' + 'x'.repeat(65536) + '</Types>'));
  const bytes = original.slice(), view = new DataView(bytes.buffer);
  const end = bytes.length - 22, start = view.getUint32(end + 16, true);
  // Forge both copies of the first file size, so preflight cannot rely on a
  // mismatch alone: the streaming reader must count the actual expansion.
  const local = view.getUint32(start + 42, true);
  view.setUint32(start + 24, 1, true);
  view.setUint32(local + 22, 1, true);
  assert.equal(inspectDocxZip(bytes)[0].size, 1);
  await assert.rejects(prepareSafeDocx(bytes), /安全预算不符合要求/);
});
test('DOCX image dimensions are inspected without allocating a decoded bitmap', () => {
  const png = new Uint8Array(24), view = new DataView(png.buffer);
  view.setUint32(0, 0x89504e47); view.setUint32(4, 0x0d0a1a0a); view.setUint32(12, 0x49484452);
  view.setUint32(16, 1200); view.setUint32(20, 800); assert.equal(imagePixels(png), 960000);
  assert.throws(() => imagePixels(new Uint8Array([1, 2, 3])));
});
test('DOCX reader is isolated from app origin and disables active HTML/fonts/network', () => {
  const frame = readFileSync(new URL('../src/visualizations/extended/DocxPreview.vue', import.meta.url), 'utf8');
  const entry = readFileSync(new URL('../src/visualizations/extended/docxBrowserEntry.ts', import.meta.url), 'utf8');
  assert.match(frame, /setAttribute\('sandbox', 'allow-scripts'\)/); assert.doesNotMatch(frame, /allow-same-origin/);
  assert.match(frame, /connect-src 'none'/); assert.match(frame, /form-action 'none'/);
  assert.match(entry, /renderAltChunks: false/); assert.match(entry, /ignoreFonts: true/);
  assert.match(entry, /event.source !== parent/); assert.match(entry, /useBase64URL: false/);
  assert.match(frame, /img-src blob:/);
});
