import type { AnalysisJobView } from '../types/analysisJob';
import type { Message, StepContent, ToolContent } from '../types/message';
import { newerToolApproval } from './toolApproval.ts';

export const isAnalysisJobTerminal = (job: AnalysisJobView): boolean => (
  ['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(job.status)
);

export const newerAnalysisJob = (
  current: AnalysisJobView | null | undefined,
  incoming: AnalysisJobView | null | undefined,
): AnalysisJobView | null => {
  if (!incoming) return current || null;
  if (!current || incoming.job_id !== current.job_id) return incoming;
  if (incoming.revision <= current.revision) return current;
  // Terminal states cannot be undone by a delayed or malformed update.
  if (isAnalysisJobTerminal(current) && !isAnalysisJobTerminal(incoming)) return current;
  return incoming;
};

export const mergeAnalysisToolEvent = (current: ToolContent, incoming: ToolContent): ToolContent => ({
  ...incoming,
  analysis_job: newerAnalysisJob(current.analysis_job, incoming.analysis_job),
  tool_approval: newerToolApproval(current.tool_approval, incoming.tool_approval),
  status: current.status === 'called' ? 'called' : incoming.status,
  // Lifecycle-only events must not erase the result that arrived just before them.
  content: incoming.content ?? current.content,
  presentation: incoming.presentation ?? current.presentation,
  spill: incoming.spill ?? current.spill,
});

export const analysisJobElapsedSeconds = (job: AnalysisJobView, now = Date.now()): number => {
  const start = Date.parse(job.started_at || job.created_at);
  const end = job.finished_at ? Date.parse(job.finished_at) : now;
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, Math.floor((end - start) / 1000)) : 0;
};

export const findAnalysisTool = (messages: Message[], callId: string): ToolContent | undefined => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.type === 'tool' && (message.content as ToolContent).tool_call_id === callId) return message.content as ToolContent;
    if (message.type === 'step') {
      const found = (message.content as StepContent).tools.find((tool) => tool.tool_call_id === callId);
      if (found) return found;
    }
  }
  return undefined;
};
