import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import { create, createHistogram, createTGraph } from 'jsroot/core';
import { assertMolecularText, assertVtkInput, numericCell, numericColumns, numericPairs, parseArray, parseTable, plainLabel } from '../src/visualizations/extended/scientific/data.ts';
import { nmriumReadOnlyPreferences } from '../src/visualizations/extended/scientific/nmriumReadOnly.ts';
import { loadBrowserLibrary } from '../src/visualizations/extended/scientific/browserLibraries.ts';
import { visualizationBrowserBoundary } from '../visualization-browser-boundary.ts';
import { create as createVtkXml } from '../src/visualizations/extended/scientific/vtkBrowserXml.ts';
import * as guards from '../src/visualizations/extended/scientific/data.ts';

const module = { exports: {} };
const source = readFileSync(new URL('../src/visualizations/extended/scientific/rootData.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
new Function('require', 'module', 'exports', compiled)((id) => { assert.equal(id, './data'); return guards; }, module, module.exports);
const { buildRootNumericObject } = module.exports;
const core = { create, createHistogram, createTGraph };
const bytes = (value) => new TextEncoder().encode(value).buffer;

test('table guards preserve missing cells and reject executable or non-finite values', () => {
  const table = parseTable({ columns: ['name', '<b>value</b>'], rows: [['a', 1.5], ['b', null]] });
  assert.deepEqual(numericColumns(table), [1]);
  assert.equal(table.rows[1][1], null);
  assert.throws(() => parseTable({ columns: ['x'], rows: [[{ url: 'https://bad' }]] }));
  assert.throws(() => parseTable({ columns: ['x'], rows: [[Infinity]] }));
  assert.throws(() => parseTable({ columns: ['x'], rows: [[]] }));
  assert.equal(numericCell(''), null); assert.equal(numericCell('1.2e3'), 1200);
  const booleans = parseTable({ columns: ['flag'], rows: [[true], [false], [null]] });
  assert.deepEqual(booleans.rows, [[true], [false], [null]]); assert.deepEqual(numericColumns(booleans), [0]);
  assert.equal(numericCell(true), 1); assert.equal(numericCell(false), 0);
});
test('numeric array shapes and budgets are checked before GPU allocation', () => {
  assert.deepEqual(parseArray({ shape: [2], values: [1.25, null] }).values, [1.25, null]);
  for (const invalid of [{ shape: [3], values: [1, 2] }, { shape: [1, 1, 1], values: [1] }, { shape: [1], values: [NaN] }, { shape: [65537], values: Array(65537).fill(1) }, { shape: [-1], values: [] }]) assert.throws(() => parseArray(invalid));
});
test('coordinate pairs support reader arrays beyond table windows and reject missing points', () => {
  const values = Array.from({ length: 300 }, (_, index) => [index / 10, index * 3]).flat();
  const result = numericPairs({ array: { shape: [300, 2], values } });
  assert.equal(result.x.length, 300); assert.equal(result.x[299], 29.9); assert.equal(result.y[299], 897);
  assert.throws(() => numericPairs({ array: { shape: [1, 2], values: [1, null] } }));
  assert.throws(() => numericPairs({ array: { shape: [2], values: [1, 2] } }));
});
test('TH1 adapter uses real JSROOT constructors and preserves nonuniform bin edges', () => {
  const { object, option } = buildRootNumericObject({ metadata: { root_class: 'TH1D', x_edges: [0, 0.2, 8] }, array: { shape: [2], values: [1.2, 4] } }, core);
  assert.equal(object._typename, 'TH1D'); assert.equal(option, 'HIST');
  assert.deepEqual([...object.fXaxis.fXbins], [0, 0.2, 8]);
  assert.equal(object.fArray[1], 1.2); assert.equal(object.fArray[2], 4);
  assert.equal(object.fFunctions.arr.length, 0);
});
test('TH2 adapter preserves ROOT x-fastest memory order and physical axes', () => {
  const { object } = buildRootNumericObject({ metadata: { root_class: 'TH2F', x_edges: [10, 20, 40], y_edges: [-2, 0, 9] }, array: { shape: [2, 2], values: [1, 2, 3, 4] } }, core);
  assert.equal(object.fArray[5], 1); assert.equal(object.fArray[6], 2); assert.equal(object.fArray[9], 3); assert.equal(object.fArray[10], 4);
  assert.equal(object.fXaxis.fXmin, 10); assert.equal(object.fYaxis.fXmax, 9);
});
test('TGraph coordinates are rebuilt without functions or file-provided metadata', () => {
  const { object } = buildRootNumericObject({ metadata: { root_class: 'TGraph', fTitle: '<script>bad</script>', fFunctions: [{ _typename: 'TExec' }] }, array: { shape: [2, 2], values: [1, 3, 2, 7] } }, core);
  assert.equal(object._typename, 'TGraph'); assert.deepEqual([...object.fX], [1, 2]); assert.deepEqual([...object.fY], [3, 7]);
  assert.equal(object.fTitle, 'Local ROOT numeric preview'); assert.equal(object.fFunctions.arr.length, 0);
});
test('ROOT classes, dimensions and non-finite or fabricated bin edges fail closed', () => {
  for (const root_class of ['TF1', 'TCanvas', 'TTree', 'TExec', 'TH3D']) assert.throws(() => buildRootNumericObject({ metadata: { root_class } }, core));
  assert.throws(() => buildRootNumericObject({ metadata: { root_class: 'TH1D', x_edges: [1, 0] }, array: { shape: [1], values: [2] } }, core));
  assert.throws(() => buildRootNumericObject({ metadata: { root_class: 'TH1D' }, array: { shape: [1], values: [2] } }, core));
  assert.throws(() => buildRootNumericObject({ metadata: { root_class: 'TH2D', x_edges: [0, 1], y_edges: [0, 1] }, array: { shape: [1, 1], values: [null] } }, core));
});
test('VTK guards reject compressed XML, declarations, external materials and huge extents', () => {
  for (const xml of ['<!DOCTYPE VTKFile><VTKFile/>', '<VTKFile compressor="vtkZLibDataCompressor"/>', '<DataArray format="binary">AAAA</DataArray>', '<ImageData WholeExtent="0 9999 0 9999 0 9999"/>', '<Piece NumberOfPoints="999999999"/>', '<DataArray NumberOfComponents="999999999">1</DataArray>', '<DataArray NumberOfTuples="999999999">1</DataArray>', '<Piece NumberOfPoints="300000"><DataArray NumberOfComponents="9"/><DataArray NumberOfComponents="9"/></Piece>']) assert.throws(() => assertVtkInput(bytes(xml), 'vti'));
  assert.throws(() => assertVtkInput(bytes('mtllib https://bad/file.mtl\nv 0 0 0'), 'obj'));
  assert.doesNotThrow(() => assertVtkInput(bytes('<VTKFile><ImageData WholeExtent="0 2 0 2 0 0"><DataArray format="ascii">1 2</DataArray></ImageData></VTKFile>'), 'vti'));
});
test('binary STL triangle allocation is bounded before upstream parsing', () => {
  const good = new ArrayBuffer(84 + 50); new DataView(good).setUint32(80, 1, true); assert.doesNotThrow(() => assertVtkInput(good, 'stl'));
  const bad = new ArrayBuffer(84 + 100001 * 50); new DataView(bad).setUint32(80, 100001, true); assert.throws(() => assertVtkInput(bad, 'stl'));
  assert.throws(() => assertVtkInput(new ArrayBuffer(9 * 1024 * 1024), 'obj'));
});
test('molecular adapter accepts atoms only, with no arbitrary state or data formats', () => {
  assert.doesNotThrow(() => assertMolecularText('ATOM      1  N   ALA A   1', 'pdb'));
  assert.throws(() => assertMolecularText('{"url":"https://example.com"}', 'pdb'));
  assert.throws(() => assertMolecularText('data_small\n_cell.length_a 2', 'mmcif'));
  assert.doesNotThrow(() => assertMolecularText('data_struct\n_atom_site.id 1', 'mmcif'));
  assert.equal(plainLabel('<script>\u0000x</script>'), 'scriptx/script');
});
test('NMRium read-only workspace turns off imports, auto-processing, databases and APIs', () => {
  const prefs = nmriumReadOnlyPreferences;
  assert.equal(prefs.display.toolBarButtons.import, false); assert.equal(prefs.display.toolBarButtons.fft, false);
  assert.equal(prefs.display.toolBarButtons.exportAs, false); assert.equal(prefs.onLoadProcessing.autoProcessing, false);
  assert.deepEqual(prefs.databases.data, []); assert.deepEqual(prefs.externalAPIs, []);
  assert.equal(prefs.display.general.hideGeneralSettings, true);
  assert.ok(Object.values(prefs.display.panels).every((panel) => !panel.display));
});

function deferred() { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; }
async function flush() { for (let index = 0; index < 30; index++) await Promise.resolve(); }
function mountScientific(name, dependencies) {
  const text = readFileSync(new URL(`../src/visualizations/extended/scientific/${name}.vue`, import.meta.url), 'utf8');
  const script = compileScript(parse(text).descriptor, { id: name });
  const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const local = { exports: {} };
  const modules = { vue, '../../../composables/usePreviewLoad': { usePreviewLoad }, './data': guards, './PreviewFrame.vue': { default: {} }, './nmriumReadOnly': { nmriumReadOnlyPreferences }, './rootData': { buildRootNumericObject }, './browserLibraries': { loadNmrBrowserLibrary: () => assert.fail('Unexpected library load') }, ...dependencies };
  new Function('require', 'module', 'exports', code)((id) => { assert.ok(id in modules, `Unexpected adapter dependency ${id}`); return modules[id]; }, local, local.exports);
  const props = vue.reactive({ file: { file_id: 'file-one', filename: 'input.csv' }, plugin: { id: `viz-${name}`, enabled: true } });
  const scope = vue.effectScope();
  const state = scope.run(() => local.exports.default.setup(props, { expose() {} }));
  state.target.value = { clientWidth: 800, clientHeight: 480 };
  return { state, props, unmount: () => scope.stop() };
}
test('Plotly does not paint a late response after plugin disposal', async () => {
  const pending = deferred(); let signal, draws = 0;
  const adapter = mountScientific('PlotlyPreview', { '../runtime': { requestPreview: (_file, _plugin, _options, requestSignal) => { signal = requestSignal; return pending.promise; } }, './browserLibraries': { loadBrowserLibrary: async () => ({ react() { draws++; }, purge() {} }) } });
  adapter.unmount(); assert.equal(signal.aborted, true);
  pending.resolve({ table: { columns: ['x'], rows: [[1]] } }); await flush(); assert.equal(draws, 0);
});
test('H5Web handles default-only React CommonJS imports and disposes the real root boundary', async () => {
  let mounted, cleaned = false;
  const adapter = mountScientific('H5WebPreview', {
    '../runtime': { requestPreview: async () => ({ tree: [{ path: '/data', node_type: 'dataset', shape: [3] }], array: { shape: [3], values: [1, 3, 2] }, metadata: { strides: [2] } }) },
    react: { default: { createElement: (component, props) => ({ component, props }) } },
    'react-dom/client': { default: { createRoot: () => ({ render(value) { mounted = value; }, unmount() { cleaned = true; } }) } },
    '@h5web/lib': { LineVis: 'line', HeatmapVis: 'heatmap' },
    ndarray: { default: (values, shape) => ({ values, shape }) }, '@h5web/lib/styles.css': {},
  });
  await flush(); assert.equal(adapter.state.error.value, ''); assert.equal(mounted.component, 'line');
  assert.deepEqual([...mounted.props.dataArray.values], [1, 3, 2]);
  assert.deepEqual([...mounted.props.abscissaParams.value], [0, 2, 4]);
  adapter.unmount(); assert.equal(cleaned, true);
});
test('Molstar does not import or parse a response after unmount', async () => {
  const pending = deferred(); let signal;
  const adapter = mountScientific('MolstarPreview', { '../runtime': { loadPluginBytes: (_file, _plugin, requestSignal) => { signal = requestSignal; return pending.promise; } } });
  adapter.unmount(); pending.resolve(bytes('ATOM      1  N   ALA A   1')); await flush();
  assert.equal(signal.aborted, true); assert.equal(adapter.state.error.value, '');
});
test('NMRium refuses missing units before loading the editor library', async () => {
  const adapter = mountScientific('NmriumPreview', { '../runtime': { requestPreview: async () => ({ table: { columns: ['x', 'y'], rows: [[1, 2], [2, 3]] }, metadata: { nucleus: '1H' } }) } });
  await flush(); assert.match(adapter.state.error.value, /ppm/); adapter.unmount();
});
test('NMRium mounts only rebuilt numeric data with restricted settings, then unmounts', async () => {
  let mounted, cleaned = false;
  const adapter = mountScientific('NmriumPreview', {
    '../runtime': { requestPreview: async () => ({ array: { shape: [2, 2], values: [1, 2, 2, 3] }, metadata: { nucleus: '1H', x_unit: 'PPM', frequency: 400, source: { url: 'https://bad' } } }) },
    './browserLibraries': { loadNmrBrowserLibrary: async () => ({ mountNmr(_target, input) { mounted = input; return () => { cleaned = true; }; } }) },
  });
  await flush(); assert.equal(adapter.state.error.value, ''); assert.ok(mounted);
  assert.deepEqual(mounted, { x: [1, 2], y: [2, 3], nucleus: '1H', frequency: 400 });
  adapter.unmount(); assert.equal(cleaned, true);
});
test('standalone NMRium entry owns its React root and accepts only bounded numeric spectra', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/scientific/nmriumBrowserEntry.ts', import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  let mounted, cleaned = false; const exports = {};
  const modules = { react: { createElement: (component, props) => ({ component, props }) }, 'react-dom/client': { createRoot: () => ({ render(value) { mounted = value; }, unmount() { cleaned = true; } }) }, nmrium: { NMRium: {} }, './nmriumReadOnly': { nmriumReadOnlyPreferences }, '@blueprintjs/core/lib/css/blueprint.css': {}, '@blueprintjs/icons/lib/css/blueprint-icons.css': {} };
  new Function('require', 'exports', code)((id) => { assert.ok(id in modules); return modules[id]; }, exports);
  const input = { x: [1, 2], y: [3, 4], nucleus: '1H', source: { url: 'https://bad' } };
  const dispose = exports.mountNmr({}, input);
  assert.equal(mounted.props.data.source, undefined); assert.equal(mounted.props.preferences.onLoadProcessing.autoProcessing, false);
  assert.deepEqual(mounted.props.data.spectra[0].data, { x: [1, 2], re: [3, 4] });
  input.x[0] = 99; assert.equal(mounted.props.data.spectra[0].data.x[0], 1);
  assert.throws(() => exports.mountNmr({}, { x: [1, 2], y: [1, NaN], nucleus: '1H' }));
  assert.throws(() => exports.mountNmr({}, { x: [1, 2], y: [1, 3], nucleus: 'remote://nucleus' }));
  dispose(); assert.equal(cleaned, true);
});
test('NMRium storage bridge cannot restore workspaces or persist dataset/UI state', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/scientific/nmriumStorage.ts', import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const require = createRequire(import.meta.url), exports = {}; let memory;
  const modules = { 'lodash/get': { default: require('lodash/get') }, 'lodash/set': { default: require('lodash/set') }, react: { useCallback: (callback) => callback, useMemo: (compute) => compute(), useState: (initial) => { memory = initial; return [initial, (update) => { memory = update(memory); }]; } } };
  new Function('require', 'exports', code)((id) => { assert.ok(id in modules); return modules[id]; }, exports);
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new Proxy({}, { get() { assert.fail('NMRium must not access browser storage'); } }) });
  try {
    assert.equal(exports.getLocalStorage('nmr-general-settings'), null);
    assert.equal(exports.storeData('nmr-general-settings', 'ignored'), undefined);
    assert.equal(exports.removeData('nmr-general-settings'), undefined);
    assert.equal(exports.getValue({ settings: { axis: 3 } }, 'settings.axis'), 3);
    const [initial, update] = exports.useStateWithLocalStorage('any-key');
    assert.deepEqual(initial, {}); update({ zoom: 2 }); assert.deepEqual(memory, { zoom: 2 });
    update({ value: 3 }, 'panel'); assert.deepEqual(memory, { zoom: 2, panel: { value: 3 } });
  } finally { if (previous) Object.defineProperty(globalThis, 'localStorage', previous); else delete globalThis.localStorage; }
});
test('fixed local browser loader cancels its waiter without mounting or removing shared code', async () => {
  const previous = globalThis.document; let script, removed = false;
  globalThis.document = { createElement: () => ({ remove() { removed = true; } }), head: { appendChild(value) { script = value; } } };
  try {
    const controller = new AbortController(), pending = loadBrowserLibrary('plotly', controller.signal);
    assert.equal(script.src, '/visualization-assets/plotly/plotly-cartesian.min.js');
    controller.abort(); await assert.rejects(pending, { name: 'AbortError' }); assert.equal(removed, false);
    const library = { version: '4.1.0', react() {}, purge() {} }; globalThis.Plotly = library;
    script.onload(); await flush();
    assert.equal(await loadBrowserLibrary('plotly', new AbortController().signal), library);
  } finally { globalThis.document = previous; delete globalThis.Plotly; }
});
test('browser bundles are version checked and server exports cannot reenter Rollup', async () => {
  globalThis.JSROOT = { version: '999.0.0', draw() {}, createHistogram() {}, cleanup() {} };
  try { await assert.rejects(loadBrowserLibrary('jsroot', new AbortController().signal), /版本/); }
  finally { delete globalThis.JSROOT; }
  const plugin = visualizationBrowserBoundary();
  assert.match(plugin.resolveId('xmlbuilder2', '/workspace/node_modules/@kitware/vtk.js/IO/XML/XMLReader.js'), /vtkBrowserXml\.ts$/);
  assert.equal(plugin.resolveId('xmlbuilder2', '/workspace/node_modules/unrelated/index.js'), null);
  assert.throws(() => plugin.resolveId('@resvg/resvg-js', '/workspace/node_modules/jsroot/modules/base/BasePainter.mjs'), /server image export/);
  assert.match(plugin.resolveId('react-icons/lu', '/workspace/node_modules/nmrium/lib/component/panels/ZonesPanel.js'), /nmriumIcons\.ts$/);
  assert.equal(plugin.resolveId('react-icons/lu', '/workspace/src/unrelated.ts'), null);
});
test('VTK browser DOM bridge preserves numeric XML and rejects entity/binary/malformed XML', () => {
  const require = createRequire(import.meta.url), fromRoot = createRequire(require.resolve('jsroot'));
  const { JSDOM } = fromRoot('jsdom'), dom = new JSDOM('');
  const previous = globalThis.DOMParser; globalThis.DOMParser = dom.window.DOMParser;
  try {
    const root = createVtkXml('<VTKFile type="ImageData"><DataArray format="ascii">1 2 3</DataArray></VTKFile>').root();
    assert.equal(root.node.getAttribute('type'), 'ImageData');
    assert.equal(root.node.getElementsByTagName('DataArray')[0].firstChild.nodeValue, '1 2 3');
    assert.throws(() => root.filter(() => true), /appended/);
    for (const xml of ['<!DOCTYPE VTKFile><VTKFile/>', '<VTKFile><DataArray format="binary"/></VTKFile>', '<VTKFile><AppendedData/></VTKFile>', '<VTKFile><broken></VTKFile>', '<unrelated/>']) assert.throws(() => createVtkXml(xml));
  } finally { globalThis.DOMParser = previous; dom.window.close(); }
});
