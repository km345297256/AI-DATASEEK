import type { ToolApprovalStatus, ToolApprovalView } from '../types/toolApproval';

const TERMINAL_STATUSES = new Set<ToolApprovalStatus>([
  'rejected',
  'consumed',
  'expired',
  'cancelled',
]);

export const isToolApprovalTerminal = (
  approval: ToolApprovalView | ToolApprovalStatus,
): boolean => TERMINAL_STATUSES.has(
  typeof approval === 'string' ? approval : approval.status,
);

export const isToolApprovalExpired = (
  approval: Pick<ToolApprovalView, 'expires_at'>,
  now = Date.now(),
): boolean => {
  const expiresAt = Date.parse(approval.expires_at);
  return Number.isFinite(expiresAt) && expiresAt <= now;
};

export const effectiveToolApprovalStatus = (
  approval: ToolApprovalView,
  now = Date.now(),
): ToolApprovalStatus => {
  if (
    !isToolApprovalTerminal(approval)
    && isToolApprovalExpired(approval, now)
  ) {
    return 'expired';
  }
  return approval.status;
};

/**
 * Merge a polled or SSE approval view without allowing late revisions to
 * overwrite newer state. A final decision also cannot be reopened by a
 * malformed higher-revision response.
 */
export const newerToolApproval = (
  current: ToolApprovalView | null | undefined,
  incoming: ToolApprovalView | null | undefined,
): ToolApprovalView | null => {
  if (!incoming) return current || null;
  if (!current || incoming.approval_id !== current.approval_id) return incoming;
  if (incoming.revision <= current.revision) return current;
  if (isToolApprovalTerminal(current) && !isToolApprovalTerminal(incoming)) return current;
  return incoming;
};

// Explicit alias for event reducers that describe the operation as a revision
// merge rather than selecting a newer view.
export const mergeToolApprovalRevision = newerToolApproval;
