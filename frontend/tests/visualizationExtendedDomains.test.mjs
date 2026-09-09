import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { parse, compileScript } from '@vue/compiler-sfc';
import { boundedGeoJSON, boundedCzml, geoCenter, validateRaster, validateNiftiOrNrrd, fitsWcsHeader, chromosomeSizes, checkedGenomicText } from '../src/visualizations/extended/domains/guards.ts';
const bytes = (value) => new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value)).buffer;
const feature = (geometry = { type: 'Point', coordinates: [10, 20] }) => ({ type: 'Feature', geometry, properties: { name: 'sample', html: '<img src=https://evil.test>', 'marker-symbol': 'https://evil.test/icon.png' } });
const nc = (overrides = {}) => {
  const buffer = new ArrayBuffer(1024), view = new DataView(buffer);
  view.setInt32(0, 348, true); view.setInt16(40, 3, true); for (let i = 1; i < 4; i++) view.setInt16(40 + i * 2, 2, true);
  view.setInt16(70, 2, true); view.setInt16(72, 8, true); view.setFloat32(108, 352, true); new Uint8Array(buffer).set(new TextEncoder().encode('n+1\0'), 344);
  for (const [key, value] of Object.entries(overrides)) view.setInt16(Number(key), value, true);
  return buffer;
};
function fits(changes = {}) {
  const values = { SIMPLE: 'T', BITPIX: '-32', NAXIS: '2', NAXIS1: '4', NAXIS2: '4', CTYPE1: "'RA---TAN'", CTYPE2: "'DEC--TAN'", CRVAL1: '20', CRVAL2: '30', CRPIX1: '2', CRPIX2: '2', CDELT1: '-0.01', CDELT2: '0.01', ...changes };
  const cards = Object.entries(values).filter(([, v]) => v !== null).map(([key, value]) => `${key.padEnd(8)}= ${value}`.padEnd(80)); cards.push('END'.padEnd(80));
  const data = new Uint8Array(5760); data.fill(32, 0, 2880); data.set(new TextEncoder().encode(cards.join(''))); return data.buffer;
}
const nrrd = (extra = '') => bytes(`NRRD0005\ntype: uchar\ndimension: 3\nsizes: 2 2 2\nencoding: raw\n${extra}\nabcdefgh`);

test('GeoJSON display model strips all untrusted styles/URLs and keeps correct geographic center', () => {
  const result = boundedGeoJSON(bytes({ type: 'FeatureCollection', features: [feature(), feature({ type: 'Point', coordinates: [30, 40] })] }));
  assert.deepEqual(result.features[0].properties, { name: 'sample' }); assert.deepEqual(geoCenter(result), [20, 30]);
});
test('GeoJSON accepts six standard finite geometry shapes', () => {
  for (const [type, coordinates] of [['Point', [1, 2]], ['MultiPoint', [[1, 2]]], ['LineString', [[1, 2], [2, 3]]], ['MultiLineString', [[[1, 2], [2, 3]]]], ['Polygon', [[[1, 2], [2, 3], [1, 2]]]], ['MultiPolygon', [[[[1, 2], [2, 3], [1, 2]]]]]]) assert.equal(boundedGeoJSON(bytes(feature({ type, coordinates }))).features[0].geometry.type, type);
});
for (const [name, value] of [
  ['unknown projection', { ...feature(), crs: { name: 'EPSG:3857' } }],
  ['unsupported geometry', feature({ type: 'GeometryCollection', geometries: [] })],
  ['invalid longitude', feature({ type: 'Point', coordinates: [181, 20] })],
  ['invalid latitude', feature({ type: 'Point', coordinates: [20, -91] })],
  ['non numeric coordinates', feature({ type: 'Point', coordinates: ['20', 30] })],
  ['empty', { type: 'FeatureCollection', features: [] }],
  ['too many points', feature({ type: 'MultiPoint', coordinates: Array.from({ length: 100001 }, () => [0, 0]) })],
  ['too many features', { type: 'FeatureCollection', features: Array.from({ length: 10001 }, () => feature()) }],
]) test(`GeoJSON rejects ${name}`, () => assert.throws(() => boundedGeoJSON(bytes(value))));

test('CZML positions are rebuilt into trusted static style with exact timestamp bounds', () => {
  const value = boundedCzml(bytes([{ id: 'document', version: '1.0' }, { id: 'one', name: 'Track', position: { epoch: '2026-01-01T00:00:00Z', cartographicDegrees: [0, 10, 20, 0, 60, 11, 22, 100] }, point: { pixelSize: 9999 } }]));
  assert.equal(value[1].availability, '2026-01-01T00:00:00.000Z/2026-01-01T00:01:00.000Z'); assert.deepEqual(value[1].point.color.rgba, [255, 130, 40, 255]); assert.ok(!JSON.stringify(value).includes('evil'));
});
for (const [name, packet] of [
  ['model URI', { id: 'x', model: { gltf: 'https://evil.test' }, position: { cartographicDegrees: [1, 2, 3] } }],
  ['embedded image URI', { id: 'x', point: { image: 'data:image/png,abc' }, position: { cartographicDegrees: [1, 2, 3] } }],
  ['position reference', { id: 'x', position: { reference: 'other#position' } }],
  ['invalid latitude', { id: 'x', position: { cartographicDegrees: [1, 100, 3] } }],
  ['repeated time', { id: 'x', position: { epoch: '2026-01-01T00:00:00Z', cartographicDegrees: [0, 1, 2, 3, 0, 2, 3, 4] } }],
  ['bad epoch', { id: 'x', position: { epoch: 'today', cartographicDegrees: [0, 1, 2, 3] } }],
]) test(`CZML rejects ${name}`, () => assert.throws(() => boundedCzml(bytes([{ id: 'document', version: '1.0' }, packet]))));
test('CZML rejects empty, missing document and duplicate identifiers', () => {
  for (const value of [[], [{ id: 'document', version: '1.0' }], [{ id: 'nope', version: '1.0' }], [{ id: 'document', version: '1.0' }, { id: 'one', position: { cartographicDegrees: [1, 2, 3] } }, { id: 'one', position: { cartographicDegrees: [1, 2, 3] } }]]) assert.throws(() => boundedCzml(bytes(value)));
});

test('raster budgets enforce dimensions and aggregate channel allocation before decoding', () => {
  assert.doesNotThrow(() => validateRaster(1024, 1024, 6));
  for (const values of [[0, 2], [100000, 100000], [4096, 4096, 6], [NaN, 20], [2.1, 2]]) assert.throws(() => validateRaster(...values));
});
test('valid local raw NIfTI-1 and embedded raw NRRD pass', () => { assert.doesNotThrow(() => validateNiftiOrNrrd(nc(), 'a.nii')); assert.doesNotThrow(() => validateNiftiOrNrrd(nrrd(), 'a.nrrd')); });
for (const [name, buffer, filename] of [
  ['compressed extension', nc(), 'a.nii.gz'], ['paired-file magic', new ArrayBuffer(1024), 'a.nii'], ['truncated payload', nc().slice(0, 354), 'a.nii'], ['huge dims', nc({ 42: 32767, 44: 32767 }), 'a.nii'], ['unsupported datatype', nc({ 70: 32 }), 'a.nii'], ['mismatched bitpix', nc({ 72: 64 }), 'a.nii'], ['negative dims', nc({ 42: -1 }), 'a.nii'],
  ['NRRD detached path', nrrd('data file: /Users/private/data.raw\n'), 'a.nrrd'], ['NRRD network path', nrrd('datafile: https://evil.test\n'), 'a.nrrd'], ['NRRD compressed encoding', bytes('NRRD0005\ntype: uchar\ndimension: 3\nsizes: 2 2 2\nencoding: gzip\n\n12345678'), 'a.nrrd'], ['NRRD skip', nrrd('byte skip: 4\n'), 'a.nrrd'],
]) test(`scientific volume rejects ${name}`, () => assert.throws(() => validateNiftiOrNrrd(buffer, filename)));
test('FITS WCS retains correct sky center', () => assert.deepEqual(fitsWcsHeader(fits()), { width: 4, height: 4, ra: 20, dec: 30 }));
for (const [name, changes] of [['cube', { NAXIS: '3' }], ['missing WCS', { CTYPE1: null }], ['numeric coordinate missing', { CRVAL1: null }], ['out of sky', { CRVAL2: '95' }], ['unsupported pixels', { BITPIX: '64' }], ['oversized', { NAXIS1: '999999999' }], ['truncated image', { NAXIS1: '1000', NAXIS2: '1000' }]]) test(`FITS WCS rejects ${name}`, () => assert.throws(() => fitsWcsHeader(fits(changes))));

test('IGV requires explicit unique reference lengths and preserves exact BED coordinates', () => {
  const ref = chromosomeSizes('chr1\t1000\nchr2 500'); assert.equal(ref.first, 'chr1'); assert.equal(ref.text, 'chr1\t1000\nchr2\t500');
  assert.equal(checkedGenomicText(bytes('track name=a url=https://evil.test\nchr1\t10\t20\tgene\t0\t+\n'), 'bed', ref.sizes), 'chr1\t10\t20\tgene\t0\t+');
});
for (const text of ['', 'chr1 abc', 'chr1 100\nchr1 200', 'chr1 -1', 'https://evil.test 500']) test(`IGV rejects invalid reference ${JSON.stringify(text)}`, () => assert.throws(() => chromosomeSizes(text)));
test('IGV rejects unknown chromosome and out-of-assembly BED/VCF coordinates', () => {
  const ref = chromosomeSizes('chr1 100');
  for (const line of ['chr2\t0\t20', 'chr1\t-1\t20', 'chr1\t10\t101', 'chr1\t10\t1']) assert.throws(() => checkedGenomicText(bytes(line), 'bed', ref.sizes));
});
test('IGV core VCF preserves position, allele and failed FILTER without exposing arbitrary INFO', () => {
  const ref = chromosomeSizes('chr1 100'); const text = '##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t15\tid1\tA\tG\t25\tLowQual\tURL=https://evil.test';
  const value = checkedGenomicText(bytes(text), 'vcf', ref.sizes); assert.ok(value.includes('chr1\t15\tid1\tA\tG\t25\tLowQual\t.')); assert.ok(!value.includes('evil'));
  assert.throws(() => checkedGenomicText(bytes(text.replace('\tG\t', '\t<script>\t')), 'vcf', ref.sizes));
});
test('all seven domain components compile and use only authorized binary transport', () => {
  for (const name of ['OpenLayers', 'DeckMap', 'Cesium', 'Aladin', 'Igv', 'Viv', 'Niivue']) {
    const filename = `${name}Preview.vue`, source = readFileSync(new URL(`../src/visualizations/extended/domains/${filename}`, import.meta.url), 'utf8');
    const { descriptor, errors } = parse(source, { filename }); assert.deepEqual(errors, []); assert.ok(compileScript(descriptor, { id: name }).content);
    assert.match(source, /loadPluginBytes\(props\.file, props\.plugin, scope\.signal\)/); assert.match(source, /useDomainScope/); assert.doesNotMatch(source, /getFileDownloadUrl|createFileSignedUrl|downloadFile|window\.fetch\s*=/);
  }
});
test('Aladin is an offline lifetime-isolated real upstream viewer; no remote CDN or default survey', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/AladinPreview.vue', import.meta.url), 'utf8');
  assert.match(source, /visualization-assets\/aladin\/aladin\.js/); assert.match(source, /survey:\[\]/); assert.match(source, /A\.image\(/); assert.match(source, /frame\.remove\(\)/); assert.match(source, /connect-src 'self' data: blob:/); assert.doesNotMatch(source, /https:\/\//);
  assert.match(source, /parentOrigin=\$\{JSON.stringify\(location.origin\)\}/); assert.match(source, /event.origin!==parentOrigin/);
});
test('IGV selects the real ESM export, not the package browser IIFE', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/IgvPreview.vue', import.meta.url), 'utf8');
  assert.match(source, /import\('igv\/dist\/igv.esm.js'\)/);
});
test('Viv accepts TIFF ASCII NUL termination before strict XML parsing', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/VivPreview.vue', import.meta.url), 'utf8');
  assert.ok(source.includes(".replace(/\\0+$/, '')"));
});
test('MapLibre uses the versioned offline worker asset path', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/DeckMapPreview.vue', import.meta.url), 'utf8');
  assert.match(source, /setWorkerUrl/); assert.match(source, /maplibre\/maplibre-gl-worker.mjs/); assert.match(source, /isolatedDomainFrame/);
});
test('global SDK worker pools have isolated realms with source/origin/nonce gating', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/frame.ts', import.meta.url), 'utf8');
  assert.match(source, /event.source !== frame.contentWindow/); assert.match(source, /event.origin !== origin/); assert.match(source, /scope.signal.aborted/); assert.match(source, /frame.remove\(\)/);
  assert.match(source, /domains\/host.html/); assert.match(source, /dataseek-visualization-host/); assert.match(source, /document.write\(source\)/); assert.doesNotMatch(source, /document.write\(.*payload/);
  for (const name of ['Cesium', 'DeckMap']) assert.match(readFileSync(new URL(`../src/visualizations/extended/domains/${name}Preview.vue`, import.meta.url), 'utf8'), /isolatedDomainFrame/);
});
