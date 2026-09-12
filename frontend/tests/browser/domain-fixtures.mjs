import assert from 'node:assert/strict';
const encode = (text) => new TextEncoder().encode(text);

function fitsBytes() {
  const width = 32, height = 32;
  const headers = { SIMPLE: 'T', BITPIX: '-32', NAXIS: '2', NAXIS1: String(width), NAXIS2: String(height), CTYPE1: "'RA---TAN'", CTYPE2: "'DEC--TAN'", CRVAL1: '20', CRVAL2: '30', CRPIX1: '16', CRPIX2: '16', CDELT1: '-0.01', CDELT2: '0.01' };
  const data = new Uint8Array(2880 + Math.ceil(width * height * 4 / 2880) * 2880); data.fill(32, 0, 2880);
  data.set(encode([...Object.entries(headers).map(([key, value]) => `${key.padEnd(8)}= ${value}`.padEnd(80)), 'END'.padEnd(80)].join('')));
  const view = new DataView(data.buffer);
  for (let i = 0; i < width * height; i++) view.setFloat32(2880 + i * 4, Math.exp(-((i % width - 16) ** 2 + (Math.floor(i / width) - 16) ** 2) / 35), false);
  return data;
}

function niiBytes() {
  const size = 16, data = new Uint8Array(352 + size ** 3), view = new DataView(data.buffer);
  view.setInt32(0, 348, true); view.setInt16(40, 3, true);
  for (let i = 1; i < 4; i++) view.setInt16(40 + i * 2, size, true);
  view.setInt16(70, 2, true); view.setInt16(72, 8, true);
  for (let i = 0; i < 8; i++) view.setFloat32(76 + i * 4, 1, true);
  view.setFloat32(108, 352, true); view.setFloat32(112, 1, true);
  data.set(encode('n+1\0'), 344);
  for (let z = 0; z < size; z++) for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) data[352 + z * size ** 2 + y * size + x] = (x - 8) ** 2 + (y - 8) ** 2 + (z - 8) ** 2 < 30 ? 220 : 12;
  return data;
}

/** Minimal baseline TIFF writer for two local OME planes or a georeferenced single plane. */
function tiffBytes(ome = false) {
  const width = 32, height = 32, planes = ome ? 2 : 1;
  const description = ome ? '<?xml version="1.0" encoding="UTF-8"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0" Name="Synthetic"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16" SizeX="32" SizeY="32" SizeZ="1" SizeC="2" SizeT="1" BigEndian="false"><Channel ID="Channel:0:0" Name="Channel Red" SamplesPerPixel="1"/><Channel ID="Channel:0:1" Name="Channel Green" SamplesPerPixel="1"/><TiffData IFD="0" PlaneCount="2"/></Pixels></Image></OME>\0' : 'Synthetic GeoTIFF\0';
  const ascii = encode(description);
  const tags = ome ? 12 : 15, ifdBytes = 2 + tags * 12 + 4;
  const extraStart = 8 + planes * ifdBytes, scaleOffset = extraStart + ascii.length + (ascii.length % 2), tieOffset = scaleOffset + 24, keysOffset = tieOffset + 48;
  const pixelsStart = ome ? scaleOffset : keysOffset + 32;
  const data = new Uint8Array(pixelsStart + planes * width * height * 2), view = new DataView(data.buffer);
  data[0] = 73; data[1] = 73; view.setUint16(2, 42, true); view.setUint32(4, 8, true); data.set(ascii, extraStart);
  for (let p = 0; p < planes; p++) {
    const start = 8 + p * ifdBytes, entries = [[256, 4, 1, width], [257, 4, 1, height], [258, 3, 1, 16], [259, 3, 1, 1], [262, 3, 1, 1], [270, 2, ascii.length, extraStart], [273, 4, 1, pixelsStart + p * width * height * 2], [277, 3, 1, 1], [278, 4, 1, height], [279, 4, 1, width * height * 2], [284, 3, 1, 1], [339, 3, 1, 1]];
    if (!ome) entries.push([33550, 12, 3, scaleOffset], [33922, 12, 6, tieOffset], [34735, 3, 16, keysOffset]);
    entries.sort((a, b) => a[0] - b[0]); view.setUint16(start, entries.length, true);
    entries.forEach(([tag, type, count, value], i) => { const offset = start + 2 + i * 12; view.setUint16(offset, tag, true); view.setUint16(offset + 2, type, true); view.setUint32(offset + 4, count, true); if (type === 3 && count === 1) view.setUint16(offset + 8, value, true); else view.setUint32(offset + 8, value, true); });
    view.setUint32(start + 2 + entries.length * 12, p + 1 < planes ? start + ifdBytes : 0, true);
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) view.setUint16(pixelsStart + (p * width * height + y * width + x) * 2, Math.round(Math.max(0, 1000 - ((x - (p ? 20 : 10)) ** 2 + (y - 16) ** 2) * 15)), true);
  }
  if (!ome) {
    [0.1, 0.1, 0].forEach((v, i) => view.setFloat64(scaleOffset + i * 8, v, true));
    [0, 0, 0, 10, 20, 0].forEach((v, i) => view.setFloat64(tieOffset + i * 8, v, true));
    [1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326].forEach((v, i) => view.setUint16(keysOffset + i * 2, v, true));
  }
  return data;
}

const geo = encode(JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { name: 'Test Point' }, geometry: { type: 'Point', coordinates: [12, 22] } }, { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [[10, 20], [14, 24], [17, 21]] } }] }));
const canvasReady = (selector = 'canvas') => async (page) => { await page.locator(selector).first().waitFor(); await page.waitForTimeout(1500); };
const gpuReady = (selector = 'canvas') => async (page) => { await canvasReady(selector)(page); await page.waitForFunction(() => window.__webglDraws > 0); };
const frameGpuReady = async (page) => { await page.frameLocator('iframe').locator('canvas').first().waitFor({ timeout: 45000 }); await page.frames()[1].waitForFunction(() => window.__webglDraws > 0); await page.waitForTimeout(750); };
const cesiumReady = async (page) => { await page.getByText('本地图层已加载', { exact: false }).waitFor({ timeout: 20000 }); await frameGpuReady(page); };
const readyText = (text) => async (page) => { await page.getByText(text, { exact: false }).waitFor({ timeout: 45000 }); await page.waitForTimeout(500); };
const geotiffReady = async (page) => {
  // Match the completed fixture status, never the band selector or transient
  // “正在读取波段 1…” message. ImageStatic may paint after decoding is complete.
  await page.getByText('波段 1 / 1 · EPSG:4326 · 32×32 最近邻有界预览 · 色标为预览范围，非全量统计', { exact: true }).waitFor({ timeout: 45000 });
  await page.locator('.surface[aria-busy="false"] canvas').waitFor();
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.surface[aria-busy="false"] canvas');
    if (!canvas?.width || !canvas?.height) return false;
    const context = canvas.getContext('2d'); if (!context) return false;
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let rasterPixels = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] && pixels[i + 1] <= 190 && pixels[i] + pixels[i + 2] >= 240 && pixels[i] + pixels[i + 2] <= 270 && ++rasterPixels > 100) return true;
    }
    return false;
  }, undefined, { timeout: 20000 });
};
export const domainCases = [
  { name: 'openlayers-geojson', component: 'domains/OpenLayersPreview.vue', filename: 'sample.geojson', reader: 'binary', bytes: geo, ready: readyText('2 个要素') },
  { name: 'openlayers-geotiff', component: 'domains/OpenLayersPreview.vue', filename: 'sample.tif', reader: 'binary', bytes: tiffBytes(), ready: geotiffReady },
  { name: 'maplibre-deck', component: 'domains/DeckMapPreview.vue', filename: 'sample.geojson', reader: 'binary', bytes: geo, ready: async (page) => { await gpuReady()(page); await frameGpuReady(page); }, verify: async (page) => { assert.equal(await page.locator('canvas').count(), 1); const childCanvas = page.frameLocator('iframe').locator('canvas'); assert.ok(await childCanvas.count() > 0); const before = await page.evaluate(() => window.__webglDraws); const box = await childCanvas.boundingBox(); await page.mouse.move(box.x+box.width/2, box.y+box.height/2); await page.mouse.down(); await page.mouse.move(box.x+box.width/2+80, box.y+box.height/2+20, { steps: 5 }); await page.mouse.up(); await page.waitForFunction(before => window.__webglDraws > before, before); return { parentOverlayTracksChildCamera: true }; } },
  { name: 'cesium-geojson', component: 'domains/CesiumPreview.vue', filename: 'sample.geojson', reader: 'binary', bytes: geo, ready: cesiumReady },
  { name: 'cesium-czml', component: 'domains/CesiumPreview.vue', filename: 'sample.czml', reader: 'binary', bytes: encode(JSON.stringify([{ id: 'document', version: '1.0' }, { id: 'track', position: { epoch: '2026-01-01T00:00:00Z', cartographicDegrees: [0, 10, 20, 1000, 60, 15, 25, 1000, 120, 20, 20, 1000] } }])), ready: cesiumReady },
  { name: 'aladin-fits', component: 'domains/AladinPreview.vue', filename: 'sample.fits', reader: 'binary', bytes: fitsBytes(), ready: readyText('本地 FITS 天球 WCS'), verify: async (page) => { assert.ok(await page.frameLocator('iframe').locator('canvas').count() > 0); assert.ok(await page.frames()[1].evaluate(() => window.__webglDraws > 0)); } },
  { name: 'igv-bed', component: 'domains/IgvPreview.vue', filename: 'sample.bed', reader: 'binary', bytes: encode('chr1\t100\t500\tgene-one\t0\t+\nchr1\t600\t1000\tgene-two\t0\t-\n'), setup: async (page) => { await page.locator('textarea').fill('chr1\t2000'); await page.getByRole('button', { name: '确认参考并显示轨道' }).click(); }, ready: canvasReady('.igv-viewport canvas'), verify: async (page) => { const pixels = await page.locator('.igv-viewport canvas').evaluateAll(canvases => canvases.reduce((count, canvas) => { if(!canvas.width || !canvas.height) return count; const pixels = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data; for(let i=0;i<pixels.length;i+=4) if(pixels[i+3] && pixels[i+2] > 80 && pixels[i] < 50) count++; return count; },0)); assert.ok(pixels > 100, 'IGV must draw both local annotation intervals'); return { annotationPixels: pixels, explicitReference: true }; } },
  { name: 'viv-ome-tiff', component: 'domains/VivPreview.vue', filename: 'sample.ome.tif', reader: 'binary', bytes: tiffBytes(true), ready: readyText('2 通道'), verify: async (page) => { assert.equal(await page.getByRole('checkbox').count(), 2); await page.getByRole('checkbox').last().uncheck(); } },
  { name: 'niivue-volume', component: 'domains/NiivuePreview.vue', filename: 'sample.nii', reader: 'binary', bytes: niiBytes(), ready: async (page) => { await page.getByLabel('影像布局').waitFor({ timeout: 45000 }); await page.waitForTimeout(500); } },
];
