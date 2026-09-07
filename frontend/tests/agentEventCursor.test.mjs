import assert from 'node:assert/strict';
import test from 'node:test';

import {
  acceptAgentEvent,
  createAgentEventCursor,
  resetAgentEventCursor,
} from '../src/utils/agentEventCursor.ts';

function event({ seq, eventId, version = 1 } = {}) {
  return {
    event: 'message',
    data: {
      content: 'result',
      role: 'assistant',
      timestamp: 1,
      ...(seq === undefined ? {} : { seq }),
      ...(eventId === undefined ? {} : { event_id: eventId }),
      ...(version === undefined ? {} : { version }),
    },
  };
}

test('versioned events reject duplicate and out-of-order replay', () => {
  const cursor = createAgentEventCursor();

  assert.equal(acceptAgentEvent(cursor, event({ seq: 7, eventId: '100-0' })), true);
  assert.equal(acceptAgentEvent(cursor, event({ seq: 7, eventId: '100-0' })), false);
  assert.equal(acceptAgentEvent(cursor, event({ seq: 6, eventId: '99-0' })), false);
  assert.equal(acceptAgentEvent(cursor, event({ seq: 8, eventId: '101-0' })), true);
  assert.equal(cursor.lastSeq, 8);
});

test('legacy history without seq or version deduplicates by event_id', () => {
  const cursor = createAgentEventCursor();
  const legacy = event({ eventId: '1700000000000-1' });
  delete legacy.data.version;

  assert.equal(acceptAgentEvent(cursor, legacy), true);
  assert.equal(acceptAgentEvent(cursor, legacy), false);
  assert.equal(cursor.lastSeq, undefined);
});

test('legacy null envelope fields use event_id compatibility semantics', () => {
  const cursor = createAgentEventCursor();
  const legacy = event({
    seq: null,
    eventId: '1700000000000-2',
    version: null,
  });

  assert.equal(acceptAgentEvent(cursor, legacy), true);
  assert.equal(acceptAgentEvent(cursor, legacy), false);
  assert.equal(cursor.lastSeq, undefined);
});

test('unknown explicit versions are rejected without advancing the cursor', () => {
  const cursor = createAgentEventCursor();
  const unknown = event({ seq: 3, eventId: '3-0', version: 2 });

  assert.equal(acceptAgentEvent(cursor, unknown), false);
  assert.equal(cursor.lastSeq, undefined);
  assert.equal(acceptAgentEvent(cursor, event({ seq: 3, eventId: '3-0' })), true);
});

test('unsafe JavaScript sequence values are rejected', () => {
  const cursor = createAgentEventCursor();
  const unsafe = event({ seq: Number.MAX_SAFE_INTEGER + 1, eventId: 'legacy-safe-id' });

  assert.equal(acceptAgentEvent(cursor, unsafe), false);
  assert.equal(cursor.lastSeq, undefined);
  assert.equal(cursor.legacyEventIds.size, 0);
});

test('mixed legacy and versioned copies deduplicate by event_id in either order', () => {
  const firstCursor = createAgentEventCursor();
  const versioned = event({ seq: 1, eventId: 'mixed-1' });
  const legacy = event({ eventId: 'mixed-1', version: undefined });

  assert.equal(acceptAgentEvent(firstCursor, versioned), true);
  assert.equal(acceptAgentEvent(firstCursor, legacy), false);

  const secondCursor = createAgentEventCursor();
  assert.equal(acceptAgentEvent(secondCursor, legacy), true);
  assert.equal(acceptAgentEvent(secondCursor, versioned), false);
  assert.equal(secondCursor.lastSeq, 1);
});

test('cursor reset permits replaying the same session from the beginning', () => {
  const cursor = createAgentEventCursor();
  const first = event({ seq: 1, eventId: '1-0' });

  assert.equal(acceptAgentEvent(cursor, first), true);
  assert.equal(acceptAgentEvent(cursor, first), false);
  resetAgentEventCursor(cursor);
  assert.equal(acceptAgentEvent(cursor, first), true);
});
