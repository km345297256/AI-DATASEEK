import { apiClient, type ApiResponse } from './client.ts';
import type { AgentToolRuntimeConfig } from './agentProfile.ts';

export interface DomainPreset {
  id: string;
  name: string;
  description: string;
  plugin_ids: string[];
  initial_tools: string[];
  available_tool_count: number;
  initial_tool_count: number;
}

export interface DomainPresetCatalog {
  presets: DomainPreset[];
  defaults: AgentToolRuntimeConfig;
  catalog_revision: string | null;
}

export async function getDomainPresetCatalog(): Promise<DomainPresetCatalog> {
  const response = await apiClient.get<ApiResponse<DomainPresetCatalog>>('/plugins/presets');
  return response.data.data;
}
