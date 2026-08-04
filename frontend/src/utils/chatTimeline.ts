import type { Message, StepContent } from '../types/message';

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
