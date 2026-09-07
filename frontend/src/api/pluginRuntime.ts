import { apiClient, type ApiResponse } from './client.ts';

export interface PluginRuntimeTool {
  name: string;
  description: string;
  scopes: string[];
  timeout_seconds: number;
  plugin: string;
  version: string;
  execution?: { credentials: { slot: string; provider: string }[] };
}

export interface PluginRuntimePlugin {
  plugin: string;
  version: string;
  manifest_digest: string;
  tool_count: number;
  tools: PluginRuntimeTool[];
  errors: string[];
}

export interface PluginRuntimeSnapshot {
  engine: string;
  version: string;
  status: 'healthy' | 'unavailable' | 'error';
  healthy: boolean;
  revision: string;
  manifest_digest: string;
  execution_bundle_digest: string;
  plugin_count: number;
  tool_count: number;
  plugins: PluginRuntimePlugin[];
  tools: PluginRuntimeTool[];
  last_error?: string | null;
}

export async function getPluginRuntime(): Promise<PluginRuntimeSnapshot> {
  const response = await apiClient.get<ApiResponse<PluginRuntimeSnapshot>>('/plugins/runtime');
  return response.data.data;
}

export async function reloadPluginRuntime(): Promise<PluginRuntimeSnapshot> {
  const response = await apiClient.post<ApiResponse<PluginRuntimeSnapshot>>(
    '/plugins/runtime/reload',
    undefined,
    { headers: { 'X-AI-DataSeek-Action': 'plugin-runtime-reload' } },
  );
  return response.data.data;
}
