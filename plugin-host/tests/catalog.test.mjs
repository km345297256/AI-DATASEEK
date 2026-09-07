import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import {
  CatalogRuntime,
  buildCatalogContext,
  captureConsistentCatalogSource,
} from '../dist/catalog.js'
import { computeExecutionBundleDigest } from '../dist/bundle.js'
import { CatalogValidationError } from '../dist/errors.js'
import { loadValidatedManifests } from '../dist/manifest.js'

const here = dirname(fileURLToPath(import.meta.url))
const projectTools = resolve(here, '../../tools')
const portableRegexCorpus = JSON.parse(await readFile(
  resolve(here, '../../backend/tests/fixtures/portable_regex_corpus.json'),
  'utf8',
))

function manifest(plugin, tools, version = '1.0.0') {
  return { plugin, version, handler: 'handler.py', tools }
}

function tool(name, overrides = {}) {
  return {
    name,
    description: `Execute ${name}`,
    parameters: {
      type: 'object',
      properties: { input_path: { type: 'string' } },
      required: ['input_path'],
      additionalProperties: false,
    },
    scopes: ['dataset_fast_path'],
    timeout_seconds: 120,
    ...overrides,
  }
}

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'dataseek-cordis-catalog-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  return root
}

async function writeManifest(root, directory, value) {
  const target = join(root, directory)
  await mkdir(target, { recursive: true })
  await writeFile(join(target, 'manifest.json'), JSON.stringify(value), 'utf8')
  await writeFile(join(target, 'handler.py'), '# fixture handler\n', 'utf8')
}

async function writeRawManifest(root, directory, value) {
  const target = join(root, directory)
  await mkdir(target, { recursive: true })
  await writeFile(join(target, 'manifest.json'), value, 'utf8')
  await writeFile(join(target, 'handler.py'), '# fixture handler\n', 'utf8')
}

test('matches the shared Python/ECMAScript portable regex corpus', async (t) => {
  for (const [index, entry] of portableRegexCorpus.entries()) {
    const root = await fixture(t)
    await writeManifest(root, `portable-${index}`, manifest(`portable-${index}`, [tool(
      `portable_${index}`,
      {
        parameters: {
          type: 'object',
          properties: { value: { type: 'string', pattern: entry.pattern } },
        },
      },
    )]))
    if (entry.accepted) {
      await loadValidatedManifests(root)
    } else {
      await assert.rejects(() => loadValidatedManifests(root), CatalogValidationError)
    }
  }
})

test('credential slots are bounded metadata, require an effect, and cannot override environment', async (t) => {
  const valid = { effects: ['credential_use'], credentials: [{ slot: 'api_key', provider: 'example' }] }
  const root = await fixture(t)
  await writeManifest(root, 'credential', manifest('credential', [tool('credential_read', { execution: valid })]))
  await loadValidatedManifests(root)
  for (const execution of [
    { credentials: valid.credentials },
    { ...valid, credentials: valid.credentials.concat(valid.credentials) },
    { ...valid, credentials: [{ slot: 'PATH', provider: 'example' }] },
    { ...valid, credentials: [{ slot: 'api_key', provider: 'example', env: 'PATH' }] },
    { permissions: ['https://private.example/secret'] },
  ]) {
    await writeManifest(root, 'credential', manifest('credential', [tool('credential_read', { execution })]))
    await assert.rejects(() => loadValidatedManifests(root), CatalogValidationError)
  }
})

test('loads the complete production tool catalog through Cordis', async () => {
  const runtime = new CatalogRuntime(projectTools)
  const snapshot = await runtime.start()
  try {
    assert.equal(snapshot.engine, 'cordis')
    assert.equal(snapshot.version, '4.0.2')
    assert.equal(snapshot.plugin_count, 15)
    assert.equal(snapshot.tool_count, 280)
    assert.match(snapshot.revision, /^[a-f0-9]{64}$/)
    assert.match(snapshot.manifest_digest, /^[a-f0-9]{64}$/)
    assert.match(snapshot.execution_bundle_digest, /^[a-f0-9]{64}$/)

    const longRunning = snapshot.tools.find(({ name }) => name === 'netcdf_mask_by_vector')
    assert.equal(longRunning.timeout_seconds, 120)
    assert.equal(longRunning.contract_version, 2)
    assert.equal(longRunning.output_schema, null)
    assert.deepEqual(longRunning.execution, {
      timeout_seconds: 120,
      cancellable: false,
      concurrency: 'exclusive',
      effects: ['sandbox_read', 'sandbox_write'],
      permissions: [],
      credentials: [],
    })
    assert.deepEqual(longRunning.presentation, { kind: 'auto' })
    assert.deepEqual(Object.keys(longRunning).sort(), [
      'contract_version',
      'description',
      'execution',
      'name',
      'output_schema',
      'parameters',
      'plugin',
      'presentation',
      'scopes',
      'timeout_seconds',
      'version',
    ])
    const presentationInspect = snapshot.tools.find(
      ({ name }) => name === 'presentation_inspect',
    )
    assert.deepEqual(presentationInspect.execution, {
      timeout_seconds: 60,
      cancellable: false,
      concurrency: 'parallel',
      effects: ['sandbox_read'],
      permissions: [],
      credentials: [],
    })
    assert.deepEqual(presentationInspect.presentation, {
      kind: 'generic',
      title: 'Presentation inspection',
    })
    assert.equal(JSON.stringify(snapshot).includes(projectTools), false)
  } finally {
    await runtime.shutdown()
  }
})

test('normalizes explicit Tool Contract v2 metadata', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'reporting', manifest('reporting', [tool(
    'reporting_table',
    {
      timeout_seconds: undefined,
      output_schema: {
        type: 'object',
        properties: {
          rows: { type: 'array', items: { type: 'object' } },
        },
        required: ['rows'],
        additionalProperties: false,
      },
      execution: {
        timeout_seconds: 45,
        cancellable: true,
        concurrency: 'parallel',
        effects: ['sandbox_read', 'network', 'credential_use'],
        permissions: ['dataset:read', 'service:example'],
      },
      presentation: {
        kind: 'table',
        title: 'Analysis rows',
        description: 'A bounded preview of the normalized rows.',
      },
    },
  )]))

  const validated = await loadValidatedManifests(root)
  const definition = validated.plugins[0].tools[0]
  assert.equal(definition.contract_version, 2)
  assert.equal(definition.timeout_seconds, 45)
  assert.deepEqual(definition.output_schema, {
    type: 'object',
    properties: {
      rows: { type: 'array', items: { type: 'object' } },
    },
    required: ['rows'],
    additionalProperties: false,
  })
  assert.deepEqual(definition.execution, {
    timeout_seconds: 45,
    cancellable: true,
    concurrency: 'parallel',
    effects: ['sandbox_read', 'network', 'credential_use'],
    permissions: ['dataset:read', 'service:example'],
    credentials: [],
  })
  assert.deepEqual(definition.presentation, {
    kind: 'table',
    title: 'Analysis rows',
    description: 'A bounded preview of the normalized rows.',
  })
})

test('rejects invalid or ambiguous Tool Contract v2 metadata', async (t) => {
  const cases = [
    {
      name: 'bad-output',
      overrides: { output_schema: { type: 'not-a-json-schema-type' } },
      error: /output_schema is not a valid JSON Schema|output_schema is not a compilable JSON Schema/,
    },
    {
      name: 'timeout-mismatch',
      overrides: { timeout_seconds: 20, execution: { timeout_seconds: 21 } },
      error: /timeout_seconds and execution.timeout_seconds must match/,
    },
    {
      name: 'unsafe-effect',
      overrides: { execution: { effects: ['host_filesystem'] } },
      error: /unsupported execution effect: host_filesystem/,
    },
    {
      name: 'bad-concurrency',
      overrides: { execution: { concurrency: 'unbounded' } },
      error: /execution.concurrency must be "exclusive" or "parallel"/,
    },
    {
      name: 'null-concurrency',
      overrides: { execution: { concurrency: null } },
      error: /execution.concurrency must be "exclusive" or "parallel"/,
    },
    {
      name: 'null-cancellable',
      overrides: { execution: { cancellable: null } },
      error: /execution.cancellable must be a boolean/,
    },
    {
      name: 'unbounded-timeout',
      overrides: { timeout_seconds: 121 },
      error: /timeout_seconds must be an integer from 1 to 120/,
    },
    {
      name: 'wrong-contract-version',
      overrides: { contract_version: 1 },
      error: /contract_version must be 2/,
    },
    {
      name: 'contract-field-typo',
      overrides: { output_shema: { type: 'object' } },
      error: /unsupported tool field: output_shema/,
    },
    {
      name: 'unsupported-schema-dialect',
      overrides: {
        parameters: {
          $schema: 'https://json-schema.org/draft/2020-12/schema',
          type: 'object',
          properties: {},
        },
      },
      error: /parameters must use JSON Schema draft-07/,
    },
    {
      name: 'non-portable-pattern',
      overrides: {
        parameters: {
          type: 'object',
          properties: { value: { type: 'string', pattern: '\\p{L}+' } },
        },
      },
      error: /parameters uses a non-portable regular-expression escape/,
    },
    {
      name: 'absolute-self-reference',
      overrides: {
        parameters: {
          $id: 'https://schemas.example.test/self.json',
          type: 'object',
          definitions: { value: { type: 'string' } },
          properties: {
            value: { $ref: 'https://schemas.example.test/self.json#/definitions/value' },
          },
        },
      },
      error: /parameters references must be local JSON pointers/,
    },
    {
      name: 'noncanonical-array-reference',
      overrides: {
        parameters: {
          type: 'object',
          definitions: {
            choices: {
              anyOf: [{ type: 'string' }, { type: 'number' }],
            },
          },
          properties: {
            value: { $ref: '#/definitions/choices/anyOf/01' },
          },
        },
      },
      error: /parameters contains an unresolved JSON pointer/,
    },
    {
      name: 'duplicate-permission',
      overrides: { execution: { permissions: ['dataset:read', 'dataset:read'] } },
      error: /duplicate execution.permissions entry: dataset:read/,
    },
    {
      name: 'bad-presentation',
      overrides: { presentation: { kind: 'raw_html' } },
      error: /presentation.kind is not supported/,
    },
    {
      name: 'presentation-payload',
      overrides: { presentation: { kind: 'table', data: [] } },
      error: /unsupported presentation field: data/,
    },
  ]

  for (const entry of cases) {
    const root = await fixture(t)
    await writeManifest(
      root,
      entry.name,
      manifest(entry.name, [tool(`${entry.name.replaceAll('-', '_')}_tool`, entry.overrides)]),
    )
    await assert.rejects(loadValidatedManifests(root), entry.error)
  }
})

test('a plugin fiber owns and reverses its CatalogService registration', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'alpha', manifest('alpha', [tool('alpha_read')]))
  await writeManifest(root, 'beta', manifest('beta', [tool('beta_read')]))

  const candidate = await buildCatalogContext(root)
  try {
    assert.equal(candidate.catalog.pluginCount(), 2)
    assert.equal(candidate.pluginFibers.length, 2)
    await candidate.pluginFibers[0].dispose()
    assert.equal(candidate.catalog.pluginCount(), 1)
    const afterDispose = candidate.catalog.createSnapshot(
      '0'.repeat(64),
      '1'.repeat(64),
    )
    assert.deepEqual(afterDispose.plugins.map(({ plugin }) => plugin), ['beta'])
    assert.deepEqual(afterDispose.tools.map(({ name }) => name), ['beta_read'])
  } finally {
    await candidate.context.fiber.dispose()
  }
})

test('reload is all-or-nothing and retains the last valid snapshot', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'alpha', manifest('alpha', [tool('alpha_read')]))
  const runtime = new CatalogRuntime(root)
  const initial = await runtime.start()

  try {
    await writeManifest(root, 'bad', manifest('bad', [tool('alpha_read')]))
    await assert.rejects(runtime.reload(), CatalogValidationError)
    assert.deepEqual(runtime.snapshot(), initial)

    await rm(join(root, 'bad'), { recursive: true, force: true })
    await writeManifest(root, 'beta', manifest('beta', [tool('beta_read')]))
    const reloaded = await runtime.reload()
    assert.notEqual(reloaded.revision, initial.revision)
    assert.equal(reloaded.plugin_count, 2)
    assert.equal(reloaded.tool_count, 2)
  } finally {
    await runtime.shutdown()
  }
})

test('catalog capture retries a manifest change around the execution bundle walk', async () => {
  const alpha = { plugins: [], manifestDigest: 'a'.repeat(64) }
  const beta = { plugins: [], manifestDigest: 'b'.repeat(64) }
  const observed = [alpha, beta, beta, beta]
  let manifestReads = 0
  let bundleReads = 0

  const captured = await captureConsistentCatalogSource(
    '/fixture/tools',
    '/fixture/sandbox',
    {
      async loadManifests() {
        const value = observed[manifestReads]
        manifestReads += 1
        return value
      },
      async computeBundleDigest() {
        bundleReads += 1
        return 'c'.repeat(64)
      },
    },
  )

  assert.equal(captured.validated, beta)
  assert.equal(captured.executionBundleDigest, 'c'.repeat(64))
  assert.equal(manifestReads, 4)
  assert.equal(bundleReads, 2)
})

test('catalog capture fails closed after bounded consecutive manifest changes', async () => {
  let manifestReads = 0
  let bundleReads = 0

  await assert.rejects(
    captureConsistentCatalogSource(
      '/fixture/tools',
      undefined,
      {
        async loadManifests() {
          manifestReads += 1
          return { plugins: [], manifestDigest: String(manifestReads).padStart(64, '0') }
        },
        async computeBundleDigest() {
          bundleReads += 1
          return 'd'.repeat(64)
        },
      },
    ),
    /Tool plugin catalog changed while loading/,
  )

  assert.equal(manifestReads, 6)
  assert.equal(bundleReads, 3)
})

test('reload uses an isolated schema registry for valid schemas with $id', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'identified', manifest('identified', [tool(
    'identified_read',
    {
      parameters: {
        $id: 'https://schemas.example.test/identified-read.json',
        type: 'object',
        properties: { input_path: { type: 'string' } },
        required: ['input_path'],
        additionalProperties: false,
      },
    },
  )]))
  const runtime = new CatalogRuntime(root)
  const initial = await runtime.start()

  try {
    const reloaded = await runtime.reload()
    assert.deepEqual(reloaded, initial)
    assert.equal(reloaded.plugin_count, 1)
    assert.equal(reloaded.tool_count, 1)
  } finally {
    await runtime.shutdown()
  }

  const duplicateRoot = await fixture(t)
  const identifiedParameters = {
    $id: 'https://schemas.example.test/duplicate-id.json',
    type: 'object',
    properties: {},
  }
  await writeManifest(duplicateRoot, 'duplicates', manifest('duplicates', [
    tool('duplicate_id_one', { parameters: identifiedParameters }),
    tool('duplicate_id_two', { parameters: identifiedParameters }),
  ]))
  await assert.rejects(
    loadValidatedManifests(duplicateRoot),
    /parameters is not a compilable JSON Schema/,
  )
})

test('manifest digest matches the sandbox cross-language protocol vector', async (t) => {
  const root = await fixture(t)
  const alpha = '{"plugin":"alpha","version":"1.0.0","handler":"handler.py","tools":[{"name":"alpha_read","description":"Read alpha","parameters":{"type":"object","properties":{}}}]}'
  const beta = '{"plugin":"beta","version":"2.1.0-rc.1","handler":"handler.py","tools":[{"name":"beta_read","description":"Read beta","parameters":{"type":"object","properties":{}}}]}'
  // Deliberately reverse directory and plugin order. The digest is ordered by
  // ASCII plugin identity so Python and JavaScript cannot disagree on Unicode
  // directory-name collation.
  await writeRawManifest(root, 'a-directory', beta)
  await writeRawManifest(root, 'z-directory', alpha)

  const validated = await loadValidatedManifests(root)
  assert.deepEqual(
    validated.plugins.map(({ descriptor }) => descriptor.plugin),
    ['beta', 'alpha'],
  )
  assert.equal(
    validated.manifestDigest,
    'f986ce181c99df9a05172b01f222b6adf5d67d0e191849b3da76e270c46982ad',
  )
})

test('execution bundle digest matches the sandbox cross-language protocol vector', async (t) => {
  const root = await fixture(t)
  const toolsRoot = join(root, 'tools')
  const pluginRoot = join(toolsRoot, 'echo')
  await mkdir(pluginRoot, { recursive: true })
  await writeFile(
    join(pluginRoot, 'manifest.json'),
    '{"plugin":"echo","version":"1.0.0","handler":"handler.py","tools":[{"name":"echo_value"}]}',
  )
  await writeFile(
    join(pluginRoot, 'handler.py'),
    'def build_command(name, arguments): return ["true"]\n',
  )

  const contractRoot = join(root, 'sandbox')
  await mkdir(contractRoot)
  for (const filename of ['Dockerfile', 'pyproject.toml', 'supervisord.conf', 'uv.lock']) {
    await writeFile(join(contractRoot, filename), `fixture:${filename}\n`)
  }
  for (const directory of ['app', 'scientific_operators', 'scripts']) {
    const sourceRoot = join(contractRoot, directory)
    await mkdir(sourceRoot)
    await writeFile(join(sourceRoot, 'runtime.py'), `SOURCE = "${directory}"\n`)
  }

  assert.equal(
    await computeExecutionBundleDigest(toolsRoot, contractRoot),
    '823bc42462e71e25db409afbf989b910d5e5e2e93ccbadc8f19ac9f997dd5ccd',
  )
})

test('prevalidation rejects duplicate plugins before building a context', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'first', manifest('same', [tool('first_tool')]))
  await writeManifest(root, 'second', manifest('same', [tool('second_tool')]))

  await assert.rejects(
    loadValidatedManifests(root),
    /Duplicate plugin name: same/,
  )
})

test('prevalidation rejects duplicate tools and invalid JSON schemas', async (t) => {
  const duplicateRoot = await fixture(t)
  await writeManifest(duplicateRoot, 'first', manifest('first', [tool('same_tool')]))
  await writeManifest(duplicateRoot, 'second', manifest('second', [tool('same_tool')]))
  await assert.rejects(
    loadValidatedManifests(duplicateRoot),
    /Duplicate tool name: same_tool/,
  )

  const invalidRoot = await fixture(t)
  await writeManifest(invalidRoot, 'invalid', manifest('invalid', [tool('invalid_tool', {
    parameters: {
      type: 'object',
      properties: { value: { type: 'not-a-json-schema-type' } },
    },
  })]))
  await assert.rejects(
    loadValidatedManifests(invalidRoot),
    /parameters is not a valid JSON Schema|parameters is not a compilable JSON Schema/,
  )

  const invalidVersionRoot = await fixture(t)
  await writeManifest(
    invalidVersionRoot,
    'invalid-version',
    manifest('invalid-version', [tool('valid_tool')], '1.0.0 unsafe'),
  )
  await assert.rejects(
    loadValidatedManifests(invalidVersionRoot),
    /version must be a printable ASCII release identifier/,
  )

  const reservedNameRoot = await fixture(t)
  await writeManifest(
    reservedNameRoot,
    'reserved-name',
    manifest('reserved-name', [tool('message_ask_user')]),
  )
  await assert.rejects(
    loadValidatedManifests(reservedNameRoot),
    /tool name is reserved by the Agent runtime: message_ask_user/,
  )

  const maximumNameRoot = await fixture(t)
  const maximumName = `t${'o'.repeat(63)}`
  await writeManifest(
    maximumNameRoot,
    'maximum-name',
    manifest('maximum-name', [tool(maximumName)]),
  )
  const maximumNameCatalog = await loadValidatedManifests(maximumNameRoot)
  assert.equal(maximumNameCatalog.plugins[0].tools[0].name, maximumName)

  const oversizedNameRoot = await fixture(t)
  await writeManifest(
    oversizedNameRoot,
    'oversized-name',
    manifest('oversized-name', [tool(`t${'o'.repeat(64)}`)]),
  )
  await assert.rejects(
    loadValidatedManifests(oversizedNameRoot),
    /name must be a 1-64 character model-facing tool name/,
  )

  for (const reservedAlias of [
    'mcp_list_tools', 'shell_read', 'shell_write', 'write_stdin',
    'tool_catalog_search', 'tool_catalog_load', 'code_mode_run',
  ]) {
    const aliasRoot = await fixture(t)
    await writeManifest(
      aliasRoot,
      'reserved-alias',
      manifest('reserved-alias', [tool(reservedAlias)]),
    )
    await assert.rejects(
      loadValidatedManifests(aliasRoot),
      new RegExp(`tool name is reserved by the Agent runtime: ${reservedAlias}`),
    )
  }
})

test('prevalidation rejects missing and escaping plugin handlers', async (t) => {
  const missingRoot = await fixture(t)
  await writeManifest(missingRoot, 'missing', manifest('missing', [tool('missing_tool')]))
  await rm(join(missingRoot, 'missing', 'handler.py'))
  await assert.rejects(
    loadValidatedManifests(missingRoot),
    /handler cannot be read/,
  )

  const escapingRoot = await fixture(t)
  await writeManifest(
    escapingRoot,
    'escaping',
    { ...manifest('escaping', [tool('escaping_tool')]), handler: '../../escape.py' },
  )
  await assert.rejects(
    loadValidatedManifests(escapingRoot),
    /handler must stay inside the plugin directory/,
  )
})

test('prevalidation decodes manifest JSON as strict UTF-8', async (t) => {
  const root = await fixture(t)
  const prefix = Buffer.from(
    '{"plugin":"invalid-utf8","version":"1.0.0","handler":"handler.py","tools":[{"name":"invalid_utf8_tool","description":"',
  )
  const suffix = Buffer.from(
    '","parameters":{"type":"object","properties":{}}}]}',
  )
  await writeRawManifest(
    root,
    'invalid-utf8',
    Buffer.concat([prefix, Buffer.from([0x80]), suffix]),
  )

  await assert.rejects(
    loadValidatedManifests(root),
    /manifest is not valid UTF-8 JSON/,
  )
})

test('reload keeps the active snapshot when a handler becomes invalid', async (t) => {
  const root = await fixture(t)
  await writeManifest(root, 'alpha', manifest('alpha', [tool('alpha_read')]))
  const runtime = new CatalogRuntime(root)
  const initial = await runtime.start()

  try {
    await rm(join(root, 'alpha', 'handler.py'))
    await assert.rejects(runtime.reload(), /handler cannot be read/)
    assert.deepEqual(runtime.snapshot(), initial)
  } finally {
    await runtime.shutdown()
  }
})

test('directories without manifest.json are ignored like the legacy glob', async (t) => {
  const root = await fixture(t)
  await mkdir(join(root, '__pycache__'))
  await writeManifest(root, 'alpha', manifest('alpha', [tool('alpha_read')]))

  const validated = await loadValidatedManifests(root)
  assert.equal(validated.plugins.length, 1)
  assert.equal(validated.plugins[0].descriptor.plugin, 'alpha')
})
