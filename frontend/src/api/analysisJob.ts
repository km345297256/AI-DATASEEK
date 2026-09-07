import { apiClient, type ApiResponse } from './client';
import type { AnalysisJobView } from '../types/analysisJob';

const jobPath = (sessionId: string, jobId: string) => (
  `/sessions/${encodeURIComponent(sessionId)}/analysis-jobs/${encodeURIComponent(jobId)}`
);

export async function getAnalysisJob(sessionId: string, jobId: string, signal?: AbortSignal): Promise<AnalysisJobView> {
  const response = await apiClient.get<ApiResponse<AnalysisJobView>>(jobPath(sessionId, jobId), { signal });
  return response.data.data;
}

export async function cancelAnalysisJob(sessionId: string, jobId: string): Promise<AnalysisJobView> {
  const response = await apiClient.post<ApiResponse<AnalysisJobView>>(`${jobPath(sessionId, jobId)}/cancel`, {}, {
    headers: { 'X-Analysis-Job-Action': 'cancel' },
  });
  return response.data.data;
}
