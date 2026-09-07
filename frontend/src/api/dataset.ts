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
  domain?: string;
  temporal_coverage: string;
  spatial_coverage: string;
  data_type: string;
  tags: string[];
  preview_url: string;
  ncViewUrl?: string | null;
  files: DataCenterDatasetFile[];
  metadata: Record<string, unknown>;
  locations: DatasetLocation[];
  enabled: boolean;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DatasetSubmissionPayload {
  name: string;
  summary: string;
  storage_directory: string;
}

export interface DatasetRegistrationPayload {
  name: string;
  description: string;
  domain: string;
  storage_directory: string;
}

export async function listManagedDatasets(offset = 0): Promise<{ datasets: DataCenterDataset[]; total: number }> {
  const response = await apiClient.get<ApiResponse<{ datasets: DataCenterDataset[]; total: number }>>(
    '/datasets/manage', { params: { limit: 100, offset } },
  );
  return response.data.data;
}

export async function registerDataset(payload: DatasetRegistrationPayload): Promise<DataCenterDataset> {
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>('/datasets/registrations', payload);
  return response.data.data;
}

export async function updateDatasetRegistration(
  id: string, payload: Pick<DatasetRegistrationPayload, 'name' | 'description' | 'domain'>,
): Promise<DataCenterDataset> {
  const response = await apiClient.patch<ApiResponse<DataCenterDataset>>(`/datasets/${encodeURIComponent(id)}`, payload);
  return response.data.data;
}

export async function archiveDatasetRegistration(id: string): Promise<DataCenterDataset> {
  const response = await apiClient.delete<ApiResponse<DataCenterDataset>>(`/datasets/${encodeURIComponent(id)}`);
  return response.data.data;
}

export interface DataProductFile {
  file_id: string;
  filename: string;
  relative_path: string;
  role: 'data' | 'chart' | 'source' | 'report' | 'other';
  content_type?: string | null;
  size: number;
  source_artifact_id?: string | null;
  source_tool?: string | null;
  is_primary: boolean;
  created_at?: string | null;
}

export interface DataProduct {
  product_id: string;
  dataset_id: string;
  source_session_id: string;
  name: string;
  description: string;
  generation_method: string;
  created_by: string;
  owner_id?: string | null;
  version: number;
  files: DataProductFile[];
  directories: string[];
  metadata: Record<string, unknown>;
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

export async function createDatasetSubmission(
  payload: DatasetSubmissionPayload,
): Promise<DataCenterDataset> {
  const response = await apiClient.post<ApiResponse<DataCenterDataset>>(
    '/datasets/submissions',
    payload,
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

export async function listDatasetDataProducts(datasetId: string): Promise<DataProduct[]> {
  const response = await apiClient.get<ApiResponse<DataProduct[]>>(
    '/datasets/' + encodeURIComponent(datasetId) + '/data-products',
  );
  return response.data.data;
}

export async function updateDatasetDataProduct(datasetId: string, productId: string, payload: { name: string; description: string; generation_method: string; created_by: string; directories: string[]; files: DataProductFile[] }): Promise<DataProduct> {
  const response = await apiClient.put<ApiResponse<DataProduct>>(
    '/datasets/' + encodeURIComponent(datasetId) + '/data-products/' + encodeURIComponent(productId),
    payload,
  );
  return response.data.data;
}

export async function deleteDatasetDataProduct(datasetId: string, productId: string): Promise<void> {
  await apiClient.delete<ApiResponse<{ deleted: boolean }>>(
    '/datasets/' + encodeURIComponent(datasetId) + '/data-products/' + encodeURIComponent(productId),
  );
}

export async function downloadDatasetDataProduct(datasetId: string, productId: string): Promise<Blob> {
  const response = await apiClient.get(
    '/datasets/' + encodeURIComponent(datasetId) + '/data-products/' + encodeURIComponent(productId) + '/download',
    { responseType: 'blob' },
  );
  return response.data as Blob;
}
