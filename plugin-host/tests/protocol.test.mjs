import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const hostEntry = resolve(here, '../dist/index.js')

function validManifest() {
  return {
    plugin: 'fixture',
    version: '1.2.3',
    tools: [{
      name: 'fixture_inspect',
      description: 'Inspect one fixture',
      parameters: {
        type: 'object',
        properties: { input_path: { type: 'string' } },
        required: ['input_path'],
        additionalProperties: false,
      },
      scopes: ['dataset_fast_path'],
      timeout_seconds: 120,
    }],
  }
}

async function writeManifest(root, directory, value) {
  const target = join(root, directory)
  await mkdir(target, { recursive: true })
  await writeFile(join(target, 'manifest.json'), JSON.stringify(value), 'utf8')
  await writeFile(join(target, 'handler.py'), '# fixture handler\n', 'utf8')
}

function startClient(child) {
  let nextId = 1
  const pending = new Map()
  const frames = []
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity })
  lines.on('line', (line) => {
    const frame = JSON.parse(line)
    frames.push(frame)
    const waiter = pending.get(frame.id)
    if (waiter) {
      pending.delete(frame.id)
      waiter.resolve(frame)
    }
  })
  child.once('exit', (code) => {
    for (const waiter of pending.values()) {
      waiter.reject(new Error(`plugin host exited before responding (${code})`))
    }
    pending.clear()
  })

  return {
    frames,
    request(method, params = {}) {
      const id = nextId++
      const result = new Promise((resolve, reject) => pending.set(id, { resolve, reject }))
      child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
      return result
    },
    close() {
      lines.close()
      for (const waiter of pending.values()) waiter.reject(new Error('client closed'))
      pending.clear()
    },
  }
}

test('stdio server speaks only NDJSON JSON-RPC and keeps a valid snapshot after failed reload', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dataseek-cordis-protocol-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  await writeManifest(root, 'fixture', validManifest())

  const child = spawn(process.execPath, [hostEntry, '--tools-dir', root], {
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  t.after(() => {
    if (child.exitCode === null) child.kill('SIGKILL')
  })
  const client = startClient(child)
  const stderr = []
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => stderr.push(chunk))

  const health = await client.request('host.health')
  assert.deepEqual(health.result, {
    status: 'ok',
    engine: 'cordis',
    version: '4.0.2',
    revision: health.result.revision,
    manifest_digest: health.result.manifest_digest,
    execution_bundle_digest: health.result.execution_bundle_digest,
    plugin_count: 1,
    tool_count: 1,
  })
  assert.match(health.result.revision, /^[a-f0-9]{64}$/)
  assert.match(health.result.execution_bundle_digest, /^[a-f0-9]{64}$/)

  const initial = await client.request('catalog.snapshot')
  assert.equal(initial.result.tools[0].timeout_seconds, 120)
  assert.equal(initial.result.tools[0].contract_version, 2)
  assert.equal(initial.result.tools[0].output_schema, null)
  assert.deepEqual(initial.result.tools[0].execution, {
    timeout_seconds: 120,
    cancellable: false,
    concurrency: 'exclusive',
    effects: ['sandbox_read', 'sandbox_write'],
    permissions: [],
    credentials: [],
  })
  assert.deepEqual(initial.result.tools[0].presentation, { kind: 'auto' })
  assert.equal(JSON.stringify(initial).includes(root), false)

  const visualizations = await client.request('visualizations.snapshot')
  assert.equal(visualizations.result.engine, 'cordis')
  assert.equal(visualizations.result.plugins.length, 36)
  assert.equal(visualizations.result.plugins.filter(plugin => plugin.contract_version === 2).length, 36)
  const visualizationReload = await client.request('visualizations.reload')
  assert.deepEqual(visualizationReload.result, visualizations.result)

  await writeManifest(root, 'duplicate', {
    ...validManifest(),
    plugin: 'duplicate',
  })
  const failedReload = await client.request('plugins.reload')
  assert.deepEqual(failedReload.error, {
    code: -32010,
    message: 'Plugin catalog candidate rejected',
  })
  assert.equal(JSON.stringify(failedReload).includes(root), false)

  const retained = await client.request('catalog.snapshot')
  assert.deepEqual(retained.result, initial.result)

  const unknown = await client.request('unknown.method')
  assert.deepEqual(unknown.error, { code: -32601, message: 'Method not found' })

  const shutdown = await client.request('shutdown')
  assert.deepEqual(shutdown.result, { status: 'shutting_down' })
  const exitCode = await new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('exit', resolve)
  })
  client.close()

  assert.equal(exitCode, 0)
  assert.equal(stderr.join(''), '')
  assert.equal(client.frames.length, 8)
  for (const frame of client.frames) {
    assert.equal(frame.jsonrpc, '2.0')
    assert.equal(JSON.stringify(frame).includes(root), false)
  }
})
