import type { AgentSSEEvent } from '../types/event';

/** PENDING also describes a newly created session with no submitted task. */
export function shouldRecoverSession(status: string, events: AgentSSEEvent[]): boolean {
  if (status === 'running') return true;
  if (status !== 'pending') return false;
  let hasUnfinishedInput = false;
  for (const event of events) {
    if (event.event === 'run' || (event.event === 'message' && 'role' in event.data && event.data.role === 'user')) {
      hasUnfinishedInput = true;
    } else if (['done', 'error', 'wait'].includes(event.event)) {
      hasUnfinishedInput = false;
    }
  }
  return hasUnfinishedInput;
}
