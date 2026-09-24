import type { AgentSSEEvent, MessageEventData, StepEventData, ToolEventData, ErrorEventData } from '../types/event';
import type { Message, MessageContent, AttachmentsContent, StepContent, ToolContent } from '../types/message';
import { acceptAgentEvent, createAgentEventCursor } from './agentEventCursor.ts';
import { mergeAnalysisToolEvent } from './analysisJob.ts';
import { AnalysisTimelineIndex } from './analysisTimelineIndex.ts';
import { completeRunningSteps, failRunningSteps, insertTaskExecutionSummary } from './chatTimeline.ts';
import { isPlaceholderAssistantMessage } from './datasetResultPresentation.ts';
import { isAnalysisProgressMessage } from './analysisProgress.ts';

export function isLegacyPlanProgressMessage(content: string): boolean {
  return /^(正在(?:联合)?分析指定|正在快速分析当前数据集|Analyzing the selected file|Jointly analyzing \d+ selected files|Analyzing the current dataset)/.test(content.trim());
}

/** Historical pages are complete user turns. Never fold them into the live cursor/state. */
export function projectHistoryMessages(events: AgentSSEEvent[], datasetMode = false): Message[] {
  const messages: Message[] = [];
  const cursor = createAgentEventCursor();
  const index = new AnalysisTimelineIndex();
  for (const event of events) {
    if (!acceptAgentEvent(cursor, event)) continue;
    if (event.event === 'message') {
      const data = event.data as MessageEventData;
      if (isAnalysisProgressMessage(data)) continue;
      if (data.role === 'user') failRunningSteps(messages, false);
      if (data.role === 'assistant' && (isPlaceholderAssistantMessage(data.content)
        || (datasetMode && isLegacyPlanProgressMessage(data.content)))) continue;
      messages.push({ type: data.role, content: { ...data } as MessageContent });
      if (data.attachments?.length) messages.push({ type: 'attachments', content: { ...data } as AttachmentsContent });
    } else if (event.event === 'tool') {
      const tool = { ...event.data } as ToolEventData;
      const current = index.tool(messages, tool.tool_call_id);
      if (current) Object.assign(current, mergeAnalysisToolEvent(current, tool));
      else {
        const step = index.runningStep(messages);
        if (step) { step.tools.push(tool); index.addTool(tool); }
        else messages.push({ type: 'tool', content: tool });
      }
    } else if (event.event === 'step') {
      const data = event.data as StepEventData;
      const current = index.step(messages, data.id);
      if (current) {
        current.status = data.status;
        current.description = data.description;
        if (data.status === 'completed' || data.status === 'failed') current.ended_at = data.timestamp;
      } else if (data.status === 'running') {
        messages.push({ type: 'step', content: { ...data, started_at: data.timestamp, tools: [] } as StepContent });
      }
    } else if (event.event === 'error') {
      const data = event.data as ErrorEventData;
      failRunningSteps(messages);
      messages.push({ type: 'assistant', content: { content: data.error, timestamp: data.timestamp } as MessageContent });
    } else if (event.event === 'done') {
      completeRunningSteps(messages, event.data.timestamp);
      insertTaskExecutionSummary(messages, event.data.timestamp);
    }
  }
  return messages;
}

/** Stable keys preserve tool/approval component state when older pages are prepended. */
export function createMessageKey() {
  const keys = new WeakMap<Message, number>();
  let counter = 0;
  return (message: Message) => {
    if (!keys.has(message)) keys.set(message, ++counter);
    return keys.get(message)!;
  };
}

/** A late lifecycle event may have appeared before its original tool page was loaded. */
export function prependHistoricalMessages(older: Message[], current: Message[]): Message[] {
  const olderTools = new Map<string, ToolContent>();
  for (const message of older) {
    const tools = message.type === 'tool' ? [message.content as ToolContent]
      : message.type === 'step' ? (message.content as StepContent).tools : [];
    for (const tool of tools) olderTools.set(tool.tool_call_id, tool);
  }
  const tail: Message[] = [];
  for (const message of current) {
    if (message.type === 'tool') {
      const tool = message.content as ToolContent;
      const original = olderTools.get(tool.tool_call_id);
      if (original) {
        Object.assign(original, mergeAnalysisToolEvent(original, tool));
        continue;
      }
    }
    if (message.type === 'step') {
      const step = message.content as StepContent;
      const remaining = step.tools.filter((tool) => {
        const original = olderTools.get(tool.tool_call_id);
        if (!original) return true;
        Object.assign(original, mergeAnalysisToolEvent(original, tool));
        return false;
      });
      if (remaining.length !== step.tools.length) step.tools = remaining;
    }
    tail.push(message);
  }
  // Preserve live message objects, including results updated while the page was in flight.
  return [...older, ...tail];
}
