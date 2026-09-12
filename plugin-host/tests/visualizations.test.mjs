import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm, readFile, symlink, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { execFileSync } from 'node:child_process'

import { CatalogRuntime } from '../dist/catalog.js'
import { dispatchRequest } from '../dist/protocol.js'
import { buildVisualizationContext, loadVisualizationManifests, validateVisualization, VisualizationRuntime } from '../dist/visualizations.js'

const productionDirectory = new URL('../visualizations/', import.meta.url).pathname
const fixture = JSON.parse(await readFile(new URL('../visualizations/netcdf-map.json', import.meta.url), 'utf8'))

async function directory(t, manifest = fixture) {
  const path = await mkdtemp(join(tmpdir(), 'dataseek-visualizations-'))
  t.after(() => rm(path, { recursive: true, force: true }))
  await writeFile(join(path, 'map.json'), JSON.stringify(manifest))
  return path
}

test('all visualization plugins use one capability contract as real Cordis registrations', async () => {
  const context = await buildVisualizationContext(productionDirectory)
  try {
    assert.equal(context.snapshot.engine, 'cordis')
    assert.equal(context.pluginFibers.length, 81)
    assert.equal(context.catalog.snapshot().plugins.length, 81)
    assert.equal(context.snapshot.plugins.filter(item => item.contract_version === 2).length, 81)
    assert.ok(context.snapshot.plugins.every(item => !item.adapter.startsWith('v2-') && !('data_kind' in item)))
    assert.equal(context.snapshot.plugins.filter(item => item.capabilities.shared).length, 9)
    assert.match(context.snapshot.revision, /^[a-f0-9]{64}$/)
    assert.deepEqual(context.snapshot.plugins.filter(item => item.reader === 'netcdf').map(item => item.view_kind).sort(), ['map', 'series'])
  } finally { await context.context.fiber.dispose() }
})

test('disposing one visualization Cordis fiber removes only its registration', async () => {
  const context = await buildVisualizationContext(productionDirectory)
  try {
    const before = context.catalog.snapshot()
    await context.pluginFibers[0].dispose()
    const after = context.catalog.snapshot()
    assert.equal(after.plugins.length, before.plugins.length - 1)
    assert.notEqual(after.revision, before.revision)
    await context.context.fiber.dispose()
    assert.equal(context.catalog.snapshot().plugins.length, 0)
  } finally { await context.context.fiber.dispose() }
})

for (const id of ['viz-columnar-window', 'viz-nexus-window', 'viz-scientific-graph', 'viz-phylogeny', 'viz-envi-window', 'viz-grib-window', 'viz-seismic-window', 'viz-mass-spectrum', 'viz-diffraction', 'viz-fcs-window', 'viz-ripple-window', 'viz-dicom-window', 'viz-spatial-window', 'viz-pointcloud-window', 'viz-gro-trajectory', 'viz-simulation-mesh', 'viz-sqlite-table', 'viz-radar-window', 'viz-ugrid-window', 'viz-duckdb-table', 'viz-dbf-table', 'viz-access-table', 'viz-sql-dump', 'viz-postgres-dump', 'viz-bson', 'viz-redis-rdb']) {
  test(`new domain plugin ${id} is owned by its own real Cordis fiber`, async () => {
    const context = await buildVisualizationContext(productionDirectory)
    try {
      const before = context.catalog.snapshot()
      const index = before.plugins.findIndex(plugin => plugin.id === id)
      assert.ok(index >= 0)
      await context.pluginFibers[index].dispose()
      const after = context.catalog.snapshot()
      assert.deepEqual(after.plugins.map(plugin => plugin.id), before.plugins.filter(plugin => plugin.id !== id).map(plugin => plugin.id))
      assert.notEqual(after.revision, before.revision)
      assert.equal(after.plugins.filter(plugin => plugin.reader === 'netcdf').length, 2)
    } finally { await context.context.fiber.dispose() }
  })
}

test('visualization migration retains every former builtin file suffix and special filename', async () => {
  const plugins = await loadVisualizationManifests(productionDirectory)
  const extensions = new Set(plugins.flatMap(plugin => plugin.extensions))
  for (const extension of [
    'py', 'js', 'ts', 'jsx', 'tsx', 'vue', 'java', 'c', 'cpp', 'h', 'hpp',
    'go', 'rust', 'php', 'ruby', 'swift', 'kotlin', 'scala', 'haskell',
    'erlang', 'elixir', 'ocaml', 'fsharp', 'dart', 'julia', 'lua', 'perl',
    'r', 'sh', 'bash', 'css', 'scss', 'sass', 'less', 'txt', 'xml', 'json',
    'yaml', 'yml', 'sql', 'dockerfile', 'toml', 'ini', 'conf', 'jpg',
    'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'ico', 'heic', 'heif',
    'md', 'csv', 'tsv', 'html', 'htm', 'tif', 'tiff', 'shp', 'shx',
    'dbf', 'prj', 'cpg', 'cif', 'pdb', 'ent', 'mol', 'sdf', 'xyz',
    'mol2', 'vasp', 'obj',
  ]) assert.ok(extensions.has(extension), `missing migrated suffix: ${extension}`)
  assert.deepEqual(plugins.find(plugin => plugin.id === 'molecular').filenames, ['poscar', 'contcar'])
  assert.deepEqual(plugins.find(plugin => plugin.id === 'fastq-quality').extensions, ['fastq', 'fq'])
  assert.equal(plugins.find(plugin => plugin.id === 'text').limits.max_input_bytes, 65536)
  assert.equal(plugins.find(plugin => plugin.id === 'csv').limits.max_input_bytes, 131072)
})

for (const [name, update] of [
  ['untrusted script entry', { entry: 'https://attacker.test/plugin.js' }],
  ['untrusted adapter', { adapter: 'https://attacker.test/plugin.js' }],
  ['unsupported former protocol version', { contract_version: 1 }],
  ['unknown protocol version', { contract_version: 3 }],
  ['coerced protocol version', { contract_version: '2' }],
  ['legacy data classification', { data_kind: 'scientific' }],
  ['null reader', { reader: null }],
  ['empty operations', { capabilities: { operations: [], input_mode: 'whole', shared: false } }],
  ['unknown operation', { capabilities: { operations: ['eval'], input_mode: 'whole', shared: false } }],
  ['duplicate operations', { capabilities: { operations: ['preview', 'preview'], input_mode: 'whole', shared: false } }],
  ['unapproved operation', { capabilities: { operations: ['bytes'], input_mode: 'whole', shared: false } }],
  ['unapproved prefix reading', { capabilities: { operations: ['preview'], input_mode: 'prefix', shared: false } }],
  ['unapproved shared access', { capabilities: { operations: ['preview'], input_mode: 'whole', shared: true } }],
  ['unknown capability field', { capabilities: { operations: ['preview'], input_mode: 'whole', shared: false, execute: true } }],
  ['missing capability field', { capabilities: { operations: ['preview'], input_mode: 'whole' } }],
  ['coerced capability boolean', { capabilities: { operations: ['preview'], input_mode: 'whole', shared: 0 } }],
  ['coerced boolean', { default_enabled: 'false' }],
  ['unsafe wildcard', { extensions: ['*'] }],
  ['duplicate extension', { extensions: ['nc', 'nc'] }],
  ['path matcher', { filenames: ['../private'] }],
  ['reader mismatch', { reader: 'fits' }],
  ['view mismatch', { view_kind: 'table' }],
  ['write permission', { permissions: ['file:read', 'file:write'] }],
  ['unbounded output', { limits: { max_input_bytes: 100, max_output_bytes: 0 } }],
]) {
  test(`visualization contract rejects ${name}`, () => {
    assert.throws(() => validateVisualization({ ...structuredClone(fixture), ...update }))
  })
}

test('single approved adapter source and every independent build copy remain synchronized', () => {
  execFileSync(process.execPath, [new URL('../../scripts/sync-visualization-contract.mjs', import.meta.url).pathname, '--check'])
})

test('generated contract verification catches drift and rejects malformed source without changing production files', async t => {
  const root = await mkdtemp(join(tmpdir(), 'dataseek-viz-generated-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const paths = ['scripts/sync-visualization-contract.mjs', 'contracts/visualization-adapters.json',
    'plugin-host/src/visualization-adapters.generated.ts', 'frontend/src/visualizations/adapters.generated.ts',
    'backend/app/domain/models/visualization_adapters_generated.py']
  for (const path of paths) {
    await mkdir(dirname(join(root, path)), { recursive: true })
    await writeFile(join(root, path), await readFile(new URL(`../../${path}`, import.meta.url)))
  }
  const check = () => execFileSync(process.execPath, [join(root, paths[0]), '--check'], { stdio: 'pipe' })
  check()
  for (const path of paths.slice(2)) {
    const before = await readFile(join(root, path), 'utf8')
    await writeFile(join(root, path), before + '\n// drift\n')
    assert.throws(check, /generated files differ/)
    await writeFile(join(root, path), before)
  }
  const source = JSON.parse(await readFile(join(root, paths[1]), 'utf8'))
  source.adapters.image.capabilities.execute = 'untrusted'
  await writeFile(join(root, paths[1]), JSON.stringify(source))
  assert.throws(check, /Invalid visualization adapter specification/)
})

test('unified operations preserve bounded page/prefix readers and explicit full quality jobs', async () => {
  const plugins = await loadVisualizationManifests(productionDirectory)
  const byId = new Map(plugins.map(plugin => [plugin.id, plugin]))
  assert.deepEqual(byId.get('text').capabilities, { operations: ['page'], input_mode: 'page', shared: true })
  assert.deepEqual(byId.get('csv').capabilities, { operations: ['page'], input_mode: 'page', shared: true })
  assert.deepEqual(byId.get('shapefile').capabilities.operations, ['bytes'])
  assert.deepEqual(byId.get('molecular').capabilities.operations, ['prepare', 'bytes'])
  assert.deepEqual(byId.get('fastq-quality').capabilities, { operations: ['preview'], input_mode: 'prefix', shared: false })
  assert.deepEqual(byId.get('viz-fastqc').capabilities, { operations: ['job'], input_mode: 'whole', shared: false })
})

test('visualization reload commits atomically, keeps old catalog after rejection, and releases contexts', async t => {
  const path = await directory(t)
  const runtime = new VisualizationRuntime(path)
  t.after(() => runtime.shutdown())
  const before = await runtime.snapshot()
  await writeFile(join(path, 'map.json'), JSON.stringify({ ...fixture, entry: 'bad.js' }))
  await assert.rejects(runtime.reload())
  assert.deepEqual(await runtime.snapshot(), before)
  await writeFile(join(path, 'map.json'), JSON.stringify({ ...fixture, name: 'Updated map' }))
  const after = await runtime.reload()
  assert.notEqual(after.revision, before.revision)
  assert.equal(after.plugins[0].name, 'Updated map')
  await runtime.shutdown()
  await assert.rejects(runtime.snapshot())
})

test('visualization loader rejects duplicate IDs, symlinks and oversized manifests', async t => {
  const path = await directory(t)
  await writeFile(join(path, 'duplicate.json'), JSON.stringify(fixture))
  await assert.rejects(loadVisualizationManifests(path))
  await rm(join(path, 'duplicate.json'))
  await symlink(join(path, 'map.json'), join(path, 'symlink.json'))
  await assert.rejects(loadVisualizationManifests(path))
  await rm(join(path, 'symlink.json'))
  await writeFile(join(path, 'map.json'), ' '.repeat(20 * 1024))
  await assert.rejects(loadVisualizationManifests(path))
})

test('separate visualization RPC revisions never alter Agent tool catalog semantics', async t => {
  const path = await directory(t)
  const tools = await mkdtemp(join(tmpdir(), 'dataseek-viz-tools-'))
  t.after(() => rm(tools, { recursive: true, force: true }))
  const runtime = new CatalogRuntime(tools, undefined, undefined, path)
  t.after(() => runtime.shutdown())
  const toolsBefore = await runtime.start()
  const before = await dispatchRequest(runtime, { jsonrpc: '2.0', id: 1, method: 'visualizations.snapshot' })
  await writeFile(join(path, 'map.json'), JSON.stringify({ ...fixture, priority: 77 }))
  const after = await dispatchRequest(runtime, { jsonrpc: '2.0', id: 2, method: 'visualizations.reload' })
  assert.notEqual(before.result.revision, after.result.revision)
  assert.deepEqual(runtime.snapshot(), toolsBefore)
  await assert.rejects(dispatchRequest(runtime, { jsonrpc: '2.0', method: 'visualizations.reload', params: { path: '/private' } }))
})
