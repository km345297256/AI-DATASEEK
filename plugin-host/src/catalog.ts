import { Context, Service, type Fiber, type Plugin } from '@deepseek-ai/cordis'

import { CatalogValidationError } from './errors.js'
import { computeExecutionBundleDigest } from './bundle.js'
import { cloneJson, deepFreeze, sha256, stableStringify } from './json.js'
import { loadValidatedManifests } from './manifest.js'
import {
  CORDIS_ENGINE,
  CORDIS_VERSION,
  type CatalogSnapshot,
  type HealthSnapshot,
  type ValidatedPlugin,
} from './types.js'
import type { ValidatedManifestSet } from './manifest.js'
import { VisualizationRuntime } from './visualizations.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    dataSeekCatalog: CatalogService
  }
}

interface Registration {
  token: symbol
  plugin: ValidatedPlugin
}

/** Cordis-owned registry. Every registration belongs to the caller's fiber. */
export class CatalogService extends Service {
  private readonly registrations = new Map<string, Registration>()

  constructor(ctx: Context) {
    super(ctx, 'dataSeekCatalog')
  }

  register(owner: Context, plugin: ValidatedPlugin): void {
    const name = plugin.descriptor.plugin
    if (this.registrations.has(name)) {
      throw new CatalogValidationError(`Duplicate plugin registration: ${name}`)
    }
    const token = Symbol(name)
    const immutablePlugin = deepFreeze(cloneJson(plugin))

    owner.effect(() => {
      this.registrations.set(name, { token, plugin: immutablePlugin })
      return () => {
        const current = this.registrations.get(name)
        if (current?.token === token) this.registrations.delete(name)
      }
    })
  }

  pluginCount(): number {
    return this.registrations.size
  }

  createSnapshot(
    manifestDigest: string,
    executionBundleDigest: string,
  ): CatalogSnapshot {
    const registered = [...this.registrations.values()].map(({ plugin }) => plugin)
    const plugins = registered.map(({ descriptor }) => cloneJson(descriptor))
    const tools = registered.flatMap(({ tools: items }) => cloneJson(items))
    const revision = sha256(stableStringify({
      engine: CORDIS_ENGINE,
      version: CORDIS_VERSION,
      manifest_digest: manifestDigest,
      execution_bundle_digest: executionBundleDigest,
      plugins,
      tools,
    }))
    return deepFreeze({
      engine: CORDIS_ENGINE,
      version: CORDIS_VERSION,
      revision,
      manifest_digest: manifestDigest,
      execution_bundle_digest: executionBundleDigest,
      plugin_count: plugins.length,
      tool_count: tools.length,
      plugins,
      tools,
    })
  }
}

function createManifestPlugin(plugin: ValidatedPlugin): Plugin.Function<void> {
  const apply = ((ctx: Context) => {
    ctx.dataSeekCatalog.register(ctx, plugin)
  }) as Plugin.Function<void>
  apply.inject = ['dataSeekCatalog']
  Object.defineProperty(apply, 'name', {
    configurable: true,
    value: `dataseek-plugin-${plugin.descriptor.plugin}`,
  })
  return apply
}

export interface CatalogContext {
  context: Context
  catalog: CatalogService
  pluginFibers: Fiber[]
  snapshot: CatalogSnapshot
}

interface CatalogSourceCapture {
  validated: ValidatedManifestSet
  executionBundleDigest: string
}

interface CatalogSourceLoaders {
  loadManifests: typeof loadValidatedManifests
  computeBundleDigest: typeof computeExecutionBundleDigest
}

const MAX_SOURCE_CAPTURE_ATTEMPTS = 3

/**
 * Capture a self-consistent catalog source generation.
 *
 * Manifest validation and execution-bundle hashing intentionally use separate
 * filesystem walks. Re-reading the manifests after the bundle walk prevents a
 * concurrent manifest replacement from pairing definitions from one generation
 * with the digest of another. A moving tree is retried only a bounded number of
 * times so reload remains fail-closed instead of waiting forever.
 */
export async function captureConsistentCatalogSource(
  toolsDirectory: string,
  executionContractDirectory?: string,
  loaders: CatalogSourceLoaders = {
    loadManifests: loadValidatedManifests,
    computeBundleDigest: computeExecutionBundleDigest,
  },
): Promise<CatalogSourceCapture> {
  for (let attempt = 0; attempt < MAX_SOURCE_CAPTURE_ATTEMPTS; attempt += 1) {
    const before = await loaders.loadManifests(toolsDirectory)
    const executionBundleDigest = await loaders.computeBundleDigest(
      toolsDirectory,
      executionContractDirectory,
    )
    const after = await loaders.loadManifests(toolsDirectory)
    if (before.manifestDigest === after.manifestDigest) {
      return { validated: after, executionBundleDigest }
    }
  }
  throw new CatalogValidationError(
    'Tool plugin catalog changed while loading; retry the reload',
  )
}

/** Build an isolated candidate. The caller decides when it becomes active. */
export async function buildCatalogContext(
  toolsDirectory: string,
  executionContractDirectory?: string,
): Promise<CatalogContext> {
  // This is deliberately completed before Context construction. A bad catalog
  // must not partially execute even one plugin.
  const { validated, executionBundleDigest } = await captureConsistentCatalogSource(
    toolsDirectory,
    executionContractDirectory,
  )
  const context = new Context()
  const pluginFibers: Fiber[] = []
  try {
    await context.plugin(CatalogService)
    for (const plugin of validated.plugins) {
      pluginFibers.push(await context.plugin(createManifestPlugin(plugin)))
    }
    const catalog = context.dataSeekCatalog
    if (catalog.pluginCount() !== validated.plugins.length) {
      throw new Error('Cordis catalog did not activate every validated plugin')
    }
    return {
      context,
      catalog,
      pluginFibers,
      snapshot: catalog.createSnapshot(
        validated.manifestDigest,
        executionBundleDigest,
      ),
    }
  } catch (error) {
    await context.fiber.dispose().catch(() => undefined)
    throw error
  }
}

export type HostLogger = (message: string, error?: unknown) => void

export class CatalogRuntime {
  readonly visualizations: VisualizationRuntime
  private current: CatalogContext | undefined
  private operation: Promise<void> = Promise.resolve()
  private stopped = false

  constructor(
    readonly toolsDirectory: string,
    private readonly log: HostLogger = () => undefined,
    readonly executionContractDirectory?: string,
    visualizationsDirectory?: string,
  ) {
    this.visualizations = new VisualizationRuntime(visualizationsDirectory)
  }

  async start(): Promise<CatalogSnapshot> {
    return this.reload()
  }

  snapshot(): CatalogSnapshot {
    if (!this.current) throw new Error('Plugin catalog is not initialized')
    return cloneJson(this.current.snapshot)
  }

  health(): HealthSnapshot {
    const snapshot = this.snapshot()
    return {
      status: 'ok',
      engine: snapshot.engine,
      version: snapshot.version,
      revision: snapshot.revision,
      manifest_digest: snapshot.manifest_digest,
      execution_bundle_digest: snapshot.execution_bundle_digest,
      plugin_count: snapshot.plugin_count,
      tool_count: snapshot.tool_count,
    }
  }

  reload(): Promise<CatalogSnapshot> {
    if (this.stopped) return Promise.reject(new Error('Plugin host is shutting down'))
    const result = this.operation.then(() => this.reloadNow())
    this.operation = result.then(() => undefined, () => undefined)
    return result
  }

  private async reloadNow(): Promise<CatalogSnapshot> {
    const candidate = await buildCatalogContext(
      this.toolsDirectory,
      this.executionContractDirectory,
    )
    const previous = this.current
    // A complete candidate becomes visible in one assignment. Failed builds
    // never reach this point, so the last valid snapshot remains active.
    this.current = candidate
    if (previous) {
      try {
        await previous.context.fiber.dispose()
      } catch (error) {
        this.log('Previous Cordis context failed to dispose cleanly', error)
      }
    }
    return this.snapshot()
  }

  async shutdown(): Promise<void> {
    if (this.stopped) return
    this.stopped = true
    await this.operation
    const current = this.current
    this.current = undefined
    if (current) await current.context.fiber.dispose()
    await this.visualizations.shutdown()
  }
}
