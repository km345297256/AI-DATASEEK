import { constants } from 'node:fs'
import { open, readdir } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Context, Service, type Fiber, type Plugin } from '@deepseek-ai/cordis'

import { CatalogValidationError } from './errors.js'
import { cloneJson, deepFreeze, sha256, stableStringify } from './json.js'
import { VISUALIZATION_ADAPTERS, VISUALIZATION_CONTRACT_VERSION, type VisualizationAdapter,
  type VisualizationReader, type VisualizationOperation, type VisualizationInputMode,
  type VisualizationKind } from './visualization-adapters.generated.js'
export { VISUALIZATION_ADAPTERS } from './visualization-adapters.generated.js'

export interface VisualizationCapabilities {
  operations: VisualizationOperation[]
  input_mode: VisualizationInputMode
  shared: boolean
}

export interface VisualizationDescriptor {
  contract_version: 2
  id: string
  version: string
  name: string
  description: string
  extensions: string[]
  filenames: string[]
  view_kind: VisualizationKind
  adapter: VisualizationAdapter
  reader: VisualizationReader
  capabilities: VisualizationCapabilities
  default_enabled: boolean
  priority: number
  permissions: ['file:read']
  limits: { max_input_bytes: number; max_output_bytes: number }
}

export interface VisualizationSnapshot {
  engine: 'cordis'
  revision: string
  plugins: VisualizationDescriptor[]
}

const FIELDS = new Set(['contract_version', 'id', 'version', 'name', 'description',
  'extensions', 'filenames', 'view_kind', 'adapter', 'reader', 'capabilities',
  'default_enabled', 'priority', 'permissions', 'limits'])
const DEFAULT_DIRECTORY = resolve(dirname(fileURLToPath(import.meta.url)), '../visualizations')
const MAX_MANIFEST_BYTES = 16 * 1024
const MAX_MANIFESTS = 256

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function reject(): never {
  throw new CatalogValidationError('Visualization manifest failed contract validation')
}

/** The descriptor is data only: it cannot introduce executable code or URLs. */
export function validateVisualization(value: unknown): VisualizationDescriptor {
  if (!record(value) || Object.keys(value).length !== FIELDS.size
    || Object.keys(value).some(key => !FIELDS.has(key))) reject()
  if (value.contract_version !== VISUALIZATION_CONTRACT_VERSION || typeof value.id !== 'string'
    || !/^[a-z][a-z0-9-]{0,63}$/.test(value.id)
    || typeof value.version !== 'string' || !/^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/.test(value.version)
    || typeof value.name !== 'string' || !value.name.trim() || value.name.length > 120
    || typeof value.description !== 'string' || value.description.length > 1000
    || typeof value.default_enabled !== 'boolean'
    || !Number.isSafeInteger(value.priority) || Math.abs(value.priority as number) > 1000) reject()
  for (const [field, pattern] of [
    ['extensions', /^[a-z0-9][a-z0-9.-]{0,31}$/],
    ['filenames', /^[a-z0-9][a-z0-9._-]{0,127}$/],
  ] as const) {
    const items = value[field]
    if (!Array.isArray(items) || items.length > 128 || new Set(items).size !== items.length
      || items.some(item => typeof item !== 'string' || !pattern.test(item))) reject()
  }
  if (!(value.extensions as unknown[]).length && !(value.filenames as unknown[]).length) reject()
  if (typeof value.adapter !== 'string' || !Object.hasOwn(VISUALIZATION_ADAPTERS, value.adapter)) reject()
  const spec = VISUALIZATION_ADAPTERS[value.adapter as keyof typeof VISUALIZATION_ADAPTERS]
  if (value.view_kind !== spec.view_kind || !(spec.readers as readonly unknown[]).includes(value.reader)) reject()
  if (!record(value.capabilities)
    || Object.keys(value.capabilities).sort().join(',') !== 'input_mode,operations,shared'
    || !Array.isArray(value.capabilities.operations)
    || value.capabilities.operations.length !== spec.capabilities.operations.length
    || new Set(value.capabilities.operations).size !== value.capabilities.operations.length
    || value.capabilities.operations.some(operation => !(spec.capabilities.operations as readonly unknown[]).includes(operation))
    || value.capabilities.input_mode !== spec.capabilities.input_mode
    || value.capabilities.shared !== spec.capabilities.shared) reject()
  if (!Array.isArray(value.permissions) || value.permissions.length !== 1
    || value.permissions[0] !== 'file:read') reject()
  if (!record(value.limits) || Object.keys(value.limits).sort().join(',') !== 'max_input_bytes,max_output_bytes') reject()
  for (const [key, maximum] of [['max_input_bytes', 512 * 1024 * 1024], ['max_output_bytes', 16 * 1024 * 1024]] as const) {
    const limit = value.limits[key]
    if (!Number.isSafeInteger(limit) || (limit as number) < 1 || (limit as number) > maximum) reject()
  }
  return cloneJson(value) as unknown as VisualizationDescriptor
}

export async function loadVisualizationManifests(directory: string): Promise<VisualizationDescriptor[]> {
  const entries = (await readdir(directory, { withFileTypes: true }))
    .filter(entry => entry.name.endsWith('.json')).sort((a, b) => a.name.localeCompare(b.name))
  if (!entries.length || entries.length > MAX_MANIFESTS) reject()
  const plugins: VisualizationDescriptor[] = []
  for (const entry of entries) {
    if (!entry.isFile()) reject()
    const handle = await open(join(directory, entry.name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    try {
      const info = await handle.stat()
      if (!info.isFile() || info.size > MAX_MANIFEST_BYTES) reject()
      const bytes = Buffer.alloc(MAX_MANIFEST_BYTES + 1)
      const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0)
      if (bytesRead > MAX_MANIFEST_BYTES) reject()
      const decoded = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, bytesRead))
      plugins.push(validateVisualization(JSON.parse(decoded)))
    } finally {
      await handle.close()
    }
  }
  if (new Set(plugins.map(plugin => plugin.id)).size !== plugins.length) reject()
  return plugins.sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
}

declare module '@deepseek-ai/cordis' {
  interface Context { dataSeekVisualizations: VisualizationCatalogService }
}

/** Each adapter registration is owned and disposed by its actual Cordis fiber. */
export class VisualizationCatalogService extends Service {
  private readonly registrations = new Map<string, { token: symbol; plugin: VisualizationDescriptor }>()
  constructor(ctx: Context) { super(ctx, 'dataSeekVisualizations') }
  register(owner: Context, input: VisualizationDescriptor): void {
    const plugin = deepFreeze(validateVisualization(input))
    if (this.registrations.has(plugin.id)) reject()
    const token = Symbol(plugin.id)
    owner.effect(() => {
      this.registrations.set(plugin.id, { token, plugin })
      return () => {
        if (this.registrations.get(plugin.id)?.token === token) this.registrations.delete(plugin.id)
      }
    })
  }
  snapshot(): VisualizationSnapshot {
    const plugins = [...this.registrations.values()].map(item => cloneJson(item.plugin))
      .sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
    return deepFreeze({ engine: 'cordis', revision: sha256(stableStringify({ contract_version: VISUALIZATION_CONTRACT_VERSION, plugins })), plugins })
  }
}

export async function buildVisualizationContext(directory: string) {
  const plugins = await loadVisualizationManifests(directory)
  const context = new Context()
  const pluginFibers: Fiber[] = []
  try {
    await context.plugin(VisualizationCatalogService)
    for (const descriptor of plugins) {
      const apply = ((ctx: Context) => ctx.dataSeekVisualizations.register(ctx, descriptor)) as Plugin.Function<void>
      apply.inject = ['dataSeekVisualizations']
      Object.defineProperty(apply, 'name', { value: `dataseek-visualization-${descriptor.id}` })
      pluginFibers.push(await context.plugin(apply))
    }
    const catalog = context.dataSeekVisualizations
    if (catalog.snapshot().plugins.length !== plugins.length) reject()
    return { context, pluginFibers, catalog, snapshot: catalog.snapshot() }
  } catch (error) {
    await context.fiber.dispose().catch(() => undefined)
    throw error
  }
}

/** Independent control plane: visualization reload never changes the Agent tool digest. */
export class VisualizationRuntime {
  private current: Awaited<ReturnType<typeof buildVisualizationContext>> | undefined
  private operation: Promise<void> = Promise.resolve()
  private stopped = false
  constructor(readonly directory = DEFAULT_DIRECTORY) {}
  async snapshot(): Promise<VisualizationSnapshot> {
    if (this.stopped) throw new Error('Visualization runtime stopped')
    if (!this.current) return this.reload()
    return cloneJson(this.current.snapshot)
  }
  reload(): Promise<VisualizationSnapshot> {
    if (this.stopped) return Promise.reject(new Error('Visualization runtime stopped'))
    const result = this.operation.then(async () => {
      const candidate = await buildVisualizationContext(this.directory)
      const previous = this.current
      this.current = candidate
      if (previous) await previous.context.fiber.dispose().catch(() => undefined)
      return cloneJson(candidate.snapshot)
    })
    this.operation = result.then(() => undefined, () => undefined)
    return result
  }
  async shutdown(): Promise<void> {
    this.stopped = true
    await this.operation
    await this.current?.context.fiber.dispose()
    this.current = undefined
  }
}
