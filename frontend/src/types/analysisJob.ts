import type { SpillArtifactRef } from './event';

export type AnalysisJobStatus = 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out' | 'interrupted';

export interface AnalysisJobView {
  schema_version: 1;
  job_id: string;
  revision: number;
  status: AnalysisJobStatus;
  tool_name: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  cancel_requested_at?: string | null;
  cancellable: boolean;
  timeout_seconds?: number | null;
  execution_snapshot_id?: string | null;
  catalog_revision?: string | null;
  result_spill?: SpillArtifactRef | null;
  error_code?: string | null;
}
