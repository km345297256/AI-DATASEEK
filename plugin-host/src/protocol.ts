import { createInterface } from 'node:readline'

import { CatalogRuntime } from './catalog.js'
import { CatalogValidationError, ProtocolError } from './errors.js'
import type {
  JsonRpcErrorObject,
  JsonRpcId,
  JsonRpcRequest,
  JsonRpcResponse,
} from './types.js'

const MAX_REQUEST_BYTES = 1024 * 1024

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function validateRequest(value: unknown): JsonRpcRequest {
  if (!isRecord(value) || value.jsonrpc !== '2.0' || typeof value.method !== 'string') {
    throw new ProtocolError(-32600, 'Invalid Request')
  }
  if (
    'id' in value
    && value.id !== null
    && typeof value.id !== 'string'
    && typeof value.id !== 'number'
  ) {
    throw new ProtocolError(-32600, 'Invalid Request')
  }
  return value as unknown as JsonRpcRequest
}

function validateNoParams(params: unknown): void {
  if (params === undefined || params === null) return
  if (isRecord(params) && Object.keys(params).length === 0) return
  throw new ProtocolError(-32602, 'Invalid params')
}

export interface DispatchResult {
  result: unknown
  shutdown: boolean
}

export async function dispatchRequest(
  runtime: CatalogRuntime,
  request: JsonRpcRequest,
): Promise<DispatchResult> {
  validateNoParams(request.params)
  switch (request.method) {
    case 'host.health':
      return { result: runtime.health(), shutdown: false }
    case 'catalog.snapshot':
      return { result: runtime.snapshot(), shutdown: false }
    case 'plugins.reload':
      return { result: await runtime.reload(), shutdown: false }
    case 'visualizations.snapshot':
      return { result: await runtime.visualizations.snapshot(), shutdown: false }
    case 'visualizations.reload':
      return { result: await runtime.visualizations.reload(), shutdown: false }
    case 'shutdown':
      return { result: { status: 'shutting_down' }, shutdown: true }
    default:
      throw new ProtocolError(-32601, 'Method not found')
  }
}

function safeError(
  error: unknown,
  log: (message: string, error?: unknown) => void,
): JsonRpcErrorObject {
  if (error instanceof ProtocolError) {
    return { code: error.code, message: error.message }
  }
  if (error instanceof CatalogValidationError) {
    return { code: error.code, message: 'Plugin catalog candidate rejected' }
  }
  log('JSON-RPC request failed', error)
  return { code: -32603, message: 'Internal plugin host error' }
}

function responseId(value: unknown): JsonRpcId {
  if (value === null || typeof value === 'string' || typeof value === 'number') return value
  return null
}

async function writeResponse(
  output: NodeJS.WritableStream,
  response: JsonRpcResponse,
): Promise<void> {
  const frame = `${JSON.stringify(response)}\n`
  await new Promise<void>((resolve, reject) => {
    output.write(frame, (error?: Error | null) => {
      if (error) reject(error)
      else resolve()
    })
  })
}

export interface ProtocolServerOptions {
  input?: NodeJS.ReadableStream
  output?: NodeJS.WritableStream
  log?: (message: string, error?: unknown) => void
}

/** Run a sequential NDJSON JSON-RPC loop until EOF or the shutdown method. */
export async function runProtocolServer(
  runtime: CatalogRuntime,
  options: ProtocolServerOptions = {},
): Promise<void> {
  const input = options.input ?? process.stdin
  const output = options.output ?? process.stdout
  const log = options.log ?? (() => undefined)
  const lines = createInterface({ input, crlfDelay: Infinity, terminal: false })

  try {
    for await (const line of lines) {
      if (!line.trim()) continue
      let raw: unknown
      let id: JsonRpcId = null
      try {
        if (Buffer.byteLength(line, 'utf8') > MAX_REQUEST_BYTES) {
          throw new ProtocolError(-32600, 'Request is too large')
        }
        try {
          raw = JSON.parse(line)
        } catch {
          throw new ProtocolError(-32700, 'Parse error')
        }
        if (isRecord(raw) && 'id' in raw) id = responseId(raw.id)
        const request = validateRequest(raw)
        const dispatched = await dispatchRequest(runtime, request)
        if ('id' in request) {
          await writeResponse(output, {
            jsonrpc: '2.0',
            id: responseId(request.id),
            result: dispatched.result,
          })
        }
        if (dispatched.shutdown) break
      } catch (error) {
        // Notifications have no response. Parse/validation failures do not
        // yield a valid notification, so they retain the standard null id.
        const isNotification = isRecord(raw) && !('id' in raw) && raw.jsonrpc === '2.0'
        if (!isNotification) {
          await writeResponse(output, {
            jsonrpc: '2.0',
            id,
            error: safeError(error, log),
          })
        } else if (!(error instanceof ProtocolError)) {
          log('JSON-RPC notification failed', error)
        }
      }
    }
  } finally {
    lines.close()
    await runtime.shutdown()
  }
}
