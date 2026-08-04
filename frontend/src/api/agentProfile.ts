import { apiClient, ApiResponse } from './client';

export interface AgentProfile {
  id: string;
  name: string;
  user_id: string | null;
  owner_user_id?: string | null;
  workspace_id?: string | null;
  scope: 'global' | 'user';
  model_config_id?: string | null;
  model_name: string;
  model_provider: string;
  api_base: string | null;
  api_key: string | null;
  temperature: number;
  max_tokens: number;
  system_prompt: string | null;
  planner_config: AgentPlannerConfig;
  subagents: AgentSubAgentConfig[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentPlannerConfig {
  system_prompt?: string | null;
  model_provider?: string | null;
  model_name?: string | null;
  api_base?: string | null;
  api_key?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
}

export interface AgentSubAgentConfig {
  key: string;
  name: string;
  handler_type: string;
  enabled: boolean;
  planner_capability: string;
  use_when: string;
  avoid_when?: string;
  input_contract?: string;
  output_contract?: string;
  system_prompt?: string | null;
  model_config_id?: string | null;
  model_config?: Record<string, unknown>;
  tool_permissions?: Record<string, unknown>;
}

export interface CreateAgentProfileRequest {
  name: string;
  model_config_id?: string | null;
  model_name: string;
  model_provider: string;
  api_base?: string | null;
  api_key?: string | null;
  temperature: number;
  max_tokens: number;
  system_prompt?: string | null;
  planner_config?: AgentPlannerConfig;
  subagents?: AgentSubAgentConfig[];
  is_global: boolean;
}

export interface UpdateAgentProfileRequest {
  name?: string;
  model_config_id?: string | null;
  model_name?: string;
  model_provider?: string;
  api_base?: string | null;
  api_key?: string | null;
  temperature?: number;
  max_tokens?: number;
  system_prompt?: string | null;
  planner_config?: AgentPlannerConfig;
  subagents?: AgentSubAgentConfig[];
  is_global?: boolean;
}

export async function listAgentProfiles(): Promise<AgentProfile[]> {
  const response = await apiClient.get<ApiResponse<AgentProfile[]>>('/agent-profiles');
  return response.data.data;
}

export async function createAgentProfile(request: CreateAgentProfileRequest): Promise<AgentProfile> {
  const response = await apiClient.post<ApiResponse<AgentProfile>>('/agent-profiles', request);
  return response.data.data;
}

export async function updateAgentProfile(profileId: string, request: UpdateAgentProfileRequest): Promise<AgentProfile> {
  const response = await apiClient.put<ApiResponse<AgentProfile>>(`/agent-profiles/${profileId}`, request);
  return response.data.data;
}

export async function deleteAgentProfile(profileId: string): Promise<void> {
  await apiClient.delete<ApiResponse<void>>(`/agent-profiles/${profileId}`);
}
