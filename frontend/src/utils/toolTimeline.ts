import type { ToolContent } from '../types/message';
import { mergeAnalysisToolEvent } from './analysisJob.ts';

export interface DisplayToolItem {
  key: string;
  tool: ToolContent;
  panelTool: ToolContent;
  count: number;
  summary?: string;
  revisions: ToolContent[];
}

const isMutation = (tool: ToolContent): boolean => tool.name === 'file'
  && ['file_write', 'file_str_replace'].includes(tool.function)
  && typeof tool.args?.file === 'string' && !!tool.args.file;

export const toolOperationState = (tool: ToolContent): string => {
  if (tool.status !== 'called') return '进行中';
  if (tool.execution_status === 'failed') return '失败';
  if (tool.execution_status === 'succeeded') return '已完成';
  const result = tool.content?.result ?? tool.content;
  if (result?.success === false) return '失败';
  if (result?.success === true) return '已完成';
  // A terminal event alone does not attest successful execution.
  return '已结束';
};

/** Display-only aggregation within one step. No event history is rewritten. */
export function buildToolTimeline(events: ToolContent[]): DisplayToolItem[] {
  const calls = new Map<string, ToolContent>();
  for (const event of events) {
    const previous = calls.get(event.tool_call_id);
    // Status snapshots are not separate operations. Keep a result when the
    // terminal snapshot only updates job metadata without another result.
    calls.set(event.tool_call_id, previous ? mergeAnalysisToolEvent(previous, event) : { ...event });
  }
  const items: DisplayToolItem[] = [];
  const groups = new Map<string, DisplayToolItem>();
  for (const tool of calls.values()) {
    // Approval and declarative output cards must remain visible individually.
    const groupable = isMutation(tool) && !tool.tool_approval && !tool.presentation;
    const target = groupable ? tool.args.file as string : '';
    const group = target ? groups.get(target) : undefined;
    if (group) {
      group.revisions.push(tool);
      group.count = group.revisions.length;
      group.tool = tool;
      group.panelTool = tool;
      const failures = group.revisions.filter((revision) => toolOperationState(revision) === '失败').length;
      group.summary = `${group.count} 次修改${failures ? ` · ${failures} 次失败` : ''}`;
    } else {
      const item = { key: tool.tool_call_id, tool, panelTool: tool, count: 1,
        revisions: groupable ? [tool] : [] };
      items.push(item);
      if (target) groups.set(target, item);
    }
  }
  return items;
}
