export interface DataRun {
  run_id: string;
  source_event_seq: number;
  revision: number;
  status: 'running' | 'waiting' | 'succeeded' | 'partial' | 'failed' | 'unknown';
  started_at: number;
  ended_at: number | null;
  plan_revision: number | null;
  steps: Array<{ step_id: string; ordinal: number; status: 'pending' | 'running' | 'succeeded' | 'failed' | 'unknown'; outcome_status: string | null }>;
  skills: Array<{ package_id: string; version: string; sha256: string }>;
  legacy_skill_count: number;
  environment_fingerprint: string | null;
  catalog_revision: string | null;
  history_complete: boolean;
  recovery: 'inspect_only';
}
export interface DataRunPage { runs: DataRun[]; next_before_seq: number | null }
