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
  source_path: string;
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

export interface DatasetPayload {
  name: string;
  data_center_id: string;
  data_center_name: string;
  description: string;
  temporal_coverage: string;
  spatial_coverage: string;
  data_type: string;
  tags: string[];
  metadata: Record<string, unknown>;
  enabled: boolean;
}

export interface DatasetSubmissionPayload {
  external_id: string;
  name: string;
  summary: string;
  keywords: string[];
  storage_directory: string;
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

export async function createDatasetSubmission(payload: DatasetSubmissionPayload): Promise<DataCenterDataset> {
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>('/datasets/submissions', payload);
  return response.data.data;
}

export async function generateDatasetSuggestedQuestions(datasetId: string): Promise<string[]> {
  const response = await apiClient.post<ApiResponse<DatasetSuggestedQuestionsResponse>>(
    `/datasets/${encodeURIComponent(datasetId)}/suggested-questions`,
  );
  return response.data.data.questions;
}

export async function listAdminDatasets(params: { query?: string; limit?: number; offset?: number } = {}) {
  const response = await apiClient.get<ApiResponse<{ datasets: DataCenterDataset[]; total: number }>>('/admin/datasets', { params });
  return response.data.data;
}

export async function createAdminDataset(payload: DatasetPayload): Promise<DataCenterDataset> {
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>('/admin/datasets', payload);
  return response.data.data;
}

export async function updateAdminDataset(datasetId: string, payload: Partial<DatasetPayload>): Promise<DataCenterDataset> {
  const response = await apiClient.patch<ApiResponse<DataCenterDataset>>(`/admin/datasets/${encodeURIComponent(datasetId)}`, payload);
  return response.data.data;
}

export async function deleteAdminDataset(datasetId: string): Promise<void> {
  await apiClient.delete(`/admin/datasets/${encodeURIComponent(datasetId)}`);
}

export async function addDatasetLocation(datasetId: string, payload: { node_id: string; storage_type: 'host_path'; source_path: string; version: string }): Promise<DataCenterDataset> {
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>(`/admin/datasets/${encodeURIComponent(datasetId)}/locations`, payload);
  return response.data.data;
}

export async function removeDatasetLocation(datasetId: string, locationId: string): Promise<DataCenterDataset> {
  const response = await apiClient.delete<ApiResponse<DataCenterDataset>>(`/admin/datasets/${encodeURIComponent(datasetId)}/locations/${encodeURIComponent(locationId)}`);
  return response.data.data;
}

export async function uploadDatasetFiles(datasetId: string, files: File[], relativePaths: string[]): Promise<DataCenterDataset> {
  const body = new FormData();
  files.forEach((file) => body.append('files', file));
  body.append('relative_paths_json', JSON.stringify(relativePaths));
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>(`/admin/datasets/${encodeURIComponent(datasetId)}/files`, body, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 0,
  });
  return response.data.data;
}

export async function uploadDatasetPreview(datasetId: string, file: File): Promise<DataCenterDataset> {
  const body = new FormData();
  body.append('file', file);
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>(`/admin/datasets/${encodeURIComponent(datasetId)}/preview`, body, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data.data;
}

export async function listDatasetChatSessions(datasetId: string): Promise<DatasetChatSession[]> {
  const response = await apiClient.get<ApiResponse<{ sessions: DatasetChatSession[] }>>(
    `/datasets/${encodeURIComponent(datasetId)}/sessions`,
  );
  return response.data.data.sessions;
}
