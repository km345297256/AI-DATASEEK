export type ToolApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'consumed'
  | 'expired'
  | 'cancelled';

export type ToolApprovalDecision = 'approved' | 'rejected';

export interface ToolApprovalView {
  approval_id: string;
  revision: number;
  status: ToolApprovalStatus;
  tool_name: string;
  effects: string[];
  permissions: string[];
  arguments_preview: Record<string, unknown>;
  credential_refs: string[];
  created_at: string;
  expires_at: string;
  decided_at?: string | null;
  execution_snapshot_id?: string | null;
  catalog_revision?: string | null;
}
