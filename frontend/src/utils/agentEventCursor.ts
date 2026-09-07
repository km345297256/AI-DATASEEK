import type { AgentSSEEvent } from '../types/event';

const LEGACY_EVENT_ID_WINDOW = 1024;
export const SUPPORTED_AGENT_EVENT_VERSION = 1;
export const MAX_EVENT_SEQUENCE = Number.MAX_SAFE_INTEGER;

export interface AgentEventCursor {
  lastSeq?: number;
  readonly legacyEventIds: Set<string>;
  readonly legacyEventIdOrder: string[];
}

export function createAgentEventCursor(): AgentEventCursor {
  return {
    lastSeq: undefined,
    legacyEventIds: new Set<string>(),
    legacyEventIdOrder: [],
  };
}

export function resetAgentEventCursor(cursor: AgentEventCursor): void {
  cursor.lastSeq = undefined;
  cursor.legacyEventIds.clear();
  cursor.legacyEventIdOrder.length = 0;
}

function validSequence(value: unknown): value is number {
  return Number.isSafeInteger(value)
    && Number(value) >= 1
    && Number(value) <= MAX_EVENT_SEQUENCE;
}

function rememberEventId(cursor: AgentEventCursor, eventId: string): void {
  cursor.legacyEventIds.add(eventId);
  cursor.legacyEventIdOrder.push(eventId);
  if (cursor.legacyEventIdOrder.length > LEGACY_EVENT_ID_WINDOW) {
    const expired = cursor.legacyEventIdOrder.shift();
    if (expired) cursor.legacyEventIds.delete(expired);
  }
}

/**
 * Admit one event exactly once without changing any event-specific UI logic.
 *
 * Versioned streams use the session sequence, so reconnect replay and Redis
 * overlap cannot regress UI state. Legacy events fall back to a bounded event
 * ID set and therefore remain compatible without retaining unbounded memory.
 */
export function acceptAgentEvent(
  cursor: AgentEventCursor,
  event: AgentSSEEvent,
): boolean {
  // An absent version means legacy v1. An explicit unknown version must not be
  // rendered using v1 assumptions; a reload after a frontend upgrade can
  // safely replay it because the cursor is left untouched.
  const version = event.data?.version;
  if (version != null && version !== SUPPORTED_AGENT_EVENT_VERSION) {
    return false;
  }
  const seq = event.data?.seq;
  if (seq != null) {
    if (!validSequence(seq)) return false;
    if (cursor.lastSeq !== undefined && seq <= cursor.lastSeq) return false;
    cursor.lastSeq = seq;
    const eventId = event.data?.event_id;
    if (eventId && cursor.legacyEventIds.has(eventId)) return false;
    if (eventId) rememberEventId(cursor, eventId);
    return true;
  }

  const eventId = event.data?.event_id;
  if (!eventId) return true;
  if (cursor.legacyEventIds.has(eventId)) return false;
  rememberEventId(cursor, eventId);
  return true;
}
