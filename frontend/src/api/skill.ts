import { apiClient, ApiResponse } from './client';

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
  triggers: string[];
  scope: 'global' | 'user';
  user_id?: string | null;
  owner_user_id?: string | null;
  workspace_id?: string | null;
  created_from_session_id?: string | null;
  installed: boolean;
  source: 'official' | 'personal';
}

export interface SkillListResponse {
  skills: SkillInfo[];
}

export interface SkillPreferencesResponse {
  auto_enabled_skills: string[];
}

export interface SkillFileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: SkillFileNode[];
}

export interface SkillFileContent {
  path: string;
  content: string;
  binary: boolean;
}

export interface SkillDetailResponse {
  skill: SkillInfo;
  tree: SkillFileNode[];
  files: SkillFileContent[];
}

export interface SkillUploadResponse {
  skills: SkillInfo[];
}

export interface CreateSkillFromSessionResponse {
  skill: SkillInfo;
}

export async function listSkills(): Promise<SkillInfo[]> {
  const response = await apiClient.get<ApiResponse<SkillListResponse>>('/skills');
  return response.data.data.skills;
}

export async function listSkillCatalog(): Promise<SkillInfo[]> {
  const response = await apiClient.get<ApiResponse<SkillListResponse>>('/skills/catalog');
  return response.data.data.skills;
}

export async function installSkill(id: string): Promise<SkillInfo> {
  const response = await apiClient.post<ApiResponse<SkillInfo>>(`/skills/${encodeURIComponent(id)}/install`);
  return response.data.data;
}

export async function uninstallSkill(id: string): Promise<SkillInfo> {
  const response = await apiClient.delete<ApiResponse<SkillInfo>>(`/skills/${encodeURIComponent(id)}/install`);
  return response.data.data;
}

export async function getSkillPreferences(): Promise<string[]> {
  const response = await apiClient.get<ApiResponse<SkillPreferencesResponse>>('/skills/preferences');
  return response.data.data.auto_enabled_skills;
}

export async function updateSkillPreferences(autoEnabledSkills: string[]): Promise<string[]> {
  const response = await apiClient.put<ApiResponse<SkillPreferencesResponse>>(
    '/skills/preferences',
    { auto_enabled_skills: autoEnabledSkills },
  );
  return response.data.data.auto_enabled_skills;
}

export async function getSkillDetail(name: string): Promise<SkillDetailResponse> {
  const response = await apiClient.get<ApiResponse<SkillDetailResponse>>(`/skills/${encodeURIComponent(name)}`);
  return response.data.data;
}

export async function updateSkillFile(name: string, path: string, content: string): Promise<SkillDetailResponse> {
  const response = await apiClient.put<ApiResponse<SkillDetailResponse>>(
    `/skills/${encodeURIComponent(name)}/files`,
    { path, content },
  );
  return response.data.data;
}

export async function updateSkillScope(id: string, scope: 'global' | 'user'): Promise<SkillInfo> {
  const response = await apiClient.patch<ApiResponse<SkillInfo>>(
    `/skills/${encodeURIComponent(id)}/scope`,
    { scope },
  );
  return response.data.data;
}

export async function uploadSkill(file: File): Promise<SkillInfo[]> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await apiClient.post<ApiResponse<SkillUploadResponse>>('/skills/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data.data.skills;
}

export async function createSkillFromSession(params: {
  session_id: string;
}): Promise<SkillInfo> {
  const response = await apiClient.post<ApiResponse<CreateSkillFromSessionResponse>>('/skills/from-session', params);
  return response.data.data.skill;
}
