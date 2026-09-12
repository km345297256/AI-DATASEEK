import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as geo from '../src/visualizations/extended/geoFormatData.ts';
import * as czi from '../src/visualizations/extended/cziData.ts';

const flush = async () => { for (let i = 0; i < 70; i++) await Promise.resolve(); await vue.nextTick(); };
const pending = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: value => resolve(value) }; };
const raster = () => ({ kind: 'map', array: { shape: [2, 2], dimensions: ['y', 'x'], values: [1, 2, 3, null] }, metadata: { format: 'ESRI ASCII', crs: 'unknown', extent: [10, 20, 12, 22], source_extent: [10, 20, 12, 22], source_shape: [2, 2], nodata: -9999, registration: 'cell-corner', row_order: 'north-to-south', sampling: 'nearest cell-centre sample; no aggregation', crs_source: 'unspecified' }, warnings: [], version: 'a'.repeat(64) });
const kml = () => ({ kind: 'map', geojson: { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { name: '<script>is text</script>' }, geometry: { type: 'Point', coordinates: [10, 20] } }] }, metadata: { format: 'KML 2.2', crs: 'EPSG:4326', crs_source: 'format', feature_count: 1, coordinate_count: 1 }, warnings: [], version: 'a'.repeat(64) });
const inspect = () => ({ kind: 'tree', media_type: 'application/json', tree: [{ path: '/0', node_type: 'object', attributes: { label: 'Single-scene CZI', children_count: 0 } }], metadata: { format: 'CZI', engine: 'pylibCZIrw 6.1.0', scene: 0, scene_shape: [6, 8], dimension_sizes: { C: 2, Z: 2, T: 1 }, pixel_types: ['Gray16', 'Gray16'], input_mode: 'whole', input_bytes: 1024, subblocks: 4, decoded_block_budget: 16777216, selection_mode: 'explicit C/Z/T and pixel ROI' }, choices: { channels: [0, 1], z_count: 2, time_count: 1, max_roi_size: 1024 }, selected: { indices: [0, 0, 0], roi: [0, 0, 8, 6] }, warnings: [], version: 'a'.repeat(64) });
function plane() {
  const result = inspect(); result.kind = 'image'; result.media_type = 'image/png'; delete result.tree;
  Object.assign(result.metadata, { display_range: [0, 47], normalization: 'ROI min-max to uint8 grayscale; original data unchanged', output_shape: [6, 8], roi_origin: 'scene top-left; x right, y down', invalid_pixels: 0 });
  // Header-only unit fixture exercises budget checks; browser uses a real PNG.
  const header = Buffer.alloc(33); Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82]).copy(header); header.writeUInt32BE(8, 16); header.writeUInt32BE(6, 20); header[24] = 8; header[25] = 6; result.data_base64 = header.toString('base64'); return result;
}
function mount(t, component, request, config = {}) {
  const seen = { requests: [], fits: [], maps: [], layers: [], sources: [], revoked: [], urls: [], canvas: [] }, mounted = [];
  class View { constructor(options) { this.options = options; } fit(value) { seen.fits.push(value); } }
  class Map { constructor(options) { this.options = options; seen.maps.push(this); } addLayer(layer) { seen.layers.push(layer); } getView() { return this.options.view; } setTarget(value) { this.target = value; } dispose() { this.disposed = true; } updateSize() {} }
  class Source { constructor(options) { this.options = options; seen.sources.push(this); } getExtent() { return [9, 19, 11, 21]; } clear() { this.cleared = true; } dispose() { this.disposed = true; } }
  class Layer { constructor(options) { this.options = options; } setSource(value) { this.source = value; } dispose() { this.disposed = true; } }
  const modules = { vue: { ...vue, onMounted: callback => mounted.push(callback) }, '../../composables/usePreviewLoad': { usePreviewLoad }, './runtime': { requestPreview: async (_f, _p, options, signal) => { seen.requests.push({ options, signal }); return request(options, signal); } }, './geoFormatData': geo, './cziData': czi, './domains/lifecycle': { displayError: error => error.message }, 'ol/ol.css': {}, 'ol/Map.js': { default: Map }, 'ol/View.js': { default: View }, 'ol/proj/Projection.js': { default: class { constructor(options) { this.options = options; } } }, 'ol/layer/Image.js': { default: Layer }, 'ol/source/ImageStatic.js': { default: Source }, 'ol/layer/Vector.js': { default: Layer }, 'ol/source/Vector.js': { default: Source }, 'ol/format/GeoJSON.js': { default: class { readFeatures(data) { return data.features; } } } };
  const previous = { document: globalThis.document, ResizeObserver: globalThis.ResizeObserver, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  globalThis.ResizeObserver = class { observe() {} disconnect() { seen.disconnected = true; } };
  globalThis.document = { createElement() { const canvas = { width: 0, height: 0, getContext: () => ({ createImageData: (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }), putImageData(value) { seen.pixels = value.data; } }), toBlob(callback) { if (config.encode) config.encode(callback); else callback(new Blob(['test'])); } }; seen.canvas.push(canvas); return canvas; } };
  URL.createObjectURL = () => { const url = `blob:test-${seen.urls.length}`; seen.urls.push(url); return url; }; URL.revokeObjectURL = url => seen.revoked.push(url);
  t.after(() => { Object.assign(globalThis, { document: previous.document, ResizeObserver: previous.ResizeObserver }); URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke; });
  const source = readFileSync(new URL(`../src/visualizations/extended/${component}.vue`, import.meta.url), 'utf8');
  const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: component }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }; new Function('require', 'module', 'exports', compiled)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-1', filename: config.kml ? 'data.kml' : component === 'CziPreview' ? 'data.czi' : 'data.asc' }, plugin: { id: 'viz-test', version: '1', enabled: true } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} })); if (state.target) state.target.value = {};
  if (state.imageElement) state.imageElement.value = { removeAttribute() { seen.imageSourceRemoved = true; } };
  const stop = () => scope.stop(); t.after(stop);
  return { state, props, seen, stop, start: async () => { for (const cb of mounted) await cb(); await flush(); } };
}

test('geographical arrays preserve NoData and never infer CRS', () => { const data = geo.parseGeoData(raster()); assert.equal(data.metadata.crs, 'unknown'); assert.deepEqual(data.array.values, [1, 2, 3, null]); const pixels = geo.rasterPixels(data.array.values); assert.equal(pixels.invalid, 1); assert.equal(pixels.pixels[15], 0); });
test('geographical numeric colors remain finite for extreme values and empty bands', () => { assert.deepEqual([...geo.rasterPixels([-1e308, 1e308]).pixels].slice(4), [255, 0, 0, 255]); assert.equal(geo.rasterPixels([null, null]).min, null); });
test('geographical schema rejects unsafe geometry fields and incompatible coordinate declarations', () => {
  for (const mutate of [r => { r.array.values[0] = true; }, r => { r.metadata.crs = 'EPSG:32650'; }, r => { r.metadata.extent = [10, 20, Infinity, 22]; }, r => { r.array.shape = [1024, 1024]; }, r => { r.metadata.crs = 'EPSG:4326'; r.metadata.extent = [500, 20, 502, 22]; }]) { const raw = raster(); mutate(raw); assert.throws(() => geo.parseGeoData(raw)); }
  const raw = kml(); assert.equal(geo.parseGeoData(raw).geojson.features[0].properties.name, '<script>is text</script>'); raw.geojson.features[0].properties.href = 'https://evil'; assert.throws(() => geo.parseGeoData(raw));
});
test('Geo preview shows unknown CRS in pixel space and pins explicit CRS reload', async t => {
  const view = mount(t, 'GeoFormatPreview', options => { const raw = raster(); if (options.crs) { raw.metadata.crs = options.crs; raw.metadata.crs_source = 'user'; } return raw; });
  await view.start(); assert.equal(view.state.error.value, ''); assert.deepEqual(view.seen.fits[0], [0, 0, 2, 2]); assert.match(view.state.status.value, /纯数值/);
  assert.deepEqual(view.seen.maps[0].options.view.options.projection.options, { code: 'local-grid-preview', units: 'pixels', metersPerUnit: 1, extent: [0, 0, 2, 2] });
  view.state.crs.value = 'EPSG:4326'; await view.state.render(); assert.equal(view.seen.requests[1].options.version, 'a'.repeat(64)); assert.deepEqual(view.seen.fits[1], [10, 20, 12, 22]); assert.equal(view.seen.maps[0].disposed, true); assert.deepEqual(view.seen.revoked, ['blob:test-0']);
  view.stop(); assert.equal(view.seen.maps[1].disposed, true); assert.equal(view.seen.disconnected, true); assert.deepEqual(view.seen.revoked, view.seen.urls);
});
test('KML uses only controlled offline vector data and cleans its source', async t => { const view = mount(t, 'GeoFormatPreview', () => kml(), { kml: true }); await view.start(); assert.equal(view.state.error.value, ''); assert.equal(view.seen.requests[0].options.crs, undefined); assert.equal(view.seen.urls.length, 0); assert.equal(view.seen.maps[0].options.view.options.projection, 'EPSG:4326'); view.stop(); assert.equal(view.seen.sources[0].cleared, true); });
test('Geo cancellation during canvas encoding never publishes stale image', async t => { let callback; const view = mount(t, 'GeoFormatPreview', () => raster(), { encode: cb => { callback = cb; } }); await view.start(); view.stop(); assert.equal(view.seen.canvas[0].width, 0); callback(new Blob(['late'])); await flush(); assert.equal(view.seen.urls.length, 0); });
test('CZI checks metadata without pixels and requires explicit version-pinned image action', async t => {
  const view = mount(t, 'CziPreview', options => options.kind === 'tree' ? inspect() : plane()); await view.start(); assert.equal(view.state.imageUrl.value, ''); assert.deepEqual(view.seen.requests[0].options, { kind: 'tree' }); assert.equal(view.state.details.value.dimensions.C, 2);
  await view.state.load('image'); assert.equal(view.state.error.value, ''); assert.deepEqual(view.seen.requests[1].options, { kind: 'image', version: 'a'.repeat(64), indices: [0, 0, 0], roi: [0, 0, 8, 6] }); assert.equal(view.state.imageUrl.value, 'blob:test-0'); view.state.imageElement.value = undefined; view.stop(); assert.equal(view.seen.imageSourceRemoved, true); assert.deepEqual(view.seen.revoked, ['blob:test-0']);
});
test('CZI rejects invalid selections before making image request', async t => { const view = mount(t, 'CziPreview', () => inspect()); await view.start(); view.state.roi.value = [0, 0, 999, 999]; await view.state.load('image'); assert.equal(view.seen.requests.length, 1); assert.match(view.state.error.value, /越界/); });
test('Geo refuses a different pinned version or CRS and clears stale labels', async t => {
  for (const mode of ['version', 'crs']) {
    let calls = 0;
    const view = mount(t, 'GeoFormatPreview', () => { const value = raster(); if (++calls > 1) { if (mode === 'version') value.version = 'b'.repeat(64); else value.metadata.crs = 'EPSG:4326'; } return value; });
    await view.start(); await view.state.render(); assert.match(view.state.error.value, mode === 'version' ? /版本/ : /坐标系/); assert.deepEqual(view.state.metadata.value, {}); assert.deepEqual(view.state.warnings.value, []); view.stop();
  }
});
test('CZI refuses valid-shaped images for a different version or selected plane', async t => {
  for (const mode of ['version', 'indices']) {
    const view = mount(t, 'CziPreview', options => { if (options.kind === 'tree') return inspect(); const p = plane(); if (mode === 'version') p.version = 'b'.repeat(64); else p.selected.indices = [1, 0, 0]; return p; });
    await view.start(); await view.state.load('image'); assert.match(view.state.error.value, mode === 'version' ? /版本/ : /不一致/); assert.equal(view.seen.urls.length, 0); view.stop();
  }
});
test('enum arrays cannot impersonate accepted strings', () => { const raw = raster(); raw.metadata.crs = ['unknown']; assert.throws(() => geo.parseGeoData(raw)); const p = plane(); p.metadata.pixel_types[0] = ['Gray16']; assert.throws(() => czi.parseCziData(p)); });
test('CZI late stopped inspection cannot populate controls or create blobs', async t => { const task = pending(); const view = mount(t, 'CziPreview', () => task.promise); await view.start(); view.stop(); assert.equal(view.seen.requests[0].signal.aborted, true); task.resolve(inspect()); await flush(); assert.equal(view.state.details.value, undefined); assert.equal(view.seen.urls.length, 0); });
test('new preview catalog clones do not reread files and changed identities do', async t => {
  for (const component of ['GeoFormatPreview', 'CziPreview']) {
    const view = mount(t, component, () => component === 'CziPreview' ? inspect() : raster()); await view.start();
    for (let i = 0; i < 3; i++) { view.props.plugin = { ...view.props.plugin }; view.props.file = { ...view.props.file }; await flush(); }
    assert.equal(view.seen.requests.length, 1); view.props.plugin.version = '2'; await flush(); assert.equal(view.seen.requests.length, 2); view.stop();
  }
});
test('CZI schema refuses decoder mismatch, oversized/mismatched PNG and foreign choices', () => {
  assert.deepEqual(czi.parseCziData(inspect()).shape, [6, 8]); assert.equal(czi.parseCziData(plane()).png.length, 33);
  for (const mutate of [p => { p.metadata.engine = 'unknown'; }, p => { p.selected.roi[2] = 1025; }, p => { p.data_base64 = '<svg>'; }, p => { p.choices.channels = [0, 2]; }, p => { p.metadata.output_shape = [7, 8]; }, p => { p.metadata.pixel_types[0] = 'Bgr96Float'; }]) { const p = plane(); mutate(p); assert.throws(() => czi.parseCziData(p)); }
});
test('new templates remain inert and never accept user URLs or HTML', () => { for (const name of ['GeoFormatPreview', 'CziPreview']) { const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8'); assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|https?:\/\//); assert.match(source, /watch\(\[/); } });
