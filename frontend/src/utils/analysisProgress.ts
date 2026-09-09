import type { AgentSSEEvent, MessageEventData } from '../types/event';

/** Progress is a transport hint, not another assistant answer or a final result. */
export function isAnalysisProgressMessage(data: MessageEventData): boolean {
  return data.role === 'assistant'
    && Boolean(data.metadata && Object.prototype.hasOwnProperty.call(data.metadata, 'analysis_progress'))
    && !data.metadata?.analysis_outcome
    && !data.attachments?.length;
}

export function isAnalysisProgressEvent(event: AgentSSEEvent): boolean {
  return event.event === 'message' && isAnalysisProgressMessage(event.data as MessageEventData);
}

/** Never display untrusted progress text, diagnostic details or host paths. */
export function nextAnalysisProgress(current: string, event: AgentSSEEvent): string {
  if (isAnalysisProgressEvent(event)) {
    const stage = (event.data as MessageEventData).metadata?.analysis_progress?.stage;
    if (stage === 'verifying_execution') return '正在核验执行状态…';
    if (stage === 'completing_results') return '正在补全分析结果…';
    return '正在处理分析…';
  }
  if (['message', 'done', 'wait', 'error'].includes(event.event)) return '';
  return current;
}
