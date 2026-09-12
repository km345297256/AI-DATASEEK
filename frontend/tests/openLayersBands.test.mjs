import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as guards from '../src/visualizations/extended/domains/guards.ts';

const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); await vue.nextTick(); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const compile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
function mount(t, config = {}) {
  const observations = { downloads: 0, parsed: 0, imageReads: 0, rasterReads: [], fits: [], layers: [], sources: [], urls: [], revoked: [], pixels: [], canvases: [], closes: 0, removed: [], observers: [] };
  const mounted = [], unmounted = [];
  const lifeModule = { exports: {} };
  const lifeSource = readFileSync(new URL('../src/visualizations/extended/domains/lifecycle.ts', import.meta.url), 'utf8');
  new Function('require', 'module', 'exports', compile(lifeSource))(() => ({ onBeforeUnmount: callback => unmounted.push(callback) }), lifeModule, lifeModule.exports);
  class View { constructor(options) { this.center = options.center; } fit(extent, options) { observations.fits.push({ extent, options }); } }
  class Map {
    constructor(options) { this.view = options.view; this.target = options.target; observations.map = this; }
    addLayer(layer) { observations.layers.push(layer); }
    removeLayer(layer) { observations.removed.push(layer); }
    getView() { return this.view; }
    setTarget(value) { this.target = value; }
    dispose() { this.disposed = true; }
    updateSize() {}
  }
  class ImageLayer { constructor(options) { this.source = options.source; } setSource(value) { this.source = value; } dispose() { this.disposed = true; } }
  class ImageStatic { constructor(options) { this.options = options; observations.sources.push(this); } dispose() { this.disposed = true; } }
  const image = {
    getSamplesPerPixel: () => config.samples ?? 2, getWidth: () => config.width ?? 2, getHeight: () => config.height ?? 2,
    getGeoKeys: () => ({ GeographicTypeGeoKey: config.epsg ?? 4326 }), fileDirectory: { hasTag: () => config.rotated ?? false },
    getBoundingBox: () => config.extent ?? [100, 30, 102, 32], getGDALNoData: () => config.nodata ?? -999,
    readRasters: async options => {
      observations.rasterReads.push(options);
      return config.read ? config.read(options) : options.samples[0] === 0 ? new Float64Array([-999, 0, 1, NaN]) : new Float64Array([5, 10, 15, 20]);
    },
  };
  const modules = {
    vue: { ...vue, onMounted: callback => mounted.push(callback) }, './guards': guards, './lifecycle': lifeModule.exports, 'ol/ol.css': {},
    '../runtime': { loadPluginBytes: async () => { observations.downloads++; return new TextEncoder().encode(config.geojson ?? 'local-tiff').buffer; } },
    'ol/Map.js': { default: Map }, 'ol/View.js': { default: View }, 'ol/layer/Vector.js': { default: class { constructor(options) { this.options = options; } } },
    'ol/source/Vector.js': { default: class { getExtent() { return [0, 0, 1, 1]; } } },
    'ol/format/GeoJSON.js': { default: class { readFeatures(value) { return value.features; } } },
    geotiff: { fromArrayBuffer: async () => { observations.parsed++; return { getImage: async () => { observations.imageReads++; return image; }, close: async () => { observations.closes++; } }; } },
    'ol/layer/Image.js': { default: ImageLayer }, 'ol/source/ImageStatic.js': { default: ImageStatic }, 'ol/proj.js': { transformExtent: extent => extent },
  };
  const previous = { document: globalThis.document, ResizeObserver: globalThis.ResizeObserver, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  globalThis.ResizeObserver = class { constructor() { observations.observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  globalThis.document = { createElement(tag) {
    assert.equal(tag, 'canvas');
    const canvas = { width: 0, height: 0, getContext: () => ({ createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }), putImageData: pixels => observations.pixels.push([...pixels.data]) }),
      toBlob(callback) { if (config.encode) config.encode(callback); else callback(new Blob(['png'])); } };
    observations.canvases.push(canvas); return canvas;
  } };
  URL.createObjectURL = blob => { const url = `blob:raster-${observations.urls.length}`; observations.urls.push({ url, blob }); return url; };
  URL.revokeObjectURL = url => observations.revoked.push(url);
  t.after(() => { globalThis.document = previous.document; globalThis.ResizeObserver = previous.ResizeObserver; URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke; });
  const source = readFileSync(new URL('../src/visualizations/extended/domains/OpenLayersPreview.vue', import.meta.url), 'utf8');
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compile(compileScript(parse(source).descriptor, { id: 'openlayers' }).content))(
    id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup({ file: { file_id: 'raster', filename: config.geojson ? 'local.geojson' : 'local.tif' }, plugin: { id: 'openlayers' } }, { expose() {} }));
  state.target.value = {};
  let stopped = false;
  const stop = () => { if (!stopped) { stopped = true; for (const callback of unmounted) callback(); scope.stop(); } };
  t.after(stop);
  return { state, observations, stop, start: async () => { for (const callback of mounted) await callback(); await flush(); } };
}

test('band one remains default, NoData/nonfinite pixels are transparent and range is visible', async t => {
  const view = mount(t); await view.start(); const { state, observations: seen } = view;
  assert.equal(state.band.value, 1); assert.equal(state.bandCount.value, 2); assert.equal(state.busy.value, false);
  assert.deepEqual(state.colorRange.value, { min: 0, max: 1 }); assert.equal(state.invalidCount.value, 2); assert.match(state.nodataLabel.value, /-999/);
  assert.deepEqual(seen.rasterReads[0].samples, [0]); assert.equal(seen.rasterReads[0].resampleMethod, 'nearest');
  assert.equal(seen.pixels[0][3], 0); assert.equal(seen.pixels[0][15], 0); assert.equal(seen.pixels[0][7], 255);
  assert.deepEqual(seen.pixels[0].slice(8, 12), [255, 0, 0, 255]); assert.equal(seen.sources[0].options.interpolate, false);
});
test('switching bands reuses authorized TIFF input, preserves map view and releases replaced layers', async t => {
  const view = mount(t); await view.start(); const { state, observations: seen } = view;
  seen.map.view.center = [333, 444]; state.band.value = 2; await flush();
  assert.equal(seen.downloads, 1); assert.equal(seen.parsed, 1); assert.equal(seen.imageReads, 1);
  assert.deepEqual(seen.rasterReads.map(item => item.samples), [[0], [1]]); assert.equal(seen.fits.length, 1); assert.deepEqual(seen.map.view.center, [333, 444]);
  assert.equal(seen.layers[0].disposed, true); assert.equal(seen.sources[0].disposed, true); assert.deepEqual(seen.revoked, ['blob:raster-0']);
  assert.deepEqual(state.colorRange.value, { min: 5, max: 20 }); view.stop();
  assert.equal(seen.layers[1].disposed, true); assert.equal(seen.sources[1].disposed, true); assert.deepEqual(seen.revoked, ['blob:raster-0', 'blob:raster-1']);
  assert.equal(seen.closes, 1); assert.equal(seen.map.disposed, true); assert.equal(seen.observers[0].disconnected, true);
});
test('late cancelled first band cannot repaint or clear the newer busy state', async t => {
  const one = deferred(), two = deferred();
  const view = mount(t, { read: options => options.samples[0] === 0 ? one.promise : two.promise });
  const initial = view.start(); await flush(); view.state.band.value = 2; await flush();
  assert.equal(view.observations.rasterReads[0].signal.aborted, true);
  one.resolve(new Float32Array([1, 2, 3, 4])); await initial;
  assert.equal(view.state.busy.value, true); assert.equal(view.observations.urls.length, 0);
  two.resolve(new Float32Array([5, 6, 7, 8])); await flush();
  assert.equal(view.state.busy.value, false); assert.deepEqual(view.state.colorRange.value, { min: 5, max: 8 }); assert.equal(view.observations.layers.length, 1);
});
test('unmount during PNG encoding zeros the scratch canvas and ignores its late callback', async t => {
  let encoded;
  const view = mount(t, { encode: callback => { encoded = callback; } });
  const initial = view.start(); await flush(); assert.equal(typeof encoded, 'function'); view.stop();
  assert.equal(view.observations.rasterReads[0].signal.aborted, true); assert.equal(view.observations.canvases[0].width, 0);
  encoded(new Blob(['old'])); await initial; assert.equal(view.observations.urls.length, 0); assert.equal(view.observations.layers.length, 0); assert.equal(view.observations.closes, 1);
});
test('a NoData-only band can fail without discarding the TIFF or preventing a retry', async t => {
  const view = mount(t, { read: options => new Float32Array(options.samples[0] === 0 ? [-999, -999, NaN, Infinity] : [4, 4, 4, 4]) });
  await view.start(); assert.match(view.state.error.value, /没有有效数值/); assert.equal(view.state.busy.value, false); assert.equal(view.observations.closes, 0);
  view.state.band.value = 2; await flush(); assert.equal(view.state.error.value, ''); assert.deepEqual(view.state.colorRange.value, { min: 4, max: 4 });
  assert.equal(view.state.colorStyle.value.background, 'rgb(0,190,255)'); assert.equal(view.observations.downloads, 1);
});
test('extreme finite Float64 values produce bounded colors without overflow', async t => {
  const view = mount(t, { read: () => new Float64Array([-1e308, 0, 1e308, -999]) }); await view.start();
  assert.deepEqual(view.state.colorRange.value, { min: -1e308, max: 1e308 }); assert.equal(view.state.error.value, '');
  assert.deepEqual(view.observations.pixels[0].slice(0, 4), [0, 190, 255, 255]); assert.deepEqual(view.observations.pixels[0].slice(8, 12), [255, 0, 0, 255]);
});
for (const config of [{ epsg: 32650 }, { rotated: true }, { width: 5000, height: 5000 }, { samples: 257 }, { extent: [1, 2, 0, 3] }]) test(`projection/geometry/sample budgets reject before raster decoding: ${JSON.stringify(config)}`, async t => {
  const view = mount(t, config); await view.start(); assert.notEqual(view.state.error.value, ''); assert.equal(view.observations.rasterReads.length, 0); assert.equal(view.observations.urls.length, 0); assert.equal(view.observations.closes, 1);
});
test('invalid band and malformed data fail closed without allocating a layer', async t => {
  const view = mount(t, { read: () => new Float32Array([1]) }); await view.start(); assert.match(view.state.error.value, /长度/);
  view.state.band.value = 99; await flush(); assert.match(view.state.error.value, /编号/); assert.equal(view.observations.rasterReads.length, 1); assert.equal(view.observations.layers.length, 0);
});
test('existing GeoJSON preview remains local and does not initialize raster readers', async t => {
  const view = mount(t, { geojson: JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [110, 30] }, properties: {} }] }) });
  await view.start(); assert.equal(view.state.error.value, ''); assert.equal(view.state.bandCount.value, 0); assert.equal(view.state.busy.value, false);
  assert.equal(view.observations.parsed, 0); assert.equal(view.observations.fits.length, 1); assert.match(view.state.status.value, /1 个要素/);
});
test('raster controls disable re-entry while busy and never fetch external basemaps', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/domains/OpenLayersPreview.vue', import.meta.url), 'utf8');
  assert.match(source, /v-model\.number="band" :disabled="busy"/); assert.match(source, /validateRaster\(image.getWidth\(\), image.getHeight\(\), samples\)/);
  assert.doesNotMatch(source, /https?:\/\/|new OSM|new XYZ|fetch\(/); assert.equal((source.match(/loadPluginBytes\(/g) ?? []).length, 1);
});
