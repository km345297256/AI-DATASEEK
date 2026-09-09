import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm, readFile, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

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

test('14 legacy and 20 extended visualization contracts load as real Cordis registrations', async () => {
  const context = await buildVisualizationContext(productionDirectory)
  try {
    assert.equal(context.snapshot.engine, 'cordis')
    assert.equal(context.pluginFibers.length, 34)
    assert.equal(context.catalog.snapshot().plugins.length, 34)
    assert.equal(context.snapshot.plugins.filter(item => item.contract_version === 1).length, 14)
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
  ['unknown protocol version', { contract_version: 2 }],
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
