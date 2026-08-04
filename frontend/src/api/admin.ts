import { apiClient, type ApiResponse } from './client';
import type { RegistrationStatus, UserRole } from './auth';
import type { SharedSessionResponse } from '@/types/response';

export type TaskStatus = 'pending' | 'running' | 'waiting' | 'completed';
export type ResourceScope = 'global' | 'user' | 'workspace' | 'private' | 'shared';
export type MCPRiskLevel = 'standard' | 'internal' | 'sensitive' | 'restricted';
export type MCPTransport = 'stdio' | 'sse' | 'streamable-http';
export type ExecutionNodeType = 'local_docker' | 'worker_agent' | 'remote_docker' | 'kubernetes' | 'fixed_sandbox';
export type ExecutionNodeStatus = 'unknown' | 'checking' | 'healthy' | 'degraded' | 'unhealthy' | 'disabled' | 'draining' | 'deleted';
export type ExecutionNodeAuthType = 'none' | 'bearer' | 'basic' | 'mtls' | 'kubeconfig' | 'docker_tls';

export interface AdminUserInfo {
  id: string;
  fullname: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  registration_status: RegistrationStatus;
  registration_reviewed_by?: string | null;
  registration_reviewed_at?: string | null;
  registration_review_note?: string | null;
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
  workspace_count: number;
  member_count: number;
  token_balance: number | null;
  token_daily_refill: number | null;
  token_daily_refill_override?: number | null;
  token_last_refill_date?: string | null;
}

export interface AdminUserListResponse {
  users: AdminUserInfo[];
  total: number;
}

export interface RoleTokenQuotaInfo {
  role: UserRole;
  initial_tokens: number | null;
  daily_refill_tokens: number | null;
  created_at: string;
  updated_at: string;
}

export interface RoleTokenQuotaListResponse {
  quotas: RoleTokenQuotaInfo[];
}

export interface AdminTaskInfo {
  session_id: string;
  user_id: string;
  user_fullname?: string | null;
  agent_id: string;
  task_id?: string | null;
  sandbox_id?: string | null;
  title?: string | null;
  latest_message?: string | null;
  latest_message_at?: string | null;
  status: TaskStatus;
  unread_message_count: number;
  is_shared: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminTaskListResponse {
  tasks: AdminTaskInfo[];
  total: number;
}

export interface AdminSkillInfo {
  id: string;
  name: string;
  description: string;
  triggers: string[];
  scope: ResourceScope;
  user_id?: string | null;
  owner_user_id?: string | null;
  owner_fullname?: string | null;
  workspace_id?: string | null;
  path: string;
  created_from_session_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminSkillListResponse {
  skills: AdminSkillInfo[];
  total: number;
}

export interface AdminMCPServerInfo {
  name: string;
  transport: MCPTransport;
  enabled: boolean;
  description?: string | null;
  command?: string | null;
  args?: string[] | null;
  url?: string | null;
  scope: ResourceScope;
  user_id?: string | null;
  owner_user_id?: string | null;
  owner_fullname?: string | null;
  workspace_id?: string | null;
  risk_level: MCPRiskLevel;
}

export interface AdminMCPServerListResponse {
  servers: AdminMCPServerInfo[];
  total: number;
}

export interface ExecutionNodeCapacity {
  max_sandboxes: number;
  cpu_cores?: number | null;
  memory_bytes?: number | null;
  disk_bytes?: number | null;
  gpu_count: number;
}

export interface ExecutionNodeHealth {
  running_sandboxes: number;
  warm_sandboxes?: number;
  assigned_sandboxes?: number;
  paused_sandboxes?: number;
  cpu_percent?: number | null;
  memory_used_bytes?: number | null;
  disk_used_bytes?: number | null;
  raw: Record<string, unknown>;
}

export interface ExecutionNodeInfo {
  id: string;
  name: string;
  description: string;
  type: ExecutionNodeType;
  status: ExecutionNodeStatus;
  enabled: boolean;
  base_url?: string | null;
  auth_type: ExecutionNodeAuthType;
  credential_ref?: string | null;
  runtime_config: Record<string, unknown>;
  capacity: ExecutionNodeCapacity;
  labels: Record<string, string>;
  taints: Record<string, string>;
  health: ExecutionNodeHealth;
  last_heartbeat_at?: string | null;
  last_checked_at?: string | null;
  failure_reason?: string | null;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ExecutionNodeListResponse {
  nodes: ExecutionNodeInfo[];
  total: number;
}

export interface TokenUsageByModelInfo {
  model_name: string;
  record_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface TokenUsageDimensionInfo {
  key: string;
  label?: string | null;
  record_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ResourceUsageOverview {
  token_usage: {
    record_count: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    by_model: TokenUsageByModelInfo[];
  };
  token_usage_by_user: TokenUsageDimensionInfo[];
  token_usage_by_workspace: TokenUsageDimensionInfo[];
  auth_usage: {
    users_total: number;
    users_active: number;
    api_keys_total: number;
    api_keys_active: number;
    sessions_total: number;
  };
  server_usage: {
    cpu: Record<string, unknown>;
    memory: Record<string, unknown>;
    disk: Record<string, unknown>;
    docker: Record<string, unknown>;
    file_storage?: Record<string, unknown>;
  };
  sandbox_usage: Array<Record<string, unknown>>;
  execution_nodes_usage: Array<{
    id: string;
    name: string;
    type: ExecutionNodeType;
    status: ExecutionNodeStatus;
    enabled: boolean;
    base_url?: string | null;
    capacity: Record<string, unknown>;
    health: Record<string, unknown>;
    last_checked_at?: string | null;
    last_heartbeat_at?: string | null;
    failure_reason?: string | null;
  }>;
  generated_at: string;
}

export async function getResourceUsage(params?: {
  start_at?: string;
  end_at?: string;
  include_sandboxes?: boolean;
}): Promise<ResourceUsageOverview> {
  const response = await apiClient.get<ApiResponse<ResourceUsageOverview>>('/admin/resource-usage', { params });
  return response.data.data;
}

export async function listAdminTasks(params?: {
  query?: string;
  user_id?: string;
  status?: TaskStatus;
  limit?: number;
  offset?: number;
}): Promise<AdminTaskListResponse> {
  const response = await apiClient.get<ApiResponse<AdminTaskListResponse>>('/admin/tasks', { params });
  return response.data.data;
}

export async function getAdminTaskReplay(sessionId: string): Promise<SharedSessionResponse> {
  const response = await apiClient.get<ApiResponse<SharedSessionResponse>>(
    `/admin/tasks/${encodeURIComponent(sessionId)}/replay`,
  );
  return response.data.data;
}

export async function listAdminSkills(params?: {
  query?: string;
  scope?: ResourceScope;
  owner_user_id?: string;
  limit?: number;
  offset?: number;
}): Promise<AdminSkillListResponse> {
  const response = await apiClient.get<ApiResponse<AdminSkillListResponse>>('/admin/skills', { params });
  return response.data.data;
}

export async function updateAdminSkill(
  skillId: string,
  request: {
    scope?: ResourceScope;
    user_id?: string | null;
    owner_user_id?: string | null;
    workspace_id?: string | null;
  },
): Promise<AdminSkillInfo> {
  const response = await apiClient.patch<ApiResponse<AdminSkillInfo>>(
    `/admin/skills/${encodeURIComponent(skillId)}`,
    request,
  );
  return response.data.data;
}

export async function deleteAdminSkill(skillId: string): Promise<Record<string, never>> {
  const response = await apiClient.delete<ApiResponse<Record<string, never>>>(
    `/admin/skills/${encodeURIComponent(skillId)}`,
  );
  return response.data.data;
}

export async function listAdminMCPServers(params?: {
  query?: string;
  scope?: ResourceScope;
  owner_user_id?: string;
  limit?: number;
  offset?: number;
}): Promise<AdminMCPServerListResponse> {
  const response = await apiClient.get<ApiResponse<AdminMCPServerListResponse>>('/admin/mcp/servers', { params });
  return response.data.data;
}

export async function updateAdminMCPServer(
  name: string,
  request: {
    enabled?: boolean;
    scope?: ResourceScope;
    risk_level?: MCPRiskLevel;
    user_id?: string | null;
    owner_user_id?: string | null;
    workspace_id?: string | null;
  },
): Promise<AdminMCPServerInfo> {
  const response = await apiClient.patch<ApiResponse<AdminMCPServerInfo>>(
    `/admin/mcp/servers/${encodeURIComponent(name)}`,
    request,
  );
  return response.data.data;
}

export async function deleteAdminMCPServer(name: string): Promise<Record<string, never>> {
  const response = await apiClient.delete<ApiResponse<Record<string, never>>>(
    `/admin/mcp/servers/${encodeURIComponent(name)}`,
  );
  return response.data.data;
}

export async function listAdminUsers(params?: {
  query?: string;
  registration_status?: RegistrationStatus;
  limit?: number;
  offset?: number;
}): Promise<AdminUserListResponse> {
  const response = await apiClient.get<ApiResponse<AdminUserListResponse>>('/admin/users', { params });
  return response.data.data;
}

export async function decideAdminUserRegistration(
  userId: string,
  request: { status: Exclude<RegistrationStatus, 'pending'>; decision_note?: string },
): Promise<AdminUserInfo> {
  const response = await apiClient.post<ApiResponse<AdminUserInfo>>(
    `/admin/users/${encodeURIComponent(userId)}/registration-decision`,
    request,
  );
  return response.data.data;
}

export async function listRoleTokenQuotas(): Promise<RoleTokenQuotaListResponse> {
  const response = await apiClient.get<ApiResponse<RoleTokenQuotaListResponse>>('/admin/token-quotas/roles');
  return response.data.data;
}

export async function updateRoleTokenQuota(
  role: UserRole,
  request: { initial_tokens: number | null; daily_refill_tokens: number | null },
): Promise<RoleTokenQuotaInfo> {
  const response = await apiClient.patch<ApiResponse<RoleTokenQuotaInfo>>(
    `/admin/token-quotas/roles/${encodeURIComponent(role)}`,
    request,
  );
  return response.data.data;
}

export async function activateAdminUser(userId: string): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>(
    `/admin/users/${encodeURIComponent(userId)}/activate`,
  );
  return response.data.data;
}

export async function deactivateAdminUser(userId: string): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>(
    `/admin/users/${encodeURIComponent(userId)}/deactivate`,
  );
  return response.data.data;
}

export async function updateAdminUser(
  userId: string,
  request: {
    role?: UserRole;
    token_balance?: number | null;
    token_daily_refill_override?: number | null;
  },
): Promise<AdminUserInfo> {
  const response = await apiClient.patch<ApiResponse<AdminUserInfo>>(
    `/admin/users/${encodeURIComponent(userId)}`,
    request,
  );
  return response.data.data;
}

// Dataset management only needs a read-only execution-node catalogue to register host paths.
export async function listExecutionNodes(params?: {
  query?: string;
  include_deleted?: boolean;
  limit?: number;
  offset?: number;
}): Promise<ExecutionNodeListResponse> {
  const response = await apiClient.get<ApiResponse<ExecutionNodeListResponse>>('/admin/execution-nodes', { params });
  return response.data.data;
}
