/** Real, deterministic OOXML packages for the local ONLYOFFICE acceptance run.
 * All content is synthetic. No macros, external relationships, remote resources,
 * embedded objects, formulas, user paths or production data are included.
 */
import JSZip from 'jszip';

const expectedText = 'DataSeek native readonly test';
const xml = body => '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + body;
const officeRel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/';
const rels = entries => xml('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
  + entries.map(([id, type, target]) => `<Relationship Id="${id}" Type="${officeRel}${type}" Target="${target}"/>`).join('')
  + '</Relationships>');
const contentTypes = entries => xml('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
  + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
  + '<Default Extension="xml" ContentType="application/xml"/>'
  + entries.map(([part, type]) => `<Override PartName="/${part}" ContentType="application/vnd.openxmlformats-officedocument.${type}"/>`).join('')
  + '</Types>');
const pack = async entries => {
  const archive = new JSZip();
  for (const [name, data] of Object.entries(entries)) {
    archive.file(name, data, { createFolders: false, date: new Date('2026-01-01T00:00:00Z') });
  }
  return archive.generateAsync({ type: 'uint8array', compression: 'DEFLATE', platform: 'UNIX' });
};

const wordParagraph = text => `<w:p><w:r><w:t>${text}</w:t></w:r></w:p>`;
const wordCell = text => `<w:tc><w:tcPr><w:tcW w:w="3600" w:type="dxa"/></w:tcPr>${wordParagraph(text)}</w:tc>`;
const docx = {
  '[Content_Types].xml': contentTypes([
    ['word/document.xml', 'wordprocessingml.document.main+xml'],
    ['word/styles.xml', 'wordprocessingml.styles+xml'],
  ]),
  '_rels/.rels': rels([['rId1', 'officeDocument', 'word/document.xml']]),
  'word/_rels/document.xml.rels': rels([['rId1', 'styles', 'styles.xml']]),
  'word/styles.xml': xml('<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    + '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>'
    + '<w:sz w:val="24"/></w:rPr></w:rPrDefault><w:pPrDefault/></w:docDefaults>'
    + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'),
  'word/document.xml': xml('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    + `<w:p><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>${expectedText}</w:t></w:r></w:p>`
    + wordParagraph('Synthetic local document. Editing is disabled by the preview integration.')
    + '<w:tbl><w:tblPr><w:tblW w:w="7200" w:type="dxa"/><w:tblBorders>'
    + ['top', 'left', 'bottom', 'right', 'insideH', 'insideV'].map(edge => `<w:${edge} w:val="single" w:sz="4" w:color="336699"/>`).join('')
    + '</w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="3600"/><w:gridCol w:w="3600"/></w:tblGrid>'
    + `<w:tr>${wordCell('Variable')}${wordCell('Value')}</w:tr><w:tr>${wordCell('Signal')}${wordCell('7')}</w:tr></w:tbl>`
    + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
    + '</w:body></w:document>'),
};

const sheetCell = (ref, text, style = 0) => `<c r="${ref}" s="${style}" t="inlineStr"><is><t>${text}</t></is></c>`;
const xlsx = {
  '[Content_Types].xml': contentTypes([
    ['xl/workbook.xml', 'spreadsheetml.sheet.main+xml'],
    ['xl/worksheets/sheet1.xml', 'spreadsheetml.worksheet+xml'],
    ['xl/styles.xml', 'spreadsheetml.styles+xml'],
  ]),
  '_rels/.rels': rels([['rId1', 'officeDocument', 'xl/workbook.xml']]),
  'xl/_rels/workbook.xml.rels': rels([['rId1', 'worksheet', 'worksheets/sheet1.xml'], ['rId2', 'styles', 'styles.xml']]),
  'xl/workbook.xml': xml('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    + '<bookViews><workbookView activeTab="0"/></bookViews><sheets><sheet name="ReadOnlyFixture" sheetId="1" r:id="rId1"/></sheets></workbook>'),
  'xl/styles.xml': xml('<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    + '<fonts count="2"><font><sz val="12"/><name val="Arial"/></font><font><b/><sz val="16"/><name val="Arial"/></font></fonts>'
    + '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    + '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    + '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    + '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    + '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'),
  'xl/worksheets/sheet1.xml': xml('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    + '<dimension ref="A1:C4"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="20"/>'
    + '<cols><col min="1" max="1" width="42" customWidth="1"/><col min="2" max="3" width="20" customWidth="1"/></cols><sheetData>'
    + `<row r="1" ht="30" customHeight="1">${sheetCell('A1', expectedText, 1)}</row>`
    + `<row r="3">${sheetCell('A3', 'Variable')}${sheetCell('B3', 'Value')}${sheetCell('C3', 'Units')}</row>`
    + `<row r="4">${sheetCell('A4', 'Signal')}<c r="B4" t="n"><v>7</v></c>${sheetCell('C4', 'm')}</row>`
    + '</sheetData><mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells></worksheet>'),
};

const drawingNs = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"';
const group = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>';
const slideText = (id, y, text, size) => `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="Text ${id}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>`
  + `<p:spPr><a:xfrm><a:off x="685800" y="${y}"/><a:ext cx="10515600" cy="914400"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>`
  + `<p:txBody><a:bodyPr wrap="square"/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="${size}"><a:solidFill><a:srgbClr val="123456"/></a:solidFill><a:latin typeface="Arial"/></a:rPr><a:t>${text}</a:t></a:r></a:p></p:txBody></p:sp>`;
const tableCell = text => '<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="1800"><a:latin typeface="Arial"/></a:rPr>'
  + `<a:t>${text}</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>`;
const slideTable = '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Scientific values"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
  + '<p:xfrm><a:off x="914400" y="3200400"/><a:ext cx="7315200" cy="1371600"/></p:xfrm>'
  + '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
  + '<a:tblPr firstRow="1" bandRow="1"/><a:tblGrid><a:gridCol w="3657600"/><a:gridCol w="3657600"/></a:tblGrid>'
  + `<a:tr h="685800">${tableCell('Variable')}${tableCell('Value')}</a:tr><a:tr h="685800">${tableCell('Signal')}${tableCell('7')}</a:tr>`
  + '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>';
const colorMap = 'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"';
const colors = { dk1: '000000', lt1: 'FFFFFF', dk2: '123456', lt2: 'EEEEEE', accent1: '336699', accent2: 'CC6600', accent3: '669966', accent4: '996699', accent5: '339999', accent6: 'CC9966', hlink: '0000FF', folHlink: '800080' };
const themeFill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>';
const themeLine = `<a:ln w="12700">${themeFill}<a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>`;
const pptx = {
  '[Content_Types].xml': contentTypes([
    ['ppt/presentation.xml', 'presentationml.presentation.main+xml'],
    ['ppt/slides/slide1.xml', 'presentationml.slide+xml'],
    ['ppt/slideLayouts/slideLayout1.xml', 'presentationml.slideLayout+xml'],
    ['ppt/slideMasters/slideMaster1.xml', 'presentationml.slideMaster+xml'],
    ['ppt/theme/theme1.xml', 'theme+xml'],
  ]),
  '_rels/.rels': rels([['rId1', 'officeDocument', 'ppt/presentation.xml']]),
  'ppt/presentation.xml': xml(`<p:presentation ${drawingNs}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>`
    + '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'),
  'ppt/_rels/presentation.xml.rels': rels([['rId1', 'slideMaster', 'slideMasters/slideMaster1.xml'], ['rId2', 'slide', 'slides/slide1.xml']]),
  'ppt/slides/slide1.xml': xml(`<p:sld ${drawingNs}><p:cSld name="ReadOnlyFixture"><p:spTree>${group}`
    + slideText(2, 685800, expectedText, 2800)
    + slideText(3, 1828800, 'Synthetic local slide - read-only integration check', 1800)
    + slideTable + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'),
  'ppt/slides/_rels/slide1.xml.rels': rels([['rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml']]),
  'ppt/slideLayouts/slideLayout1.xml': xml(`<p:sldLayout ${drawingNs} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>${group}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>`),
  'ppt/slideLayouts/_rels/slideLayout1.xml.rels': rels([['rId1', 'slideMaster', '../slideMasters/slideMaster1.xml']]),
  'ppt/slideMasters/slideMaster1.xml': xml(`<p:sldMaster ${drawingNs}><p:cSld><p:spTree>${group}</p:spTree></p:cSld><p:clrMap ${colorMap}/>`
    + '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>'),
  'ppt/slideMasters/_rels/slideMaster1.xml.rels': rels([['rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml'], ['rId2', 'theme', '../theme/theme1.xml']]),
  'ppt/theme/theme1.xml': xml('<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="DataSeek Synthetic"><a:themeElements><a:clrScheme name="DataSeek">'
    + Object.entries(colors).map(([name, color]) => `<a:${name}><a:srgbClr val="${color}"/></a:${name}>`).join('')
    + '</a:clrScheme><a:fontScheme name="Arial"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    + '<a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Simple">'
    + `<a:fillStyleLst>${themeFill.repeat(3)}</a:fillStyleLst><a:lnStyleLst>${themeLine.repeat(3)}</a:lnStyleLst>`
    + `<a:effectStyleLst>${'<a:effectStyle><a:effectLst/></a:effectStyle>'.repeat(3)}</a:effectStyleLst><a:bgFillStyleLst>${themeFill.repeat(3)}</a:bgFillStyleLst>`
    + '</a:fmtScheme></a:themeElements></a:theme>'),
};

export const nativeOfficeFixtures = await Promise.all(Object.entries({ docx, xlsx, pptx }).map(async ([extension, entries]) => ({
  filename: `dataseek-native-readonly.${extension}`,
  bytes: await pack(entries),
  expectedText,
})));
