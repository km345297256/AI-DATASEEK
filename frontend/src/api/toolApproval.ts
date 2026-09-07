import { apiClient, type ApiResponse } from './client';
import type { ToolApprovalDecision, ToolApprovalView } from '../types/toolApproval';

const approvalPath = (sessionId: string, approvalId: string) => (
  `/sessions/${encodeURIComponent(sessionId)}/tool-approvals/${encodeURIComponent(approvalId)}`
);

export async function getToolApproval(
  sessionId: string,
  approvalId: string,
  signal?: AbortSignal,
): Promise<ToolApprovalView> {
  const response = await apiClient.get<ApiResponse<ToolApprovalView>>(
    approvalPath(sessionId, approvalId),
    { signal },
  );
  return response.data.data;
}

export async function decideToolApproval(
  sessionId: string,
  approvalId: string,
  decision: ToolApprovalDecision,
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<ToolApprovalView> {
  const response = await apiClient.post<ApiResponse<ToolApprovalView>>(
    `${approvalPath(sessionId, approvalId)}/decision`,
    { decision, expected_revision: expectedRevision },
    {
      headers: { 'X-Tool-Approval-Action': 'decide' },
      signal,
    },
  );
  return response.data.data;
}
