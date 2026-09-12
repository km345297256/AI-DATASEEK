import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';

const extensions = ['shp', 'dbf', 'prj', 'shx', 'cpg'];
const encode = value => new TextEncoder().encode(value).buffer;
const clone = value => JSON.parse(JSON.stringify(value));
const shapefilePlugin = { ...JSON.parse(readFileSync(new URL('../../plugin-host/visualizations/shapefile.json', import.meta.url), 'utf8')), enabled: true };
const flush = async () => { for (let index = 0; index < 24; index++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

// Synthetic on-disk format bytes, consumed by the real SFC parsers. No user
// datasets, map servers, parser stubs, or external downloads are involved.
function pointShp(x, y) {
  const buffer = new ArrayBuffer(128);
  const view = new DataView(buffer);
  view.setInt32(0, 9994, false);
  view.setInt32(24, 64, false);
  view.setInt32(28, 1000, true);
  view.setInt32(32, 1, true);
  for (const [offset, value] of [[36, x], [44, y], [52, x], [60, y]]) view.setFloat64(offset, value, true);
  view.setInt32(100, 1, false);
  view.setInt32(104, 10, false);
  view.setInt32(108, 1, true);
  view.setFloat64(112, x, true);
  view.setFloat64(120, y, true);
  return buffer;
}

function oneRowDbf(label) {
  const bytes = new Uint8Array(79);
  const view = new DataView(bytes.buffer);
  bytes[0] = 3;
  view.setUint32(4, 1, true);
  view.setUint16(8, 65, true);
  view.setUint16(10, 13, true);
  bytes.set(new TextEncoder().encode('NAME'), 32);
  bytes[43] = 'C'.charCodeAt(0);
  bytes[48] = 12;
  bytes[64] = 0x0d;
  bytes.fill(0x20, 65, 78);
  bytes.set(new TextEncoder().encode(label), 66);
  bytes[78] = 0x1a;
  return bytes.buffer;
}

function recordShp(points) {
  const lengths = points.map(point => point === null ? 4 : 20);
  const buffer = new ArrayBuffer(100 + lengths.reduce((sum, length) => sum + 8 + length, 0));
  const view = new DataView(buffer);
  view.setInt32(0, 9994, false);
  view.setInt32(24, buffer.byteLength / 2, false);
  view.setInt32(28, 1000, true);
  view.setInt32(32, 1, true);
  let offset = 100;
  points.forEach((point, index) => {
    view.setInt32(offset, index + 1, false);
    view.setInt32(offset + 4, lengths[index] / 2, false);
    view.setInt32(offset + 8, point === null ? 0 : 1, true);
    if (point) {
      view.setFloat64(offset + 12, point[0], true);
      view.setFloat64(offset + 20, point[1], true);
    }
    offset += 8 + lengths[index];
  });
  return buffer;
}

function polygonShp(rings) {
  const points = rings.flat();
  const contentLength = 44 + rings.length * 4 + points.length * 16;
  const buffer = new ArrayBuffer(108 + contentLength);
  const view = new DataView(buffer);
  view.setInt32(0, 9994, false);
  view.setInt32(24, buffer.byteLength / 2, false);
  view.setInt32(28, 1000, true);
  view.setInt32(32, 5, true);
  view.setInt32(100, 1, false);
  view.setInt32(104, contentLength / 2, false);
  view.setInt32(108, 5, true);
  view.setInt32(144, rings.length, true);
  view.setInt32(148, points.length, true);
  let pointIndex = 0;
  rings.forEach((ring, index) => {
    view.setInt32(152 + index * 4, pointIndex, true);
    pointIndex += ring.length;
  });
  const pointOffset = 152 + rings.length * 4;
  points.forEach(([x, y], index) => {
    view.setFloat64(pointOffset + index * 16, x, true);
    view.setFloat64(pointOffset + index * 16 + 8, y, true);
  });
  return buffer;
}

function recordDbf(rows, fields = ['OBJECTID'], deleted = []) {
  const fieldLength = 9;
  const headerLength = 33 + fields.length * 32;
  const recordLength = 1 + fields.length * fieldLength;
  const bytes = new Uint8Array(headerLength + rows.length * recordLength + 1);
  const view = new DataView(bytes.buffer);
  bytes[0] = 3;
  view.setUint32(4, rows.length, true);
  view.setUint16(8, headerLength, true);
  view.setUint16(10, recordLength, true);
  fields.forEach((field, index) => {
    const offset = 32 + index * 32;
    bytes.set(new TextEncoder().encode(field), offset);
    bytes[offset + 11] = 'N'.charCodeAt(0);
    bytes[offset + 16] = fieldLength;
  });
  bytes[headerLength - 1] = 0x0d;
  rows.forEach((row, index) => {
    const offset = headerLength + index * recordLength;
    bytes.fill(0x20, offset, offset + recordLength);
    if (deleted.includes(index)) bytes[offset] = 0x2a;
    fields.forEach((field, fieldIndex) => {
      const text = String(row[field] ?? '').padStart(fieldLength);
      bytes.set(new TextEncoder().encode(text), offset + 1 + fieldIndex * fieldLength);
    });
  });
  bytes[bytes.length - 1] = 0x1a;
  return bytes.buffer;
}

// A screen CTM with both padding and letterboxing. The actual SFC must invert
// it; mapping against the element's CSS rectangle is deliberately incorrect.
function svgSurface(state, { width = 1000, height = 500, left = 17, top = 29 } = {}) {
  const [x, y, vw, vh] = state.viewBox.value.split(/\s+/).map(Number);
  const scale = Math.min(width / vw, height / vh);
  const tx = left + (width - vw * scale) / 2 - x * scale;
  const ty = top + (height - vh * scale) / 2 - y * scale;
  const captured = new Set();
  const surface = {
    getBoundingClientRect: () => ({ left, top, width, height }),
    getScreenCTM: () => ({ inverse: () => ({ a: 1 / scale, b: 0, c: 0, d: 1 / scale, e: -tx / scale, f: -ty / scale }) }),
    createSVGPoint: () => ({ x: 0, y: 0, matrixTransform(matrix) {
      return { x: matrix.a * this.x + matrix.c * this.y + matrix.e, y: matrix.b * this.x + matrix.d * this.y + matrix.f };
    } }),
    setPointerCapture: id => captured.add(id),
    hasPointerCapture: id => captured.has(id),
    releasePointerCapture: id => captured.delete(id),
    focus() {},
    querySelector: () => null,
  };
  const pointer = (point, overrides = {}) => ({
    currentTarget: surface, target: surface, pointerId: 7, button: 0, isPrimary: true,
    clientX: tx + point[0] * scale, clientY: ty - point[1] * scale,
    preventDefault() {}, stopPropagation() {}, ...overrides,
  });
  return { surface, pointer, captured };
}

function group(id, { directory = 'layer', stem = 'roads', x = 1200, y = 1300 } = {}) {
  const files = Object.fromEntries(extensions.map(ext => [ext, {
    file_id: `${id}-${ext}`, filename: `${stem}.${ext}`, upload_date: '',
    metadata: { logical_path: `${directory}/${stem}.${ext}` },
  }]));
  const projection = `LOCAL_CS["Synthetic ${id}",UNIT["metre",1]]`;
  const bytes = new Map([
    [files.shp.file_id, pointShp(x, y)], [files.dbf.file_id, oneRowDbf(id)],
    [files.prj.file_id, encode(projection)], [files.shx.file_id, new ArrayBuffer(100)],
    [files.cpg.file_id, encode('UTF-8')],
  ]);
  return { files, bytes, projection, point: [x, y], key: `${directory}/${stem}` };
}

const source = readFileSync(new URL('../src/components/filePreviews/ShapefilePreview.vue', import.meta.url), 'utf8');
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'shapefile-preview-test' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;

function mount(t, selected, groups, { omit = [], request, plugin = clone(shapefilePlugin) } = {}) {
  const bytes = new Map(groups.flatMap(item => [...item.bytes]));
  const relatedFiles = vue.ref(groups.flatMap(item => Object.values(item.files)).filter(file => !omit.includes(file.file_id)));
  const calls = [];
  const beforeUnmount = [];
  const dependencies = {
    // The no-DOM setup harness owns its lifecycle, but executes the component's
    // actual cleanup callbacks rather than dropping or merely silencing them.
    vue: { ...vue, onBeforeUnmount: callback => beforeUnmount.push(callback) },
    '../../composables/usePreviewLoad': { usePreviewLoad },
    '../../composables/useFilePanel': { useFilePanel: () => ({ relatedFiles }) },
    '../../visualizations/runtime': { loadPluginBytes: async (sourceFile, plugin, signal, resource) => {
      const target = resource || sourceFile;
      const call = { source: sourceFile, plugin, signal, resource, target };
      calls.push(call);
      // Intentionally ignore AbortSignal in this boundary: the component must
      // reject stale results even when a remote response ignores cancellation.
      if (request) {
        const value = request(call, bytes);
        if (value !== undefined) return value;
      }
      assert.ok(bytes.has(target.file_id), `Unexpected synthetic resource ${target.file_id}`);
      return bytes.get(target.file_id).slice(0);
    } },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(name => {
    if (name === './shapefileSelection' || name === '../../visualizations/previewIdentity') {
      const helper = { exports: {} };
      const helperFile = name === './shapefileSelection'
        ? '../src/components/filePreviews/shapefileSelection.ts' : '../src/visualizations/previewIdentity.ts';
      const helperSource = readFileSync(new URL(helperFile, import.meta.url), 'utf8');
      const helperCode = ts.transpileModule(helperSource,
        { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
      new Function('module', 'exports', helperCode)(helper, helper.exports);
      return helper.exports;
    }
    assert.ok(name in dependencies, `Unexpected Shapefile dependency ${name}`);
    return dependencies[name];
  }, module, module.exports);
  const props = vue.reactive({ file: selected, plugin });
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  let disposed = false;
  const unmount = () => {
    if (disposed) return;
    disposed = true;
    beforeUnmount.forEach(callback => callback());
    scope.stop();
  };
  t.after(unmount);
  return { state, props, calls, relatedFiles, unmount,
    async select(file) { props.file = file; await vue.nextTick(); await flush(); } };
}

function visibleGeometry(preview, expected) {
  assert.equal(preview.state.status.value, '');
  assert.deepEqual(preview.state.geometries.value, [{ type: 'Point', coordinates: expected.point }]);
  assert.ok(preview.state.viewBox.value);
  assert.deepEqual(preview.state.basemapTiles.value, []); // LOCAL_CS never asks OSM for tiles.
}

function snapshot(preview) {
  return JSON.stringify(Object.fromEntries(['status', 'warnings', 'geometries', 'attributes',
    'projectionText', 'bounds', 'viewBounds', 'selectedLayerKey'].map(name => [name, preview.state[name].value])));
}

for (const extension of extensions) {
  test(`${extension} entry loads the same SHP anchor and its exact DBF/PRJ sidecars`, async t => {
    const layer = group('alpha');
    const preview = mount(t, layer.files[extension], [layer]);
    await flush();
    visibleGeometry(preview, layer);
    assert.deepEqual(preview.state.attributes.value, [{ NAME: 'alpha' }]);
    assert.equal(preview.state.projectionText.value, layer.projection);
    assert.deepEqual(preview.state.warnings.value, []);
    assert.deepEqual(preview.calls.map(call => call.target.file_id), ['alpha-shp', 'alpha-dbf', 'alpha-prj']);
    assert.ok(preview.calls.every(call => call.source.file_id === 'alpha-shp'));
    assert.equal(preview.calls[0].resource, undefined);
    assert.equal(preview.calls[1].resource.file_id, 'alpha-dbf');
    assert.equal(preview.calls[2].resource.file_id, 'alpha-prj');
  });
}

for (const mode of ['read', 'parse']) {
  test(`DBF ${mode} failure keeps geometry and still independently reads PRJ`, async t => {
    const layer = group('alpha');
    const preview = mount(t, layer.files.shp, [layer], { request: call => {
      if (call.target.file_id !== layer.files.dbf.file_id) return undefined;
      if (mode === 'read') throw new Error('Synthetic DBF transfer failure');
      return new ArrayBuffer(4);
    } });
    await flush();
    visibleGeometry(preview, layer);
    assert.deepEqual(preview.state.attributes.value, []);
    assert.match(preview.state.warnings.value.join(' '), /DBF/);
    assert.equal(preview.state.projectionText.value, layer.projection);
    assert.equal(preview.calls.at(-1).target.file_id, layer.files.prj.file_id);
  });
}

for (const mode of ['read', 'empty', 'invalid-encoding']) {
  test(`PRJ ${mode} failure keeps both geometry and DBF attributes`, async t => {
    const layer = group('alpha');
    const preview = mount(t, layer.files.prj, [layer], { request: call => {
      if (call.target.file_id !== layer.files.prj.file_id) return undefined;
      if (mode === 'read') throw new Error('Synthetic PRJ transfer failure');
      return mode === 'empty' ? new ArrayBuffer(0) : new Uint8Array([0xff, 0xfe]).buffer;
    } });
    await flush();
    visibleGeometry(preview, layer);
    assert.deepEqual(preview.state.attributes.value, [{ NAME: 'alpha' }]);
    assert.equal(preview.state.projectionText.value, '');
    assert.match(preview.state.warnings.value.join(' '), /PRJ/);
  });
}

for (const missing of [['dbf'], ['prj'], ['dbf', 'prj']]) {
  test(`missing ${missing.join('/')} is a warning, not a geometry blocker`, async t => {
    const layer = group('alpha');
    const preview = mount(t, layer.files.shp, [layer], { omit: missing.map(ext => layer.files[ext].file_id) });
    await flush();
    visibleGeometry(preview, layer);
    for (const extension of missing) assert.match(preview.state.warnings.value.join(' '), new RegExp(`缺少同名 \\.${extension}`));
    assert.equal(preview.state.warnings.value.length, missing.length);
    assert.equal(preview.state.attributes.value.length, missing.includes('dbf') ? 0 : 1);
  });
}

for (const other of [{ directory: 'other', stem: 'roads' }, { directory: 'layer', stem: 'Roads' }]) {
  test(`full stem identity separates ${other.directory}/${other.stem} from layer/roads`, async t => {
    const alpha = group('alpha');
    const beta = group('beta', { ...other, x: 2400, y: 2600 });
    const preview = mount(t, alpha.files.prj, [beta, alpha]);
    await flush();
    visibleGeometry(preview, alpha);
    assert.equal(preview.state.layerChoices.value.length, 2);
    assert.ok(preview.calls.every(call => call.target.file_id.startsWith('alpha-')));
    await preview.select(beta.files.cpg);
    visibleGeometry(preview, beta);
    assert.equal(preview.state.selectedLayerKey.value, beta.key);
    assert.equal(preview.state.activeFile.value.file_id, beta.files.shp.file_id);
    assert.deepEqual(preview.state.attributes.value, [{ NAME: 'beta' }]);
    assert.ok(preview.calls.slice(3).every(call => call.source.file_id === beta.files.shp.file_id));
  });
}

test('selecting a file resets a manually selected layer instead of retaining the previous group', async t => {
  const alpha = group('alpha');
  const beta = group('beta', { directory: 'other', x: 2400, y: 2600 });
  const preview = mount(t, alpha.files.shp, [alpha, beta]);
  await flush();
  preview.state.selectedLayerKey.value = beta.key;
  await vue.nextTick();
  await flush();
  visibleGeometry(preview, beta);
  await preview.select(alpha.files.prj);
  visibleGeometry(preview, alpha);
  assert.equal(preview.state.selectedLayerKey.value, alpha.key);
});

for (const stage of ['shp', 'dbf', 'prj']) {
  for (const settle of ['success', 'failure']) {
    test(`file switch ignores obsolete ${stage} ${settle} and never mixes layer resources`, async t => {
      const alpha = group('alpha');
      const beta = group('beta', { directory: 'other', x: 2400, y: 2600 });
      const old = deferred();
      const preview = mount(t, alpha.files.prj, [alpha, beta], {
        request: call => call.target.file_id === alpha.files[stage].file_id ? old.promise : undefined,
      });
      await flush();
      const pending = preview.calls.find(call => call.target.file_id === alpha.files[stage].file_id);
      assert.ok(pending);
      await preview.select(beta.files.dbf);
      visibleGeometry(preview, beta);
      const current = snapshot(preview);
      if (settle === 'success') old.resolve(alpha.bytes.get(alpha.files[stage].file_id));
      else old.reject(new Error('Late obsolete response'));
      await flush();
      assert.equal(pending.signal.aborted, true);
      assert.equal(snapshot(preview), current);
      assert.ok(preview.calls.every(call => call.target.file_id.startsWith(call.source.file_id.split('-')[0] + '-')));
      if (stage !== 'prj') assert.ok(!preview.calls.some(call => call.target.file_id === alpha.files.prj.file_id));
    });

    test(`unmount ignores late ${stage} ${settle} without adding errors or starting sidecars`, async t => {
      const layer = group('alpha');
      const old = deferred();
      const preview = mount(t, layer.files.shp, [layer], {
        request: call => call.target.file_id === layer.files[stage].file_id ? old.promise : undefined,
      });
      await flush();
      const before = snapshot(preview);
      const count = preview.calls.length;
      const pending = preview.calls.at(-1);
      preview.unmount();
      if (settle === 'success') old.resolve(layer.bytes.get(layer.files[stage].file_id));
      else old.reject(new Error('Late disposed response'));
      await flush();
      assert.equal(pending.signal.aborted, true);
      assert.equal(snapshot(preview), before);
      assert.equal(preview.calls.length, count);
    });
  }
}

for (const mode of ['read', 'parse']) {
  test(`SHP ${mode} failure remains blocking and does not request sidecars`, async t => {
    const layer = group('alpha');
    const preview = mount(t, layer.files.shp, [layer], { request: () => {
      if (mode === 'read') throw new Error('Synthetic geometry failure');
      return new ArrayBuffer(4);
    } });
    await flush();
    assert.match(preview.state.status.value, /SHP 几何文件/);
    assert.deepEqual(preview.state.geometries.value, []);
    assert.equal(preview.calls.length, 1);
  });
}

test('a sidecar without its same-group SHP shows the missing-geometry message', async t => {
  const layer = group('alpha');
  const preview = mount(t, layer.files.prj, [layer], { omit: [layer.files.shp.file_id] });
  await flush();
  assert.match(preview.state.status.value, /未找到同名 \.shp/);
  assert.deepEqual(preview.state.geometries.value, []);
  assert.equal(preview.calls.length, 0);
});

async function selectable(t, points, { fields = ['OBJECTID'], deleted = [] } = {}) {
  const layer = group('selection');
  layer.bytes.set(layer.files.shp.file_id, recordShp(points));
  const rows = points.map((_, index) => Object.fromEntries(fields.map((field, fieldIndex) => [field, index + 1 + fieldIndex])));
  layer.bytes.set(layer.files.dbf.file_id, recordDbf(rows, fields, deleted));
  const preview = mount(t, layer.files.shp, [layer]);
  await flush();
  assert.equal(preview.state.status.value, '');
  assert.deepEqual(preview.state.basemapTiles.value, []);
  return { ...preview, layer, rows };
}

test('screen CTM inverse maps padded letterboxed SVG pixels to the actual data point', async t => {
  const { state } = await selectable(t, [[20, 80]]);
  state.viewBounds.value = [0, 0, 100, 100];
  const { pointer } = svgSurface(state);
  const actual = state.svgDataPoint(pointer([20, 80]));
  assert.ok(Math.abs(actual[0] - 20) < 1e-10);
  assert.ok(Math.abs(actual[1] - 80) < 1e-10);
  state.selectionMode.value = true;
  state.startSelection(pointer([19, 79]));
  state.finishSelection(pointer([21, 81]));
  assert.deepEqual([...state.selectedIndices.value], [0]);
});

test('unavailable or singular screen transforms cannot start a data-space selection', async t => {
  const { state } = await selectable(t, [[2, 2], [8, 8]]);
  const { pointer, surface } = svgSurface(state);
  surface.getScreenCTM = () => null;
  assert.equal(state.svgDataPoint(pointer([2, 2])), null);
  surface.getScreenCTM = () => ({ inverse() { throw new Error('Singular matrix'); } });
  assert.equal(state.svgDataPoint(pointer([2, 2])), null);
});

test('selection rectangle uses a one-pixel non-scaling stroke, not one geographic unit', () => {
  const rect = source.match(/<rect\b[^>]*data-testid="selection-rectangle"[^>]*\/>/s)?.[0];
  assert.ok(rect, 'The actual SFC must identify its selection rectangle');
  assert.match(rect, /vector-effect="non-scaling-stroke"/);
  assert.match(rect, /stroke-width="1"/);
  assert.match(rect, /fill-opacity="0\.14"/);
});

test('an empty region inside a line bounding box does not select the line', async t => {
  const { state } = await selectable(t, [[0, 0]]);
  state.geometries.value = [{ type: 'LineString', coordinates: [[0, 0], [0, 10], [10, 10]] }];
  state.viewBounds.value = [0, 0, 10, 10];
  state.selectionMode.value = true;
  const { pointer } = svgSurface(state);
  state.startSelection(pointer([4, 4]));
  state.finishSelection(pointer([6, 6]));
  assert.equal(state.selectedIndices.value.size, 0);
  assert.equal(state.previewRows.value.length, 0);
});

test('box selection retains every exact hit and an empty box clears previous hits', async t => {
  const { state } = await selectable(t, [[2, 2], [8, 8], [20, 20]]);
  state.viewBounds.value = [0, 0, 25, 25];
  state.selectionMode.value = true;
  const { pointer } = svgSurface(state);
  state.startSelection(pointer([1, 1]));
  state.finishSelection(pointer([9, 9]));
  assert.deepEqual([...state.selectedIndices.value], [0, 1]);
  assert.equal(state.tableFilter.value, 'selected');
  assert.deepEqual(state.previewRows.value.map(row => [row.recordIndex, row.values.OBJECTID]), [[0, 1], [1, 2]]);
  state.startSelection(pointer([11, 11]));
  state.finishSelection(pointer([14, 14]));
  assert.equal(state.selectedIndices.value.size, 0);
  assert.deepEqual(state.previewRows.value, []);
});

test('a selection captures its pointer and ignores another pointer before releasing', async t => {
  const { state } = await selectable(t, [[2, 2], [8, 8]]);
  state.selectionMode.value = true;
  const { pointer, captured } = svgSurface(state);
  state.startSelection(pointer([1, 1]));
  assert.equal(captured.has(7), true);
  state.moveSelection(pointer([9, 9], { pointerId: 19 }));
  assert.equal(state.selectionRect.value, null);
  state.finishSelection(pointer([9, 9], { pointerId: 19 }));
  assert.equal(captured.has(7), true);
  state.finishSelection(pointer([9, 9]));
  assert.equal(captured.has(7), false);
  assert.deepEqual([...state.selectedIndices.value], [0, 1]);
});

test('less than three screen pixels of pointer motion is not a replacement box selection', async t => {
  const { state } = await selectable(t, [[2, 2], [8, 8]]);
  state.selectFeature(1);
  state.selectionMode.value = true;
  const { pointer, captured } = svgSurface(state);
  const start = pointer([2, 2]);
  state.startSelection(start);
  state.finishSelection({ ...start, clientX: start.clientX + 1, clientY: start.clientY + 1 });
  assert.deepEqual([...state.selectedIndices.value], [1]);
  assert.equal(state.selectionRect.value, null);
  assert.equal(captured.size, 0);
});

test('pointer cancellation and leaving selection mode remove the rectangle but keep completed selection', async t => {
  const { state } = await selectable(t, [[2, 2], [8, 8]]);
  state.selectionMode.value = true;
  const { pointer, captured } = svgSurface(state);
  state.startSelection(pointer([1, 1]));
  state.finishSelection(pointer([9, 9]));
  assert.ok(state.selectionRect.value);
  state.toggleSelectionMode();
  assert.equal(state.selectionMode.value, false);
  assert.equal(state.selectionRect.value, null);
  assert.deepEqual([...state.selectedIndices.value], [0, 1]);
  state.toggleSelectionMode();
  state.startSelection(pointer([1, 1]));
  state.moveSelection(pointer([4, 4]));
  assert.ok(state.selectionRect.value);
  state.cancelSelection();
  assert.equal(state.selectionRect.value, null);
  assert.equal(state.selectionStart.value, null);
  assert.equal(captured.size, 0);
  assert.deepEqual([...state.selectedIndices.value], [0, 1]);
});

test('clear selection restores the complete table and its first page', async t => {
  const { state } = await selectable(t, Array.from({ length: 125 }, (_, index) => [index, index]));
  state.selectFeature(120);
  assert.equal(state.tablePage.value, 2);
  state.selectionRect.value = [0, 0, 1, 1];
  state.clearSelection();
  assert.equal(state.selectionRect.value, null);
  assert.equal(state.selectedIndices.value.size, 0);
  assert.equal(state.tableFilter.value, 'all');
  assert.equal(state.tablePage.value, 1);
  assert.equal(state.previewRows.value.length, 100);
});

test('changing layer clears in-progress and completed selection state', async t => {
  const alpha = group('alpha');
  const beta = group('beta', { directory: 'other', x: 2400, y: 2600 });
  const preview = mount(t, alpha.files.shp, [alpha, beta]);
  await flush();
  const { state } = preview;
  state.selectedIndices.value = new Set([0]);
  state.selectionMode.value = true;
  state.selectionStart.value = [1, 1];
  state.selectionRect.value = [1, 1, 2, 2];
  await preview.select(beta.files.shp);
  assert.equal(state.selectionRect.value, null);
  assert.equal(state.selectionStart.value, null);
  assert.equal(state.selectedIndices.value.size, 0);
  assert.equal(state.tableFilter.value, 'all');
  assert.equal(state.tablePage.value, 1);
});

test('map selection beyond row 100 locates the matching attribute page without truncating records', async t => {
  const { state } = await selectable(t, Array.from({ length: 125 }, (_, index) => [index, index]));
  assert.equal(state.pageCount.value, 2);
  assert.equal(state.filteredRows.value.length, 125);
  assert.equal(state.previewRows.value.length, 100);
  state.selectFeature(120);
  await vue.nextTick();
  assert.deepEqual([...state.selectedIndices.value], [120]);
  assert.equal(state.tablePage.value, 2);
  assert.equal(state.previewRows.value.length, 25);
  assert.equal(state.previewRows.value.find(row => row.recordIndex === 120)?.values.OBJECTID, 121);
  state.selectRow(124);
  assert.deepEqual([...state.selectedIndices.value], [124]);
  state.selectRow(123, { ctrlKey: true });
  assert.deepEqual([...state.selectedIndices.value].sort((a, b) => a - b), [123, 124]);
});

test('attribute columns come from the complete DBF schema including more than twelve fields', async t => {
  const fields = Array.from({ length: 15 }, (_, index) => `FIELD${index + 1}`);
  const { state } = await selectable(t, [[1, 1]], { fields });
  assert.deepEqual(state.fields.value, fields);
  assert.deepEqual(Object.keys(state.previewRows.value[0].values), fields);
});

test('null SHP records preserve the DBF record identity of subsequent geometry', async t => {
  const { state } = await selectable(t, [null, [10, 20]]);
  assert.equal(state.geometries.value.length, 2);
  assert.equal(state.geometries.value[0], null);
  assert.deepEqual(state.renderFeatures.value.map(feature => feature.recordIndex), [1]);
  state.selectFeature(1);
  assert.deepEqual([...state.selectedIndices.value], [1]);
  assert.equal(state.previewRows.value.find(row => row.recordIndex === 1)?.values.OBJECTID, 2);
});

test('deleted DBF records remain placeholders and never shift or display another feature properties', async t => {
  const { state } = await selectable(t, [[1, 2], [10, 20]], { deleted: [0] });
  assert.equal(state.attributes.value.length, 2);
  assert.equal(state.attributes.value[0], null);
  assert.deepEqual(state.renderFeatures.value.map(feature => feature.recordIndex), [1]);
  assert.deepEqual(state.filteredRows.value.map(row => [row.recordIndex, row.values.OBJECTID]), [[1, 2]]);
  state.selectFeature(1);
  assert.deepEqual([...state.selectedIndices.value], [1]);
  assert.equal(state.previewRows.value[0].values.OBJECTID, 2);
});

for (const rowCount of [1, 3]) {
  test(`a ${rowCount}-row DBF paired with two SHP records cannot create misleading attribute associations`, async t => {
    const layer = group('mismatch');
    layer.bytes.set(layer.files.shp.file_id, recordShp([[1, 2], [10, 20]]));
    layer.bytes.set(layer.files.dbf.file_id, recordDbf(Array.from({ length: rowCount }, (_, index) => ({ OBJECTID: index + 1 }))));
    const { state, calls } = mount(t, layer.files.shp, [layer]);
    await flush();
    assert.equal(state.status.value, '');
    assert.equal(state.renderFeatures.value.length, 2);
    assert.deepEqual(state.attributes.value, []);
    assert.deepEqual(state.filteredRows.value, []);
    assert.match(state.warnings.value.join(' '), /DBF.*记录对应/);
    assert.equal(state.projectionText.value, layer.projection);
    assert.equal(calls.at(-1).target.file_id, layer.files.prj.file_id);
    state.selectFeature(1);
    assert.deepEqual([...state.selectedIndices.value], [1]);
    assert.equal(state.selectedAttributeCount.value, 0);
  });
}

test('unmount releases an active pointer capture and discards the unfinished rectangle', async t => {
  const preview = await selectable(t, [[2, 2], [8, 8]]);
  const { state } = preview;
  state.selectionMode.value = true;
  const { pointer, captured } = svgSurface(state);
  state.startSelection(pointer([1, 1]));
  state.moveSelection(pointer([9, 9]));
  assert.equal(captured.has(7), true);
  assert.ok(state.selectionRect.value);
  preview.unmount();
  assert.equal(captured.size, 0);
  assert.equal(state.selectionRect.value, null);
  assert.equal(state.selectionStart.value, null);
});

test('real SHP polygon records connect unordered exterior rings and holes to exact feature selection', async t => {
  const outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]];
  const hole = [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]];
  const otherOuter = [[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]];
  const layer = group('polygon');
  layer.bytes.set(layer.files.shp.file_id, polygonShp([hole, otherOuter, outer]));
  layer.bytes.set(layer.files.dbf.file_id, recordDbf([{ OBJECTID: 1 }]));
  const { state } = mount(t, layer.files.shp, [layer]);
  await flush();
  assert.equal(state.status.value, '');
  assert.equal(state.geometries.value.length, 1);
  assert.equal(state.geometries.value[0].type, 'MultiPolygon');
  assert.deepEqual(state.geometries.value[0].coordinates, [[otherOuter], [outer, hole]]);
  state.selectionMode.value = true;
  const { pointer } = svgSurface(state);
  for (const [box, expected] of [
    [[4, 4, 6, 6], []], // The polygon's hole is not an actual hit.
    [[1, 1, 2, 2], [0]],
    [[21, 1, 22, 2], [0]], // The second exterior belongs to the same record.
    [[12, 1, 18, 2], []], // The empty gap is inside only the aggregate bbox.
  ]) {
    state.startSelection(pointer([box[0], box[1]]));
    state.finishSelection(pointer([box[2], box[3]]));
    assert.deepEqual([...state.selectedIndices.value], expected, `Incorrect hit for ${JSON.stringify(box)}`);
  }
});

test('real SHP polygon parser preserves an island inside a hole as a separate exterior of one record', async t => {
  const outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]];
  const hole = [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]];
  const island = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]];
  const layer = group('island');
  layer.bytes.set(layer.files.shp.file_id, polygonShp([island, hole, outer]));
  const { state } = mount(t, layer.files.shp, [layer]);
  await flush();
  assert.equal(state.status.value, '');
  assert.deepEqual(state.geometries.value[0], { type: 'MultiPolygon', coordinates: [[island], [outer, hole]] });
  state.selectionMode.value = true;
  const { pointer } = svgSurface(state);
  state.startSelection(pointer([4.5, 4.5]));
  state.finishSelection(pointer([5.5, 5.5]));
  assert.deepEqual([...state.selectedIndices.value], [0]);
  state.startSelection(pointer([2.5, 2.5]));
  state.finishSelection(pointer([3.5, 3.5]));
  assert.equal(state.selectedIndices.value.size, 0);
});

test('a feature selected before its DBF arrives is located on the correct page when attributes finish loading', async t => {
  const layer = group('late-dbf');
  layer.bytes.set(layer.files.shp.file_id, recordShp(Array.from({ length: 125 }, (_, index) => [index, index])));
  layer.bytes.set(layer.files.dbf.file_id, recordDbf(Array.from({ length: 125 }, (_, index) => ({ OBJECTID: index + 1 }))));
  const pending = deferred();
  const { state } = mount(t, layer.files.shp, [layer], {
    request: call => call.target.file_id === layer.files.dbf.file_id ? pending.promise : undefined,
  });
  await flush();
  assert.equal(state.renderFeatures.value.length, 125);
  assert.deepEqual(state.attributes.value, []);
  state.selectFeature(120);
  assert.deepEqual([...state.selectedIndices.value], [120]);
  pending.resolve(layer.bytes.get(layer.files.dbf.file_id));
  await flush();
  assert.equal(state.tablePage.value, 2);
  assert.deepEqual([...state.selectedIndices.value], [120]);
  assert.equal(state.previewRows.value.find(row => row.recordIndex === 120)?.values.OBJECTID, 121);
});

test('locating one point scales marker and line width to the current view, not the entire dataset', async t => {
  const { state } = await selectable(t, [[0, 0], [1000000, 1000000]]);
  assert.equal(state.pointRadius.value, 4000);
  state.selectRow(0);
  assert.deepEqual(state.bounds.value, [0, 0, 1000000, 1000000]);
  assert.deepEqual(state.viewBounds.value, [-0.5, -0.5, 0.5, 0.5]);
  assert.equal(state.pointRadius.value, 0.004);
  assert.equal(state.strokeWidth.value, 0.0024);
  state.zoom(0.5);
  assert.equal(state.pointRadius.value, 0.002);
  assert.equal(state.strokeWidth.value, 0.0012);
});

for (const userZoomed of [false, true]) {
  test(`late deleted-record discovery excludes its extent and ${userZoomed ? 'preserves user zoom' : 'updates the untouched initial view'}`, async t => {
    const layer = group('extents');
    layer.bytes.set(layer.files.shp.file_id, recordShp([[-1000000, -1000000], [10, 20], [11, 21]]));
    layer.bytes.set(layer.files.dbf.file_id, recordDbf([{ OBJECTID: 1 }, { OBJECTID: 2 }, { OBJECTID: 3 }], ['OBJECTID'], [0]));
    const pending = deferred();
    const { state } = mount(t, layer.files.shp, [layer], {
      request: call => call.target.file_id === layer.files.dbf.file_id ? pending.promise : undefined,
    });
    await flush();
    assert.deepEqual(state.bounds.value, [-1000000, -1000000, 11, 21]);
    state.selectFeature(0);
    if (userZoomed) state.zoom(0.5);
    const beforeAttributes = [...state.viewBounds.value];
    pending.resolve(layer.bytes.get(layer.files.dbf.file_id));
    await flush();
    assert.deepEqual(state.renderFeatures.value.map(feature => feature.recordIndex), [1, 2]);
    assert.deepEqual(state.bounds.value, [10, 20, 11, 21]);
    assert.deepEqual(state.viewBounds.value, userZoomed ? beforeAttributes : [10, 20, 11, 21]);
    assert.equal(state.selectedIndices.value.size, 0, 'A newly confirmed deleted record cannot remain selected');
  });
}

function interactionSnapshot(state) {
  return JSON.stringify({
    view: state.viewBounds.value, selection: [...state.selectedIndices.value],
    rectangle: state.selectionRect.value, mode: state.selectionMode.value,
    filter: state.tableFilter.value, page: state.tablePage.value,
    layer: state.selectedLayerKey.value,
  });
}

test('periodic equivalent plugin, file and related-list objects preserve selection, page, zoom and loaded bytes', async t => {
  const preview = await selectable(t, Array.from({ length: 125 }, (_, index) => [index, index]));
  const { state, props, relatedFiles, calls } = preview;
  state.selectFeature(120);
  state.zoom(0.5);
  state.selectionMode.value = true;
  state.selectionRect.value = [10, 10, 20, 20];
  await vue.nextTick();
  const before = interactionSnapshot(state);
  const originalSignals = calls.map(call => call.signal);
  assert.equal(calls.length, 3);
  for (let poll = 0; poll < 5; poll++) {
    props.plugin = { ...clone(props.plugin), extensions: [...props.plugin.extensions].reverse() };
    props.file = clone(props.file);
    relatedFiles.value = clone(relatedFiles.value).reverse();
    await vue.nextTick();
    await flush();
    assert.equal(calls.length, 3, `Equivalent refresh ${poll + 1} must not reread any resource`);
    assert.equal(interactionSnapshot(state), before);
    assert.ok(originalSignals.every(signal => !signal.aborted));
  }
});

test('equivalent selected-file DTO refresh does not undo a manually selected different layer', async t => {
  const alpha = group('alpha');
  const beta = group('beta', { directory: 'other', x: 2400, y: 2600 });
  const preview = mount(t, alpha.files.prj, [alpha, beta]);
  await flush();
  preview.state.selectedLayerKey.value = beta.key;
  await vue.nextTick();
  await flush();
  visibleGeometry(preview, beta);
  preview.state.selectFeature(0);
  preview.state.zoom(0.5);
  const before = interactionSnapshot(preview.state);
  const count = preview.calls.length;
  preview.props.file = clone(preview.props.file);
  preview.relatedFiles.value = clone(preview.relatedFiles.value);
  await vue.nextTick();
  await flush();
  assert.equal(preview.calls.length, count);
  assert.equal(interactionSnapshot(preview.state), before);
  visibleGeometry(preview, beta);
});

test('changes to unrelated related-files do not restart the current Shapefile preview', async t => {
  const alpha = group('alpha');
  const beta = group('beta', { directory: 'other' });
  const preview = mount(t, alpha.files.shp, [alpha, beta]);
  await flush();
  preview.state.selectFeature(0);
  const before = interactionSnapshot(preview.state);
  preview.relatedFiles.value = preview.relatedFiles.value.map(file => file.file_id.startsWith('beta-')
    ? { ...clone(file), metadata: { ...file.metadata, content_sha256: 'b'.repeat(64) } }
    : clone(file));
  await vue.nextTick();
  await flush();
  assert.equal(preview.calls.length, 3);
  assert.equal(interactionSnapshot(preview.state), before);
});

for (const extension of extensions) {
  test(`a real ${extension.toUpperCase()} content-version change restarts the group and cancels its prior load`, async t => {
    const layer = group('versioned');
    for (const file of Object.values(layer.files)) file.metadata.content_sha256 = 'a'.repeat(64);
    const preview = mount(t, layer.files.shp, [layer]);
    await flush();
    preview.state.selectFeature(0);
    const oldSignal = preview.calls[0].signal;
    const changedId = layer.files[extension].file_id;
    preview.relatedFiles.value = preview.relatedFiles.value.map(file => file.file_id === changedId
      ? { ...clone(file), metadata: { ...file.metadata, content_sha256: 'b'.repeat(64) } }
      : clone(file));
    if (extension === 'shp') preview.props.file = clone(preview.relatedFiles.value.find(file => file.file_id === changedId));
    await vue.nextTick();
    await flush();
    assert.equal(oldSignal.aborted, true);
    assert.equal(preview.calls.length, 6);
    assert.equal(preview.state.selectedIndices.value.size, 0);
    assert.ok(preview.calls.slice(3).every(call => call.signal !== oldSignal));
  });
}

for (const changed of ['version', 'read-budget']) {
  test(`a genuine plugin ${changed} change reloads instead of retaining old resource authority`, async t => {
    const layer = group('plugin');
    const preview = mount(t, layer.files.shp, [layer]);
    await flush();
    const oldSignal = preview.calls[0].signal;
    const updated = clone(preview.props.plugin);
    if (changed === 'version') updated.version = '1.0.1';
    else updated.limits.max_input_bytes = Math.floor(updated.limits.max_input_bytes / 2);
    preview.props.plugin = updated;
    await vue.nextTick();
    await flush();
    assert.equal(oldSignal.aborted, true);
    assert.equal(preview.calls.length, 6);
    assert.ok(preview.calls.slice(3).every(call => call.plugin.version === updated.version
      && call.plugin.limits.max_input_bytes === updated.limits.max_input_bytes));
  });
}

test('equivalent refresh leaves an in-flight geometry request alive, but a changed version aborts and supersedes it', async t => {
  const layer = group('pending');
  const old = deferred();
  let firstShp = true;
  const preview = mount(t, layer.files.shp, [layer], { request: call => {
    if (call.target.file_id === layer.files.shp.file_id && firstShp) {
      firstShp = false;
      return old.promise;
    }
  } });
  await flush();
  const pendingSignal = preview.calls[0].signal;
  preview.props.plugin = clone(preview.props.plugin);
  preview.relatedFiles.value = clone(preview.relatedFiles.value);
  await vue.nextTick();
  await flush();
  assert.equal(preview.calls.length, 1);
  assert.equal(pendingSignal.aborted, false);
  preview.props.plugin = { ...clone(preview.props.plugin), version: '1.0.1' };
  await vue.nextTick();
  await flush();
  assert.equal(pendingSignal.aborted, true);
  assert.equal(preview.calls.length, 4);
  visibleGeometry(preview, layer);
  const current = snapshot(preview);
  old.resolve(pointShp(-9999, -9999));
  await flush();
  assert.equal(snapshot(preview), current);
  assert.equal(preview.calls.length, 4);
});

test('an in-place approved plugin limit change is detected without relying on object replacement', async t => {
  const layer = group('in-place');
  const preview = mount(t, layer.files.shp, [layer]);
  await flush();
  const originalSignal = preview.calls[0].signal;
  preview.props.plugin.limits.max_input_bytes /= 2;
  await vue.nextTick();
  await flush();
  assert.equal(originalSignal.aborted, true);
  assert.equal(preview.calls.length, 6);
});

for (const extension of extensions) {
  for (const changedSource of ['related-list', 'selected-file']) {
    test(`${extension.toUpperCase()} entry detects ${changedSource} version updates even when the other DTO remains stale`, async t => {
      const layer = group(`${extension}-entry`);
      const selectedFile = layer.files[extension];
      selectedFile.metadata.content_sha256 = 'a'.repeat(64);
      const preview = mount(t, selectedFile, [layer]);
      await flush();
      const originalSignal = preview.calls[0].signal;
      const newer = { ...clone(selectedFile), metadata: { ...selectedFile.metadata, content_sha256: 'b'.repeat(64) } };
      if (changedSource === 'related-list') {
        preview.relatedFiles.value = preview.relatedFiles.value.map(file => file.file_id === selectedFile.file_id ? newer : clone(file));
      } else preview.props.file = newer;
      await vue.nextTick();
      await flush();
      assert.equal(originalSignal.aborted, true);
      assert.equal(preview.calls.length, 6);
    });
  }
}
