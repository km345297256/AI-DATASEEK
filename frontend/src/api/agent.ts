// Backend API service
import { apiClient, API_CONFIG, ApiResponse, createSSEConnection, SSECallbacks } from './client';
import { AgentSSEEvent } from '../types/event';
import { CreateSessionResponse, GetSessionResponse, ShellViewResponse, FileViewResponse, ListSessionResponse, SignedUrlResponse, ShareSessionResponse, SharedSessionResponse, SessionCollaboratorsResponse, SessionCollaboratorUser, UserSearchResponse } from '../types/response';
import type { FileInfo } from './file';



/**
 * Create Session
 * @returns Session
 */
export async function createSession(
  agentProfileId?: string | null,
): Promise<CreateSessionResponse> {
  const response = await apiClient.put<ApiResponse<CreateSessionResponse>>(
    '/sessions',
    {
      agent_profile_id: agentProfileId || undefined,
    }
  );
  return response.data.data;
}

export async function getSession(sessionId: string): Promise<GetSessionResponse> {
  const response = await apiClient.get<ApiResponse<GetSessionResponse>>(`/sessions/${sessionId}`);
  return response.data.data;
}

export async function getSessions(): Promise<ListSessionResponse> {
  const response = await apiClient.get<ApiResponse<ListSessionResponse>>('/sessions');
  return response.data.data;
}

export async function getSessionsSSE(callbacks?: SSECallbacks<ListSessionResponse>): Promise<() => void> {
  return createSSEConnection<ListSessionResponse>(
    '/sessions',
    {
      method: 'POST',
      // This POST only opens the read-only session-list stream and has no body.
      retryOnError: true,
    },
    callbacks
  );
}

export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete<ApiResponse<void>>(`/sessions/${sessionId}`);
}

export async function updateSessionTitle(sessionId: string, title: string): Promise<void> {
  await apiClient.patch<ApiResponse<void>>(`/sessions/${sessionId}/title`, { title });
}

export async function stopSession(sessionId: string): Promise<void> {
  await apiClient.post<ApiResponse<void>>(`/sessions/${sessionId}/stop`);
}

/**
 * Create VNC signed URL
 * @param sessionId Session ID to create signed URL for
 * @param expireMinutes URL expiration time in minutes (default: 15)
 * @returns Signed URL response for VNC WebSocket access
 */
export async function createVncSignedUrl(sessionId: string, expireMinutes: number = 15): Promise<SignedUrlResponse> {
  const response = await apiClient.post<ApiResponse<SignedUrlResponse>>(`/sessions/${sessionId}/vnc/signed-url`, {
    expire_minutes: expireMinutes
  });
  return response.data.data;
}

/**
 * Get VNC WebSocket URL with signed URL
 * @param sessionId Session ID
 * @param expireMinutes URL expiration time in minutes (default: 60)
 * @returns Promise resolving to signed VNC WebSocket URL string
 * 
 * @example
 * // Signed URL (no Authorization header needed, more secure)
 * const url = await getVNCUrl('session123');
 * const url = await getVNCUrl('session123', 120);
 */
export const getVNCUrl = async (
  sessionId: string, 
  expireMinutes: number = 15
): Promise<string> => {
    const signedUrlResponse = await createVncSignedUrl(sessionId, expireMinutes);
    const wsBaseUrl = API_CONFIG.host.replace(/^http/, 'ws');
    return `${wsBaseUrl}${signedUrlResponse.signed_url}`;
}

/**
 * Chat with Session (using SSE to receive streaming responses)
 * @returns A function to cancel the SSE connection
 */
export const chatWithSession = async (
  sessionId: string, 
  message: string = '',
  eventId?: string,
  attachments?: Array<{ file_id: string; filename: string }>,
  skills?: string[],
  mcpServers?: string[],
  agentProfileId?: string | null,
  callbacks?: SSECallbacks<AgentSSEEvent['data']>,
  datasetIds?: string[],
  clientMessageId?: string,
): Promise<() => void> => {
  const effectiveClientMessageId = message
    ? clientMessageId || createClientMessageId()
    : undefined;

  return createSSEConnection<AgentSSEEvent['data']>(
    `/sessions/${sessionId}/chat`,
    {
      method: 'POST',
      body: { 
        message, 
        timestamp: Math.floor(Date.now() / 1000), 
        event_id: eventId,
        agent_profile_id: agentProfileId,
        attachments,
        skills,
        mcp_servers: mcpServers,
        dataset_ids: datasetIds,
        client_message_id: effectiveClientMessageId,
      }
    },
    callbacks
  );
};

function createClientMessageId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `msg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

/**
 * View Shell session output
 * @param sessionId Session ID
 * @param shellSessionId Shell session ID
 * @returns Shell session output content
 */
export async function viewShellSession(sessionId: string, shellSessionId: string): Promise<ShellViewResponse> {
  const response = await apiClient.post<ApiResponse<ShellViewResponse>>(
    `/sessions/${sessionId}/shell`,
    { session_id: shellSessionId }
  );
  return response.data.data;
}

/**
 * View file content
 * @param sessionId Session ID
 * @param file File path
 * @returns File content
 */
export async function viewFile(sessionId: string, file: string): Promise<FileViewResponse> {
  const response = await apiClient.post<ApiResponse<FileViewResponse>>(
    `/sessions/${sessionId}/file`,
    { file }
  );
  return response.data.data;
}

export type SessionFileSortBy = 'filename' | 'size' | 'upload_date';
export type SessionFileSortOrder = 'asc' | 'desc';

export interface SessionFileSortOptions {
  sort_by?: SessionFileSortBy;
  sort_order?: SessionFileSortOrder;
}

export async function getSessionFiles(sessionId: string, options: SessionFileSortOptions = {}): Promise<FileInfo[]> {
  const response = await apiClient.get<ApiResponse<FileInfo[]>>(
    `/sessions/${sessionId}/files`,
    { params: options }
  );
  return response.data.data;
}

export async function clearUnreadMessageCount(sessionId: string): Promise<void> {
  await apiClient.post<ApiResponse<void>>(`/sessions/${sessionId}/clear_unread_message_count`);
}

/**
 * Share a session to make it publicly accessible
 * @param sessionId Session ID to share
 * @returns Share session response with current sharing status
 * 
 * @example
 * ```typescript
 * // Share a session
 * const result = await shareSession('session123');
 * console.log(result.is_shared); // true
 * ```
 */
export async function shareSession(sessionId: string): Promise<ShareSessionResponse> {
  const response = await apiClient.post<ApiResponse<ShareSessionResponse>>(`/sessions/${sessionId}/share`);
  return response.data.data;
}

/**
 * Unshare a session to make it private again
 * @param sessionId Session ID to unshare
 * @returns Share session response with current sharing status
 * 
 * @example
 * ```typescript
 * // Unshare a session
 * const result = await unshareSession('session123');
 * console.log(result.is_shared); // false
 * ```
 */
export async function unshareSession(sessionId: string): Promise<ShareSessionResponse> {
  const response = await apiClient.delete<ApiResponse<ShareSessionResponse>>(`/sessions/${sessionId}/share`);
  return response.data.data;
}

export type TaskFeedbackPreference = 'like' | 'dislike';
export interface TaskFeedbackResponse {
  preference: TaskFeedbackPreference | null;
  dislike_reasons: string[];
  detail: string;
}

export async function getTaskFeedback(sessionId: string): Promise<TaskFeedbackResponse> {
  const response = await apiClient.get<ApiResponse<TaskFeedbackResponse>>(`/sessions/${sessionId}/feedback`);
  return response.data.data;
}

export async function saveTaskFeedback(
  sessionId: string,
  feedback: { preference: TaskFeedbackPreference; dislike_reasons?: string[]; detail?: string },
): Promise<TaskFeedbackResponse> {
  const response = await apiClient.put<ApiResponse<TaskFeedbackResponse>>(`/sessions/${sessionId}/feedback`, feedback);
  return response.data.data;
}

export async function deleteTaskFeedback(sessionId: string): Promise<void> {
  await apiClient.delete<ApiResponse<TaskFeedbackResponse>>(`/sessions/${sessionId}/feedback`);
}

/**
 * Get a shared session without authentication
 * This endpoint allows public access to sessions that have been marked as shared.
 * No authentication token is required.
 * 
 * @param sessionId Session ID to retrieve
 * @returns Shared session data (accessible publicly)
 * 
 * @example
 * ```typescript
 * // Get a shared session (no auth required)
 * try {
 *   const sharedSession = await getSharedSession('session123');
 *   console.log(sharedSession.title);
 *   console.log(sharedSession.events);
 * } catch (error) {
 *   console.error('Session not found or not shared');
 * }
 * ```
 */
export async function getSharedSession(sessionId: string): Promise<SharedSessionResponse> {
  const response = await apiClient.get<ApiResponse<SharedSessionResponse>>(`/sessions/shared/${sessionId}`);
  return response.data.data;
}

export async function getSharedSessionFiles(sessionId: string, options: SessionFileSortOptions = {}): Promise<FileInfo[]> {
  const response = await apiClient.get<ApiResponse<FileInfo[]>>(
    `/sessions/${sessionId}/share/files`,
    { params: options }
  );
  return response.data.data;
}

export async function searchSessionCollaboratorUsers(sessionId: string, email: string): Promise<SessionCollaboratorUser[]> {
  const response = await apiClient.get<ApiResponse<UserSearchResponse>>(
    `/sessions/${sessionId}/collaborators/search`,
    { params: { email } }
  );
  return response.data.data.users;
}

export async function getSessionCollaborators(sessionId: string): Promise<SessionCollaboratorUser[]> {
  const response = await apiClient.get<ApiResponse<SessionCollaboratorsResponse>>(`/sessions/${sessionId}/collaborators`);
  return response.data.data.collaborators;
}

export async function updateSessionCollaborators(sessionId: string, userIds: string[]): Promise<SessionCollaboratorUser[]> {
  const response = await apiClient.put<ApiResponse<SessionCollaboratorsResponse>>(
    `/sessions/${sessionId}/collaborators`,
    { user_ids: userIds }
  );
  return response.data.data.collaborators;
}
