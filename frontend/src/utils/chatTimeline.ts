import type { AttachmentsContent, Message, StepContent, TaskSummaryContent } from '../types/message';

export const getCurrentTurnStartIndex = (messages: Message[]): number => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].type === 'user') return index + 1;
  }
  return 0;
};

export const findCurrentTurnStep = (
  messages: Message[],
  stepId: StepContent['id'],
): StepContent | undefined => {
  const turnStart = getCurrentTurnStartIndex(messages);
  for (let index = messages.length - 1; index >= turnStart; index -= 1) {
    const message = messages[index];
    if (message.type !== 'step') continue;
    const step = message.content as StepContent;
    if (step.id === stepId) return step;
  }
  return undefined;
};

export const findCurrentTurnRunningStep = (messages: Message[]): StepContent | undefined => {
  const turnStart = getCurrentTurnStartIndex(messages);
  for (let index = messages.length - 1; index >= turnStart; index -= 1) {
    const message = messages[index];
    if (message.type !== 'step') continue;
    const step = message.content as StepContent;
    if (step.status === 'running') return step;
  }
  return undefined;
};

export const failRunningSteps = (
  messages: Message[],
  currentTurnOnly = true,
): StepContent[] => {
  const turnStart = currentTurnOnly ? getCurrentTurnStartIndex(messages) : 0;
  const failedSteps: StepContent[] = [];
  for (let index = turnStart; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.type !== 'step') continue;
    const step = message.content as StepContent;
    if (step.status !== 'running') continue;
    step.status = 'failed';
    failedSteps.push(step);
  }
  return failedSteps;
};

export const insertTaskExecutionSummary = (
  messages: Message[],
  endedAt: number,
  elapsedMs?: number,
): TaskSummaryContent | undefined => {
  let userMessageIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].type === 'user') {
      userMessageIndex = index;
      break;
    }
  }
  if (userMessageIndex < 0) return undefined;

  for (let index = messages.length - 1; index > userMessageIndex; index -= 1) {
    if (messages[index].type === 'task-summary') messages.splice(index, 1);
  }

  const startedAt = Math.min(
    Number(messages[userMessageIndex].content.timestamp) || endedAt,
    endedAt,
  );

  const summary: TaskSummaryContent = {
    timestamp: endedAt,
    duration_ms: Math.max(0, Math.round(elapsedMs ?? ((endedAt - startedAt) * 1000))),
  };
  const attachmentOffset = messages
    .slice(userMessageIndex + 1)
    .findIndex((message) => (
      message.type === 'attachments'
      && (message.content as AttachmentsContent).role === 'assistant'
    ));
  const insertionIndex = attachmentOffset >= 0
    ? userMessageIndex + 1 + attachmentOffset
    : messages.length;
  messages.splice(insertionIndex, 0, { type: 'task-summary', content: summary });
  return summary;
};
