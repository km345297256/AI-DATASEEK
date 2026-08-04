import { apiClient, ApiResponse } from './client';

export type APIKeyScope = 'full';
export type APIKeyStatus = 'active' | 'revoked';

export interface APIKey {
  id: string;
  name: string;
  key_prefix: string;
  scopes: APIKeyScope[];
  status: APIKeyStatus;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface CreateAPIKeyResponse extends APIKey {
  key: string;
}

export interface CreateAPIKeyRequest {
  name: string;
  scopes: APIKeyScope[];
  expires_in_days: number | null;
}

export async function listAPIKeys(): Promise<APIKey[]> {
  const response = await apiClient.get<ApiResponse<APIKey[]>>('/api-keys');
  return response.data.data;
}

export async function createAPIKey(request: CreateAPIKeyRequest): Promise<CreateAPIKeyResponse> {
  const response = await apiClient.post<ApiResponse<CreateAPIKeyResponse>>('/api-keys', request);
  return response.data.data;
}

export async function revokeAPIKey(keyId: string): Promise<void> {
  await apiClient.delete<ApiResponse<{}>>(`/api-keys/${keyId}`);
}
