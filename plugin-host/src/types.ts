export const CORDIS_ENGINE = 'cordis' as const
export const CORDIS_VERSION = '4.0.2' as const
export const TOOL_CONTRACT_VERSION = 2 as const

export const TOOL_EFFECTS = [
  'sandbox_read',
  'sandbox_write',
  'network',
  'credential_use',
  'external_side_effect',
] as const

export type ToolEffect = typeof TOOL_EFFECTS[number]
export type ToolConcurrency = 'exclusive' | 'parallel'

export interface ToolExecutionDescriptor {
  timeout_seconds: number
  cancellable: boolean
  concurrency: ToolConcurrency
  effects: ToolEffect[]
  permissions: string[]
  credentials: { slot: string; provider: string }[]
}

export type ToolPresentationKind =
  | 'auto'
  | 'generic'
  | 'table'
  | 'chart'
  | 'map'
  | 'image'
  | 'artifact'
  | 'log'

export interface ToolPresentationDescriptor {
  kind: ToolPresentationKind
  title?: string
  description?: string
}

export interface ToolDescriptor {
  contract_version: typeof TOOL_CONTRACT_VERSION
  name: string
  description: string
  parameters: Record<string, unknown>
  /**
   * Schema for the normalized ToolResult data payload. `null` deliberately
   * means that a legacy tool has not declared a machine-verifiable result.
   */
  output_schema: Record<string, unknown> | null
  execution: ToolExecutionDescriptor
  presentation: ToolPresentationDescriptor
  /** Compatibility aliases consumed by the existing Agent adapter. */
  scopes: string[]
  timeout_seconds: number
  plugin: string
  version: string
}

export interface PluginDescriptor {
  plugin: string
  version: string
  manifest_digest: string
  tool_count: number
}

export interface CatalogSnapshot {
  engine: typeof CORDIS_ENGINE
  version: typeof CORDIS_VERSION
  revision: string
  manifest_digest: string
  execution_bundle_digest: string
  plugin_count: number
  tool_count: number
  plugins: PluginDescriptor[]
  tools: ToolDescriptor[]
}

export interface ValidatedPlugin {
  descriptor: PluginDescriptor
  tools: ToolDescriptor[]
}

export interface HealthSnapshot {
  status: 'ok'
  engine: typeof CORDIS_ENGINE
  version: typeof CORDIS_VERSION
  revision: string
  manifest_digest: string
  execution_bundle_digest: string
  plugin_count: number
  tool_count: number
}

export type JsonRpcId = string | number | null

export interface JsonRpcRequest {
  jsonrpc: '2.0'
  id?: JsonRpcId
  method: string
  params?: unknown
}

export interface JsonRpcErrorObject {
  code: number
  message: string
}

export type JsonRpcResponse =
  | { jsonrpc: '2.0'; id: JsonRpcId; result: unknown }
  | { jsonrpc: '2.0'; id: JsonRpcId; error: JsonRpcErrorObject }
