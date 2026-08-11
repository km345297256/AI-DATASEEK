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
  const steps = messages
    .slice(userMessageIndex + 1)
    .filter((message): message is Message & { content: StepContent } => message.type === 'step')
    .map((message) => {
      const step = message.content;
      const stepStartedAt = Math.min(step.started_at || step.timestamp || endedAt, endedAt);
      const latestToolTimestamp = step.tools.reduce(
        (latest, tool) => Math.max(latest, tool.timestamp || 0),
        0,
      );
      const stepEndedAt = Math.max(
        stepStartedAt,
        Math.min(step.ended_at || latestToolTimestamp || endedAt, endedAt),
      );
      return {
        id: step.id,
        description: step.description,
        status: step.status,
        started_at: stepStartedAt,
        ended_at: stepEndedAt,
        duration_seconds: Math.max(0, stepEndedAt - stepStartedAt),
      };
    });

  const summary: TaskSummaryContent = {
    timestamp: endedAt,
    started_at: startedAt,
    ended_at: endedAt,
    duration_seconds: Math.max(0, endedAt - startedAt),
    steps,
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
