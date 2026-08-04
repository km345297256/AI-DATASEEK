import { apiClient, ApiResponse } from './client';

export type MCPTransport = 'stdio' | 'sse' | 'streamable-http';

export interface MCPServerInfo {
  name: string;
  transport: MCPTransport;
  enabled: boolean;
  description?: string;
  command?: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
  env?: Record<string, string>;
  scope?: 'global' | 'user';
  user_id?: string | null;
  owner_user_id?: string | null;
  installed?: boolean;
  source?: 'official' | 'personal' | 'community';
}

export interface MCPServerListResponse {
  servers: MCPServerInfo[];
}

export interface MCPServerUpsertResponse {
  server: MCPServerInfo;
}

export async function listMCPServers(): Promise<MCPServerInfo[]> {
  const response = await apiClient.get<ApiResponse<MCPServerListResponse>>('/mcp/servers');
  return response.data.data.servers;
}

export async function listMCPCatalog(): Promise<MCPServerInfo[]> {
  const response = await apiClient.get<ApiResponse<MCPServerListResponse>>('/mcp/servers/catalog');
  return response.data.data.servers;
}

export async function installMCPServer(name: string): Promise<MCPServerInfo> {
  const response = await apiClient.post<ApiResponse<MCPServerInfo>>(`/mcp/servers/${encodeURIComponent(name)}/install`);
  return response.data.data;
}

export async function uninstallMCPServer(name: string): Promise<MCPServerInfo> {
  const response = await apiClient.delete<ApiResponse<MCPServerInfo>>(`/mcp/servers/${encodeURIComponent(name)}/install`);
  return response.data.data;
}

export async function saveMCPServer(server: MCPServerInfo): Promise<MCPServerInfo> {
  const response = await apiClient.put<ApiResponse<MCPServerUpsertResponse>>(
    `/mcp/servers/${encodeURIComponent(server.name)}`,
    server,
  );
  return response.data.data.server;
}

export async function deleteMCPServer(name: string): Promise<MCPServerInfo[]> {
  const response = await apiClient.delete<ApiResponse<MCPServerListResponse>>(
    `/mcp/servers/${encodeURIComponent(name)}`,
  );
  return response.data.data.servers;
}
