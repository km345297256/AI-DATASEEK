import assert from 'node:assert/strict';
import JSZip from 'jszip';
import { deflateSync } from 'node:zlib';

const pngChunk = (type, data) => {
  const payload = Buffer.concat([Buffer.from(type), data]); let crc = 0xffffffff;
  for (const byte of payload) { crc ^= byte; for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
  const length = Buffer.alloc(4), checksum = Buffer.alloc(4); length.writeUInt32BE(data.length); checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  return Buffer.concat([length, payload, checksum]);
};
const header = Buffer.alloc(13); header.writeUInt32BE(64, 0); header.writeUInt32BE(32, 4); header[8] = 8; header[9] = 2;
const scanlines = Buffer.alloc(32 * (1 + 64 * 3));
for (let y = 0; y < 32; y++) for (let x = 0; x < 64; x++) { const offset = y * 193 + 1 + x * 3; scanlines[offset] = x * 4; scanlines[offset + 1] = y * 8; scanlines[offset + 2] = (x + y) % 2 ? 0 : 255; }
const png = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), pngChunk('IHDR', header), pngChunk('IDAT', deflateSync(scanlines)), pngChunk('IEND', Buffer.alloc(0))]);

const zip = new JSZip();
zip.file('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>');
zip.file('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>');
zip.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="externalLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/no-network" TargetMode="External"/><Relationship Id="image" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/picture.png"/></Relationships>');
zip.file('word/media/picture.png', png);
zip.file('word/fontTable.xml', '<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:font w:name="Arial"><w:embedRegular w:fontKey="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"/></w:font></w:fonts>');
zip.file('docProps/custom.xml', '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="Research"><vt:lpwstr>Scientific data</vt:lpwstr></property></Properties>');
zip.file('word/document.xml', `<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:r><w:rPr><w:sz w:val="16"/></w:rPr><w:t>科学数据清晰阅读 / Selectable sharp text 0123456789</w:t></w:r></w:p>
<w:p><w:hyperlink r:id="externalLink"><w:r><w:t>External link is inert</w:t></w:r></w:hyperlink></w:p>
<w:p><w:r><w:t>&lt;script&gt;parent.__docxUnsafe=true&lt;/script&gt;</w:t></w:r></w:p>
<w:p><w:r><w:pict xmlns:v="urn:schemas-microsoft-com:vml"><v:shape style="width:48pt;height:24pt"><v:imagedata r:id="image"/></v:shape></w:pict></w:r></w:p>
<w:tbl><w:tblPr><w:tblW w:w="4800" w:type="dxa"/></w:tblPr><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>变量</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>数值 3.14159</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr></w:body></w:document>`);
const bytes = await zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE' });
export const docxCases = [1, 2, 3].map(deviceScaleFactor => ({
  name: `docx-dpr${deviceScaleFactor}`, component: 'DocxPreview.vue', reader: 'binary', filename: 'readonly.docx', bytes, deviceScaleFactor,
  ready: async page => {
    await page.frameLocator('iframe[title="DOCX 隔离只读阅读"]').getByText('科学数据清晰阅读', { exact: false }).waitFor();
    await page.getByText('个排版区段', { exact: false }).waitFor();
  },
  verify: async page => {
    const child = page.frames().find(frame => frame !== page.mainFrame());
    const check = await child.evaluate(() => {
      let isolated = false; try { void parent.document; } catch { isolated = true; }
      const paragraph = document.querySelector('section.docx p');
      const range = document.createRange(); range.selectNodeContents(paragraph);
      getSelection().removeAllRanges(); getSelection().addRange(range);
      return { isolated, selection: getSelection().toString(), links: document.querySelectorAll('a[href]').length, tables: document.querySelectorAll('table').length, htmlFontSize: getComputedStyle(paragraph.querySelector('span')).fontSize, canvases: document.querySelectorAll('canvas').length };
    });
    assert.equal(check.isolated, true); assert.match(check.selection, /科学数据/); assert.equal(check.links, 0); assert.equal(check.tables, 1); assert.equal(check.canvases, 0);
    assert.equal(await page.evaluate(() => window.__docxUnsafe === undefined), true);
    const bitmap = await child.evaluate(async () => {
      const image = document.querySelector('svg image'); const source = image?.getAttribute('href') || image?.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
      if (!source?.startsWith('blob:')) return null;
      const decoded = new Image(); decoded.src = source; await decoded.decode(); return [decoded.naturalWidth, decoded.naturalHeight];
    });
    assert.deepEqual(bitmap, [64, 32]); check.bitmap = bitmap;
    check.zoomGeometry = [];
    for (const zoom of ['0.5', '2', '3']) {
      await page.getByLabel('DOCX 缩放').selectOption(zoom);
      await child.waitForFunction(value => document.getElementById('content').style.zoom === value, zoom);
      const geometry = await child.evaluate(() => {
        const section = document.querySelector('section.docx');
        const scroller = document.scrollingElement;
        scroller.scrollLeft = 0;
        const start = section.getBoundingClientRect();
        const paragraphLeft = section.querySelector('p').getBoundingClientRect().left;
        scroller.scrollLeft = scroller.scrollWidth;
        const end = section.getBoundingClientRect();
        const result = { zoom: document.getElementById('content').style.zoom, viewport: innerWidth,
          width: start.width, leftAtStart: start.left, paragraphLeftAtStart: paragraphLeft,
          rightAtEnd: end.right, scrollLeftAtEnd: scroller.scrollLeft };
        scroller.scrollLeft = 0;
        return result;
      });
      assert.equal(geometry.zoom, zoom);
      assert.ok(geometry.leftAtStart >= -1, `DOCX ${zoom}x left edge must be reachable at scroll start`);
      assert.ok(geometry.paragraphLeftAtStart >= -1, `DOCX ${zoom}x text must not overflow left of the scroll range`);
      assert.ok(geometry.rightAtEnd <= geometry.viewport + 1, `DOCX ${zoom}x right edge must be reachable at scroll end`);
      if (zoom === '0.5') {
        assert.ok(Math.abs(geometry.leftAtStart + geometry.width / 2 - geometry.viewport / 2) <= 1, 'Narrow DOCX pages remain centered');
        assert.equal(geometry.scrollLeftAtEnd, 0);
      } else assert.ok(geometry.scrollLeftAtEnd > 0, 'Wide DOCX pages preserve selected zoom and horizontal scrolling');
      check.zoomGeometry.push(geometry);
    }
    return check;
  },
}));

for (const [name, update, expectedError] of [
  ['external', archive => archive.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="image" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="https://example.invalid/private.png" TargetMode="External"/></Relationships>'), '外部内容'],
  ['active', archive => archive.file('word/embeddings/payload.bin', 'not-an-office-object'), '活动内容'],
  ['doctype', archive => archive.file('word/document.xml', '<!DOCTYPE x [<!ENTITY a "bad">]><x>&a;</x>'), '包结构'],
  ['css-resource', archive => archive.file('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml"><w:body><w:p><w:r><w:pict><v:shape style="background-image:url(data:image/png;base64,AAA=)"/></w:pict></w:r></w:p></w:body></w:document>'), '样式资源'],
  ['image-relationship', archive => { archive.file('word/picture.xml', '<svg xmlns="http://www.w3.org/2000/svg"/>'); archive.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="image" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="picture.xml"/></Relationships>'); }, '图片关系'],
  ['xml-depth', archive => archive.file('word/document.xml', `<x>${'<x>'.repeat(130)}${'</x>'.repeat(130)}</x>`), 'XML 层级'],
  ['image-disguised-type', archive => {
    archive.file('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body><w:p><w:r><a:blip r:embed="image"/></w:r></w:p></w:body></w:document>');
    archive.file('word/picture.xml', '<svg xmlns="http://www.w3.org/2000/svg"/>');
    archive.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="image" Type="unrecognized" Target="picture.xml"/></Relationships>');
  }, '图片引用'],
  ['page-expansion', archive => {
    archive.file('word/document.xml', `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>${'<w:p><w:r><w:t>body</w:t><w:br w:type="page"/></w:r></w:p>'.repeat(300)}<w:sectPr><w:headerReference w:type="default" r:id="header"/></w:sectPr></w:body></w:document>`);
    archive.file('word/header1.xml', `<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">${'<w:p><w:r><w:t>header</w:t></w:r></w:p>'.repeat(100)}</w:hdr>`);
    archive.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="header" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>');
  }, '排版展开'],
]) {
  const archive = await JSZip.loadAsync(bytes); update(archive);
  docxCases.push({ name: `docx-reject-${name}`, component: 'DocxPreview.vue', reader: 'binary', filename: 'unsafe.docx', bytes: await archive.generateAsync({ type: 'uint8array', compression: 'DEFLATE' }), expectedError,
    ready: async page => { await page.getByRole('alert').waitFor(); },
    verify: async page => { const clear = await page.frames().find(frame => frame !== page.mainFrame()).evaluate(() => document.getElementById('content').childElementCount === 0); assert.equal(clear, true); return { clear }; },
  });
}
