import { apiClient, ApiResponse } from './client';

export type RendererKind = 'builtin' | 'api' | 'component';
export type RendererScope = 'global' | 'user';

export interface RendererInfo {
  id: string;
  name: string;
  description: string;
  kind: RendererKind;
  extensions: string[];
  scope: RendererScope;
  user_id?: string | null;
  enabled: boolean;
  api_url?: string | null;
  entry?: string | null;
  config?: Record<string, any>;
  owner_user_id?: string | null;
  installed: boolean;
  source: 'official' | 'personal';
}

export interface RendererRequest {
  name: string;
  description: string;
  kind: RendererKind;
  extensions: string[];
  enabled: boolean;
  api_url?: string | null;
  entry?: string | null;
  config?: Record<string, any>;
  is_global: boolean;
}

interface RendererListResponse {
  renderers: RendererInfo[];
}

interface RendererUpsertResponse {
  renderer: RendererInfo;
}

export async function listRendererConfigs(): Promise<RendererInfo[]> {
  const response = await apiClient.get<ApiResponse<RendererListResponse>>('/renderers');
  return response.data.data.renderers;
}

export async function listRendererCatalog(): Promise<RendererInfo[]> {
  const response = await apiClient.get<ApiResponse<RendererListResponse>>('/renderers/catalog');
  return response.data.data.renderers;
}

export async function installRenderer(id: string): Promise<RendererInfo> {
  const response = await apiClient.post<ApiResponse<RendererInfo>>(`/renderers/${encodeURIComponent(id)}/install`);
  return response.data.data;
}

export async function uninstallRenderer(id: string): Promise<RendererInfo> {
  const response = await apiClient.delete<ApiResponse<RendererInfo>>(`/renderers/${encodeURIComponent(id)}/install`);
  return response.data.data;
}

export async function createRendererConfig(request: RendererRequest): Promise<RendererInfo> {
  const response = await apiClient.post<ApiResponse<RendererUpsertResponse>>('/renderers', request);
  return response.data.data.renderer;
}

export async function updateRendererConfig(id: string, request: RendererRequest): Promise<RendererInfo> {
  const response = await apiClient.put<ApiResponse<RendererUpsertResponse>>(`/renderers/${encodeURIComponent(id)}`, request);
  return response.data.data.renderer;
}

export async function deleteRendererConfig(id: string): Promise<RendererInfo[]> {
  const response = await apiClient.delete<ApiResponse<RendererListResponse>>(`/renderers/${encodeURIComponent(id)}`);
  return response.data.data.renderers;
}
