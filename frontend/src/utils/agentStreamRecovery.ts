import type { AgentSSEEvent } from '../types/event';
import { acceptAgentEvent, createAgentEventCursor, SUPPORTED_AGENT_EVENT_VERSION } from './agentEventCursor.ts';
import { SSEConnectionError } from './sseConnection.ts';

interface ChatRequestBody {
  message: string;
  event_id?: string;
  event_seq?: number;
  client_message_id?: string;
  resume_from?: string;
  [key: string]: unknown;
}

const EVENT_NAMES = new Set(['tool', 'step', 'message', 'error', 'done', 'title', 'wait', 'plan', 'attachments']);

/** Keep acceptance, the durable cursor and terminal state outside any one HTTP attempt. */
export function createAgentStreamRecovery(request: ChatRequestBody) {
  // Selections may change in the UI while disconnected. An ambiguous retry
  // must retain both the idempotency key AND the original request payload.
  const initialBody = JSON.parse(JSON.stringify(request)) as ChatRequestBody;
  const cursor = createAgentEventCursor();
  cursor.lastSeq = initialBody.event_seq;
  let eventId = initialBody.event_id;
  let accepted = !initialBody.message && !initialBody.resume_from;
  let complete = false;

  return {
    getBody: () => ({
      ...initialBody,
      // HTTP headers alone do not acknowledge input: the generator bootstraps
      // after sending 200. Until an event arrives, retry the SAME stable ID.
      message: accepted ? '' : initialBody.message,
      client_message_id: accepted ? undefined : initialBody.client_message_id,
      resume_from: accepted ? undefined : initialBody.resume_from,
      event_id: eventId,
      // New clients recover from durable history, even before their first event.
      event_seq: cursor.lastSeq ?? (eventId ? undefined : 0),
    }),
    isComplete: () => complete,
    accept(event: AgentSSEEvent): boolean {
      const data = event.data;
      if (!EVENT_NAMES.has(event.event) || !data || typeof data !== 'object'
        || (data.version != null && data.version !== SUPPORTED_AGENT_EVENT_VERSION)
        || (data.seq != null && (!Number.isSafeInteger(data.seq) || data.seq < 1))) {
        throw new SSEConnectionError('protocol', '事件协议不兼容，请刷新页面；后台任务状态尚未确认。');
      }
      if (!acceptAgentEvent(cursor, event)) return false;
      accepted = true;
      if (data.event_id) eventId = data.event_id;
      complete = ['done', 'wait', 'error'].includes(event.event);
      return true;
    },
  };
}
