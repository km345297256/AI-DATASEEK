import { apiClient, type ApiResponse } from './client';

export interface ToolCredentialView {
  reference: string;
  provider: string;
  tool_name: string;
  slot: string;
  revision: number;
  revoked: boolean;
  created_at: string;
}

export interface ToolCredentialList {
  configured: boolean;
  credentials: ToolCredentialView[];
}

export interface CreateToolCredentialRequest {
  provider: string;
  tool_name: string;
  slot: string;
  secret: string;
}

export async function getCredentials(signal?: AbortSignal): Promise<ToolCredentialList> {
  const response = await apiClient.get<ApiResponse<ToolCredentialList>>('/credentials', { signal });
  return response.data.data;
}

export async function createCredential(
  request: CreateToolCredentialRequest,
): Promise<ToolCredentialView> {
  const response = await apiClient.post<ApiResponse<ToolCredentialView>>(
    '/credentials',
    request,
    { headers: { 'X-Credential-Action': 'create' } },
  );
  return response.data.data;
}

export async function revokeCredential(reference: string): Promise<ToolCredentialView> {
  const response = await apiClient.post<ApiResponse<ToolCredentialView>>(
    `/credentials/${encodeURIComponent(reference)}/revoke`,
    undefined,
    { headers: { 'X-Credential-Action': 'revoke' } },
  );
  return response.data.data;
}
