import type { FileInfo } from '../api/file';
import type { ToolPresentation } from './toolPresentation';
import type { AnalysisJobView } from './analysisJob';
import type { ToolApprovalView } from './toolApproval';

export interface SpillArtifactRef {
  schema_version: 1;
  locator: string;
  byte_count: number;
  sha256: string;
  media_type: string;
  retrieval_hint: string;
}

export interface SpillArtifactNotice {
  schema_version: 1;
  status: 'stored' | 'unavailable';
  reference?: SpillArtifactRef | null;
  preview: string;
  original_bytes: number;
  retained_bytes: number;
  omitted_bytes: number;
}

export type AgentSSEEvent = {
  event: 'tool' | 'step' | 'message' | 'error' | 'done' | 'title' | 'wait' | 'plan' | 'attachments';
  data: ToolEventData | StepEventData | MessageEventData | ErrorEventData | DoneEventData | TitleEventData | WaitEventData | PlanEventData;
}

export interface BaseEventData {
  /** Existing Redis stream cursor; kept for transport-level resume. */
  event_id?: string | null;
  /** Monotonic within one session. Missing on pre-versioning history. */
  seq?: number | null;
  /** Missing versions are interpreted as v1 for legacy history. */
  version?: 1 | null;
  timestamp: number;
}

export interface ToolEventData extends BaseEventData {
  tool_call_id: string;
  name: string;
  status: "calling" | "called";
  function: string;
  args: {[key: string]: any};
  content?: any;
  presentation?: ToolPresentation | null;
  spill?: SpillArtifactNotice | null;
  analysis_job?: AnalysisJobView | null;
  tool_approval?: ToolApprovalView | null;
}

export interface StepEventData extends BaseEventData {
  status: "pending" | "running" | "completed" | "failed"
  id: string
  description: string
}

export interface MessageEventData extends BaseEventData {
    content: string;
    role: "user" | "assistant";
    attachments?: FileInfo[];
    metadata?: {
      skills?: string[];
      mcp_servers?: string[];
      dataset_ids?: string[];
      safety_review?: {
        decision: 'allow' | 'reject';
        risk_level: 'low' | 'medium' | 'high' | 'critical';
        categories: string[];
        reason?: string;
        suggestion?: string;
      };
    };
}

export interface ErrorEventData extends BaseEventData {
  error: string;
}

export interface DoneEventData extends BaseEventData {
  advice?: CompletionAdviceData;
}

export interface CompletionAdviceData {
  recommendations: string[];
  is_skill_candidate: boolean;
  skill_reason: string;
  shapefile_preview_available?: boolean;
  molecular_preview_available?: boolean;
}

export interface WaitEventData extends BaseEventData {
}

export interface TitleEventData extends BaseEventData {
  title: string;
}

export interface PlanEventData extends BaseEventData {
  steps: StepEventData[];
}
