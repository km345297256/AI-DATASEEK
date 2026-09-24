import type { Message, StepContent, ToolContent } from '../types/message';

/** Session-owned lookup only; the message objects remain the display source of truth.
 * Appends cost only the new suffix. A history replacement/prepend rebuilds once,
 * including late tool results merged into their original historical card.
 */
export class AnalysisTimelineIndex {
  private source?: Message[];
  private length = 0;
  private tail?: Message;
  private lastDialogueIndex = -1;
  private readonly tools = new Map<string, ToolContent>();
  private readonly steps = new Map<string, StepContent>();

  clear() {
    this.source = undefined; this.length = 0; this.tail = undefined;
    this.lastDialogueIndex = -1;
    this.tools.clear(); this.steps.clear();
  }

  sync(messages: Message[]) {
    if (messages !== this.source || messages.length < this.length
      || (this.length > 0 && messages[this.length - 1] !== this.tail)) {
      this.clear(); this.source = messages;
    }
    for (; this.length < messages.length; this.length += 1) {
      const message = messages[this.length];
      if (message.type === 'user' || message.type === 'assistant') this.lastDialogueIndex = this.length;
      if (message.type === 'user') this.steps.clear();
      if (message.type === 'tool') this.addTool(message.content as ToolContent);
      if (message.type === 'step') {
        const step = message.content as StepContent;
        this.steps.set(step.id, step);
        // Preserve the old reverse-message / first-tool-within-step lookup.
        for (let index = step.tools.length - 1; index >= 0; index -= 1) this.addTool(step.tools[index]);
      }
    }
    this.tail = messages[messages.length - 1];
  }

  addTool(tool: ToolContent) { this.tools.set(tool.tool_call_id, tool); }
  tool(messages: Message[], id: string) { this.sync(messages); return this.tools.get(id); }
  step(messages: Message[], id: string) { this.sync(messages); return this.steps.get(id); }
  isLastDialogue(messages: Message[], index: number) { this.sync(messages); return index === this.lastDialogueIndex; }
  runningStep(messages: Message[]) {
    this.sync(messages);
    // Only the current turn is inspected, never the historical transcript.
    return [...this.steps.values()].reverse().find(step => step.status === 'running');
  }
}
