import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import { compileScript, parse } from '@vue/compiler-sfc';
import { cesiumBootstrap, isCesiumBasemap } from '../src/visualizations/extended/domains/cesiumBootstrap.ts';

const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return { promise, resolve, reject }; };
function provider(name = 'natural-earth') {
  const listeners = new Set();
  return { name, listeners, errorEvent: { addEventListener(handler) { listeners.add(handler); return () => listeners.delete(handler); } },
    fail() { for (const handler of [...listeners]) handler({ message: 'private SDK details' }); } };
}
function harness({ natural, gridThrows = false, dataPromise } = {}) {
  const events = new Map(), notifications = [], viewers = [], naturalCalls = [], layers = [], removed = [], loads = [], timers = new Map();
  let nextTimer = 0, destroys = 0, zooms = 0, renders = 0;
  const source = { id: 'same-data-source' }, globe = {}, clock = { currentTime: 'initial-time', shouldAnimate: false };
  const camera = { position: 'initial-camera', heading: 0.1, pitch: 0.2, roll: 0.3 };
  const C = {
    Ellipsoid: { WGS84: { name: 'WGS84' } },
    Color: { fromCssColorString: (value) => value, TRANSPARENT: 'transparent', ORANGE: 'orange', CYAN: { withAlpha: (alpha) => alpha } },
    GeographicTilingScheme: class { constructor(options) { this.options = options; } },
    EllipsoidTerrainProvider: class { constructor(options) { this.options = options; } },
    Credit: class { constructor(text, showOnScreen) { this.text = text; this.showOnScreen = showOnScreen; } },
    GridImageryProvider: class { constructor(options) { if (gridThrows) throw new Error('grid failed'); Object.assign(this, provider('grid'), { options }); } },
    TileMapServiceImageryProvider: { fromUrl(url, options) { naturalCalls.push({ url, options }); return natural ? natural() : Promise.resolve(provider()); } },
    GeoJsonDataSource: { load: async (data, options) => { loads.push({ type: 'geojson', data, options }); return dataPromise ? dataPromise : source; } },
    CzmlDataSource: { load: async (data) => { loads.push({ type: 'czml', data }); return dataPromise ? dataPromise : source; } },
    Viewer: class {
      constructor(element, options) {
        this.element = element; this.options = options; this.destroyed = false; this.camera = camera; this.clock = clock;
        this.scene = { globe, requestRender: () => renders++ };
        this.imageryLayers = {
          addImageryProvider(value, index) { const layer = { provider: value }; layers.splice(index, 0, layer); return layer; },
          remove(layer, destroy) { const index = layers.indexOf(layer); assert.notEqual(index, -1); layers.splice(index, 1); removed.push({ layer, destroy }); },
        };
        this.dataSources = { values: [], add: async (value) => { this.dataSources.values.push(value); } };
        this.zoomTo = async () => { zooms++; camera.position = 'initial-data-extent'; };
        viewers.push(this);
      }
      isDestroyed() { return this.destroyed; }
      destroy() { this.destroyed = true; destroys++; }
    },
  };
  const context = vm.createContext({
    window: { Cesium: C, addEventListener(type, listener) { events.set(type, listener); } },
    document: { getElementById: (id) => ({ id }) },
    notify: (type, payload = {}) => notifications.push({ type, payload }),
    trusted: (event) => event.trusted === true,
    assetRoot: 'http://localhost:7001/nested/visualization-assets/',
    setTimeout: (handler, milliseconds) => { const id = ++nextTimer; timers.set(id, { handler, milliseconds }); return id; },
    clearTimeout: (id) => timers.delete(id),
  });
  vm.runInContext(cesiumBootstrap, context);
  return {
    C, notifications, viewers, naturalCalls, layers, removed, loads, timers, source,
    counters: () => ({ destroys, zooms, renders }),
    send: (type, payload, trusted = true) => events.get('message')({ trusted, data: { type, payload } }),
    close: () => events.get('pagehide')(),
    timeOut: async () => { for (const [id, timer] of [...timers]) { timers.delete(id); timer.handler(); } await settle(); },
    latest: () => notifications.filter((event) => event.type === 'basemap').at(-1)?.payload,
  };
}

test('basemap identifiers are an exact fixed enum, never arbitrary URLs or truthy data', () => {
  for (const mode of ['natural-earth', 'grid', 'none']) assert.equal(isCesiumBasemap(mode), true);
  for (const mode of ['https://example.test/map', 'Natural-earth', '', null, undefined, {}, ['grid'], 1]) assert.equal(isCesiumBasemap(mode), false);
});

for (const czml of [false, true]) test(`default ${czml ? 'CZML' : 'GeoJSON'} viewer loads only bundled Natural Earth with WGS84 and credits`, async () => {
  const h = harness(), data = { tag: 'dataset' };
  assert.deepEqual(h.notifications.map((item) => item.type), ['boot']);
  await h.send('load', { data, czml }); await settle();
  assert.equal(h.viewers.length, 1); assert.equal(h.loads.length, 1); assert.equal(h.loads[0].data, data);
  assert.equal(h.loads[0].type, czml ? 'czml' : 'geojson');
  assert.equal(h.naturalCalls.length, 1);
  const { url, options } = h.naturalCalls[0];
  assert.equal(url, 'http://localhost:7001/nested/visualization-assets/cesium/Assets/Textures/NaturalEarthII/');
  assert.equal(options.maximumLevel, 2); assert.equal(options.minimumLevel, 0); assert.equal(options.fileExtension, 'jpg');
  assert.equal(options.tilingScheme.options.ellipsoid, h.C.Ellipsoid.WGS84);
  assert.match(options.credit.text, /Natural Earth II.*public domain/); assert.equal(options.credit.showOnScreen, true);
  const viewer = h.viewers[0];
  assert.equal(viewer.options.terrainProvider.options.ellipsoid, h.C.Ellipsoid.WGS84);
  for (const key of ['baseLayer', 'baseLayerPicker', 'geocoder', 'skyBox', 'skyAtmosphere']) assert.equal(viewer.options[key], false);
  assert.equal(viewer.options.animation, czml); assert.equal(viewer.options.timeline, czml);
  assert.equal(viewer.dataSources.values[0], h.source); assert.equal(h.counters().zooms, 1);
  assert.equal(h.layers.length, 1); assert.equal(h.layers[0].provider.name, 'natural-earth');
  assert.equal(h.latest().mode, 'natural-earth'); assert.equal(h.latest().status, 'ready');
  assert.equal(h.timers.size, 0); assert.equal(h.notifications.filter((item) => item.type === 'ready').length, 1);
});

for (const czml of [false, true]) test(`switching ${czml ? 'CZML' : 'GeoJSON'} basemaps changes imagery only, not data/camera/clock`, async () => {
  const h = harness(); await h.send('load', { data: {}, czml }); await settle();
  const viewer = h.viewers[0];
  Object.assign(viewer.camera, { position: 'user-camera', heading: 1.1, pitch: -0.5, roll: 0.7 });
  Object.assign(viewer.clock, { currentTime: 'user-time', shouldAnimate: true });
  const camera = { ...viewer.camera }, clock = { ...viewer.clock };
  const dataLayer = viewer.dataSources.values[0], external = { provider: provider('other-data-layer') };
  h.layers.push(external);
  for (const mode of ['none', 'grid', 'natural-earth', 'grid', 'none']) {
    await h.send('basemap', { mode }); await settle();
    assert.equal(h.latest().mode, mode); assert.equal(h.latest().status, 'ready');
    assert.deepEqual(viewer.camera, camera); assert.deepEqual(viewer.clock, clock);
    assert.equal(h.viewers.length, 1); assert.equal(h.loads.length, 1); assert.equal(h.counters().zooms, 1);
    assert.equal(viewer.dataSources.values.length, 1); assert.equal(viewer.dataSources.values[0], dataLayer);
    assert.ok(h.layers.includes(external)); assert.equal(h.layers.length, mode === 'none' ? 1 : 2);
    assert.equal(h.counters().destroys, 0);
  }
  assert.ok(h.removed.every((item) => item.destroy === true && item.layer !== external));
});

test('untrusted messages, invalid mode and repeated load cannot reload or change the viewer', async () => {
  const h = harness(); await h.send('load', { data: {}, czml: false }); await settle();
  const initialCount = h.notifications.length;
  await h.send('load', { data: 'MUST_NOT_RELOAD', czml: true });
  await h.send('basemap', { mode: 'none' }, false);
  for (const mode of [null, undefined, {}, ['grid'], 'https://example.test/tiles']) await h.send('basemap', { mode });
  assert.equal(h.notifications.length, initialCount); assert.equal(h.loads.length, 1); assert.equal(h.layers[0].provider.name, 'natural-earth');
});

test('failed local metadata falls back to offline grid without hiding the data viewer', async () => {
  const h = harness({ natural: () => Promise.reject(new Error('private server URL')) });
  await h.send('load', { data: {}, czml: false }); await settle();
  assert.equal(h.latest().mode, 'grid'); assert.equal(h.latest().fallback, true); assert.equal(h.latest().status, 'ready');
  assert.equal(h.layers.length, 1); assert.equal(h.layers[0].provider.name, 'grid');
  assert.equal(h.layers[0].provider.options.tilingScheme.options.ellipsoid, h.C.Ellipsoid.WGS84);
  assert.equal(h.loads.length, 1); assert.equal(h.counters().zooms, 1); assert.equal(h.counters().destroys, 0);
  assert.ok(h.notifications.some((item) => item.type === 'ready')); assert.ok(!h.notifications.some((item) => item.type === 'error'));
  assert.ok(!JSON.stringify(h.notifications).includes('private server URL')); assert.equal(h.timers.size, 0);
});

test('tile failure falls back once and removes the old provider error listener', async () => {
  const earth = provider(), h = harness({ natural: () => Promise.resolve(earth) });
  await h.send('load', { data: {}, czml: false }); await settle();
  assert.equal(earth.listeners.size, 1); earth.fail(); await settle();
  assert.equal(h.latest().mode, 'grid'); assert.equal(h.latest().fallback, true); assert.equal(earth.listeners.size, 0);
  const count = h.notifications.length; earth.fail(); await settle(); assert.equal(h.notifications.length, count);
  assert.equal(h.counters().zooms, 1); assert.equal(h.counters().destroys, 0);
  await h.send('basemap', { mode: 'none' }); assert.equal(h.latest().fallback, false);
});

test('even a grid construction failure leaves a usable no-basemap data view', async () => {
  const h = harness({ natural: () => Promise.reject(new Error('missing asset')), gridThrows: true });
  await h.send('load', { data: {}, czml: true }); await settle();
  assert.equal(h.latest().mode, 'none'); assert.equal(h.latest().status, 'ready'); assert.equal(h.latest().fallback, true);
  assert.equal(h.layers.length, 0); assert.equal(h.loads.length, 1); assert.equal(h.counters().destroys, 0);
  assert.ok(h.notifications.some((item) => item.type === 'ready'));
});

test('slow initial basemap does not block ready, times out independently and ignores its late result', async () => {
  const pending = deferred(), h = harness({ natural: () => pending.promise });
  await h.send('load', { data: {}, czml: false });
  assert.ok(h.notifications.some((item) => item.type === 'ready')); assert.equal(h.latest().status, 'loading');
  assert.equal(h.timers.size, 1); assert.equal([...h.timers.values()][0].milliseconds, 10000);
  await h.timeOut(); assert.equal(h.latest().mode, 'grid'); assert.equal(h.latest().fallback, true);
  const count = h.notifications.length; pending.resolve(provider('late-earth')); await settle();
  assert.equal(h.notifications.length, count); assert.equal(h.layers[0].provider.name, 'grid');
  assert.equal(h.loads.length, 1); assert.equal(h.counters().zooms, 1); assert.equal(h.timers.size, 0);
});

test('new selection wins over pending basemap initialization and clears its timer', async () => {
  const pending = deferred(), h = harness({ natural: () => pending.promise });
  await h.send('load', { data: {}, czml: false }); await h.send('basemap', { mode: 'none' });
  assert.equal(h.timers.size, 0); assert.equal(h.latest().mode, 'none');
  const count = h.notifications.length; pending.resolve(provider()); await settle();
  assert.equal(h.notifications.length, count); assert.equal(h.layers.length, 0); assert.equal(h.latest().mode, 'none');
});

test('frame teardown destroys the viewer once and discards pending basemap/data completion', async () => {
  const pending = deferred(), data = deferred(), h = harness({ natural: () => pending.promise, dataPromise: data.promise });
  const loading = h.send('load', { data: {}, czml: false }); await settle();
  assert.equal(h.timers.size, 1); h.close(); h.close();
  assert.equal(h.timers.size, 0); assert.equal(h.counters().destroys, 1);
  const count = h.notifications.length; pending.resolve(provider()); data.resolve(h.source); await loading; await settle();
  await h.send('basemap', { mode: 'grid' });
  assert.equal(h.notifications.length, count); assert.equal(h.layers.length, 0); assert.equal(h.counters().zooms, 0);
  assert.equal(h.viewers[0].dataSources.values.length, 0);
});

test('ready-view teardown removes imagery callbacks and never handles further basemap messages', async () => {
  const earth = provider(), h = harness({ natural: () => Promise.resolve(earth) });
  await h.send('load', { data: {}, czml: false }); await settle(); h.close();
  assert.equal(earth.listeners.size, 0); assert.equal(h.layers.length, 0); assert.equal(h.counters().destroys, 1);
  const count = h.notifications.length; earth.fail(); await h.send('basemap', { mode: 'grid' });
  assert.equal(h.notifications.length, count);
});

test('component exposes three bounded selections with attribution, failure notice and no remount watcher', () => {
  const filename = 'CesiumPreview.vue';
  const source = readFileSync(new URL('../src/visualizations/extended/domains/CesiumPreview.vue', import.meta.url), 'utf8');
  const { descriptor, errors } = parse(source, { filename }); assert.deepEqual(errors, []);
  assert.ok(compileScript(descriptor, { id: 'cesium-basemap' }).content);
  assert.match(source, /data-testid="cesium-basemap-select"/); assert.match(source, /data-testid="cesium-basemap-warning"/);
  assert.match(source, /Natural Earth II.*公共领域.*离线全球概览/);
  assert.match(source, /不含街道路网/); assert.match(source, /参考网格不代表固定经纬度间隔/);
  for (const mode of ['natural-earth', 'grid', 'none']) assert.ok(source.includes(`value="${mode}"`));
  assert.equal((source.match(/loadPluginBytes\(/g) || []).length, 1);
  assert.match(source, /child\?\.post\('basemap', \{ mode: basemap.value \}\)/);
  assert.doesNotMatch(source, /watch\(|:key=|localStorage|sessionStorage|https?:\/\//);
  assert.doesNotMatch(cesiumBootstrap, /Ion\.|createWorldTerrain|https?:\/\/|imageryLayers\.removeAll/);
  const frame = readFileSync(new URL('../src/visualizations/extended/domains/frame.ts', import.meta.url), 'utf8');
  assert.match(frame, /connect-src 'self' data: blob:/); assert.match(frame, /img-src 'self' data: blob:/);
});
