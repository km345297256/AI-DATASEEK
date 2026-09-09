import assert from 'node:assert/strict';

const utf8 = (text) => Buffer.from(text, 'utf8');
const base = (reader, kind, extra = {}) => ({ contract_version: 2, type: reader, reader, kind, media_type: 'application/json', metadata: {}, warnings: [], sampled: false, ...extra });
async function noAlert(page) { const alerts = await page.locator('[role="alert"]').allTextContents(); assert.deepEqual(alerts, []); }
async function instrumentWebgl(page) {
  await page.addInitScript(() => {
    globalThis.__scientificDrawCalls = 0;
    for (const name of ['WebGLRenderingContext', 'WebGL2RenderingContext']) {
      const type = globalThis[name]; if (!type) continue;
      for (const method of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
        const original = type.prototype[method]; if (typeof original !== 'function') continue;
        type.prototype[method] = function (...args) { globalThis.__scientificDrawCalls++; return original.apply(this, args); };
      }
    }
  });
}
async function gpuReady(page) {
  await page.waitForFunction(() => [...document.querySelectorAll('canvas')].some((canvas) => canvas.width > 0 && canvas.height > 0) && globalThis.__scientificDrawCalls > 0, undefined, { timeout: 30000 });
  await page.getByRole('status').waitFor({ state: 'hidden', timeout: 30000 });
}
async function gpuVerify(page) {
  await noAlert(page);
  const result = await page.evaluate(() => ({ canvases: [...document.querySelectorAll('canvas')].map((canvas) => ({ width: canvas.width, height: canvas.height })), drawCalls: globalThis.__scientificDrawCalls }));
  assert.ok(result.drawCalls > 0); assert.ok(result.canvases.some((canvas) => canvas.width > 0 && canvas.height > 0)); return result;
}
const vti = `<?xml version="1.0"?>
<VTKFile type="ImageData" version="0.1" byte_order="LittleEndian">
 <ImageData WholeExtent="0 3 0 3 0 2" Origin="0 0 0" Spacing="1 1 1">
  <Piece Extent="0 3 0 3 0 2"><PointData Scalars="values">
   <DataArray type="Float32" Name="values" NumberOfComponents="1" format="ascii">${Array.from({ length: 48 }, (_, index) => index + 1).join(' ')}</DataArray>
  </PointData><CellData/></Piece>
 </ImageData>
</VTKFile>`;
function pdbAtom(serial, name, residue, x, y, z, element) {
  return `ATOM  ${String(serial).padStart(5)} ${name.padEnd(4)} ALA A${String(residue).padStart(4)}    ${x.toFixed(3).padStart(8)}${y.toFixed(3).padStart(8)}${z.toFixed(3).padStart(8)}  1.00 20.00          ${element.padStart(2)}  `;
}
const pdb = [
  'HEADER    SYNTHETIC FOUR-RESIDUE FIXTURE',
  ...Array.from({ length: 4 }, (_, residue) => [pdbAtom(residue * 4 + 1, 'N', residue + 1, residue * 3.8, 0, 0, 'N'), pdbAtom(residue * 4 + 2, 'CA', residue + 1, residue * 3.8 + 1.3, 0.7, 0, 'C'), pdbAtom(residue * 4 + 3, 'C', residue + 1, residue * 3.8 + 2.6, 0, 0, 'C'), pdbAtom(residue * 4 + 4, 'O', residue + 1, residue * 3.8 + 2.8, -1.1, 0, 'O')]).flat(),
  'TER', 'END', '',
].join('\n');
const spectral = Array.from({ length: 128 }, (_, index) => [index / 16, Math.exp(-(((index - 40) / 5) ** 2)) + .4 * Math.exp(-(((index - 86) / 7) ** 2))]);

export const scientificCases = [
  {
    name: 'plotly', component: 'scientific/PlotlyPreview.vue', filename: 'synthetic.csv', reader: 'tabular', kind: 'series',
    bytes: utf8('value,flag\n1,true\n3,false\n2,true\n'),
    preview: base('tabular', 'table', { table: { columns: ['value', 'flag'], rows: [[1, true], [3, false], [2, true]], row_offset: 0, column_offset: 0, total_rows: 3, total_columns: 2 } }),
    async ready(page) { await page.waitForFunction(() => { const plot = document.querySelector('.js-plotly-plot'); return plot?._fullData?.length === 1 && !!plot.querySelector('.scatterlayer path'); }); },
    async verify(page) { await noAlert(page); const result = await page.evaluate(() => { const plot = document.querySelector('.js-plotly-plot'); return { version: globalThis.Plotly?.version, y: [...plot._fullData[0].y], traces: plot._fullData.length }; }); assert.equal(result.version, '4.1.0'); assert.deepEqual(result.y, [1, 3, 2]); return result; },
  },
  {
    name: 'jsroot', component: 'scientific/JsRootPreview.vue', filename: 'synthetic.root', reader: 'root', kind: 'series', bytes: utf8('root-fixture-uses-mocked-isolated-reader'),
    preview: base('root', 'series', { array: { shape: [3], dimensions: ['bin'], values: [1, 4, 2] }, metadata: { root_class: 'TH1D', x_edges: [0, 0.2, 1.5, 5] }, tree: [{ path: 'hist', node_type: 'dataset', dtype: 'TH1D' }], selected: { path: 'hist' } }),
    async ready(page) { await page.locator('svg.root_canvas').waitFor({ state: 'visible' }); await page.waitForFunction(() => document.querySelector('svg.root_canvas')?.querySelectorAll('path').length > 2); },
    async verify(page) { await noAlert(page); const result = await page.evaluate(() => ({ version: globalThis.JSROOT?.version, paths: document.querySelector('svg.root_canvas').querySelectorAll('path').length, text: document.querySelector('svg.root_canvas').textContent })); assert.match(result.version, /^7\.11\.1/); assert.ok(result.paths > 2); assert.match(result.text, /Local ROOT numeric preview/); return result; },
  },
  {
    name: 'h5web', component: 'scientific/H5WebPreview.vue', filename: 'synthetic.h5', reader: 'hdf5', kind: 'series', bytes: utf8('hdf5-fixture-uses-mocked-isolated-reader'), init: instrumentWebgl,
    preview: base('hdf5', 'series', { array: { shape: [4], dimensions: ['dim_0'], values: [1, 3, 2, 5] }, metadata: { source_shape: [4], strides: [1], axis_coordinates: 'index' }, tree: [{ path: '/data', node_type: 'dataset', shape: [4], dtype: 'float64' }], choices: { variables: ['/data'] }, selected: { path: '/data', indices: [] } }),
    ready: gpuReady,
    async verify(page) { const result = await gpuVerify(page); assert.ok(result.canvases.some((canvas) => canvas.height >= 300), 'H5Web plot must have usable height'); return result; },
  },
  {
    name: 'vtk', component: 'scientific/VtkPreview.vue', filename: 'synthetic.vti', reader: 'binary', kind: 'structure', bytes: utf8(vti), init: instrumentWebgl, ready: gpuReady,
    async verify(page) { const result = await gpuVerify(page); assert.equal(await page.locator('input[type="range"]').getAttribute('max'), '2'); assert.equal(result.canvases[0].height, 480); return result; },
  },
  { name: 'molstar', component: 'scientific/MolstarPreview.vue', filename: 'synthetic.pdb', reader: 'binary', kind: 'structure', bytes: utf8(pdb), init: instrumentWebgl, ready: gpuReady, verify: gpuVerify },
  {
    name: 'nmrium', component: 'scientific/NmriumPreview.vue', filename: 'synthetic.jdx', reader: 'jcamp', kind: 'series', bytes: utf8('##TITLE=Synthetic\n##END=\n'),
    async init(page) { await page.addInitScript(() => {
      // A poisoned stale workspace must never become another preview input.
      localStorage.setItem('nmr-general-settings', '{"currentWorkspace":"untrusted-stale","workspaces":{"untrusted-stale":{"externalAPIs":["https://invalid.example"]}}}');
      globalThis.__nmrStorageCalls = [];
      for (const operation of ['getItem', 'setItem', 'removeItem']) {
        const original = Storage.prototype[operation];
        Storage.prototype[operation] = function (key, ...args) { if (String(key).startsWith('nmr')) globalThis.__nmrStorageCalls.push({ operation, key }); return original.call(this, key, ...args); };
      }
    }); },
    preview: base('jcamp', 'series', { array: { shape: [128, 2], dimensions: ['point', 'xy'], values: spectral.flat() }, metadata: { is_fid: false, nucleus: '1H', x_unit: 'PPM', frequency: 400 }, selected: {} }),
    async ready(page) { await page.locator('#nmrSVG [data-testid="spectrum-line"]').waitFor({ state: 'visible', timeout: 30000 }); await page.waitForFunction(() => (document.querySelector('#nmrSVG [data-testid="spectrum-line"]')?.getAttribute('d')?.length ?? 0) > 100); },
    async verify(page) { await noAlert(page); const result = await page.evaluate(() => ({ pathLength: document.querySelector('#nmrSVG [data-testid="spectrum-line"]').getAttribute('d').length, height: document.querySelector('#nmrSVG').getBoundingClientRect().height, storageCalls: globalThis.__nmrStorageCalls, importButtons: [...document.querySelectorAll('button')].filter((button) => /import|load.*file|open.*file/i.test(button.title || button.textContent)).length })); assert.ok(result.pathLength > 100); assert.ok(result.height >= 300, 'NMR spectrum must have usable height'); assert.equal(result.importButtons, 0); assert.deepEqual(result.storageCalls, []); return result; },
  },
  {
    name: 'molecular', component: '../../components/filePreviews/MolecularStructurePreview.vue', filename: 'synthetic.pdb', reader: 'molecular', kind: 'structure', bytes: utf8(pdb),
    // 3Dmol allocates its package-owned SurfaceWorker URL once at module import.
    // The existing renderer does not start that worker. Compare repeated mounts
    // below; this explicit library baseline must never become a per-view leak.
    libraryBlobBaseline: 1,
    descriptor: { contract_version: 1, data_kind: 'molecular', adapter: 'molecular' },
    async init(page) {
      await instrumentWebgl(page);
      await page.addInitScript(() => { globalThis.__molecularFallbacks = []; const original = console.warn; console.warn = (...args) => { if (String(args[0]).includes('3Dmol parser failed')) globalThis.__molecularFallbacks.push(String(args[0])); original.apply(console, args); }; });
    },
    async api(request) {
      const path = new URL(request.url()).pathname;
      if (path === '/api/v1/files/molecular-preview/prepare' && request.method() === 'POST') {
        assert.equal(request.postDataJSON().file_id, 'synthetic-molecular');
        return { contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { source_name: 'synthetic.pdb', source_format: 'pdb', size_bytes: utf8(pdb).byteLength, content_type: 'chemical/x-pdb', periodic: false, supports_unit_cell: false } }) };
      }
      if (path === '/api/v1/files/synthetic-molecular/signed-url' && request.method() === 'POST') return { contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { signed_url: '/api/v1/files/synthetic-molecular/synthetic-content', expires_at: '2099-01-01T00:00:00Z' } }) };
      if (path === '/api/v1/files/synthetic-molecular/synthetic-content' && request.method() === 'GET') return { contentType: 'chemical/x-pdb', body: utf8(pdb) };
    },
    async ready(page) {
      await gpuReady(page);
      await page.getByLabel('二维分子结构式').waitFor({ state: 'visible' });
      await page.getByLabel('可拖动旋转的三维分子结构').waitFor({ state: 'visible' });
    },
    async verify(page) {
      const result = await gpuVerify(page);
      const details = await page.evaluate(() => ({ fallbacks: globalThis.__molecularFallbacks, upstreamCanvas: document.querySelectorAll('[aria-hidden="true"] canvas').length, bondLines: document.querySelector('[aria-label="二维分子结构式"]')?.querySelectorAll('line').length, formula: document.body.textContent }));
      assert.deepEqual(details.fallbacks, []); assert.ok(details.upstreamCanvas > 0, 'Real 3Dmol must initialize; a fallback-only parser is not sufficient'); assert.ok(details.bondLines > 0); assert.match(details.formula, /C8N4O4/);
      const libraryBlobs = await page.evaluate(() => [...window.__activeBlobs]); assert.equal(libraryBlobs.length, 1);
      await page.evaluate(() => window.unmountHarness()); await page.waitForTimeout(150);
      const disposedDraws = await page.evaluate(() => window.__webglDraws); await page.waitForTimeout(150);
      assert.equal(await page.evaluate(() => window.__webglDraws), disposedDraws, 'First molecular view must stop drawing before remount');
      await page.evaluate(() => window.mountHarness('molecular'));
      await page.getByLabel('二维分子结构式').waitFor({ state: 'visible' });
      await page.getByLabel('可拖动旋转的三维分子结构').waitFor({ state: 'visible' });
      assert.deepEqual(await page.evaluate(() => [...window.__activeBlobs]), libraryBlobs, 'Repeated molecular mounts must not accumulate worker Blob URLs');
      assert.equal(await page.evaluate(() => window.__activeWorkers.size), 0);
      return { ...result, upstreamCanvas: details.upstreamCanvas, bondLines: details.bondLines, fallbacks: details.fallbacks, repeatedMounts: 2, stableLibraryBlobs: libraryBlobs.length };
    },
  },
];
