import { apiClient, type ApiResponse } from './client';
export interface DataConnector {
  schema_version: string;
  id: string;
  version: string;
  title: string;
  description: string;
  transport: 'native' | 'mcp' | 'http';
  auth: 'none' | 'oauth2' | 'api_key';
  domains: string[];
  operations: Array<{ name: string; title: string; description: string; input_schema: Record<string, unknown> }>;
  limitations: string[];
}
export async function listDataConnectors(): Promise<DataConnector[]> {
  const response = await apiClient.get<ApiResponse<DataConnector[]>>('/data-connectors');
  return response.data.data ?? [];
}
