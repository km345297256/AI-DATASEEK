import type { FileInfo } from '../api/file';

export type AgentSSEEvent = {
  event: 'tool' | 'step' | 'message' | 'error' | 'done' | 'title' | 'wait' | 'plan' | 'attachments';
  data: ToolEventData | StepEventData | MessageEventData | ErrorEventData | DoneEventData | TitleEventData | WaitEventData | PlanEventData;
}

export interface BaseEventData {
  event_id: string;
  timestamp: number;
}

export interface ToolEventData extends BaseEventData {
  tool_call_id: string;
  name: string;
  status: "calling" | "called";
  function: string;
  args: {[key: string]: any};
  content?: any;
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
}

export interface WaitEventData extends BaseEventData {
}

export interface TitleEventData extends BaseEventData {
  title: string;
}

export interface PlanEventData extends BaseEventData {
  steps: StepEventData[];
}
