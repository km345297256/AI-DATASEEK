import { apiClient, type ApiResponse } from './client';

export interface DataCenterDatasetFile {
  name: string;
  path: string;
  size: number;
  role: string;
  content_type?: string | null;
}

export interface DatasetLocation {
  location_id: string;
  node_id: string;
  storage_type: 'managed_upload' | 'host_path';
  read_only: boolean;
  verified: boolean;
  verification_message: string;
  version: string;
}

export interface DataCenterDataset {
  dataset_id: string;
  external_id: string;
  data_center_id: string;
  data_center_name: string;
  name: string;
  description: string;
  temporal_coverage: string;
  spatial_coverage: string;
  data_type: string;
  tags: string[];
  preview_url: string;
  files: DataCenterDatasetFile[];
  metadata: Record<string, unknown>;
  locations: DatasetLocation[];
  enabled: boolean;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DatasetSuggestedQuestionsResponse {
  questions: string[];
}

export interface DatasetChatSession {
  session_id: string;
  title: string | null;
  latest_message: string | null;
  latest_message_at: number | null;
  status: 'pending' | 'running' | 'waiting' | 'completed';
}

export async function listDataCenterDatasets(): Promise<DataCenterDataset[]> {
  const response = await apiClient.get<ApiResponse<{ datasets: DataCenterDataset[] }>>('/datasets');
  return response.data.data.datasets;
}

export async function getDataCenterDataset(datasetId: string): Promise<DataCenterDataset> {
  const response = await apiClient.get<ApiResponse<DataCenterDataset>>(
    `/datasets/${encodeURIComponent(datasetId)}`,
  );
  return response.data.data;
}

export async function generateDatasetSuggestedQuestions(datasetId: string): Promise<string[]> {
  const response = await apiClient.post<ApiResponse<DatasetSuggestedQuestionsResponse>>(
    `/datasets/${encodeURIComponent(datasetId)}/suggested-questions`,
  );
  return response.data.data.questions;
}

export async function listDatasetChatSessions(datasetId: string): Promise<DatasetChatSession[]> {
  const response = await apiClient.get<ApiResponse<{ sessions: DatasetChatSession[] }>>(
    `/datasets/${encodeURIComponent(datasetId)}/sessions`,
  );
  return response.data.data.sessions;
}
