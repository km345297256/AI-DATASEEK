#!/usr/bin/env node

import { resolve } from 'node:path'

import { CatalogRuntime } from './catalog.js'
import { runProtocolServer } from './protocol.js'

function log(message: string, error?: unknown): void {
  // The supervisor forwards stderr into production logs. Never print stacks
  // or exception messages here: both may contain host paths or request data.
  const detail = error instanceof Error ? ` error_type=${error.name}` : ''
  process.stderr.write(`[plugin-host] ${message}${detail}\n`)
}

interface HostOptions {
  toolsDirectory: string
  executionContractDirectory: string | undefined
  visualizationsDirectory: string | undefined
}

function parseOptions(argv: string[]): HostOptions {
  let toolsDirectory: string | undefined
  let executionContractDirectory: string | undefined
  let visualizationsDirectory: string | undefined
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--visualizations-dir') {
      const value = argv[index + 1]
      if (!value) throw new Error('--visualizations-dir requires a value')
      visualizationsDirectory = resolve(value)
      index += 1
      continue
    }
    if (argument === '--tools-dir') {
      const value = argv[index + 1]
      if (!value) throw new Error('--tools-dir requires a value')
      toolsDirectory = value
      index += 1
      continue
    }
    if (argument === '--execution-contract-dir') {
      const value = argv[index + 1]
      if (!value) throw new Error('--execution-contract-dir requires a value')
      executionContractDirectory = value
      index += 1
      continue
    }
    throw new Error(`Unknown argument: ${argument}`)
  }
  return {
    visualizationsDirectory,
    toolsDirectory: resolve(
      toolsDirectory ?? process.env.TOOL_PLUGINS_DIR ?? resolve(process.cwd(), 'tools'),
    ),
    executionContractDirectory: executionContractDirectory
      ? resolve(executionContractDirectory)
      : undefined,
  }
}

function assertSupportedNode(): void {
  const [majorText, minorText] = process.versions.node.split('.')
  const major = Number(majorText)
  const minor = Number(minorText)
  if (major < 22 || (major === 22 && minor < 19)) {
    throw new Error('Node.js 22.19.0 or newer is required')
  }
}

async function main(): Promise<void> {
  assertSupportedNode()
  const options = parseOptions(process.argv.slice(2))
  const runtime = new CatalogRuntime(
    options.toolsDirectory,
    log,
    options.executionContractDirectory,
    options.visualizationsDirectory,
  )
  await runtime.start()

  let signalled = false
  const stopFromSignal = (signal: NodeJS.Signals) => {
    if (signalled) return
    signalled = true
    log(`received ${signal}`)
    void runtime.shutdown().finally(() => process.exit(0))
  }
  process.once('SIGINT', stopFromSignal)
  process.once('SIGTERM', stopFromSignal)

  await runProtocolServer(runtime, { log })
}

main().catch((error: unknown) => {
  log('fatal startup error', error)
  process.exitCode = 1
})
