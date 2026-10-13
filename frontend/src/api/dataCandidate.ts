import { apiClient, type ApiResponse } from './client';
export interface CandidateInput { title: string; source_url: string; external_id: string }
export interface DataCandidate extends CandidateInput {
  candidate_id: string; asset_id: string; created_at: number; provenance: 'user_reference';
}
export interface DataVersion {
  version_id: string; candidate_id: string; asset_id: string;
  files: { file_id: string; size: number; sha256: string }[];
  acquisition: { method: 'uploaded_files'; source_verified: false; recorded_at: number };
  retention: 'reference_only';
}
export interface CandidatePage { candidates: DataCandidate[]; next_cursor: string | null }
export interface VersionPage { versions: DataVersion[]; next_cursor: string | null }
const base = (session: string) => `/sessions/${encodeURIComponent(session)}/data-candidates`;
export async function listCandidates(session: string, cursor?: string | null, signal?: AbortSignal) {
  return (await apiClient.get<ApiResponse<CandidatePage>>(base(session), { params: { cursor }, signal })).data.data;
}
export async function createCandidate(session: string, input: CandidateInput, signal?: AbortSignal) {
  return (await apiClient.post<ApiResponse<DataCandidate>>(base(session), input, { signal })).data.data;
}
export async function listVersions(session: string, candidate: string, cursor?: string | null, signal?: AbortSignal) {
  return (await apiClient.get<ApiResponse<VersionPage>>(`${base(session)}/${encodeURIComponent(candidate)}/versions`, { params: { cursor }, signal })).data.data;
}
export async function registerVersion(session: string, candidate: string, fileIds: string[], signal?: AbortSignal) {
  return (await apiClient.post<ApiResponse<DataVersion>>(`${base(session)}/${encodeURIComponent(candidate)}/versions`, { file_ids: fileIds }, { signal, timeout: 120000 })).data.data;
}
